#!/usr/bin/env python3
"""
GitHub Copilot Proxy — Anthropic-Compatible API Server
=======================================================
Proxy nhận request theo format Anthropic Messages API,
convert sang OpenAI format, forward tới Copilot, rồi
convert response ngược lại thành Anthropic format.

Người dùng có thể dùng Anthropic SDK (anthropic Python lib)
trỏ về server này, sử dụng GitHub token làm api_key.

Usage:
  curl http://localhost:5001/v1/messages \\
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
import time
import uuid
import os
import re
import asyncio
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
# TIMEOUT & RETRY CONFIG
# ═══════════════════════════════════════════════════════════════
# Opus models can take 3-5 minutes for complex prompts
UPSTREAM_TIMEOUT = httpx.Timeout(
    connect=15.0,    # 15s to establish connection
    read=300.0,      # 5 min to wait for response (opus is slow)
    write=30.0,      # 30s to send request body
    pool=15.0,       # 15s to acquire connection from pool
)
STREAM_TIMEOUT = httpx.Timeout(
    connect=15.0,
    read=300.0,      # 5 min — streaming first byte can be slow too
    write=30.0,
    pool=15.0,
)
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0   # seconds, exponential backoff: 2s, 4s, 8s
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# ═══════════════════════════════════════════════════════════════
# Persistent session IDs
# ═══════════════════════════════════════════════════════════════
SESSION_ID = f"{uuid.uuid4()}{int(time.time() * 1000)}"
MACHINE_ID = uuid.uuid4().hex + uuid.uuid4().hex

# ═══════════════════════════════════════════════════════════════
# MODEL MAPPING — Claude Code sends dashes, Copilot uses dots
# Claude Code:  claude-opus-4-6,  claude-sonnet-4-6,  claude-haiku-4-5
# Copilot API:  claude-opus-4.6,  claude-sonnet-4.6,  claude-haiku-4.5
# ═══════════════════════════════════════════════════════════════
MODEL_MAP = {
    # Claude Code aliases → Copilot API model IDs
    # Opus
    "claude-opus-4-6":          "claude-opus-4.6",
    "claude-opus-4-5":          "claude-opus-4.5",
    "claude-opus-4-0":          "claude-opus-4.5",
    # Sonnet
    "claude-sonnet-4-6":        "claude-sonnet-4.6",
    "claude-sonnet-4-5":        "claude-sonnet-4.5",
    "claude-sonnet-4-0":        "claude-sonnet-4",
    "claude-sonnet-4":          "claude-sonnet-4",
    # Haiku
    "claude-haiku-4-5":         "claude-haiku-4.5",
    "claude-haiku-3-5":         "claude-haiku-4.5",
    # Pass-through — already correct format
    "claude-opus-4.6":          "claude-opus-4.6",
    "claude-opus-4.5":          "claude-opus-4.5",
    "claude-sonnet-4.6":        "claude-sonnet-4.6",
    "claude-sonnet-4.5":        "claude-sonnet-4.5",
    "claude-haiku-4.5":         "claude-haiku-4.5",
}

# Regex: strip date suffix like -20251001 or -20250514
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def resolve_model(model_name: str) -> str:
    """Map Claude Code model name → Copilot API model ID.
    Handles date suffixes (e.g. claude-haiku-4-5-20251001 → claude-haiku-4.5).
    Falls back to the original name if not in map."""
    # Direct match first
    if model_name in MODEL_MAP:
        return MODEL_MAP[model_name]

    # Strip date suffix and try again
    stripped = _DATE_SUFFIX_RE.sub("", model_name)
    if stripped in MODEL_MAP:
        return MODEL_MAP[stripped]

    # Generic fallback: convert dashes in version to dots
    # e.g. claude-something-4-6 → claude-something-4.6
    m = re.match(r"^(claude-\w+)-(\d+)-(\d+)$", stripped)
    if m:
        return f"{m.group(1)}-{m.group(2)}.{m.group(3)}"

    return model_name


# ═══════════════════════════════════════════════════════════════
# MODEL PROMPT LIMITS — max_prompt_tokens per Copilot model
# Loaded from models_with_billing.json at startup
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
# Strategy (inspired by GitHub Copilot CLI):
#   Pass 0: Check if within limit → return as-is
#   Pass 1: Truncate old tool_result / long text content
#   Pass 2: Drop oldest conversation turns (keep first + last N)
#   Pass 3: Emergency — keep only last 5 messages
#
# KEY INSIGHT: Our token estimate (~3 chars/token) is rough.
# Copilot uses real tokenizer which may count MORE tokens.
# So we use a safety margin (0.85x) to avoid edge cases where
# our estimate says "OK" but Copilot says "too many".
# ═══════════════════════════════════════════════════════════════

SAFETY_MARGIN = 0.85  # Use only 85% of max_prompt_tokens to account for estimation error


def _estimate_tokens_text(text: str) -> int:
    """Estimate token count. Conservative: ~3 chars per token."""
    if not text:
        return 0
    return len(text) // 3


def _estimate_anthropic_message_tokens(msg: dict) -> int:
    """Estimate tokens for one Anthropic-format message."""
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
            elif btype == "image":
                total += 1000  # rough estimate for images
            else:
                total += _estimate_tokens_text(json.dumps(block))
        return total

    return 0


def _estimate_system_tokens(system) -> int:
    """Estimate tokens for the system prompt."""
    if isinstance(system, str):
        return _estimate_tokens_text(system)
    if isinstance(system, list):
        return sum(_estimate_tokens_text(b.get("text", "")) for b in system if b.get("type") == "text")
    return 0


def _estimate_tools_tokens(tools: list) -> int:
    """Estimate tokens for tool definitions."""
    if not tools:
        return 0
    return _estimate_tokens_text(json.dumps(tools))


def _truncate_tool_result_content(block: dict, max_chars: int = 500) -> dict:
    """Truncate a tool_result content block if too long."""
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
    """Truncate Anthropic messages to fit within max_prompt_tokens.

    Returns (truncated_messages, was_truncated, stats_dict).
    """
    # Apply safety margin — our estimate is rough, real tokenizer counts more
    effective_limit = int(max_prompt_tokens * SAFETY_MARGIN)

    # Reserve tokens for system + tools + overhead
    system_tokens = _estimate_system_tokens(system)
    tools_tokens = _estimate_tools_tokens(tools)
    overhead = 500  # formatting overhead
    available = effective_limit - system_tokens - tools_tokens - overhead

    if available <= 0:
        available = effective_limit // 2

    # Calculate total message tokens
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

    # ─── Pass 1: Truncate old tool_result / long text content ───
    result = list(messages)
    safe_zone = min(10, len(result))

    for i in range(len(result) - safe_zone):
        msg = result[i]
        content = msg.get("content")

        # Truncate long string content in old messages
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

    # Recalculate
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

        # Find safe cut point — don't break tool call chains
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
# SESSION CACHE  (github_token -> (copilot_token, api_base, expires_at))
# ═══════════════════════════════════════════════════════════════
_token_cache: Dict[str, Tuple[str, str, int]] = {}


async def exchange_token(github_token: str) -> Tuple[str, str]:
    """Đổi GitHub token -> Copilot session token + api_base."""
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
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": "Invalid API key. Please check your x-api-key header.",
            }
        })
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail={
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": f"GitHub token exchange failed (HTTP {resp.status_code})",
            }
        })

    data = resp.json()
    copilot_token = data["token"]
    api_base = data.get("endpoints", {}).get("api", "https://api.individual.githubcopilot.com")
    expires_at = data.get("expires_at", 0)

    _token_cache[github_token] = (copilot_token, api_base, expires_at)
    return copilot_token, api_base


def require_auth(request: Request) -> str:
    """Extract GitHub token từ x-api-key hoặc Authorization header.
    Anthropic SDK gửi qua x-api-key header."""
    # Anthropic style: x-api-key header
    token = request.headers.get("x-api-key", "").strip()
    if token:
        return token

    # Fallback: Authorization: Bearer xxx
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


def copilot_headers(copilot_token: str) -> dict:
    """Build headers cho request tới Copilot upstream."""
    return {
        "Authorization": f"Bearer {copilot_token}",
        "X-Request-Id": str(uuid.uuid4()),
        "X-Interaction-Type": "conversation-agent",
        "OpenAI-Intent": "conversation-agent",
        "X-Interaction-Id": str(uuid.uuid4()),
        "X-Initiator": "agent",
        "VScode-SessionId": SESSION_ID,
        "VScode-MachineId": MACHINE_ID,
        "X-GitHub-Api-Version": COPILOT_API_VERSION,
        "Editor-Plugin-Version": "copilot-chat/0.31.5",
        "Editor-Version": "vscode/1.104.1",
        "Copilot-Integration-Id": "vscode-chat",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }


# ═══════════════════════════════════════════════════════════════
# FORMAT CONVERSION: Anthropic → OpenAI (request)
# ═══════════════════════════════════════════════════════════════

def anthropic_to_openai_messages(anthropic_messages: list) -> list:
    """Convert Anthropic messages format → OpenAI messages format.
    
    Anthropic: {"role": "user", "content": "text"} hoặc
               {"role": "user", "content": [{"type": "text", "text": "..."}]}
    OpenAI:    {"role": "user", "content": "text"} hoặc
               {"role": "user", "content": [{"type": "text", "text": "..."}]}
    
    Tool use cũng cần convert:
    Anthropic assistant: content=[{type: "tool_use", id, name, input}]
    OpenAI assistant:    tool_calls=[{id, type: "function", function: {name, arguments}}]
    
    Anthropic tool_result: {role: "user", content: [{type: "tool_result", tool_use_id, content}]}
    OpenAI tool:           {role: "tool", tool_call_id, content}
    """
    openai_msgs = []

    for msg in anthropic_messages:
        role = msg.get("role", "user")
        content = msg.get("content")

        # Simple string content
        if isinstance(content, str):
            openai_msgs.append({"role": role, "content": content})
            continue

        # Array content blocks
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
                    # Anthropic image → OpenAI image_url
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

            # Tool results → separate OpenAI tool messages
            if tool_results:
                for tr in tool_results:
                    tr_content = tr.get("content", "")
                    if isinstance(tr_content, list):
                        # Extract text from content blocks
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

            # Assistant with tool_use → OpenAI tool_calls
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

            # Regular message with text + optional images
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
    """Convert Anthropic tools → OpenAI tools format.
    
    Anthropic: {"name": "get_weather", "description": "...", "input_schema": {...}}
    OpenAI:    {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    openai_tools = []
    for tool in anthropic_tools:
        # Skip special Anthropic tools (bash, text_editor, etc.)
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

    # System prompt
    system = body.get("system")
    if system:
        if isinstance(system, str):
            openai_body["messages"].append({"role": "system", "content": system})
        elif isinstance(system, list):
            # Array of text blocks
            sys_text = " ".join(
                b.get("text", "") for b in system if b.get("type") == "text"
            )
            if sys_text:
                openai_body["messages"].append({"role": "system", "content": sys_text})

    # Messages
    openai_body["messages"].extend(
        anthropic_to_openai_messages(body.get("messages", []))
    )

    # Temperature
    if "temperature" in body:
        openai_body["temperature"] = body["temperature"]

    # Top P
    if "top_p" in body:
        openai_body["top_p"] = body["top_p"]

    # Stop sequences
    if "stop_sequences" in body:
        openai_body["stop"] = body["stop_sequences"]

    # Stream
    if body.get("stream"):
        openai_body["stream"] = True
        openai_body["stream_options"] = {"include_usage": True}

    # Tools
    if body.get("tools"):
        openai_body["tools"] = anthropic_to_openai_tools(body["tools"])

    # Tool choice
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
    """Convert OpenAI finish_reason → Anthropic stop_reason."""
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }
    return mapping.get(finish_reason, "end_turn")



def openai_to_anthropic_response(raw: dict, model_requested: str) -> dict:
    """Convert OpenAI chat completion → Anthropic Message response.
    
    OpenAI:
    {
      "id": "chatcmpl-xxx",
      "choices": [{"message": {"role": "assistant", "content": "...", "tool_calls": [...]}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    }
    
    Anthropic:
    {
      "id": "msg_xxx",
      "type": "message",
      "role": "assistant",
      "model": "claude-sonnet-4",
      "content": [{"type": "text", "text": "..."}],
      "stop_reason": "end_turn",
      "stop_sequence": null,
      "usage": {"input_tokens": 10, "output_tokens": 20}
    }
    """
    choice = (raw.get("choices") or [{}])[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")
    usage = raw.get("usage", {})

    # Build content blocks
    content_blocks = []

    # Text content
    text = message.get("content")
    if text:
        content_blocks.append({
            "type": "text",
            "text": text,
        })

    # Tool calls → tool_use blocks
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

    # Nếu không có content, trả empty text
    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    # Adjust stop_reason
    if finish_reason == "tool_calls" and not tool_calls:
        finish_reason = "stop"

    # Generate anthropic-style message ID
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
# Anthropic streaming events:
#   1. message_start   — full message skeleton with usage.input_tokens
#   2. content_block_start — {type: "text", text: ""} or {type: "tool_use",...}
#   3. content_block_delta — {type: "text_delta", text: "chunk"} or
#                            {type: "input_json_delta", partial_json: "..."}
#   4. content_block_stop  — end of content block
#   5. message_delta   — stop_reason + usage.output_tokens
#   6. message_stop    — end of stream
# ═══════════════════════════════════════════════════════════════

def _sse_event(event_type: str, data: dict) -> str:
    """Format 1 Anthropic SSE event."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"



# ═══════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════
app = FastAPI(title="GitHub Copilot Proxy — Anthropic Compatible", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def root():
    return {
        "status": "running",
        "service": "GitHub Copilot Proxy — Anthropic Compatible",
        "usage": "Set api_key to your GitHub token (gho_xxx), base_url to http://localhost:{port}",
    }


# ═══════════════════════════════════════════════════════════════
# COUNT TOKENS — Required by Claude Code gateway spec
# ═══════════════════════════════════════════════════════════════
@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Fake count_tokens endpoint required by Claude Code.
    Returns a reasonable estimate — Copilot doesn't have a native count endpoint."""
    github_token = require_auth(request)
    # Validate token still works
    await exchange_token(github_token)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Invalid JSON body"}
        })

    # Rough estimate: 4 chars ≈ 1 token
    total_chars = 0
    system = body.get("system", "")
    if isinstance(system, str):
        total_chars += len(system)
    elif isinstance(system, list):
        for b in system:
            total_chars += len(b.get("text", ""))

    for msg in body.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for b in content:
                if b.get("type") == "text":
                    total_chars += len(b.get("text", ""))

    # Add tool definitions size
    for tool in body.get("tools", []):
        total_chars += len(json.dumps(tool))

    estimated_tokens = max(1, total_chars // 4)

    return JSONResponse(content={
        "input_tokens": estimated_tokens,
    })


@app.post("/v1/messages")
async def create_message(request: Request):
    """Anthropic Messages API compatible endpoint.
    Nhận request theo format Anthropic, convert → OpenAI, forward tới Copilot,
    rồi convert response ngược lại → Anthropic format."""

    github_token = require_auth(request)
    copilot_token, api_base = await exchange_token(github_token)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Invalid JSON body"}
        })

    # Validate required fields
    if "model" not in body:
        raise HTTPException(status_code=400, detail={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Missing required field: model"}
        })
    if "max_tokens" not in body:
        raise HTTPException(status_code=400, detail={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Missing required field: max_tokens"}
        })
    if "messages" not in body:
        raise HTTPException(status_code=400, detail={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Missing required field: messages"}
        })

    model_requested = body.get("model", "")
    is_stream = body.get("stream", False)

    # ─── Smart Truncation: fit messages within Copilot's max_prompt_tokens ───
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

    # Convert Anthropic request → OpenAI format (AFTER truncation)
    openai_body = anthropic_to_openai_request(body)

    # Log
    if resolved != model_requested:
        print(f"\n  ↳ Model mapped: {model_requested} → {resolved}")
    else:
        print(f"\n  ↳ Model: {resolved}")
    print(f"  ↳ Stream: {is_stream} | max_tokens: {body.get('max_tokens', 'N/A')} | messages: {len(body.get('messages', []))} | prompt_limit: {max_prompt:,}")

    # Forward to Copilot
    headers = copilot_headers(copilot_token)
    url = f"{api_base}/chat/completions"

    if is_stream:
        async def generate():
            """Convert OpenAI SSE stream → Anthropic SSE stream."""
            msg_id = f"msg_{uuid.uuid4().hex[:24]}"
            content_index = 0
            current_block_type = None  # "text" or "tool_use"
            block_started = False
            tool_call_buffers: Dict[int, dict] = {}  # index → {id, name, arguments_json}
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
                                    # Start text block if needed
                                    if first_text:
                                        yield _sse_event("content_block_start", {
                                            "type": "content_block_start",
                                            "index": content_index,
                                            "content_block": {"type": "text", "text": ""},
                                        })
                                        first_text = False
                                        current_block_type = "text"
                                        block_started = True

                                    if text_content:  # Non-empty text
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
                                            # New tool call — close previous text block first
                                            if block_started and current_block_type == "text":
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

                                            # Emit content_block_start for tool_use
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

                                        # Accumulate arguments
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
                                        # Check if we had any real tool calls
                                        if not first_tool_index_seen:
                                            finish = "stop"
                                    stop_reason = _openai_stop_to_anthropic(finish)

                        # ─── Close any open blocks ───
                        if block_started:
                            yield _sse_event("content_block_stop", {
                                "type": "content_block_stop",
                                "index": content_index,
                            })

                        # ─── message_delta with stop_reason + output_tokens ───
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
        # Non-streaming — with retry logic for transient errors
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
                    # Refresh Copilot token in case it expired during wait
                    copilot_token, api_base = await exchange_token(github_token)
                    headers = copilot_headers(copilot_token)
                    url = f"{api_base}/chat/completions"
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
                    # Refresh Copilot token in case it expired
                    copilot_token, api_base = await exchange_token(github_token)
                    headers = copilot_headers(copilot_token)
                    url = f"{api_base}/chat/completions"
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
                    copilot_token, api_base = await exchange_token(github_token)
                    headers = copilot_headers(copilot_token)
                    url = f"{api_base}/chat/completions"
                    continue
                else:
                    print(f"  ✗ All {MAX_RETRIES} attempts failed with HTTPError: {e}")

        # All retries exhausted — return Anthropic-format error (not 500 crash)
        err_detail = str(last_err) if last_err else "Unknown error"
        print(f"  ✗ Request failed after {MAX_RETRIES} retries: {err_detail}")
        return JSONResponse(
            status_code=529,  # Anthropic overloaded status code
            content={
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": f"Upstream server timeout after {MAX_RETRIES} retries. The model may be overloaded — please retry. Detail: {err_detail}",
                }
            },
            headers={"anthropic-version": "2023-06-01"},
        )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5001))
    print(f"""
╔══════════════════════════════════════════════════════╗
║  GitHub Copilot Proxy — Anthropic Compatible         ║
║  http://127.0.0.1:{port}{' ' * (39 - len(str(port)))}║
╠══════════════════════════════════════════════════════╣
║  POST /v1/messages              - Create Message     ║
║  POST /v1/messages/count_tokens - Count Tokens       ║
╠══════════════════════════════════════════════════════╣
║  Model Mapping (Claude Code → Copilot):              ║
║    claude-opus-4-6   → claude-opus-4.6               ║
║    claude-sonnet-4-6 → claude-sonnet-4.6             ║
║    claude-sonnet-4   → claude-sonnet-4               ║
║    claude-haiku-4-5  → claude-haiku-4.5              ║
╠══════════════════════════════════════════════════════╣
║  x-api-key: gho_xxxYOUR_GITHUB_TOKEN                ║
║  anthropic-version: 2023-06-01                       ║
╚══════════════════════════════════════════════════════╝
""")
    uvicorn.run(app, host="0.0.0.0", port=port)
