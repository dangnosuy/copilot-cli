#!/usr/bin/env python3
"""
GitHub Copilot Proxy — Anthropic-Compatible API Server (CLI Identity)
=====================================================================
Proxy nhận request theo format Anthropic Messages API,
convert sang OpenAI format, forward tới Copilot, rồi
convert response ngược lại thành Anthropic format.

*** Giả dạng GitHub Copilot CLI chính chủ ***
- Dùng gho_ token trực tiếp (KHÔNG exchange sang JWT)
- Headers, User-Agent, Integration-Id giống hệt CLI thật
- Dựa trên traffic capture từ Copilot CLI v1.0.10

Người dùng có thể dùng Anthropic SDK (anthropic Python lib)
trỏ về server này, sử dụng GitHub token làm api_key.

Usage:
  curl http://localhost:5005/v1/messages \\
    -H "x-api-key: gho_xxxYOUR_TOKEN" \\
    -H "anthropic-version: 2023-06-01" \\
    -H "Content-Type: application/json" \\
    -d '{
      "model": "claude-sonnet-4",
      "max_tokens": 1024,
      "messages": [{"role": "user", "content": "Hello!"}]
    }'
"""

import json
import hashlib
import time
import uuid
import os
import re
import platform
import asyncio
from typing import Optional, Dict, Tuple

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

# ═══════════════════════════════════════════════════════════════
# CONSTANTS — Giả dạng Copilot CLI chính chủ
# ═══════════════════════════════════════════════════════════════
GITHUB_API = "https://api.github.com"
COPILOT_API = "https://api.individual.githubcopilot.com"

# CLI identity — copy chính xác từ traffic capture
CLI_VERSION = "1.0.10"
_os_name = "linux" if platform.system() == "Linux" else platform.system().lower()
_node_version = "v24.11.1"

# User-Agent giống hệt CLI thật (2 dạng)
USER_AGENT = f"copilot/{CLI_VERSION} ({_os_name} {_node_version}) term/unknown"
USER_AGENT_CHAT = f"copilot/{CLI_VERSION} (client/github/cli {_os_name} {_node_version}) term/unknown"

# API version CLI dùng (KHÁC với VSCode)
GITHUB_API_VERSION = "2025-05-01"

# ═══════════════════════════════════════════════════════════════
# TIMEOUT & RETRY CONFIG
# ═══════════════════════════════════════════════════════════════
UPSTREAM_TIMEOUT = httpx.Timeout(
    connect=15.0,
    read=300.0,      # 5 min — opus is slow
    write=30.0,
    pool=15.0,
)
STREAM_TIMEOUT = httpx.Timeout(
    connect=15.0,
    read=300.0,
    write=30.0,
    pool=15.0,
)
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# ═══════════════════════════════════════════════════════════════
# Persistent session IDs — giống CLI thật
# ═══════════════════════════════════════════════════════════════
SESSION_ID = str(uuid.uuid4())
MACHINE_ID = hashlib.sha256(uuid.getnode().to_bytes(6, 'big')).hexdigest()

# ═══════════════════════════════════════════════════════════════
# MODEL MAPPING — Claude Code sends dashes, Copilot uses dots
# ═══════════════════════════════════════════════════════════════
MODEL_MAP = {
    # Opus
    "claude-opus-4-6":          "claude-opus-4.6",
    "claude-opus-4-5":          "claude-opus-4.6",
    "claude-opus-4-0":          "claude-opus-4.6",
    # Sonnet
    "claude-sonnet-4-6":        "claude-sonnet-4.6",
    "claude-sonnet-4-5":        "claude-sonnet-4.6",
    "claude-sonnet-4-0":        "claude-sonnet-4.6",
    "claude-sonnet-4":          "claude-sonnet-4.6",
    # Haiku
    "claude-haiku-4-5":         "claude-haiku-4.5",
    "claude-haiku-3-5":         "claude-haiku-4.5",
    # Pass-through
    "claude-opus-4.6":          "claude-opus-4.6",
    "claude-opus-4.5":          "claude-opus-4.6",
    "claude-sonnet-4.6":        "claude-sonnet-4.6",
    "claude-sonnet-4.5":        "claude-sonnet-4.6",
    "claude-haiku-4.5":         "claude-haiku-4.5",
}

_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def resolve_model(model_name: str) -> str:
    """Map Claude Code model name → Copilot API model ID."""
    if model_name in MODEL_MAP:
        return MODEL_MAP[model_name]
    stripped = _DATE_SUFFIX_RE.sub("", model_name)
    if stripped in MODEL_MAP:
        return MODEL_MAP[stripped]
    m = re.match(r"^(claude-\w+)-(\d+)-(\d+)$", stripped)
    if m:
        return f"{m.group(1)}-{m.group(2)}.{m.group(3)}"
    return model_name


# ═══════════════════════════════════════════════════════════════
# MODEL PROMPT LIMITS
# ═══════════════════════════════════════════════════════════════
_MODEL_PROMPT_LIMITS: Dict[str, int] = {}


def _load_model_limits():
    """Load max_prompt_tokens from models_with_billing.json."""
    global _MODEL_PROMPT_LIMITS
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_with_billing.json")
    try:
        with open(json_path, "r") as f:
            data = json.load(f)
        for m in data.get("data", []):
            model_id = m.get("id", "")
            limits = m.get("capabilities", {}).get("limits", {})
            max_prompt = limits.get("max_prompt_tokens")
            if model_id and max_prompt:
                _MODEL_PROMPT_LIMITS[model_id] = max_prompt
    except Exception as e:
        print(f"  ⚠ Could not load model limits: {e}")

    # Hardcoded fallbacks
    for k, v in {
        "claude-opus-4.6": 128000, "claude-opus-4.5": 128000,
        "claude-sonnet-4.6": 128000, "claude-sonnet-4.5": 128000,
        "claude-sonnet-4": 128000, "claude-haiku-4.5": 128000,
    }.items():
        _MODEL_PROMPT_LIMITS.setdefault(k, v)


_load_model_limits()


def get_model_prompt_limit(model_id: str) -> int:
    """Get max_prompt_tokens for a model. Default 128000."""
    return _MODEL_PROMPT_LIMITS.get(model_id, 128000)


# ═══════════════════════════════════════════════════════════════
# TOKEN ESTIMATION & MESSAGE TRUNCATION
# (Giữ nguyên logic từ server-anthropic.py)
# ═══════════════════════════════════════════════════════════════

SAFETY_MARGIN = 0.75  # Conservative margin — estimation can undercount by ~30% for image-heavy content


def _estimate_tokens_text(text: str) -> int:
    if not text:
        return 0
    return len(text) // 3


def _estimate_image_tokens(block: dict) -> int:
    """Estimate tokens for an image block more accurately.

    Anthropic counts image tokens based on pixel dimensions.
    A rough formula: (width * height) / 750 tokens.
    Since we often don't know dimensions, estimate from base64 data size:
    - base64 string length * 3/4 = raw bytes
    - Typical compression: raw_bytes → roughly (raw_bytes / 500) tokens
    - Minimum 1600 tokens (small image), cap at 6400 (1568x1568 max tile)
    For URL-type images, use a conservative estimate.
    """
    source = block.get("source", {})
    if source.get("type") == "base64":
        data = source.get("data", "")
        # base64 length → raw bytes → estimate tokens
        raw_bytes = len(data) * 3 // 4
        # Anthropic's vision: images are resized to fit within tiles of ~1568px
        # Each tile costs ~1600 tokens. Estimate from byte size:
        # A typical 1568x1568 PNG ≈ 200-800KB → ~1600 tokens per tile
        # Conservative: assume 1 tile per 200KB, minimum 1600
        num_tiles = max(1, raw_bytes // 200_000 + 1)
        return min(num_tiles * 1600, 6400)  # max 4 tiles
    elif source.get("type") == "url":
        return 1600  # conservative single-tile estimate
    return 1600


def _estimate_anthropic_message_tokens(msg: dict) -> int:
    content = msg.get("content", "")
    if isinstance(content, str):
        return _estimate_tokens_text(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            btype = block.get("type", "")
            if btype == "text":
                total += _estimate_tokens_text(block.get("text", ""))
            elif btype == "tool_use":
                total += _estimate_tokens_text(block.get("name", ""))
                total += _estimate_tokens_text(json.dumps(block.get("input", {})))
            elif btype == "tool_result":
                inner = block.get("content", "")
                if isinstance(inner, str):
                    total += _estimate_tokens_text(inner)
                elif isinstance(inner, list):
                    for ib in inner:
                        if ib.get("type") == "text":
                            total += _estimate_tokens_text(ib.get("text", ""))
                        elif ib.get("type") == "image":
                            total += _estimate_image_tokens(ib)
            elif btype == "image":
                total += _estimate_image_tokens(block)
            else:
                total += _estimate_tokens_text(json.dumps(block))
        return total
    return 0


def _estimate_system_tokens(system) -> int:
    if isinstance(system, str):
        return _estimate_tokens_text(system)
    if isinstance(system, list):
        return sum(_estimate_tokens_text(b.get("text", "")) for b in system if b.get("type") == "text")
    return 0


def _estimate_tools_tokens(tools: list) -> int:
    if not tools:
        return 0
    return _estimate_tokens_text(json.dumps(tools))


def _truncate_tool_result_content(block: dict, max_chars: int = 500) -> dict:
    inner = block.get("content", "")
    if isinstance(inner, str) and len(inner) > max_chars * 1.5:
        block = dict(block)
        block["content"] = inner[:max_chars] + "\n... [truncated] ..."
        return block
    if isinstance(inner, list):
        new_inner = []
        changed = False
        for ib in inner:
            if ib.get("type") == "text":
                text = ib.get("text", "")
                if len(text) > max_chars * 1.5:
                    ib = dict(ib)
                    ib["text"] = text[:max_chars] + "\n... [truncated] ..."
                    changed = True
            new_inner.append(ib)
        if changed:
            block = dict(block)
            block["content"] = new_inner
        return block
    return block


def truncate_messages_for_context(
    messages: list,
    system,
    tools: list,
    max_prompt_tokens: int,
) -> tuple:
    """Truncate Anthropic messages to fit within max_prompt_tokens."""
    effective_limit = int(max_prompt_tokens * SAFETY_MARGIN)
    system_tokens = _estimate_system_tokens(system)
    tools_tokens = _estimate_tools_tokens(tools)
    overhead = 500
    available = effective_limit - system_tokens - tools_tokens - overhead

    if available <= 0:
        available = effective_limit // 2

    msg_tokens = [_estimate_anthropic_message_tokens(m) for m in messages]
    total = sum(msg_tokens)

    stats = {
        "total_estimated": total + system_tokens + tools_tokens,
        "max_prompt_tokens": max_prompt_tokens,
        "effective_limit": effective_limit,
        "system_tokens": system_tokens,
        "tools_tokens": tools_tokens,
        "available_for_messages": available,
        "original_count": len(messages),
        "truncated": False,
        "pass": 0,
    }

    if total <= available:
        return messages, False, stats

    # ─── Pass 1: Truncate old tool_result / long text / image content ───
    result = list(messages)
    safe_zone = min(10, len(result))

    for i in range(len(result) - safe_zone):
        msg = result[i]
        content = msg.get("content")

        if isinstance(content, str) and len(content) > 2000:
            result[i] = dict(msg)
            result[i]["content"] = content[:1500] + "\n... [truncated] ..."
            continue

        if not isinstance(content, list):
            continue

        new_content = []
        changed = False
        for block in content:
            btype = block.get("type", "")
            if btype == "tool_result":
                # Also strip images from old tool_result inner content
                inner = block.get("content", "")
                if isinstance(inner, list):
                    new_inner = []
                    inner_changed = False
                    for ib in inner:
                        if ib.get("type") == "image":
                            # Replace image with text placeholder
                            new_inner.append({
                                "type": "text",
                                "text": "[image removed to save context]"
                            })
                            inner_changed = True
                        else:
                            new_inner.append(ib)
                    if inner_changed:
                        block = dict(block)
                        block["content"] = new_inner
                        changed = True
                new_block = _truncate_tool_result_content(block, max_chars=500)
                if new_block is not block:
                    changed = True
                new_content.append(new_block)
            elif btype == "tool_use":
                inp = block.get("input", {})
                inp_str = json.dumps(inp)
                if len(inp_str) > 1000:
                    block = dict(block)
                    try:
                        block["input"] = json.loads(inp_str[:800] + "}")
                    except json.JSONDecodeError:
                        block["input"] = {"_truncated": inp_str[:800]}
                    changed = True
                new_content.append(block)
            elif btype == "image":
                # Remove old image blocks, replace with placeholder text
                new_content.append({
                    "type": "text",
                    "text": "[image removed to save context]"
                })
                changed = True
            elif btype == "text" and len(block.get("text", "")) > 2000:
                block = dict(block)
                block["text"] = block["text"][:1500] + "\n... [truncated] ..."
                changed = True
                new_content.append(block)
            else:
                new_content.append(block)

        if changed:
            result[i] = dict(msg)
            result[i]["content"] = new_content

    total = sum(_estimate_anthropic_message_tokens(m) for m in result)

    if total <= available:
        stats["truncated"] = True
        stats["pass"] = 1
        stats["final_count"] = len(result)
        stats["final_estimated"] = total + system_tokens + tools_tokens
        return result, True, stats

    # ─── Pass 2: Drop oldest conversation turns ───
    for keep_last in (40, 30, 20, 15, 10, 5):
        if len(result) <= keep_last + 2:
            continue

        cut_end = len(result) - keep_last
        while cut_end > 1:
            msg = result[cut_end]
            content = msg.get("content")
            is_tool_result = False
            if isinstance(content, list):
                is_tool_result = any(b.get("type") == "tool_result" for b in content)
            if msg.get("role") == "tool" or is_tool_result:
                cut_end -= 1
            else:
                break

        first_msg = result[0]
        truncation_marker = {
            "role": "user",
            "content": "[... earlier conversation truncated to fit context window ...]"
        }
        kept_msgs = [first_msg, truncation_marker] + result[cut_end:]

        kept_total = sum(_estimate_anthropic_message_tokens(m) for m in kept_msgs)
        if kept_total <= available:
            stats["truncated"] = True
            stats["pass"] = 2
            stats["dropped_messages"] = cut_end - 1
            stats["final_count"] = len(kept_msgs)
            stats["final_estimated"] = kept_total + system_tokens + tools_tokens
            return kept_msgs, True, stats

    # ─── Pass 3 (emergency): Keep only last 5 messages ───
    emergency = [
        {"role": "user", "content": "[Context truncated. Previous conversation dropped.]"},
    ] + result[-5:]
    stats["truncated"] = True
    stats["pass"] = 3
    stats["final_count"] = len(emergency)
    stats["final_estimated"] = sum(_estimate_anthropic_message_tokens(m) for m in emergency) + system_tokens + tools_tokens
    return emergency, True, stats


# ═══════════════════════════════════════════════════════════════
# AUTH — CLI style: gho_ token dùng TRỰC TIẾP
# KHÔNG exchange sang JWT như VSCode
# ═══════════════════════════════════════════════════════════════

async def validate_token(github_token: str) -> bool:
    """Kiểm tra gho_ token còn hợp lệ qua GET /copilot_internal/user."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API}/copilot_internal/user",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
                timeout=15,
            )
        return resp.status_code == 200
    except Exception:
        return False


# Cache token validation — avoid hitting /copilot_internal/user every request
_validated_tokens: Dict[str, float] = {}  # token → last_validated_time
_VALIDATION_TTL = 3600  # Re-validate every 1 hour


async def ensure_valid_token(github_token: str):
    """Validate token, cache result. Raise 401 if invalid."""
    now = time.time()
    cached_time = _validated_tokens.get(github_token)
    if cached_time and (now - cached_time) < _VALIDATION_TTL:
        return  # Still valid

    if not await validate_token(github_token):
        raise HTTPException(status_code=401, detail={
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": "Invalid API key. GitHub token (gho_xxx) is invalid or expired.",
            }
        })

    _validated_tokens[github_token] = now


def require_auth(request: Request) -> str:
    """Extract GitHub token từ x-api-key hoặc Authorization header."""
    token = request.headers.get("x-api-key", "").strip()
    if token:
        return token

    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        if token:
            return token

    raise HTTPException(status_code=401, detail={
        "type": "error",
        "error": {
            "type": "authentication_error",
            "message": "Missing API key. Set x-api-key header with your GitHub token (gho_xxx).",
        }
    })


def copilot_headers(github_token: str, interaction_id: str = "", initiator: str = "user") -> dict:
    """Build headers giả dạng Copilot CLI chính chủ.

    KEY DIFFERENCE vs server-anthropic.py:
    - Dùng gho_ token trực tiếp (KHÔNG JWT)
    - Copilot-Integration-Id: copilot-developer-cli (KHÔNG vscode-chat)
    - KHÔNG có Editor-Version, Editor-Plugin-Version, VScode-*
    - User-Agent format CLI
    - X-GitHub-Api-Version: 2025-05-01 (KHÔNG 2025-07-16)
    """
    return {
        "Authorization": f"Bearer {github_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT_CHAT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "Copilot-Integration-Id": "copilot-developer-cli",
        "OpenAI-Intent": "conversation-agent",
        "X-Initiator": "agent",
        "X-Interaction-Id": interaction_id or str(uuid.uuid4()),
        "X-Interaction-Type": "conversation-user",
        "X-Client-Session-Id": SESSION_ID,
    }


# ═══════════════════════════════════════════════════════════════
# FORMAT CONVERSION: Anthropic → OpenAI (request)
# (Giữ nguyên 100% logic từ server-anthropic.py)
# ═══════════════════════════════════════════════════════════════

def anthropic_to_openai_messages(anthropic_messages: list) -> list:
    """Convert Anthropic messages format → OpenAI messages format."""
    openai_msgs = []

    for msg in anthropic_messages:
        role = msg.get("role", "user")
        content = msg.get("content")

        if isinstance(content, str):
            openai_msgs.append({"role": role, "content": content})
            continue

        if isinstance(content, list):
            text_parts = []
            image_parts = []
            tool_uses = []
            tool_results = []

            for block in content:
                block_type = block.get("type", "text")

                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "image":
                    source = block.get("source", {})
                    if source.get("type") == "base64":
                        media_type = source.get("media_type", "image/png")
                        data = source.get("data", "")
                        image_parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{data}"}
                        })
                    elif source.get("type") == "url":
                        image_parts.append({
                            "type": "image_url",
                            "image_url": {"url": source.get("url", "")}
                        })
                elif block_type == "tool_use":
                    tool_uses.append(block)
                elif block_type == "tool_result":
                    tool_results.append(block)

            if tool_results:
                for tr in tool_results:
                    tr_content = tr.get("content", "")
                    if isinstance(tr_content, list):
                        tr_content = " ".join(
                            b.get("text", "") for b in tr_content
                            if b.get("type") == "text"
                        )
                    openai_msgs.append({
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id", ""),
                        "content": str(tr_content),
                    })
                continue

            if role == "assistant" and tool_uses:
                oai_msg = {
                    "role": "assistant",
                    "content": "\n".join(text_parts) if text_parts else None,
                    "tool_calls": [],
                }
                for tu in tool_uses:
                    oai_msg["tool_calls"].append({
                        "id": tu.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                        "type": "function",
                        "function": {
                            "name": tu.get("name", ""),
                            "arguments": json.dumps(tu.get("input", {})),
                        }
                    })
                openai_msgs.append(oai_msg)
                continue

            if image_parts:
                parts = []
                if text_parts:
                    parts.append({"type": "text", "text": "\n".join(text_parts)})
                parts.extend(image_parts)
                openai_msgs.append({"role": role, "content": parts})
            elif text_parts:
                openai_msgs.append({"role": role, "content": "\n".join(text_parts)})
            else:
                openai_msgs.append({"role": role, "content": ""})

    return openai_msgs


def anthropic_to_openai_tools(anthropic_tools: list) -> list:
    """Convert Anthropic tools → OpenAI tools format."""
    openai_tools = []
    for tool in anthropic_tools:
        if tool.get("type") in ("bash_20250124", "text_editor_20250124", "computer_20250124"):
            continue
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            }
        })
    return openai_tools


def anthropic_to_openai_request(body: dict) -> dict:
    """Convert full Anthropic request body → OpenAI request body."""
    openai_body = {
        "model": resolve_model(body.get("model", "")),
        "messages": [],
        "max_tokens": body.get("max_tokens", 4096),
    }

    system = body.get("system")
    if system:
        if isinstance(system, str):
            openai_body["messages"].append({"role": "system", "content": system})
        elif isinstance(system, list):
            sys_text = " ".join(
                b.get("text", "") for b in system if b.get("type") == "text"
            )
            if sys_text:
                openai_body["messages"].append({"role": "system", "content": sys_text})

    openai_body["messages"].extend(
        anthropic_to_openai_messages(body.get("messages", []))
    )

    if "temperature" in body:
        openai_body["temperature"] = body["temperature"]
    if "top_p" in body:
        openai_body["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        openai_body["stop"] = body["stop_sequences"]

    if body.get("stream"):
        openai_body["stream"] = True
        openai_body["stream_options"] = {"include_usage": True}

    if body.get("tools"):
        openai_body["tools"] = anthropic_to_openai_tools(body["tools"])

    tc = body.get("tool_choice")
    if tc:
        tc_type = tc.get("type", "auto")
        if tc_type == "auto":
            openai_body["tool_choice"] = "auto"
        elif tc_type == "any":
            openai_body["tool_choice"] = "required"
        elif tc_type == "tool":
            openai_body["tool_choice"] = {
                "type": "function",
                "function": {"name": tc.get("name", "")}
            }
        elif tc_type == "none":
            openai_body["tool_choice"] = "none"

    return openai_body


# ═══════════════════════════════════════════════════════════════
# FORMAT CONVERSION: OpenAI → Anthropic (response)
# ═══════════════════════════════════════════════════════════════

def _openai_stop_to_anthropic(finish_reason: str) -> str:
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }
    return mapping.get(finish_reason, "end_turn")


def openai_to_anthropic_response(raw: dict, model_requested: str) -> dict:
    """Convert OpenAI chat completion → Anthropic Message response."""
    choice = (raw.get("choices") or [{}])[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")
    usage = raw.get("usage", {})

    content_blocks = []

    text = message.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function", {})
        try:
            input_data = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            input_data = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
            "name": fn.get("name", ""),
            "input": input_data,
        })

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    if finish_reason == "tool_calls" and not tool_calls:
        finish_reason = "stop"

    msg_id = raw.get("id", "")
    if not msg_id.startswith("msg_"):
        msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model_requested,
        "content": content_blocks,
        "stop_reason": _openai_stop_to_anthropic(finish_reason),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ═══════════════════════════════════════════════════════════════
# STREAMING: OpenAI SSE → Anthropic SSE
# ═══════════════════════════════════════════════════════════════

def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ═══════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════
app = FastAPI(title="GitHub Copilot Proxy — CLI Identity", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def root():
    return {
        "status": "running",
        "service": "GitHub Copilot Proxy — CLI Identity",
        "identity": "copilot-developer-cli",
        "note": "Uses gho_ token directly (no JWT exchange)",
        "usage": "Set api_key to your GitHub token (gho_xxx), base_url to http://localhost:{port}",
    }


# ═══════════════════════════════════════════════════════════════
# COUNT TOKENS
# ═══════════════════════════════════════════════════════════════
@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Fake count_tokens endpoint required by Claude Code."""
    github_token = require_auth(request)
    await ensure_valid_token(github_token)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Invalid JSON body"}
        })

    total_chars = 0
    system = body.get("system", "")
    if isinstance(system, str):
        total_chars += len(system)
    elif isinstance(system, list):
        for b in system:
            total_chars += len(b.get("text", ""))

    total_image_tokens = 0
    for msg in body.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for b in content:
                if b.get("type") == "text":
                    total_chars += len(b.get("text", ""))
                elif b.get("type") == "image":
                    total_image_tokens += _estimate_image_tokens(b)
                elif b.get("type") == "tool_result":
                    inner = b.get("content", "")
                    if isinstance(inner, str):
                        total_chars += len(inner)
                    elif isinstance(inner, list):
                        for ib in inner:
                            if ib.get("type") == "text":
                                total_chars += len(ib.get("text", ""))
                            elif ib.get("type") == "image":
                                total_image_tokens += _estimate_image_tokens(ib)

    for tool in body.get("tools", []):
        total_chars += len(json.dumps(tool))

    estimated_tokens = max(1, total_chars // 4) + total_image_tokens

    return JSONResponse(content={"input_tokens": estimated_tokens})


# ═══════════════════════════════════════════════════════════════
# MAIN ENDPOINT — /v1/messages
# ═══════════════════════════════════════════════════════════════
@app.post("/v1/messages")
async def create_message(request: Request):
    """Anthropic Messages API compatible endpoint.
    Giả dạng Copilot CLI khi forward request tới upstream."""

    github_token = require_auth(request)
    await ensure_valid_token(github_token)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Invalid JSON body"}
        })

    # Validate required fields
    for field in ("model", "max_tokens", "messages"):
        if field not in body:
            raise HTTPException(status_code=400, detail={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": f"Missing required field: {field}"}
            })

    model_requested = body.get("model", "")
    is_stream = body.get("stream", False)

    # ─── Smart Truncation ───
    resolved = resolve_model(model_requested)
    max_prompt = get_model_prompt_limit(resolved)

    truncated_messages, was_truncated, trunc_stats = truncate_messages_for_context(
        messages=body.get("messages", []),
        system=body.get("system"),
        tools=body.get("tools", []),
        max_prompt_tokens=max_prompt,
    )

    if was_truncated:
        body = dict(body)
        body["messages"] = truncated_messages
        print(f"\n  ⚡ Context truncated (pass {trunc_stats['pass']}): "
              f"{trunc_stats['original_count']} → {trunc_stats['final_count']} messages | "
              f"~{trunc_stats.get('total_estimated', 0):,} → ~{trunc_stats.get('final_estimated', 0):,} est. tokens "
              f"(limit: {max_prompt:,}, effective: {trunc_stats['effective_limit']:,})")
        if trunc_stats.get("dropped_messages"):
            print(f"  ⚡ Dropped {trunc_stats['dropped_messages']} old messages")

    # Convert Anthropic → OpenAI
    openai_body = anthropic_to_openai_request(body)

    # Log
    if resolved != model_requested:
        print(f"\n  ↳ Model mapped: {model_requested} → {resolved}")
    else:
        print(f"\n  ↳ Model: {resolved}")
    print(f"  ↳ Identity: copilot-developer-cli | Stream: {is_stream} | max_tokens: {body.get('max_tokens', 'N/A')} | messages: {len(body.get('messages', []))} | prompt_limit: {max_prompt:,}")

    # Build CLI-style headers — gho_ token trực tiếp, KHÔNG JWT
    interaction_id = str(uuid.uuid4())
    headers = copilot_headers(github_token, interaction_id=interaction_id)
    url = f"{COPILOT_API}/chat/completions"

    if is_stream:
        async def generate():
            """Convert OpenAI SSE stream → Anthropic SSE stream."""
            msg_id = f"msg_{uuid.uuid4().hex[:24]}"
            content_index = 0
            current_block_type = None
            block_started = False
            tool_call_buffers: Dict[int, dict] = {}
            usage_data = {"input_tokens": 0, "output_tokens": 0}
            stop_reason = "end_turn"
            first_text = True
            first_tool_index_seen: Dict[int, bool] = {}

            async with httpx.AsyncClient() as client:
                try:
                    async with client.stream("POST", url, headers=headers, json=openai_body, timeout=STREAM_TIMEOUT) as resp:
                        if resp.status_code != 200:
                            err_bytes = await resp.aread()
                            err_msg = err_bytes.decode("utf-8", errors="replace")[:500]
                            print(f"  ✗ Upstream error (stream) HTTP {resp.status_code}: {err_msg}")
                            yield _sse_event("error", {
                                "type": "error",
                                "error": {
                                    "type": "api_error",
                                    "message": f"Upstream error {resp.status_code}: {err_msg}",
                                }
                            })
                            return

                        # Emit message_start
                        yield _sse_event("message_start", {
                            "type": "message_start",
                            "message": {
                                "id": msg_id,
                                "type": "message",
                                "role": "assistant",
                                "model": model_requested,
                                "content": [],
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {"input_tokens": 0, "output_tokens": 0},
                            }
                        })

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
                                    break

                                try:
                                    chunk = json.loads(payload)
                                except json.JSONDecodeError:
                                    continue

                                # Extract usage
                                if "usage" in chunk and chunk["usage"]:
                                    u = chunk["usage"]
                                    usage_data["input_tokens"] = u.get("prompt_tokens", 0)
                                    usage_data["output_tokens"] = u.get("completion_tokens", 0)

                                choices = chunk.get("choices", [])
                                if not choices:
                                    continue

                                c = choices[0]
                                delta = c.get("delta", {})
                                finish = c.get("finish_reason")

                                # ─── Text content ───
                                text_content = delta.get("content")
                                if text_content is not None:
                                    if first_text:
                                        yield _sse_event("content_block_start", {
                                            "type": "content_block_start",
                                            "index": content_index,
                                            "content_block": {"type": "text", "text": ""},
                                        })
                                        first_text = False
                                        current_block_type = "text"
                                        block_started = True

                                    if text_content:
                                        yield _sse_event("content_block_delta", {
                                            "type": "content_block_delta",
                                            "index": content_index,
                                            "delta": {"type": "text_delta", "text": text_content},
                                        })

                                # ─── Tool calls ───
                                if "tool_calls" in delta:
                                    for tc in delta["tool_calls"]:
                                        tc_index = tc.get("index", 0)
                                        fn = tc.get("function", {})
                                        fn_name = fn.get("name", "")

                                        if tc_index not in first_tool_index_seen:
                                            if block_started:
                                                yield _sse_event("content_block_stop", {
                                                    "type": "content_block_stop",
                                                    "index": content_index,
                                                })
                                                content_index += 1

                                            first_tool_index_seen[tc_index] = True
                                            tool_id = tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}")
                                            tool_call_buffers[tc_index] = {
                                                "id": tool_id,
                                                "name": fn_name,
                                                "arguments_json": "",
                                            }

                                            yield _sse_event("content_block_start", {
                                                "type": "content_block_start",
                                                "index": content_index,
                                                "content_block": {
                                                    "type": "tool_use",
                                                    "id": tool_id,
                                                    "name": fn_name,
                                                    "input": {},
                                                },
                                            })
                                            current_block_type = "tool_use"
                                            block_started = True

                                        args_chunk = fn.get("arguments", "")
                                        if args_chunk and tc_index in tool_call_buffers:
                                            tool_call_buffers[tc_index]["arguments_json"] += args_chunk
                                            yield _sse_event("content_block_delta", {
                                                "type": "content_block_delta",
                                                "index": content_index,
                                                "delta": {
                                                    "type": "input_json_delta",
                                                    "partial_json": args_chunk,
                                                },
                                            })

                                # ─── Finish reason ───
                                if finish:
                                    if finish == "tool_calls":
                                        if not first_tool_index_seen:
                                            finish = "stop"
                                    stop_reason = _openai_stop_to_anthropic(finish)

                        # ─── Close any open blocks ───
                        if block_started:
                            yield _sse_event("content_block_stop", {
                                "type": "content_block_stop",
                                "index": content_index,
                            })

                        # ─── message_delta ───
                        yield _sse_event("message_delta", {
                            "type": "message_delta",
                            "delta": {
                                "stop_reason": stop_reason,
                                "stop_sequence": None,
                            },
                            "usage": {"output_tokens": usage_data["output_tokens"]},
                        })

                        # ─── message_stop ───
                        yield _sse_event("message_stop", {"type": "message_stop"})
                        print(f"  ✓ Stream complete | stop_reason: {stop_reason} | usage: in={usage_data['input_tokens']} out={usage_data['output_tokens']}")

                except httpx.ReadTimeout:
                    print(f"  ✗ Request timeout (stream)")
                    yield _sse_event("error", {
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "Upstream server timeout (stream). The model may be overloaded — please retry."}
                    })
                except (httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
                    print(f"  ✗ Connection timeout (stream): {type(e).__name__}")
                    yield _sse_event("error", {
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": f"Upstream connection timeout: {type(e).__name__}. Please retry."}
                    })
                except httpx.HTTPError as e:
                    print(f"  ✗ HTTP error (stream): {e}")
                    yield _sse_event("error", {
                        "type": "error",
                        "error": {"type": "api_error", "message": f"Upstream error: {e}"}
                    })

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    else:
        # Non-streaming — with retry logic
        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(url, headers=headers, json=openai_body, timeout=UPSTREAM_TIMEOUT)

                if resp.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                    try:
                        err = resp.json()
                        err_msg = err.get("error", {}).get("message", resp.text[:200])
                    except Exception:
                        err_msg = resp.text[:200]
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    print(f"  ⚠ Attempt {attempt}/{MAX_RETRIES} got HTTP {resp.status_code}: {err_msg}")
                    print(f"    Retrying in {delay:.0f}s...")
                    await asyncio.sleep(delay)
                    # Re-build headers (same token — CLI doesn't exchange)
                    headers = copilot_headers(github_token, interaction_id=interaction_id)
                    continue

                if resp.status_code != 200:
                    try:
                        err = resp.json()
                        err_msg = err.get("error", {}).get("message", resp.text[:500])
                    except Exception:
                        err_msg = resp.text[:500]
                    print(f"  ✗ Upstream error HTTP {resp.status_code}: {err_msg}")
                    raise HTTPException(status_code=resp.status_code, detail={
                        "type": "error",
                        "error": {"type": "api_error", "message": err_msg}
                    })

                raw = resp.json()
                anthropic_resp = openai_to_anthropic_response(raw, model_requested)
                usage = anthropic_resp.get("usage", {})
                print(f"  ✓ Response OK (attempt {attempt}) | stop_reason: {anthropic_resp.get('stop_reason')} | usage: in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}")
                return JSONResponse(
                    content=anthropic_resp,
                    headers={"anthropic-version": "2023-06-01"},
                )

            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
                last_err = e
                timeout_type = type(e).__name__
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    print(f"  ⚠ Attempt {attempt}/{MAX_RETRIES} {timeout_type}: {e}")
                    print(f"    Retrying in {delay:.0f}s...")
                    await asyncio.sleep(delay)
                    headers = copilot_headers(github_token, interaction_id=interaction_id)
                    continue
                else:
                    print(f"  ✗ All {MAX_RETRIES} attempts failed with {timeout_type}")

            except httpx.HTTPError as e:
                last_err = e
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    print(f"  ⚠ Attempt {attempt}/{MAX_RETRIES} HTTPError: {e}")
                    print(f"    Retrying in {delay:.0f}s...")
                    await asyncio.sleep(delay)
                    headers = copilot_headers(github_token, interaction_id=interaction_id)
                    continue
                else:
                    print(f"  ✗ All {MAX_RETRIES} attempts failed with HTTPError: {e}")

        # All retries exhausted
        err_detail = str(last_err) if last_err else "Unknown error"
        print(f"  ✗ Request failed after {MAX_RETRIES} retries: {err_detail}")
        return JSONResponse(
            status_code=529,
            content={
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": f"Upstream server timeout after {MAX_RETRIES} retries. Detail: {err_detail}",
                }
            },
            headers={"anthropic-version": "2023-06-01"},
        )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5005))
    print(f"""
╔══════════════════════════════════════════════════════╗
║  GitHub Copilot Proxy — CLI Identity                 ║
║  http://127.0.0.1:{port}{' ' * (39 - len(str(port)))}║
╠══════════════════════════════════════════════════════╣
║  Identity: copilot-developer-cli (NOT vscode)        ║
║  Auth: gho_ token direct (NO JWT exchange)           ║
║  API Version: {GITHUB_API_VERSION}{' ' * (39 - len(GITHUB_API_VERSION))}║
║  User-Agent: {USER_AGENT_CHAT[:39]}{' ' * max(0, 39 - len(USER_AGENT_CHAT[:39]))}║
╠══════════════════════════════════════════════════════╣
║  POST /v1/messages              - Create Message     ║
║  POST /v1/messages/count_tokens - Count Tokens       ║
╠══════════════════════════════════════════════════════╣
║  x-api-key: gho_xxxYOUR_GITHUB_TOKEN                ║
║  anthropic-version: 2023-06-01                       ║
╚══════════════════════════════════════════════════════╝
""")
    uvicorn.run(app, host="0.0.0.0", port=port)
