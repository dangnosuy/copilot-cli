#!/usr/bin/env python3
"""
GitHub Copilot Proxy — OpenAI-Compatible API Server
=====================================================
Stateless proxy: KHÔNG lưu token. Mỗi request phải gửi GitHub token
qua Authorization header, giống hệt cách gọi OpenAI API.

Usage:
  curl http://localhost:8000/v1/chat/completions \\
    -H "Authorization: Bearer gho_xxxYOUR_TOKEN" \\
    -H "Content-Type: application/json" \\
    -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hi"}]}'
"""

import json
import time
import uuid
import os
from typing import Optional, Dict, Tuple

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════
GITHUB_API = "https://api.github.com"
COPILOT_TOKEN_ENDPOINT = "/copilot_internal/v2/token"
GITHUB_API_VERSION = "2025-04-01"
COPILOT_API_VERSION = "2025-07-16"
USER_AGENT = "GitHubCopilotChat/0.31.5"

# ═══════════════════════════════════════════════════════════════
# SESSION CACHE  (github_token -> (copilot_token, api_base, expires_at))
# Server không lưu file, chỉ cache trong RAM để tránh gọi lại liên tục
# ═══════════════════════════════════════════════════════════════
_token_cache: Dict[str, Tuple[str, str, int]] = {}


async def exchange_token(github_token: str) -> Tuple[str, str]:
    """Đổi GitHub token -> Copilot session token + api_base.
    Cache trong RAM, auto-refresh khi hết hạn."""
    cached = _token_cache.get(github_token)
    if cached:
        copilot_token, api_base, expires_at = cached
        if time.time() < expires_at - 300:
            return copilot_token, api_base

    url = f"{GITHUB_API}{COPILOT_TOKEN_ENDPOINT}"
    headers = {
        "Authorization": f"token {github_token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": USER_AGENT,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, timeout=15)

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail={
            "error": {
                "message": "Invalid GitHub token. Please check your API key.",
                "type": "authentication_error",
                "code": "invalid_api_key",
            }
        })
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail={
            "error": {
                "message": f"GitHub token exchange failed (HTTP {resp.status_code})",
                "type": "authentication_error",
            }
        })

    data = resp.json()
    copilot_token = data["token"]
    api_base = data.get("endpoints", {}).get("api", "https://api.individual.githubcopilot.com")
    expires_at = data.get("expires_at", 0)

    _token_cache[github_token] = (copilot_token, api_base, expires_at)
    return copilot_token, api_base


def require_auth(request: Request) -> str:
    """Extract GitHub token từ Authorization header. Trả 401 nếu thiếu."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        if token:
            return token
    raise HTTPException(status_code=401, detail={
        "error": {
            "message": "Missing or invalid Authorization header. Expected: Bearer gho_xxxYOUR_TOKEN",
            "type": "authentication_error",
            "code": "missing_api_key",
        }
    })


def copilot_headers(copilot_token: str) -> dict:
    """Build headers cho request tới Copilot upstream."""
    return {
        "Authorization": f"Bearer {copilot_token}",
        "X-Request-Id": str(uuid.uuid4()),
        "X-Interaction-Type": "conversation-agent",
        "OpenAI-Intent": "conversation-agent",
        "X-Interaction-Id": str(uuid.uuid4()),
        "X-Initiator": "user",
        "VScode-SessionId": str(uuid.uuid4()),
        "VScode-MachineId": str(uuid.uuid4()),
        "X-GitHub-Api-Version": COPILOT_API_VERSION,
        "Editor-Plugin-Version": "copilot-chat/0.31.5",
        "Editor-Version": "vscode/1.104.1",
        "Copilot-Integration-Id": "vscode-chat",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }


# ═══════════════════════════════════════════════════════════════
# RESPONSE CLEANING
# ═══════════════════════════════════════════════════════════════

def _clean_delta(delta: dict) -> dict:
    """Chỉ giữ các field chuẩn OpenAI trong delta, bỏ hết metadata thừa."""
    clean = {}
    if "role" in delta:
        clean["role"] = delta["role"]
    if "content" in delta:
        clean["content"] = delta["content"]
    if "tool_calls" in delta:
        clean["tool_calls"] = delta["tool_calls"]
    if "function_call" in delta:
        clean["function_call"] = delta["function_call"]
    if "refusal" in delta:
        clean["refusal"] = delta["refusal"]
    return clean


def clean_sse_chunk(raw: dict) -> Optional[dict]:
    """Clean 1 SSE chunk: bỏ content_filter_results, prompt_filter_results,
    chỉ giữ format chuẩn OpenAI SDK."""
    choices = raw.get("choices", [])
    if not choices:
        return None

    clean_choices = []
    for c in choices:
        delta = c.get("delta", {})
        finish = c.get("finish_reason")

        has_useful = (
            delta.get("content") is not None
            or delta.get("role") is not None
            or "tool_calls" in delta
            or "function_call" in delta
            or delta.get("reasoning_text") is not None
            or finish is not None
        )
        if not has_useful:
            continue

        out = {
            "index": c.get("index", 0),
            "delta": _clean_delta(delta),
            "logprobs": c.get("logprobs", None),
            "finish_reason": finish,
        }
        clean_choices.append(out)

    if not clean_choices:
        return None

    result = {
        "id": raw.get("id", ""),
        "object": "chat.completion.chunk",
        "created": raw.get("created", int(time.time())),
        "model": raw.get("model", ""),
        "system_fingerprint": raw.get("system_fingerprint", None),
        "choices": clean_choices,
    }
    if "usage" in raw:
        result["usage"] = raw["usage"]
    return result


def _clean_message(msg: dict) -> dict:
    """Chỉ giữ các field chuẩn OpenAI trong message."""
    clean = {
        "role": msg.get("role", "assistant"),
        "content": msg.get("content"),
    }
    if msg.get("tool_calls"):
        clean["tool_calls"] = msg["tool_calls"]
    if msg.get("function_call"):
        clean["function_call"] = msg["function_call"]
    if msg.get("refusal") is not None:
        clean["refusal"] = msg["refusal"]
    else:
        clean["refusal"] = None
    return clean


def clean_response(raw: dict) -> dict:
    """Clean non-streaming response: giữ format chuẩn OpenAI SDK."""
    clean_choices = []
    for c in raw.get("choices", []):
        clean_choices.append({
            "index": c.get("index", 0),
            "message": _clean_message(c.get("message", {})),
            "logprobs": c.get("logprobs", None),
            "finish_reason": c.get("finish_reason", "stop"),
        })
    return {
        "id": raw.get("id", f"chatcmpl-{uuid.uuid4().hex[:12]}"),
        "object": "chat.completion",
        "created": raw.get("created", int(time.time())),
        "model": raw.get("model", ""),
        "system_fingerprint": raw.get("system_fingerprint", None),
        "choices": clean_choices,
        "usage": raw.get("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
    }


# ═══════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════
app = FastAPI(title="GitHub Copilot Proxy API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def root():
    return {
        "status": "running",
        "service": "GitHub Copilot Proxy API",
        "usage": "Set api_key to your GitHub token (gho_xxx), base_url to http://localhost:{port}/v1",
    }


@app.get("/v1/models")
async def list_models(request: Request):
    github_token = require_auth(request)
    copilot_token, api_base = await exchange_token(github_token)

    headers = copilot_headers(copilot_token)
    headers["X-Interaction-Type"] = "model-access"
    headers["OpenAI-Intent"] = "model-access"

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{api_base}/models", headers=headers, timeout=15)
    if resp.status_code != 200:
        return JSONResponse(status_code=resp.status_code, content={"error": {"message": resp.text[:500]}})

    raw = resp.json()
    models = []
    for m in raw.get("data", []):
        models.append({
            "id": m.get("id", ""),
            "object": "model",
            "created": m.get("created", int(time.time())),
            "owned_by": m.get("vendor", "github-copilot"),
        })
    return JSONResponse(content={"object": "list", "data": models})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    github_token = require_auth(request)
    copilot_token, api_base = await exchange_token(github_token)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "Invalid JSON body"}})

    headers = copilot_headers(copilot_token)
    url = f"{api_base}/chat/completions"
    is_stream = body.get("stream", False)

    if is_stream:
        async def generate():
            async with httpx.AsyncClient() as client:
                try:
                    async with client.stream("POST", url, headers=headers, json=body, timeout=120) as resp:
                        if resp.status_code != 200:
                            err_bytes = await resp.aread()
                            err_msg = err_bytes.decode("utf-8", errors="replace")[:300]
                            chunk = {
                                "id": "error",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": body.get("model", ""),
                                "choices": [{"index": 0, "delta": {"content": f"[Error {resp.status_code}] {err_msg}"}, "finish_reason": "stop"}],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                            yield "data: [DONE]\n\n"
                            return

                        buf = ""
                        async for raw_bytes in resp.aiter_bytes():
                            if not raw_bytes:
                                continue
                            buf += raw_bytes.decode("utf-8", errors="replace")
                            while "\n" in buf:
                                line, buf = buf.split("\n", 1)
                                line = line.strip()
                                if not line or not line.startswith("data: "):
                                    continue
                                payload = line[6:]
                                if payload.strip() == "[DONE]":
                                    yield "data: [DONE]\n\n"
                                    return
                                try:
                                    raw = json.loads(payload)
                                except json.JSONDecodeError:
                                    continue
                                cleaned = clean_sse_chunk(raw)
                                if cleaned:
                                    yield f"data: {json.dumps(cleaned, ensure_ascii=False)}\n\n"

                        # Flush
                        for leftover in buf.split("\n"):
                            leftover = leftover.strip()
                            if leftover.startswith("data: "):
                                p = leftover[6:]
                                if p.strip() == "[DONE]":
                                    yield "data: [DONE]\n\n"
                                    return
                                try:
                                    cleaned = clean_sse_chunk(json.loads(p))
                                    if cleaned:
                                        yield f"data: {json.dumps(cleaned, ensure_ascii=False)}\n\n"
                                except json.JSONDecodeError:
                                    pass
                        yield "data: [DONE]\n\n"
                except httpx.ReadTimeout:
                    yield f'data: {{"error":"timeout"}}\n\n'
                    yield "data: [DONE]\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    else:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=body, timeout=120)
        if resp.status_code != 200:
            try:
                err = resp.json()
            except Exception:
                err = {"error": {"message": resp.text[:500]}}
            return JSONResponse(status_code=resp.status_code, content=err)
        return JSONResponse(content=clean_response(resp.json()))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5000))
    print(f"""
======================================================
  GitHub Copilot Proxy API Server
  http://127.0.0.1:{port}/v1
------------------------------------------------------
  GET  /v1/models            - List models
  POST /v1/chat/completions  - Chat (stream & sync)
------------------------------------------------------
  Authorization: Bearer gho_xxxYOUR_GITHUB_TOKEN
======================================================
""")
    uvicorn.run(app, host="0.0.0.0", port=port)
