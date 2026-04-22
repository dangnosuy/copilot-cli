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
CLI_VERSION = "1.0.24"
_os_name = "linux" if platform.system() == "Linux" else platform.system().lower()
_node_version = "v24.11.1"

# User-Agent giống hệt CLI thật (2 dạng)
# Format từ app.js: `${name}/${version} (${clientName}${platform} ${nodeVersion}) term/${TERM_PROGRAM}`
# name = "@github/copilot" → akr() → "copilot"
# clientName = "github/cli" → "client/github/cli "
USER_AGENT = f"copilot/{CLI_VERSION} ({_os_name} {_node_version}) term/unknown"
USER_AGENT_CHAT = f"copilot/{CLI_VERSION} (client/github/cli {_os_name} {_node_version}) term/unknown"

# API version CLI dùng (KHÁC với VSCode)
GITHUB_API_VERSION = "2026-01-09"

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
RETRYABLE_STATUS_CODES = {400, 429, 500, 502, 503, 504}

# ═══════════════════════════════════════════════════════════════
# Persistent session IDs — giống CLI thật
# ═══════════════════════════════════════════════════════════════
SESSION_ID = str(uuid.uuid4())
MACHINE_ID = hashlib.sha256(uuid.getnode().to_bytes(6, 'big')).hexdigest()

# ═══════════════════════════════════════════════════════════════
# NEW: Machine ID format UUID (như CLI thật) & Telemetry config
# ═══════════════════════════════════════════════════════════════
# CLI thật dùng format UUID cho X-Client-Machine-Id
MACHINE_ID_UUID = str(uuid.UUID(int=uuid.getnode()))
TELEMETRY_HOST = "telemetry.individual.githubcopilot.com"
COPILOT_TRACKING_ID = hashlib.md5(MACHINE_ID_UUID.encode()).hexdigest()

# X-Stainless headers — CLI thật gửi kèm mỗi request
_arch = platform.machine()
if _arch == "x86_64":
    _arch = "x64"
elif _arch == "aarch64":
    _arch = "arm64"

STAINLESS_HEADERS = {
    "X-Stainless-Retry-Count": "0",
    "X-Stainless-Lang": "js",
    "X-Stainless-Package-Version": "5.20.1",
    "X-Stainless-Os": platform.system(),
    "X-Stainless-Arch": _arch,
    "X-Stainless-Runtime": "node",
    "X-Stainless-Runtime-Version": _node_version,
}

# Experiment assignment context — auto-fetched on first request (lazy init)
EXP_ASSIGNMENT_CONTEXT = ""
_telemetry_fetched = False  # Flag để chỉ fetch 1 lần duy nhất

# ═══════════════════════════════════════════════════════════════
# MODEL MAPPING — Claude Code sends dashes, Copilot uses dots
# ═══════════════════════════════════════════════════════════════
MODEL_MAP = {
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
    # Pass-through (already correct format)
    "claude-opus-4.6":          "claude-opus-4.6",
    "claude-opus-4.5":          "claude-opus-4.5",
    "claude-sonnet-4.6":        "claude-sonnet-4.6",
    "claude-sonnet-4.5":        "claude-sonnet-4.5",
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
# AUTO TELEMETRY — Tự fetch AssignmentContext ở request đầu tiên
# Giống CLI thật: lúc startup nó GET /telemetry để lấy experiment
# flags, rồi gửi kèm X-Copilot-Client-Exp-Assignment-Context
# trong MỌI request sau đó.
# ═══════════════════════════════════════════════════════════════

async def _auto_fetch_telemetry(github_token: str):
    """Lazy-init: tự động fetch experiment config từ telemetry endpoint.

    Chỉ chạy 1 lần duy nhất khi có request đầu tiên (lúc đó mới có token).
    CLI thật làm việc này lúc startup, nhưng proxy chưa có token lúc boot.
    """
    global EXP_ASSIGNMENT_CONTEXT, _telemetry_fetched

    if _telemetry_fetched:
        return

    _telemetry_fetched = True  # Set trước để tránh race condition

    print(f"\n  🔄 Auto-fetching telemetry config (first request)...")

    params = {"tas-endpoint": "githubdevdiv"}
    headers = {
        "User-Agent": "copilot-cli",
        "X-Exp-Sdk-Version": "1",
        "X-Vscode-Extensionname": "CopilotCLI",
        "X-Msedge-Clientid": str(uuid.uuid4()),
        "X-Exp-Parameters": (
            f"copilottrackingid={COPILOT_TRACKING_ID},"
            f"github_copilotcli_cliversion={CLI_VERSION}.9999,"
            "github_copilotcli_prerelease=0,"
            "github_copilotcli_audience=external,"
            "github_copilotcli_experimentationoptin=0,"
            "extensionname=CopilotCLI,"
            f"github_copilotcli_firstlaunchat={int(time.time())},"
            "github_copilotcli_copilotplan=individual"
        ),
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://{TELEMETRY_HOST}/telemetry",
                params=params,
                headers=headers,
                timeout=10,
            )

            if resp.status_code == 200:
                data = resp.json()
                EXP_ASSIGNMENT_CONTEXT = data.get("AssignmentContext", "")
                features = data.get("Features", [])
                flights = data.get("Flights", {})
                print(f"  ✅ Telemetry loaded | Context: {EXP_ASSIGNMENT_CONTEXT[:80]}...")
                print(f"     Features: {len(features)} | Flights: {len(flights)} groups")
            else:
                print(f"  ⚠ Telemetry fetch failed: HTTP {resp.status_code} (non-critical, continuing)")
    except Exception as e:
        print(f"  ⚠ Telemetry fetch error: {e} (non-critical, continuing)")


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


def copilot_headers(
    github_token: str,
    interaction_id: str = "",
    initiator: str = "user",
    interaction_type: str = "conversation-agent",
    agent_task_id: str = "",
    streaming: bool = False,
) -> dict:
    """Build headers giả dạng Copilot CLI chính chủ v1.0.24.

    KEY DIFFERENCE vs server-anthropic.py:
    - Dùng gho_ token trực tiếp (KHÔNG JWT)
    - Copilot-Integration-Id: copilot-developer-cli (KHÔNG vscode-chat)
    - KHÔNG có Editor-Version, Editor-Plugin-Version, VScode-*
    - User-Agent format CLI
    - X-GitHub-Api-Version: 2026-01-09
    - Có X-Stainless-* headers
    - Có X-Agent-Task-Id, X-Client-Machine-Id
    - X-Initiator: "user" (v1.0.24 baseHeaders dùng "user", không phải "agent")
    - Runtime-Client-Version: CLI version (header mới trong v1.0.24)
    """
    # CLI thật dùng text/event-stream cho streaming requests
    accept_header = "text/event-stream" if streaming else "application/json"

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Content-Type": "application/json",
        "Accept": accept_header,
        "User-Agent": USER_AGENT_CHAT,
        "X-Github-Api-Version": GITHUB_API_VERSION,
        "Copilot-Integration-Id": "copilot-developer-cli",
        "OpenAI-Intent": "conversation-agent",
        "X-Initiator": "agent",  # "user" in v1.0.24 baseHeaders
        "X-Interaction-Id": interaction_id or str(uuid.uuid4()),
        "X-Interaction-Type": interaction_type,
        "X-Client-Session-Id": SESSION_ID,
        "X-Client-Machine-Id": MACHINE_ID_UUID,
        "Runtime-Client-Version": CLI_VERSION,  # NEW in v1.0.24
    }

    # Add X-Stainless headers (như CLI thật)
    headers.update(STAINLESS_HEADERS)

    # Add agent task ID if provided
    if agent_task_id:
        headers["X-Agent-Task-Id"] = agent_task_id

    # Add experiment context if available
    if EXP_ASSIGNMENT_CONTEXT:
        headers["X-Copilot-Client-Exp-Assignment-Context"] = EXP_ASSIGNMENT_CONTEXT

    return headers


def copilot_headers_native(
    github_token: str,
    interaction_id: str = "",
    interaction_type: str = "conversation-agent",
    agent_task_id: str = "",
    streaming: bool = False,
    anthropic_beta: str = "",
) -> dict:
    """Build headers for Anthropic native /v1/messages passthrough.

    Similar to copilot_headers() but also forwards anthropic-beta header
    which is required for features like prompt caching, extended output, etc.
    """
    headers = copilot_headers(
        github_token=github_token,
        interaction_id=interaction_id,
        interaction_type=interaction_type,
        agent_task_id=agent_task_id,
        streaming=streaming,
    )

    # Forward anthropic-beta header if present (critical for Claude Code features)
    # e.g. "prompt-caching-2024-07-31,pdfs-2024-09-25,output-128k-2025-02-19"
    if anthropic_beta:
        headers["anthropic-beta"] = anthropic_beta

    return headers


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
            if not content.strip():
                content = " "
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
                if text_parts or image_parts:
                    parts = []
                    if text_parts:
                        parts.append({"type": "text", "text": "\n".join(text_parts)})
                    parts.extend(image_parts)
                    if len(parts) == 1 and parts[0]["type"] == "text":
                        openai_msgs.append({"role": role, "content": parts[0]["text"]})
                    else:
                        openai_msgs.append({"role": role, "content": parts})
                
                for tr in tool_results:
                    tr_content = tr.get("content", "")
                    if isinstance(tr_content, list):
                        tr_content = "\n".join(
                            b.get("text", "") for b in tr_content
                            if b.get("type") == "text"
                        )
                    content_str = str(tr_content)
                    if not content_str.strip():
                        content_str = " "
                    openai_msgs.append({
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id", ""),
                        "content": content_str,
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
                txt = "\n".join(text_parts)
                if not txt.strip():
                    txt = " "
                openai_msgs.append({"role": role, "content": txt})
            else:
                openai_msgs.append({"role": role, "content": " "})

    return openai_msgs


def strip_beta_fields_from_tools(tools: list) -> list:
    """Strip experimental beta fields from tools that Copilot API rejects with 400.

    Claude Code sends fields like 'strict', 'eager_input_streaming', 'defer_loading'
    that are first-party Anthropic-only features. Copilot API does not accept these.

    Also converts built-in Anthropic tools (e.g. web_search_20250305) to custom tool
    format that Copilot accepts, and removes unsupported built-in tools.
    """
    if not tools:
        return tools

    # Fields that cause 400 on Copilot API (1P-only experimental fields)
    BETA_FIELDS_TO_STRIP = {"strict", "eager_input_streaming", "defer_loading"}

    # Built-in Anthropic tool types to skip entirely
    BUILTIN_TOOL_TYPES = {
        "bash_20250124", "text_editor_20250124", "computer_20250124",
        "bash_20241022", "text_editor_20241022", "computer_20241022",
    }

    cleaned_tools = []
    for tool in tools:
        tool_type = tool.get("type", "")

        # Skip known unsupported built-in tools
        if tool_type in BUILTIN_TOOL_TYPES:
            continue

        # Convert web_search built-in tool → custom tool format
        if isinstance(tool_type, str) and tool_type.startswith("web_search"):
            print(f"  ↳ Converting built-in tool '{tool_type}' → custom tool 'web_search'")
            cleaned_tools.append({
                "name": tool.get("name", "web_search"),
                "description": "Search the web for real-time information.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query"},
                    },
                    "required": ["query"],
                },
            })
            continue

        # Other unknown built-in tools (have "type" field that's not standard) — skip
        if tool_type and tool_type not in ("", "custom"):
            # Check if it looks like a built-in (has type but no name/input_schema at top level)
            if "name" not in tool and "input_schema" not in tool:
                print(f"  ↳ Removing unsupported built-in tool: '{tool_type}'")
                continue

        # Custom tools: strip experimental beta fields
        cleaned_tool = {}
        for k, v in tool.items():
            if k not in BETA_FIELDS_TO_STRIP:
                cleaned_tool[k] = v

        cleaned_tools.append(cleaned_tool)

    return cleaned_tools


def anthropic_to_openai_tools(anthropic_tools: list) -> list:
    """Convert Anthropic tools → OpenAI tools format."""
    # First strip beta fields, then convert
    cleaned = strip_beta_fields_from_tools(anthropic_tools)
    openai_tools = []
    for tool in cleaned:
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
    max_tokens = body.get("max_tokens", 4096)
    if max_tokens > 4096:
        max_tokens = 4096
    
    openai_body = {
        "model": resolve_model(body.get("model", "")),
        "messages": [],
        "max_tokens": max_tokens,
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
# ANTHROPIC NATIVE PASSTHROUGH — Gửi trực tiếp format Anthropic
# tới /v1/messages trên Copilot API (không cần convert OpenAI)
# ═══════════════════════════════════════════════════════════════

# Date suffix regex for stripping (e.g. claude-sonnet-4-20250414 → claude-sonnet-4)
_DATE_SUFFIX_STRIP_RE = re.compile(r"^(claude-[\w.]+-[\d]+(?:[.-][\d]+)*)-\d{8,}$")


def _strip_model_date_suffix(model: str) -> str:
    """Strip date suffix from model name for Copilot API compatibility.
    e.g. claude-sonnet-4-20250414 → claude-sonnet-4
    """
    m = _DATE_SUFFIX_STRIP_RE.match(model.strip())
    return m.group(1) if m else model.strip()


def is_claude_model(model: str) -> bool:
    """Check if model is a Claude model (supports /v1/messages native endpoint)."""
    stripped = _strip_model_date_suffix(model)
    return stripped.startswith("claude-")


_ALLOWED_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MEDIA_TYPE_ALIASES = {
    # Common MIME types that Copilot API rejects — map to closest allowed type
    "image/jpg": "image/jpeg",
    "image/jpe": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
    "image/svg+xml": "image/png",
    "image/tiff": "image/png",
    "image/bmp": "image/png",
    "image/avif": "image/webp",
    "image/heic": "image/jpeg",
    "image/heif": "image/jpeg",
}


def _fix_image_block(block: dict) -> dict:
    """Fix or remove invalid media_type in a single image block."""
    src = block.get("source", {})
    if src.get("type") != "base64":
        return block
    mt = src.get("media_type", "")
    if mt in _ALLOWED_IMAGE_MEDIA_TYPES:
        return block
    # Try alias mapping first
    fixed = _MEDIA_TYPE_ALIASES.get(mt)
    if fixed:
        block = dict(block)
        block["source"] = dict(src)
        block["source"]["media_type"] = fixed
        return block
    # Unknown media_type → default to image/png (safest fallback)
    block = dict(block)
    block["source"] = dict(src)
    block["source"]["media_type"] = "image/png"
    return block


def _sanitize_images_in_content(content) -> tuple:
    """Recursively sanitize image media_types in a content block list or string.

    Returns (new_content, changed: bool).
    """
    if not isinstance(content, list):
        return content, False
    new_content = []
    changed = False
    for block in content:
        btype = block.get("type", "")
        if btype == "image":
            fixed = _fix_image_block(block)
            if fixed is not block:
                changed = True
            new_content.append(fixed)
        elif btype == "tool_result":
            inner, inner_changed = _sanitize_images_in_content(block.get("content", ""))
            if inner_changed:
                block = dict(block)
                block["content"] = inner
                changed = True
            new_content.append(block)
        else:
            new_content.append(block)
    return new_content, changed


def _sanitize_messages_images(messages: list) -> list:
    """Walk all messages and fix any invalid image media_types in-place (returns new list)."""
    result = []
    for msg in messages:
        content = msg.get("content")
        new_content, changed = _sanitize_images_in_content(content)
        if changed:
            msg = dict(msg)
            msg["content"] = new_content
        result.append(msg)
    return result


def build_passthrough_payload(body: dict) -> dict:
    """Build clean Anthropic payload for native /v1/messages passthrough.

    Only includes known Anthropic API fields, strips beta fields from tools,
    removes unknown top-level fields that Copilot may reject.
    Sanitizes image media_types to only allowed values.
    """
    model = _strip_model_date_suffix(body.get("model", ""))
    # Also apply MODEL_MAP for dash→dot conversion
    model = resolve_model(model)

    # Sanitize image media_types — Copilot only accepts jpeg/png/gif/webp
    messages = _sanitize_messages_images(body.get("messages", []))

    out = {
        "model": model,
        "messages": messages,
        "max_tokens": body.get("max_tokens", 4096),
    }

    # Stream
    if body.get("stream") is not None:
        out["stream"] = body["stream"]

    # Optional standard Anthropic fields — only include if present
    if body.get("system") is not None:
        out["system"] = body["system"]
    if body.get("metadata") is not None:
        out["metadata"] = body["metadata"]
    if body.get("stop_sequences") is not None:
        out["stop_sequences"] = body["stop_sequences"]
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    if body.get("top_k") is not None:
        out["top_k"] = body["top_k"]
    if body.get("tool_choice") is not None:
        out["tool_choice"] = body["tool_choice"]
    if body.get("thinking") is not None:
        out["thinking"] = body["thinking"]
    if body.get("service_tier") is not None:
        out["service_tier"] = body["service_tier"]

    # Strip beta fields from tools
    if body.get("tools") is not None:
        out["tools"] = strip_beta_fields_from_tools(body["tools"])

    return out


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
# TELEMETRY — Giả dạng Copilot CLI telemetry (Full Clone)
# ═══════════════════════════════════════════════════════════════

@app.get("/internal/telemetry")
async def get_telemetry_config(request: Request):
    """Proxy GET telemetry request to GitHub, parse experiment config.

    CLI thật gọi endpoint này lúc startup để lấy:
    - Features/Flights (A/B testing flags)
    - AssignmentContext (được gửi kèm trong mỗi request sau đó)
    """
    global EXP_ASSIGNMENT_CONTEXT

    github_token = require_auth(request)

    params = {"tas-endpoint": "githubdevdiv"}
    headers = {
        "User-Agent": "copilot-cli",
        "X-Exp-Sdk-Version": "1",
        "X-Vscode-Extensionname": "CopilotCLI",
        "X-Msedge-Clientid": str(uuid.uuid4()),
        "X-Exp-Parameters": (
            f"copilottrackingid={COPILOT_TRACKING_ID},"
            f"github_copilotcli_cliversion={CLI_VERSION}.9999,"
            "github_copilotcli_prerelease=0,"
            "github_copilotcli_audience=external,"
            "github_copilotcli_experimentationoptin=0,"
            "extensionname=CopilotCLI,"
            f"github_copilotcli_firstlaunchat={int(time.time())},"
            "github_copilotcli_copilotplan=individual"
        ),
    }

    print(f"\n  ↳ GET /internal/telemetry → {TELEMETRY_HOST}")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://{TELEMETRY_HOST}/telemetry",
                params=params,
                headers=headers,
                timeout=15,
            )

            if resp.status_code == 200:
                data = resp.json()
                EXP_ASSIGNMENT_CONTEXT = data.get("AssignmentContext", "")
                print(f"  ✓ Telemetry config loaded | AssignmentContext: {EXP_ASSIGNMENT_CONTEXT[:50]}...")
                return JSONResponse(content=data)

            print(f"  ✗ Telemetry GET failed: HTTP {resp.status_code}")
            return JSONResponse(
                status_code=resp.status_code,
                content={"error": resp.text[:500]}
            )
    except Exception as e:
        print(f"  ✗ Telemetry GET error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/internal/telemetry")
async def post_telemetry(request: Request):
    """Proxy POST telemetry (metrics) to GitHub.

    CLI thật gửi telemetry events ở background (gzip compressed).
    Format: application/x-json-stream
    """
    body = await request.body()

    headers = {
        "Content-Type": "application/x-json-stream",
        "Content-Encoding": "gzip",
        "User-Agent": "undici",
    }

    print(f"\n  ↳ POST /internal/telemetry → {TELEMETRY_HOST} ({len(body)} bytes)")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://{TELEMETRY_HOST}/telemetry",
                content=body,
                headers=headers,
                timeout=15,
            )

            if resp.status_code == 200:
                data = resp.json()
                print(f"  ✓ Telemetry POST OK | itemsAccepted: {data.get('itemsAccepted', 'N/A')}")
                return JSONResponse(content=data)

            print(f"  ✗ Telemetry POST failed: HTTP {resp.status_code}")
            return JSONResponse(
                status_code=resp.status_code,
                content={"error": resp.text[:200]}
            )
    except Exception as e:
        print(f"  ✗ Telemetry POST error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# ═══════════════════════════════════════════════════════════════
# RESPONSES — OpenAI Responses API (gpt-5-mini cho session titles)
# ═══════════════════════════════════════════════════════════════

@app.post("/internal/responses")
async def create_response(request: Request):
    """Proxy to /responses endpoint (gpt-5-mini for session titles).

    CLI thật dùng endpoint này để generate session title ở background.
    Request format là OpenAI Responses API (khác với Chat Completions).
    """
    github_token = require_auth(request)
    await ensure_valid_token(github_token)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Invalid JSON body"}
        })

    interaction_id = str(uuid.uuid4())
    agent_task_id = str(uuid.uuid4())

    headers = copilot_headers(
        github_token,
        interaction_id=interaction_id,
        interaction_type="conversation-background",  # Background task for session title
        agent_task_id=agent_task_id,
    )

    url = f"{COPILOT_API}/responses"
    model = body.get("model", "gpt-5-mini")

    print(f"\n  ↳ POST /internal/responses → {url}")
    print(f"  ↳ Model: {model} | Task: {agent_task_id[:8]}...")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=headers,
                json=body,
                timeout=UPSTREAM_TIMEOUT,
            )

            if resp.status_code == 200:
                data = resp.json()
                print(f"  ✓ Responses OK | status: {data.get('status', 'N/A')}")
                return JSONResponse(content=data)

            err_text = resp.text[:500]
            print(f"  ✗ Responses failed: HTTP {resp.status_code}: {err_text}")
            return JSONResponse(
                status_code=resp.status_code,
                content={"error": err_text}
            )
    except Exception as e:
        print(f"  ✗ Responses error: {e}")
        raise HTTPException(status_code=500, detail={"error": str(e)})


# ═══════════════════════════════════════════════════════════════
# HELPER: OpenAI Stream Generator (used by both main path & native fallback)
# ═══════════════════════════════════════════════════════════════

async def _generate_openai_stream(body: dict, github_token: str, model_requested: str):
    """Generate Anthropic SSE events from OpenAI /chat/completions stream.

    This is extracted as a helper so it can be called both from the main OpenAI path
    and as a fallback when /v1/messages rejects the model.
    """
    openai_body = anthropic_to_openai_request(body)
    # Force streaming
    openai_body["stream"] = True
    openai_body["stream_options"] = {"include_usage": True}

    interaction_id = str(uuid.uuid4())
    agent_task_id = str(uuid.uuid4())
    _headers = copilot_headers(
        github_token,
        interaction_id=interaction_id,
        interaction_type="conversation-agent",
        agent_task_id=agent_task_id,
        streaming=True,
    )
    _url = f"{COPILOT_API}/chat/completions"

    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    content_index = 0
    current_block_type = None
    block_started = False
    tool_call_buffers: Dict[int, dict] = {}
    usage_data = {"input_tokens": 0, "output_tokens": 0}
    stop_reason = "end_turn"
    first_text = True
    first_tool_index_seen: Dict[int, bool] = {}

    for stream_attempt in range(1, MAX_RETRIES + 1):
        # Reset state for each attempt
        content_index = 0
        current_block_type = None
        block_started = False
        tool_call_buffers = {}
        usage_data = {"input_tokens": 0, "output_tokens": 0}
        stop_reason = "end_turn"
        first_text = True
        first_tool_index_seen = {}

        async with httpx.AsyncClient() as client:
            try:
                async with client.stream("POST", _url, headers=_headers, json=openai_body, timeout=STREAM_TIMEOUT) as resp:
                    if resp.status_code != 200:
                        err_bytes = await resp.aread()
                        err_msg = err_bytes.decode("utf-8", errors="replace")[:500]
                        if resp.status_code in RETRYABLE_STATUS_CODES and stream_attempt < MAX_RETRIES:
                            delay = RETRY_BASE_DELAY * (2 ** (stream_attempt - 1))
                            print(f"  ⚠ OpenAI stream attempt {stream_attempt}/{MAX_RETRIES} got HTTP {resp.status_code}: {err_msg}")
                            print(f"    Retrying in {delay:.0f}s...")
                            await asyncio.sleep(delay)
                            _headers = copilot_headers(
                                github_token,
                                interaction_id=str(uuid.uuid4()),
                                interaction_type="conversation-agent",
                                agent_task_id=str(uuid.uuid4()),
                                streaming=True,
                            )
                            continue
                        print(f"  ✗ OpenAI upstream error HTTP {resp.status_code}: {err_msg}")
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

                            # Text content
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

                            # Tool calls
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

                            # Finish reason
                            if finish:
                                if finish == "tool_calls" and not first_tool_index_seen:
                                    finish = "stop"
                                stop_reason = _openai_stop_to_anthropic(finish)

                    # Close any open blocks
                    if block_started:
                        yield _sse_event("content_block_stop", {
                            "type": "content_block_stop",
                            "index": content_index,
                        })

                    # message_delta
                    yield _sse_event("message_delta", {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": stop_reason,
                            "stop_sequence": None,
                        },
                        "usage": {"output_tokens": usage_data["output_tokens"]},
                    })

                    # message_stop
                    yield _sse_event("message_stop", {"type": "message_stop"})
                    print(f"  ✓ OpenAI stream complete | stop_reason: {stop_reason} | usage: in={usage_data['input_tokens']} out={usage_data['output_tokens']}")
                    return

            except httpx.ReadTimeout:
                print(f"  ✗ OpenAI stream timeout")
                yield _sse_event("error", {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "Upstream timeout. Please retry."}
                })
                return
            except (httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
                print(f"  ✗ OpenAI connection timeout: {type(e).__name__}")
                yield _sse_event("error", {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": f"Connection timeout: {type(e).__name__}. Please retry."}
                })
                return
            except httpx.HTTPError as e:
                print(f"  ✗ OpenAI HTTP error: {e}")
                yield _sse_event("error", {
                    "type": "error",
                    "error": {"type": "api_error", "message": f"Upstream error: {e}"}
                })
                return


# ═══════════════════════════════════════════════════════════════
# MAIN ENDPOINT — /v1/messages
# ═══════════════════════════════════════════════════════════════
@app.post("/v1/messages")
async def create_message(request: Request):
    """Anthropic Messages API compatible endpoint.
    Giả dạng Copilot CLI khi forward request tới upstream."""

    github_token = require_auth(request)
    await ensure_valid_token(github_token)

    # ═══ Auto-fetch telemetry ở request đầu tiên (lazy init) ═══
    # CLI thật GET /telemetry lúc startup, nhưng proxy chưa có token lúc boot
    # nên ta fetch lần đầu ở đây. AssignmentContext sẽ được gửi kèm mọi request.
    await _auto_fetch_telemetry(github_token)

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

    # ═══════════════════════════════════════════════════════════
    # ROUTE: Anthropic Native Passthrough vs OpenAI Translation
    # ═══════════════════════════════════════════════════════════
    # Claude models → /v1/messages (native Anthropic format, no conversion)
    # Non-Claude models → /chat/completions (OpenAI translation)
    use_native_passthrough = is_claude_model(model_requested)

    # Extract anthropic-beta header from incoming request (for passthrough)
    anthropic_beta = request.headers.get("anthropic-beta", "")

    # Log
    if resolved != model_requested:
        print(f"\n  ↳ Model mapped: {model_requested} → {resolved}")
    else:
        print(f"\n  ↳ Model: {resolved}")
    route_label = "NATIVE /v1/messages" if use_native_passthrough else "OPENAI /chat/completions"
    print(f"  ↳ Identity: copilot-developer-cli | Route: {route_label} | Stream: {is_stream} | max_tokens: {body.get('max_tokens', 'N/A')} | messages: {len(body.get('messages', []))} | prompt_limit: {max_prompt:,}")
    if anthropic_beta:
        print(f"  ↳ anthropic-beta: {anthropic_beta}")

    # Build CLI-style headers
    interaction_id = str(uuid.uuid4())
    agent_task_id = str(uuid.uuid4())

    if use_native_passthrough:
        # ═══════════════════════════════════════════════════════
        # PATH A: Anthropic Native Passthrough (/v1/messages)
        # Gửi trực tiếp format Anthropic, không cần convert OpenAI
        # ═══════════════════════════════════════════════════════
        passthrough_body = build_passthrough_payload(body)
        url = f"{COPILOT_API}/v1/messages"

        headers = copilot_headers_native(
            github_token,
            interaction_id=interaction_id,
            interaction_type="conversation-agent",
            agent_task_id=agent_task_id,
            streaming=is_stream,
            anthropic_beta=anthropic_beta,
        )

        if is_stream:
            async def generate_native():
                """Stream Anthropic SSE directly from Copilot /v1/messages (passthrough)."""
                _headers = dict(headers)

                for stream_attempt in range(1, MAX_RETRIES + 1):
                    async with httpx.AsyncClient() as client:
                        try:
                            async with client.stream("POST", url, headers=_headers, json=passthrough_body, timeout=STREAM_TIMEOUT) as resp:
                                if resp.status_code != 200:
                                    err_bytes = await resp.aread()
                                    err_msg = err_bytes.decode("utf-8", errors="replace")[:500]

                                    # If /v1/messages returns 400 with "model is not supported",
                                    # fall back to OpenAI translation path
                                    if resp.status_code == 400 and "model is not supported" in err_msg.lower():
                                        print(f"  ⚠ /v1/messages rejected model '{resolved}' (400: model not supported)")
                                        print(f"  ↳ Falling back to /chat/completions translation path...")
                                        # Fall through to OpenAI path below
                                        async for event in _generate_openai_stream(body, github_token, model_requested):
                                            yield event
                                        return

                                    if resp.status_code in RETRYABLE_STATUS_CODES and stream_attempt < MAX_RETRIES:
                                        delay = RETRY_BASE_DELAY * (2 ** (stream_attempt - 1))
                                        print(f"  ⚠ Native stream attempt {stream_attempt}/{MAX_RETRIES} got HTTP {resp.status_code}: {err_msg}")
                                        print(f"    Retrying in {delay:.0f}s...")
                                        await asyncio.sleep(delay)
                                        _headers = copilot_headers_native(
                                            github_token,
                                            interaction_id=str(uuid.uuid4()),
                                            interaction_type="conversation-agent",
                                            agent_task_id=str(uuid.uuid4()),
                                            streaming=True,
                                            anthropic_beta=anthropic_beta,
                                        )
                                        continue  # retry
                                    print(f"  ✗ Native upstream error HTTP {resp.status_code}: {err_msg}")
                                    yield _sse_event("error", {
                                        "type": "error",
                                        "error": {
                                            "type": "api_error",
                                            "message": f"Upstream error {resp.status_code}: {err_msg}",
                                        }
                                    })
                                    return

                                # Success — pipe SSE events directly (passthrough)
                                buf = ""
                                async for raw_bytes in resp.aiter_bytes():
                                    if not raw_bytes:
                                        continue
                                    chunk = raw_bytes.decode("utf-8", errors="replace")
                                    # Pass through directly — already Anthropic SSE format
                                    yield chunk

                                print(f"  ✓ Native stream complete (passthrough)")
                                return  # Done

                        except httpx.ReadTimeout:
                            print(f"  ✗ Native stream timeout")
                            yield _sse_event("error", {
                                "type": "error",
                                "error": {"type": "overloaded_error", "message": "Upstream timeout (native stream). Please retry."}
                            })
                            return
                        except (httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
                            print(f"  ✗ Native connection timeout: {type(e).__name__}")
                            yield _sse_event("error", {
                                "type": "error",
                                "error": {"type": "overloaded_error", "message": f"Upstream connection timeout: {type(e).__name__}. Please retry."}
                            })
                            return
                        except httpx.HTTPError as e:
                            print(f"  ✗ Native HTTP error: {e}")
                            yield _sse_event("error", {
                                "type": "error",
                                "error": {"type": "api_error", "message": f"Upstream error: {e}"}
                            })
                            return

            return StreamingResponse(
                generate_native(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        else:
            # Non-streaming native passthrough
            last_err = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            url,
                            headers=headers,
                            json=passthrough_body,
                            timeout=UPSTREAM_TIMEOUT,
                        )

                        if resp.status_code == 200:
                            data = resp.json()
                            usage = data.get("usage", {})
                            print(f"  ✓ Native non-stream OK | stop_reason: {data.get('stop_reason')} | usage: in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}")
                            return JSONResponse(
                                content=data,
                                headers={"anthropic-version": "2023-06-01"},
                            )

                        err_msg = resp.text[:500]

                        # Fallback to OpenAI if model not supported
                        if resp.status_code == 400 and "model is not supported" in err_msg.lower():
                            print(f"  ⚠ /v1/messages rejected model '{resolved}'. Falling back to /chat/completions...")
                            break  # Fall through to OpenAI path

                        if resp.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                            print(f"  ⚠ Native attempt {attempt}/{MAX_RETRIES} got HTTP {resp.status_code}: {err_msg}")
                            await asyncio.sleep(delay)
                            headers = copilot_headers_native(
                                github_token,
                                interaction_id=str(uuid.uuid4()),
                                interaction_type="conversation-agent",
                                agent_task_id=str(uuid.uuid4()),
                                streaming=False,
                                anthropic_beta=anthropic_beta,
                            )
                            continue

                        print(f"  ✗ Native upstream error HTTP {resp.status_code}: {err_msg}")
                        raise HTTPException(status_code=resp.status_code, detail={
                            "type": "error",
                            "error": {"type": "api_error", "message": err_msg}
                        })

                except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
                    last_err = e
                    if attempt < MAX_RETRIES:
                        delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                        print(f"  ⚠ Native attempt {attempt}/{MAX_RETRIES} {type(e).__name__}")
                        await asyncio.sleep(delay)
                        continue
                except httpx.HTTPError as e:
                    last_err = e
                    if attempt < MAX_RETRIES:
                        delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                        print(f"  ⚠ Native attempt {attempt}/{MAX_RETRIES} HTTPError: {e}")
                        await asyncio.sleep(delay)
                        continue

            # If we broke out (model not supported fallback), fall through to OpenAI path
            # Otherwise all retries exhausted
            if last_err and not (resp.status_code == 400 and "model is not supported" in resp.text[:500].lower()):
                err_detail = str(last_err) if last_err else "Unknown error"
                return JSONResponse(
                    status_code=529,
                    content={
                        "type": "error",
                        "error": {
                            "type": "overloaded_error",
                            "message": f"Upstream timeout after {MAX_RETRIES} retries. Detail: {err_detail}",
                        }
                    },
                    headers={"anthropic-version": "2023-06-01"},
                )

            # Fall through to OpenAI translation path
            print(f"  ↳ Falling back to OpenAI translation path (/chat/completions)...")
            use_native_passthrough = False

    # ═══════════════════════════════════════════════════════════
    # PATH B: OpenAI Translation (/chat/completions)
    # Used for non-Claude models OR as fallback if /v1/messages rejects
    # ═══════════════════════════════════════════════════════════
    if not use_native_passthrough:
        # Rebuild everything for OpenAI path
        pass

    # Convert Anthropic → OpenAI
    openai_body = anthropic_to_openai_request(body)

    interaction_id = str(uuid.uuid4())
    agent_task_id = str(uuid.uuid4())
    # Non-stream requests are forced to stream upstream, so ALWAYS use streaming headers
    headers = copilot_headers(
        github_token,
        interaction_id=interaction_id,
        interaction_type="conversation-agent",
        agent_task_id=agent_task_id,
        streaming=True,  # Always stream upstream (non-stream path buffers the result)
    )
    url = f"{COPILOT_API}/chat/completions"

    if is_stream:
        # Delegate to the shared OpenAI stream generator helper
        return StreamingResponse(
            _generate_openai_stream(body, github_token, model_requested),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    else:
        # Non-streaming — FORCE stream upstream, buffer, return JSON
        # Copilot API often rejects non-stream requests (400) for large bodies,
        # so we always stream from upstream and assemble the response ourselves.
        print(f"  ↳ Non-stream request → forcing upstream stream + buffering")

        # Force stream in the openai body
        openai_body["stream"] = True
        openai_body["stream_options"] = {"include_usage": True}

        # Rebuild headers with streaming=True (Accept: text/event-stream)
        headers = copilot_headers(
            github_token,
            interaction_id=interaction_id,
            interaction_type="conversation-agent",
            agent_task_id=agent_task_id,
            streaming=True,
        )

        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient() as client:
                    async with client.stream("POST", url, headers=headers, json=openai_body, timeout=UPSTREAM_TIMEOUT) as resp:
                        if resp.status_code != 200:
                            err_bytes = await resp.aread()
                            err_msg = err_bytes.decode("utf-8", errors="replace")[:500]
                            if resp.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                                print(f"  ⚠ Attempt {attempt}/{MAX_RETRIES} got HTTP {resp.status_code}: {err_msg}")
                                print(f"    Retrying in {delay:.0f}s...")
                                await asyncio.sleep(delay)
                                headers = copilot_headers(
                                    github_token,
                                    interaction_id=interaction_id,
                                    interaction_type="conversation-agent",
                                    agent_task_id=agent_task_id,
                                    streaming=True,
                                )
                                continue
                            print(f"  ✗ Upstream error HTTP {resp.status_code}: {err_msg}\n  [DEBUG] Payload: {json.dumps(openai_body)[:2000]}")
                            raise HTTPException(status_code=resp.status_code, detail={
                                "type": "error",
                                "error": {"type": "api_error", "message": err_msg}
                            })

                        # Buffer the SSE stream and assemble OpenAI-style response
                        collected_text = ""
                        collected_tool_calls: Dict[int, dict] = {}
                        finish_reason = "stop"
                        usage_data = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                        model_resp = ""

                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            if not model_resp and chunk.get("model"):
                                model_resp = chunk["model"]

                            # Usage from stream_options
                            if chunk.get("usage"):
                                usage_data = chunk["usage"]

                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta", {})
                                fr = choice.get("finish_reason")
                                if fr:
                                    finish_reason = fr

                                # Text content
                                if delta.get("content"):
                                    collected_text += delta["content"]

                                # Tool calls
                                for tc in delta.get("tool_calls", []):
                                    idx = tc.get("index", 0)
                                    if idx not in collected_tool_calls:
                                        collected_tool_calls[idx] = {
                                            "id": tc.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""},
                                        }
                                    if tc.get("id"):
                                        collected_tool_calls[idx]["id"] = tc["id"]
                                    fn = tc.get("function", {})
                                    if fn.get("name"):
                                        collected_tool_calls[idx]["function"]["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        collected_tool_calls[idx]["function"]["arguments"] += fn["arguments"]

                        # Build the assembled OpenAI non-stream response
                        message = {"role": "assistant", "content": collected_text or None}
                        if collected_tool_calls:
                            message["tool_calls"] = [collected_tool_calls[k] for k in sorted(collected_tool_calls)]
                        if not collected_text and not collected_tool_calls:
                            message["content"] = ""

                        assembled_resp = {
                            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                            "object": "chat.completion",
                            "model": model_resp or openai_body.get("model", ""),
                            "choices": [{
                                "index": 0,
                                "message": message,
                                "finish_reason": finish_reason,
                            }],
                            "usage": usage_data,
                        }

                        anthropic_resp = openai_to_anthropic_response(assembled_resp, model_requested)
                        usage = anthropic_resp.get("usage", {})
                        print(f"  ✓ Non-stream (buffered) OK (attempt {attempt}) | stop_reason: {anthropic_resp.get('stop_reason')} | usage: in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}")
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
                    headers = copilot_headers(
                        github_token,
                        interaction_id=interaction_id,
                        interaction_type="conversation-agent",
                        agent_task_id=agent_task_id,
                        streaming=True,  # Always stream upstream for non-stream path
                    )
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
                    headers = copilot_headers(
                        github_token,
                        interaction_id=interaction_id,
                        interaction_type="conversation-agent",
                        agent_task_id=agent_task_id,
                        streaming=True,  # Always stream upstream for non-stream path
                    )
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
╔═══════════════════════════════════════════════════════════════╗
║  GitHub Copilot Proxy — CLI Identity (FULL CLONE v{CLI_VERSION})      ║
║  http://127.0.0.1:{port}{' ' * (48 - len(str(port)))}║
╠═══════════════════════════════════════════════════════════════╣
║  📌 Cloned from Burp Capture: 2026-04-08                      ║
║  🎯 Identity: copilot-developer-cli                           ║
║  🔑 Auth: gho_ token direct (NO JWT exchange)                 ║
║  📅 API Version: {GITHUB_API_VERSION}                                      ║
║  💻 Machine-Id: {MACHINE_ID[:24]}...                 ║
╠═══════════════════════════════════════════════════════════════╣
║  ROUTING:                                                     ║
║  ├─ Claude models → /v1/messages (Native Anthropic passthru)  ║
║  └─ Other models  → /chat/completions (OpenAI translation)    ║
╠═══════════════════════════════════════════════════════════════╣
║  ENDPOINTS (Anthropic API style):                             ║
║  ├─ POST /v1/messages              → Chat (Claude/GPT)        ║
║  ├─ POST /v1/messages/count_tokens → Token Count              ║
║  ├─ GET  /internal/telemetry       → Experiment Config        ║
║  ├─ POST /internal/telemetry       → Send Metrics             ║
║  └─ POST /internal/responses       → Session Titles (GPT-5)   ║
╠═══════════════════════════════════════════════════════════════╣
║  FIXES (v1.0.24):                                             ║
║  ├─ X-Initiator: "user" (was "agent" in ≤1.0.21)             ║
║  ├─ Runtime-Client-Version: 1.0.24 (NEW header)              ║
║  ├─ Strip beta fields (strict, eager_input_streaming, etc.)   ║
║  ├─ Convert built-in tools (web_search → custom tool)         ║
║  └─ Forward anthropic-beta header for native path             ║
╠═══════════════════════════════════════════════════════════════╣
║  CLIENT HEADERS:                                              ║
║  x-api-key: gho_xxxYOUR_GITHUB_TOKEN                          ║
║  anthropic-version: 2023-06-01                                ║
╚═══════════════════════════════════════════════════════════════╝
""")
    uvicorn.run(app, host="0.0.0.0", port=port)
