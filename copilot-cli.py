#!/usr/bin/env python3
"""
GitHub Copilot Chat CLI Tool
=============================
Tool gọi API đến các mô hình ngôn ngữ lớn của GitHub Copilot.

Luồng hoạt động:
1. Nhập GitHub token (gho_xxx)
2. Lấy Copilot token thông qua API
3. Xem danh sách models (/models)
4. Chọn model và bắt đầu chat

Commands:
  /models       - Xem danh sách models có sẵn
  /select <id>  - Chọn model theo số hoặc ID (vd: /select 1, /select gpt-4o)
  /info         - Xem thông tin model đang dùng
  /system       - Xem/chỉnh sửa system prompt
  /clear        - Xóa lịch sử hội thoại
  /history      - Xem lịch sử hội thoại
  /mcp          - Xem danh sách MCP tools
  /mcp add <dir>- Thêm thư mục cho MCP Filesystem
  /mcp fetch    - Thêm Fetch Server (tải web)
  /mcp shell    - Thêm Shell Server (chạy terminal)
  /mcp auto     - Thêm tất cả MCP servers
  /mcp stop     - Dừng tất cả MCP servers
  /help         - Xem hướng dẫn
  /exit         - Thoát
"""

import json
import sys
import os
import time
import textwrap
import io
import uuid
import hashlib

# Đảm bảo stdout xuất UTF-8 đúng cách
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
elif sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

try:
    import requests
except ImportError:
    print("[!] Cần cài đặt thư viện requests: pip install requests")
    sys.exit(1)

try:
    from mcp_client import MCPManager
except ImportError:
    MCPManager = None


# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════
GITHUB_API = "https://api.github.com"
COPILOT_TOKEN_ENDPOINT = "/copilot_internal/v2/token"
GITHUB_API_VERSION = "2025-04-01"
COPILOT_API_VERSION = "2025-07-16"
USER_AGENT = "GitHubCopilotChat/0.31.5"

# Colors
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"


# ═══════════════════════════════════════════════════════════════
# SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════
SYSTEM_PROMPT = (
    "You are a highly sophisticated automated coding agent with expert-level knowledge "
    "across many different programming languages and frameworks.\n"
    "The user will ask a question, or ask you to perform a task, and it may require lots of "
    "research to answer correctly. There is a selection of tools that let you perform actions "
    "or retrieve helpful context to answer the user's question.\n"
    "If you can infer the project type (languages, frameworks, and libraries) from the user's "
    "query or the context that you have, make sure to keep them in mind when making changes.\n"
    "If the user wants you to implement a feature and they have not specified the files to edit, "
    "first break down the user's request into smaller concepts and think about the kinds of files "
    "you need to grasp each concept.\n"
    "If you aren't sure which tool is relevant, you can call multiple tools. You can call tools "
    "repeatedly to take actions or gather as much context as needed until you have completed the "
    "task fully. Don't give up unless you are sure the request cannot be fulfilled with the tools "
    "you have. It's YOUR RESPONSIBILITY to make sure that you have done all you can to collect "
    "necessary context.\n"
    "Don't make assumptions about the situation — gather context first, then perform the task "
    "or answer the question.\n"
    "Think creatively and explore the workspace in order to make a complete fix.\n"
    "Don't repeat yourself after a tool call, pick up where you left off.\n"
    "NEVER print out a codeblock with file changes unless the user asked for it. Use the "
    "appropriate file tool instead.\n"
    "NEVER print out a codeblock with a terminal command to run unless the user asked for it. "
    "Use the execute_command tool instead.\n\n"
    "<toolUseInstructions>\n"
    "When using a tool, follow the JSON schema very carefully and make sure to include ALL "
    "required properties.\n"
    "No need to ask permission before using a tool.\n"
    "NEVER say the name of a tool to a user.\n"
    "If you think running multiple tools can answer the user's question, prefer calling them "
    "in parallel whenever possible.\n"
    "Don't call the execute_command tool multiple times in parallel. Instead, run one command "
    "and wait for the output before running the next command.\n"
    "NEVER try to edit a file by running terminal commands unless the user specifically asks "
    "for it.\n"
    "</toolUseInstructions>\n\n"
    "<outputFormatting>\n"
    "Use proper Markdown formatting in your answers.\n"
    "Keep your answers short and impersonal.\n"
    "You are working on a Linux machine.\n"
    "</outputFormatting>"
)


# ═══════════════════════════════════════════════════════════════
# COPILOT CLIENT
# ═══════════════════════════════════════════════════════════════
class CopilotClient:
    def __init__(self):
        self.github_token = None
        self.copilot_token = None
        self.copilot_token_expires = 0
        self.api_base = None
        self.models = []
        self.selected_model = None
        self.messages = []
        self.system_prompt = SYSTEM_PROMPT
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.mcp_manager = MCPManager() if MCPManager else None

        # Persistent session identifiers (like VS Code)
        self.session_id = f"{uuid.uuid4()}{int(time.time() * 1000)}"
        self.machine_id = hashlib.sha256(uuid.getnode().to_bytes(6, 'big')).hexdigest()

    # ─── Authentication ──────────────────────────────────────
    def set_github_token(self, token: str):
        """Set GitHub token (gho_xxx)."""
        self.github_token = token.strip()

    def fetch_copilot_token(self) -> bool:
        """Lấy Copilot token từ GitHub API."""
        if not self.github_token:
            print(f"{C.RED}[!] Chưa có GitHub token.{C.RESET}")
            return False

        url = f"{GITHUB_API}{COPILOT_TOKEN_ENDPOINT}"
        headers = {
            "Authorization": f"token {self.github_token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": USER_AGENT,
        }

        try:
            resp = self.session.get(url, headers=headers, timeout=15)

            if resp.status_code != 200:
                print(f"{C.RED}[!] Lấy token thất bại (HTTP {resp.status_code}){C.RESET}")
                print(f"{C.RED}[!] Response Body:{C.RESET}")
                print(f"{C.RED}{resp.text}{C.RESET}")
                return False

            data = resp.json()
            self.copilot_token = data.get("token")
            self.copilot_token_expires = data.get("expires_at", 0)
            self.api_base = data.get("endpoints", {}).get("api", "https://api.individual.githubcopilot.com")

            if not self.copilot_token:
                print(f"{C.RED}[!] Không tìm thấy token trong response.{C.RESET}")
                return False

            # Hiển thị thông tin
            sku = data.get("sku", "unknown")
            chat_enabled = data.get("chat_enabled", False)
            print(f"{C.GREEN}[+] Lấy Copilot token thành công!{C.RESET}")
            print(f"    SKU: {C.CYAN}{sku}{C.RESET}")
            print(f"    Chat: {C.CYAN}{chat_enabled}{C.RESET}")
            print(f"    API: {C.CYAN}{self.api_base}{C.RESET}")
            exp_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.copilot_token_expires))
            print(f"    Expires: {C.CYAN}{exp_time}{C.RESET}")
            return True

        except requests.exceptions.RequestException as e:
            print(f"{C.RED}[!] Lỗi kết nối: {e}{C.RESET}")
            return False

    def is_token_valid(self) -> bool:
        """Kiểm tra token còn hạn không."""
        if not self.copilot_token:
            return False
        return time.time() < self.copilot_token_expires - 60  # 1 phút buffer

    def ensure_token(self) -> bool:
        """Đảm bảo token còn hạn, refresh nếu cần."""
        if self.is_token_valid():
            return True
        print(f"{C.YELLOW}[*] Token hết hạn, đang refresh...{C.RESET}")
        return self.fetch_copilot_token()

    # ─── Models ──────────────────────────────────────────────
    def fetch_models(self) -> bool:
        """Lấy danh sách models."""
        if not self.ensure_token():
            return False

        url = f"{self.api_base}/models"
        headers = {
            "Authorization": f"Bearer {self.copilot_token}",
            "X-Request-Id": f"models-{int(time.time())}",
            "X-Interaction-Type": "model-access",
            "OpenAI-Intent": "model-access",
            "X-GitHub-Api-Version": COPILOT_API_VERSION,
            "Editor-Plugin-Version": "copilot-chat/0.31.5",
            "Editor-Version": "vscode/1.104.1",
            "Copilot-Integration-Id": "vscode-chat",
            "User-Agent": USER_AGENT,
        }

        try:
            resp = self.session.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                print(f"{C.RED}[!] Lấy models thất bại (HTTP {resp.status_code}){C.RESET}")
                return False

            data = resp.json()
            self.models = data.get("data", [])

            # Build index ngay khi fetch
            ordered = self._get_chat_models_ordered()
            self._model_index = {str(i): m.get("id") for i, m in enumerate(ordered, 1)}

            return True

        except requests.exceptions.RequestException as e:
            print(f"{C.RED}[!] Lỗi kết nối: {e}{C.RESET}")
            return False

    def _get_chat_models_ordered(self) -> list[dict]:
        """Lấy danh sách chat models theo thứ tự hiển thị (lightweight → versatile → powerful → other)."""
        chat_models = [
            m for m in self.models
            if m.get("model_picker_enabled", False)
            and m.get("capabilities", {}).get("type") == "chat"
        ]
        categories = {}
        for m in chat_models:
            cat = m.get("model_picker_category", "other")
            categories.setdefault(cat, []).append(m)

        ordered = []
        for cat in ["lightweight", "versatile", "powerful", "other"]:
            ordered.extend(categories.get(cat, []))
        return ordered

    def display_models(self):
        """Hiển thị danh sách models đẹp với số thứ tự."""
        if not self.models:
            if not self.fetch_models():
                return

        ordered = self._get_chat_models_ordered()
        if not ordered:
            print(f"{C.YELLOW}[!] Không tìm thấy model nào.{C.RESET}")
            return

        # Lưu mapping số → model_id cho /select
        self._model_index = {str(i): m.get("id") for i, m in enumerate(ordered, 1)}

        # Nhóm theo category
        categories = {}
        for m in ordered:
            cat = m.get("model_picker_category", "other")
            categories.setdefault(cat, []).append(m)

        cat_labels = {
            "lightweight": "⚡ Lightweight (Nhanh)",
            "versatile":   "🔄 Versatile (Đa năng)",
            "powerful":    "🚀 Powerful (Mạnh mẽ)",
            "other":       "📦 Other",
        }

        print()
        print(f"{C.BOLD}{C.CYAN}{'═' * 80}{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}  📋 DANH SÁCH MODELS CÓ SẴN{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}{'═' * 80}{C.RESET}")

        idx = 1
        for cat in ["lightweight", "versatile", "powerful", "other"]:
            if cat not in categories:
                continue
            print()
            print(f"  {C.BOLD}{C.YELLOW}{cat_labels.get(cat, cat)}{C.RESET}")
            print(f"  {'─' * 76}")

            for m in categories[cat]:
                model_id = m.get("id", "")
                name = m.get("name", "")
                vendor = m.get("vendor", "")
                is_premium = m.get("billing", {}).get("is_premium", False)
                multiplier = m.get("billing", {}).get("multiplier", 0)
                is_preview = m.get("preview", False)
                is_default = m.get("is_chat_default", False)
                supports_thinking = m.get("capabilities", {}).get("supports", {}).get("adaptive_thinking", False) or \
                                    m.get("capabilities", {}).get("supports", {}).get("max_thinking_budget", 0) > 0
                max_ctx = m.get("capabilities", {}).get("limits", {}).get("max_context_window_tokens", 0)
                max_out = m.get("capabilities", {}).get("limits", {}).get("max_output_tokens", 0)

                # Tags
                tags = []
                if is_default:
                    tags.append(f"{C.GREEN}DEFAULT{C.RESET}")
                if is_preview:
                    tags.append(f"{C.MAGENTA}PREVIEW{C.RESET}")
                if is_premium:
                    tags.append(f"{C.YELLOW}PREMIUM x{multiplier}{C.RESET}")
                else:
                    tags.append(f"{C.GREEN}FREE{C.RESET}")
                if supports_thinking:
                    tags.append("🧠")

                tag_str = " ".join(tags)

                # Context size in K
                ctx_k = f"{max_ctx // 1000}K" if max_ctx else "?"
                out_k = f"{max_out // 1000}K" if max_out else "?"

                # Marker cho model đang chọn
                marker = f"{C.GREEN}►" if self.selected_model and self.selected_model == model_id else " "

                # Số thứ tự
                num = f"{C.DIM}{idx:>2}.{C.RESET}"

                print(f"  {marker}{num} {C.BOLD}{C.WHITE}{model_id}{C.RESET}")
                print(f"       {C.DIM}{name} | {vendor} | ctx:{ctx_k} out:{out_k}{C.RESET}  {tag_str}")
                idx += 1

        print()
        print(f"{C.BOLD}{C.CYAN}{'═' * 80}{C.RESET}")
        print(f"  {C.DIM}Dùng /select <số> hoặc /select <model_id> để chọn model.{C.RESET}")
        print(f"  {C.DIM}VD: /select 1  hoặc  /select gpt-4o{C.RESET}")
        print()

    def select_model(self, model_id: str) -> bool:
        """Chọn model theo ID hoặc số thứ tự."""
        if not self.models:
            self.fetch_models()

        # Nếu nhập số → tra bảng index
        model_id = model_id.strip()
        index_map = getattr(self, "_model_index", {})
        if model_id.isdigit() and model_id in index_map:
            model_id = index_map[model_id]

        # Tìm model
        found = None
        for m in self.models:
            if m.get("id") == model_id:
                found = m
                break

        # Fuzzy match
        if not found:
            for m in self.models:
                if model_id.lower() in m.get("id", "").lower():
                    found = m
                    break

        if not found:
            print(f"{C.RED}[!] Không tìm thấy model: {model_id}{C.RESET}")
            print(f"{C.DIM}    Dùng /models để xem danh sách.{C.RESET}")
            return False

        # Kiểm tra có phải chat model không
        if found.get("capabilities", {}).get("type") != "chat":
            print(f"{C.RED}[!] Model '{model_id}' không hỗ trợ chat.{C.RESET}")
            return False

        self.selected_model = found.get("id")
        name = found.get("name", "")
        vendor = found.get("vendor", "")
        print(f"{C.GREEN}[+] Đã chọn model: {C.BOLD}{self.selected_model}{C.RESET}")
        print(f"    {C.DIM}{name} | {vendor}{C.RESET}")
        return True

    def display_model_info(self):
        """Hiển thị thông tin model đang dùng."""
        if not self.selected_model:
            print(f"{C.YELLOW}[!] Chưa chọn model. Dùng /select <số|id>{C.RESET}")
            return

        found = None
        for m in self.models:
            if m.get("id") == self.selected_model:
                found = m
                break

        if not found:
            print(f"{C.YELLOW}[!] Không tìm thấy thông tin model.{C.RESET}")
            return

        caps = found.get("capabilities", {})
        limits = caps.get("limits", {})
        supports = caps.get("supports", {})
        billing = found.get("billing", {})

        print()
        print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
        print(f"  {C.BOLD}Model: {C.WHITE}{found.get('name', '')}{C.RESET}")
        print(f"  {C.DIM}ID: {found.get('id', '')}{C.RESET}")
        print(f"{C.CYAN}{'─' * 60}{C.RESET}")
        print(f"  Vendor:          {found.get('vendor', '')}")
        print(f"  Version:         {found.get('version', '')}")
        print(f"  Preview:         {found.get('preview', False)}")
        print(f"  Premium:         {billing.get('is_premium', False)} (x{billing.get('multiplier', 0)})")
        print(f"  Context Window:  {limits.get('max_context_window_tokens', '?'):,} tokens")
        print(f"  Max Output:      {limits.get('max_output_tokens', '?'):,} tokens")
        print(f"  Max Prompt:      {limits.get('max_prompt_tokens', '?'):,} tokens")
        print(f"  Vision:          {supports.get('vision', False)}")
        print(f"  Tool Calls:      {supports.get('tool_calls', False)}")
        print(f"  Streaming:       {supports.get('streaming', False)}")
        print(f"  Thinking:        {supports.get('max_thinking_budget', 0) > 0}")
        print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
        print()

    # ─── Chat ────────────────────────────────────────────────
    def chat(self, user_message: str) -> str:
        """Gửi tin nhắn và nhận phản hồi (streaming + tool calling)."""
        if not self.ensure_token():
            return "[Lỗi] Token không hợp lệ."

        if not self.selected_model:
            return "[Lỗi] Chưa chọn model. Dùng /select <số|id>"

        # Thêm message của user
        self.messages.append({"role": "user", "content": user_message})

        # Tạo IDs cho toàn bộ interaction này (giống VS Code)
        # X-Request-Id: giữ nguyên qua tất cả rounds → server tính 1 premium request
        # X-Interaction-Id: unique per interaction (dùng cho tracking)
        interaction_request_id = str(uuid.uuid4())
        interaction_id = str(uuid.uuid4())

        # Tool calling loop - AI có thể gọi nhiều tools liên tiếp
        max_tool_rounds = 30
        consecutive_errors = 0
        max_consecutive_errors = 3
        for _round in range(max_tool_rounds):
            result = self._send_chat_request(
                request_id=interaction_request_id,
                interaction_id=interaction_id,
                round_number=_round,
            )
            if result is None:
                return ""

            full_content, tool_calls = result

            # Nếu không có tool calls, đã xong
            if not tool_calls:
                if full_content:
                    self.messages.append({"role": "assistant", "content": full_content})
                return full_content

            # Có tool calls → thực thi và gửi lại
            # Thêm assistant message với tool_calls
            assistant_msg = {"role": "assistant", "content": full_content or None, "tool_calls": tool_calls}
            self.messages.append(assistant_msg)

            # Thực thi từng tool call
            round_had_error = False
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                func = tc.get("function", {})
                func_name = func.get("name", "")
                func_args_str = func.get("arguments", "{}")

                try:
                    func_args = json.loads(func_args_str)
                except json.JSONDecodeError:
                    func_args = {}

                print(f"\n  {C.YELLOW}🔧 Gọi tool: {C.BOLD}{func_name}{C.RESET}")
                # Debug: luôn hiển thị raw args string
                print(f"     {C.DIM}[args_raw] {repr(func_args_str[:300])}{C.RESET}")
                if func_args:
                    # Hiển thị args ngắn gọn
                    args_display = json.dumps(func_args, ensure_ascii=False)
                    if len(args_display) > 200:
                        args_display = args_display[:197] + "..."
                    print(f"     {C.DIM}{args_display}{C.RESET}")
                else:
                    # Debug: show raw args string if parsing failed or empty
                    print(f"     {C.RED}[DEBUG] raw args: {repr(func_args_str[:200])}{C.RESET}")

                # Gọi MCP tool
                if self.mcp_manager:
                    tool_result = self.mcp_manager.execute_tool(func_name, func_args)
                else:
                    tool_result = "[Lỗi] MCP Manager chưa được khởi tạo"

                # Hiển thị kết quả ngắn gọn
                result_preview = tool_result[:200] + "..." if len(tool_result) > 200 else tool_result
                print(f"     {C.DIM}→ {result_preview}{C.RESET}")

                # Track errors
                if "[Tool Error]" in tool_result or "[Lỗi]" in tool_result:
                    round_had_error = True

                # Truncate tool result nếu quá dài để tiết kiệm token
                # (giữ đầu + đuôi để AI có context đủ)
                max_tool_result = 15000  # ~4K tokens
                if len(tool_result) > max_tool_result:
                    keep_head = int(max_tool_result * 0.7)
                    keep_tail = int(max_tool_result * 0.25)
                    tool_result = (
                        tool_result[:keep_head]
                        + f"\n\n... [truncated {len(tool_result) - keep_head - keep_tail} chars] ...\n\n"
                        + tool_result[-keep_tail:]
                    )

                # Thêm tool result vào messages
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result,
                })

            # Track consecutive errors — stop nếu model cứ gọi tool sai liên tục
            if round_had_error:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    print(f"\n{C.RED}[!] Dừng: {consecutive_errors} lần tool call liên tiếp bị lỗi.{C.RESET}")
                    self.messages.append({"role": "assistant", "content": full_content or "[Tool calling failed repeatedly]"})
                    return full_content or ""
            else:
                consecutive_errors = 0

            # Tiếp tục vòng lặp để AI xử lý kết quả tool
            print(f"\n{C.BLUE}🤖 Copilot:{C.RESET}")

        return full_content or ""

    def _estimate_tokens(self, text: str) -> int:
        """Ước tính số tokens (1 token ≈ 4 chars tiếng Anh, 2 chars tiếng Việt/CJK)."""
        if not text:
            return 0
        return len(text) // 3  # conservative estimate

    @staticmethod
    def _split_concat_json(s: str) -> list:
        """Split concatenated JSON objects: '{"a":1}{"b":2}' → ['{"a":1}', '{"b":2}']
        
        Handles Gemini's quirk of merging parallel tool calls into one string.
        Uses a simple brace-depth counter (ignores strings for speed).
        """
        objects = []
        depth = 0
        start = None
        in_string = False
        escape = False

        for i, ch in enumerate(s):
            if escape:
                escape = False
                continue
            if ch == '\\' and in_string:
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start is not None:
                    objects.append(s[start:i + 1])
                    start = None

        return objects

    def _trim_messages_for_context(self, messages: list, max_tokens: int = 100000) -> list:
        """Cắt bớt messages cũ nếu tổng tokens vượt ngưỡng.
        
        Giữ lại: message đầu (user prompt gốc) + N messages cuối (context gần nhất).
        Truncate tool results dài trong messages cũ.
        """
        # Tính tổng tokens
        total = sum(self._estimate_tokens(
            m.get("content", "") or json.dumps(m.get("tool_calls", []))
        ) for m in messages)

        if total <= max_tokens:
            return messages

        # Strategy: truncate tool results cũ trước, sau đó xóa messages cũ
        result = list(messages)

        # Pass 1: Truncate old tool results (giữ 500 chars đầu)
        for i, m in enumerate(result[:-10]):  # Không truncate 10 messages cuối
            if m.get("role") == "tool":
                content = m.get("content", "")
                if len(content) > 800:
                    result[i] = dict(m)
                    result[i]["content"] = content[:500] + "\n... [truncated for context] ..."

        # Recalculate
        total = sum(self._estimate_tokens(
            m.get("content", "") or json.dumps(m.get("tool_calls", []))
        ) for m in result)

        if total <= max_tokens:
            return result

        # Pass 2: Drop oldest conversation turns (keep first user msg + last N)
        keep_last = 20
        if len(result) > keep_last + 2:
            # Keep first user message + separator + last N
            trimmed = (
                result[:1]  # first message
                + [{"role": "user", "content": "[... earlier conversation truncated for context ...]"}]
                + result[-keep_last:]  # recent messages
            )
            return trimmed

        return result

    def _send_chat_request(self, request_id=None, interaction_id=None, round_number=0):
        """Gửi một request chat và trả về (content, tool_calls) hoặc None nếu lỗi."""
        # Build system prompt — inject MCP tools description nếu có
        effective_system = self.system_prompt

        if self.mcp_manager and self.mcp_manager.servers:
            # Inject tool capability summary vào system prompt
            # Explicit descriptions giúp model hiểu khi nào dùng tool nào
            tool_summary = {
                "read_text_file": "Read file contents from disk",
                "write_file": "Create or overwrite a file with content",
                "edit_file": "Edit an existing file (partial changes)",
                "list_directory": "List files/folders in a directory",
                "search_files": "Search for files matching a pattern",
                "fetch": "Fetch main content from a URL. Useful for summarizing or analyzing web pages, searching the web, or calling APIs",
                "execute_command": "Run shell commands (bash, python, curl, etc.)",
            }
            tool_lines = []
            for handle in self.mcp_manager.servers.values():
                for tool in handle["tools"]:
                    t_name = tool.get("name", "")
                    if t_name in (self.mcp_manager.tool_map or {}):
                        desc = tool_summary.get(t_name, "")
                        tool_lines.append(f"- **{t_name}**: {desc}" if desc else f"- {t_name}")
            if tool_lines:
                effective_system += (
                    "\n\n## YOUR TOOLS\n"
                    + "\n".join(tool_lines)
                )

        # Trim messages nếu context quá dài
        trimmed_messages = self._trim_messages_for_context(self.messages)

        # Build messages với copilot_cache_control (prompt caching giống VS Code)
        # System message: luôn cache
        sys_msg = {
            "role": "system",
            "content": effective_system,
            "copilot_cache_control": {"type": "ephemeral"},
        }
        all_messages = [sys_msg]

        # User/assistant/tool messages: cache tất cả trừ message cuối cùng
        for i, m in enumerate(trimmed_messages):
            msg = dict(m)  # shallow copy
            is_last = (i == len(trimmed_messages) - 1)
            # Cache mọi thứ trừ message cuối (latest user input hoặc latest tool result)
            if not is_last:
                msg["copilot_cache_control"] = {"type": "ephemeral"}
            all_messages.append(msg)

        body = {
            "messages": all_messages,
            "model": self.selected_model,
            "temperature": 0,
            "top_p": 1,
            "max_tokens": 64000,
            "n": 1,
            "stream": True,
        }

        # Thêm tools nếu có MCP
        if self.mcp_manager and self.mcp_manager.servers:
            tools = self.mcp_manager.get_openai_tools()
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"

        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.copilot_token}",
            "X-Request-Id": request_id or str(uuid.uuid4()),
            "X-Interaction-Type": "conversation-agent",
            "OpenAI-Intent": "conversation-agent",
            "X-Interaction-Id": interaction_id or str(uuid.uuid4()),
            "X-Initiator": "user" if round_number == 0 else "agent",
            "VScode-SessionId": self.session_id,
            "VScode-MachineId": self.machine_id,
            "X-GitHub-Api-Version": COPILOT_API_VERSION,
            "Editor-Plugin-Version": "copilot-chat/0.31.5",
            "Editor-Version": "vscode/1.104.1",
            "Copilot-Integration-Id": "vscode-chat",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }

        try:
            resp = self.session.post(url, headers=headers, json=body, stream=True, timeout=120)

            if resp.status_code != 200:
                err_text = resp.text[:500]
                print(f"{C.RED}[!] Chat thất bại (HTTP {resp.status_code}): {err_text}{C.RESET}")
                self.messages.pop()  # Xóa message lỗi
                return None

            # Force UTF-8 encoding để tránh mojibake tiếng Việt
            resp.encoding = "utf-8"

            # Stream response - dùng iter_content + tự tách line
            # để xử lý UTF-8 multi-byte characters đúng cách
            full_content = ""
            reasoning_text = ""
            showed_reasoning_header = False
            buffer = ""
            tool_calls_acc = {}  # index -> {id, function: {name, arguments}}

            for chunk_bytes in resp.iter_content(chunk_size=None):
                if not chunk_bytes:
                    continue

                # Decode UTF-8 đúng cách
                buffer += chunk_bytes.decode("utf-8", errors="replace")

                # Tách theo newline, giữ lại phần chưa hoàn chỉnh
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()

                    if not line:
                        continue
                    if not line.startswith("data: "):
                        continue

                    data_str = line[6:]  # Bỏ "data: "

                    if data_str.strip() == "[DONE]":
                        buffer = ""
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})

                    # Reasoning text (thinking)
                    r_text = delta.get("reasoning_text")
                    if r_text:
                        if not showed_reasoning_header:
                            print(f"\n{C.DIM}💭 Thinking...{C.RESET}")
                            showed_reasoning_header = True
                        reasoning_text += r_text

                    # Tool calls (streaming)
                    delta_tool_calls = delta.get("tool_calls", [])
                    for tc_delta in delta_tool_calls:
                        idx = tc_delta.get("index", 0)

                        # Detect new tool call: nếu có "id" mới → đây là tool call mới
                        # Gemini có thể gửi nhiều tool calls cùng index 0
                        # Dùng "id" để phân biệt tool calls thay vì chỉ dựa vào index
                        new_id = tc_delta.get("id", "")
                        if new_id and new_id not in {tc.get("id", "") for tc in tool_calls_acc.values()}:
                            # Tool call mới — tìm slot trống
                            actual_idx = len(tool_calls_acc)
                            tool_calls_acc[actual_idx] = {
                                "id": new_id,
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                            idx = actual_idx
                        elif new_id:
                            # Tìm idx theo id đã tồn tại
                            for existing_idx, existing_tc in tool_calls_acc.items():
                                if existing_tc.get("id") == new_id:
                                    idx = existing_idx
                                    break
                        else:
                            # Không có id → dùng index (fallback cho streaming chunks tiếp theo)
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }

                        func_delta = tc_delta.get("function", {})
                        # Name: chỉ set nếu chưa có (tool name gửi 1 lần duy nhất)
                        if func_delta.get("name"):
                            if not tool_calls_acc[idx]["function"]["name"]:
                                tool_calls_acc[idx]["function"]["name"] = func_delta["name"]
                        # Arguments: append vì streaming (JSON gửi theo từng chunk)
                        # Dùng "in" thay vì .get() để catch cả empty string ""
                        if "arguments" in func_delta and func_delta["arguments"] is not None:
                            tool_calls_acc[idx]["function"]["arguments"] += func_delta["arguments"]

                    # Content text
                    content = delta.get("content")
                    if content:
                        full_content += content
                        sys.stdout.write(content)
                        sys.stdout.flush()

                    # Finish reason
                    finish = choices[0].get("finish_reason")
                    if finish:
                        buffer = ""
                        break

            print()  # Newline sau khi stream xong

            # Build tool_calls list với validation
            tool_calls = []
            if tool_calls_acc:
                for idx in sorted(tool_calls_acc.keys()):
                    tc = tool_calls_acc[idx]
                    args_str = tc["function"]["arguments"]
                    tc_name = tc["function"]["name"]

                    # Debug: luôn in raw args để debug
                    if os.environ.get("COPILOT_DEBUG"):
                        print(f"     {C.DIM}[DEBUG] {tc_name} raw_args ({len(args_str)}): {repr(args_str[:500])}{C.RESET}")

                    # Validate arguments là valid JSON
                    try:
                        json.loads(args_str)
                        tool_calls.append(tc)
                    except (json.JSONDecodeError, TypeError):
                        # Detect concatenated JSON objects: {"cmd":"a"}{"url":"b"}{"cmd":"c"}
                        # Gemini sometimes merges parallel tool calls into one args string
                        split_objects = self._split_concat_json(args_str)
                        if len(split_objects) > 1:
                            print(f"     {C.YELLOW}[!] Tách {len(split_objects)} tool calls bị merge{C.RESET}")
                            for i, obj_str in enumerate(split_objects):
                                try:
                                    obj = json.loads(obj_str)
                                    # Infer tool name từ keys
                                    inferred_name = tc_name
                                    if "url" in obj:
                                        inferred_name = "fetch"
                                    elif "command" in obj:
                                        inferred_name = "execute_command"
                                    elif "path" in obj and "content" in obj:
                                        inferred_name = "write_file"
                                    elif "path" in obj:
                                        inferred_name = "read_text_file"

                                    split_tc = {
                                        "id": tc["id"] + f"_split{i}" if i > 0 else tc["id"],
                                        "type": "function",
                                        "function": {
                                            "name": inferred_name,
                                            "arguments": obj_str,
                                        },
                                    }
                                    tool_calls.append(split_tc)
                                except json.JSONDecodeError:
                                    pass  # Skip invalid fragments
                        else:
                            print(f"     {C.RED}[!] Tool '{tc_name}' invalid JSON args ({len(args_str)} chars): {repr(args_str[:300])}{C.RESET}")
                            tc["function"]["arguments"] = "{}"
                            tool_calls.append(tc)

            return (full_content, tool_calls)

        except requests.exceptions.RequestException as e:
            print(f"{C.RED}[!] Lỗi kết nối: {e}{C.RESET}")
            self.messages.pop()
            return None

    def clear_history(self):
        """Xóa lịch sử hội thoại."""
        self.messages.clear()
        print(f"{C.GREEN}[+] Đã xóa lịch sử hội thoại.{C.RESET}")

    def set_system_prompt(self, prompt: str):
        """Thay đổi system prompt."""
        self.system_prompt = prompt
        print(f"{C.GREEN}[+] Đã cập nhật system prompt!{C.RESET}")

    def reset_system_prompt(self):
        """Reset system prompt về mặc định."""
        self.system_prompt = SYSTEM_PROMPT
        print(f"{C.GREEN}[+] Đã reset system prompt về mặc định.{C.RESET}")

    def display_system_prompt(self):
        """Hiển thị system prompt hiện tại."""
        print()
        print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
        print(f"  {C.BOLD}🔧 SYSTEM PROMPT HIỆN TẠI{C.RESET}")
        print(f"{C.CYAN}{'─' * 60}{C.RESET}")
        for line in self.system_prompt.split("\n"):
            print(f"  {C.DIM}{line}{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
        print(f"  {C.DIM}Dùng /system set <nội dung> để thay đổi{C.RESET}")
        print(f"  {C.DIM}Dùng /system reset để reset về mặc định{C.RESET}")
        print()

    def display_history(self):
        """Hiển thị lịch sử hội thoại."""
        if not self.messages:
            print(f"{C.YELLOW}[!] Chưa có lịch sử hội thoại.{C.RESET}")
            return

        print()
        print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
        print(f"  {C.BOLD}📜 LỊCH SỬ HỘI THOẠI ({len(self.messages)} messages){C.RESET}")
        print(f"{C.CYAN}{'═' * 60}{C.RESET}")

        for i, msg in enumerate(self.messages):
            role = msg["role"]
            content = msg["content"]
            if role == "user":
                print(f"\n  {C.GREEN}👤 You:{C.RESET}")
            else:
                print(f"\n  {C.BLUE}🤖 Copilot:{C.RESET}")

            # Truncate nếu quá dài
            if len(content) > 300:
                content = content[:300] + "..."
            for line in content.split("\n"):
                print(f"    {line}")

        print(f"\n{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
        print()


# ═══════════════════════════════════════════════════════════════
# HELP
# ═══════════════════════════════════════════════════════════════
def display_help():
    print(f"""
{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}
  {C.BOLD}📖 HƯỚNG DẪN SỬ DỤNG{C.RESET}
{C.CYAN}{'═' * 60}{C.RESET}

  {C.YELLOW}/models{C.RESET}          Xem danh sách models có sẵn
  {C.YELLOW}/select <số|id>{C.RESET}  Chọn model (VD: /select 1 hoặc /select gpt-4o)
  {C.YELLOW}/info{C.RESET}            Xem thông tin model đang dùng
  {C.YELLOW}/system{C.RESET}          Xem system prompt hiện tại
  {C.YELLOW}/system set{C.RESET}      Thay đổi system prompt (nhập multi-line)
  {C.YELLOW}/system reset{C.RESET}    Reset system prompt về mặc định
  {C.YELLOW}/clear{C.RESET}           Xóa lịch sử hội thoại
  {C.YELLOW}/history{C.RESET}         Xem lịch sử hội thoại
  {C.YELLOW}/mcp{C.RESET}             Xem danh sách MCP tools đang kết nối
  {C.YELLOW}/mcp add <dir>{C.RESET}   Thêm thư mục vào MCP Filesystem Server
  {C.YELLOW}/mcp fetch{C.RESET}       Thêm MCP Fetch Server (tải web)
  {C.YELLOW}/mcp shell{C.RESET}       Thêm MCP Shell Server (chạy lệnh terminal)
  {C.YELLOW}/mcp auto{C.RESET}        Tự động thêm tất cả MCP servers
  {C.YELLOW}/mcp stop{C.RESET}        Dừng tất cả MCP servers
  {C.YELLOW}/token{C.RESET}           Đổi GitHub token
  {C.YELLOW}/refresh{C.RESET}         Refresh Copilot token
  {C.YELLOW}/help{C.RESET}            Xem hướng dẫn này
  {C.YELLOW}/exit{C.RESET}            Thoát chương trình

  {C.DIM}Nhập bất kỳ nội dung nào khác để chat với AI.{C.RESET}

{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}
""")


# ═══════════════════════════════════════════════════════════════
# BANNER
# ═══════════════════════════════════════════════════════════════
def display_banner():
    banner = f"""
{C.BOLD}{C.CYAN}
  ╔══════════════════════════════════════════════════════════╗
  ║                                                          ║
  ║     ██████╗  ██████╗ ██████╗ ██╗██╗      ██████╗ ████████╗║
  ║    ██╔════╝ ██╔═══██╗██╔══██╗██║██║     ██╔═══██╗╚══██╔══╝║
  ║    ██║      ██║   ██║██████╔╝██║██║     ██║   ██║   ██║   ║
  ║    ██║      ██║   ██║██╔═══╝ ██║██║     ██║   ██║   ██║   ║
  ║    ╚██████╗ ╚██████╔╝██║     ██║███████╗╚██████╔╝   ██║   ║
  ║     ╚═════╝  ╚═════╝ ╚═╝     ╚═╝╚══════╝ ╚═════╝    ╚═╝   ║
  ║                                                          ║
  ║         GitHub Copilot Chat CLI Tool v1.0                ║
  ║                                                          ║
  ╚══════════════════════════════════════════════════════════╝
{C.RESET}"""
    print(banner)


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    display_banner()

    client = CopilotClient()

    # ─── Bước 1: Nhập GitHub Token ───
    token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")
    token = None

    if os.path.isfile(token_file):
        with open(token_file, "r") as f:
            token = f.read().strip()
        if token:
            print(f"{C.GREEN}[+] Đã tìm thấy token.txt, tự động import token.{C.RESET}")
            print(f"    {C.DIM}{token[:10]}...{token[-4:]}{C.RESET}")
        else:
            token = None

    if not token:
        print(f"{C.BOLD}[Bước 1] Nhập GitHub Token{C.RESET}")
        print(f"{C.DIM}  Token có dạng: gho_xxxxxxxxxxxx{C.RESET}")
        print(f"{C.DIM}  (Lấy từ GitHub Copilot extension hoặc OAuth){C.RESET}")
        print()

        while True:
            try:
                token = input(f"{C.YELLOW}GitHub Token: {C.RESET}").strip()
            except (KeyboardInterrupt, EOFError):
                print(f"\n{C.RED}[!] Bye!{C.RESET}")
                sys.exit(0)

            if not token:
                print(f"{C.RED}[!] Token không được để trống.{C.RESET}")
                continue
            break

    client.set_github_token(token)

    # ─── Bước 2: Lấy Copilot Token ───
    print()
    print(f"{C.BOLD}[Bước 2] Đang lấy Copilot token...{C.RESET}")
    if not client.fetch_copilot_token():
        print(f"{C.RED}[!] Không thể lấy Copilot token. Kiểm tra lại GitHub token.{C.RESET}")
        sys.exit(1)

    # ─── Bước 3: Lấy danh sách Models ───
    print()
    print(f"{C.BOLD}[Bước 3] Đang lấy danh sách models...{C.RESET}")
    if client.fetch_models():
        print(f"{C.GREEN}[+] Đã lấy {len(client.models)} models.{C.RESET}")
    else:
        print(f"{C.YELLOW}[!] Không lấy được danh sách models.{C.RESET}")

    # ─── Bước 4: Chọn Model mặc định ───
    # Tự động chọn gpt-4o hoặc model default
    default_model = None
    for m in client.models:
        if m.get("is_chat_default"):
            default_model = m.get("id")
            break

    if not default_model:
        # Fallback: chọn gpt-4.1 hoặc gpt-4o
        for mid in ["gpt-4.1", "gpt-4o", "gpt-5-mini"]:
            for m in client.models:
                if m.get("id") == mid:
                    default_model = mid
                    break
            if default_model:
                break

    if default_model:
        client.select_model(default_model)

    print()
    print(f"{C.DIM}  Gõ /help để xem hướng dẫn. Gõ /models để xem danh sách models.{C.RESET}")
    print(f"{C.DIM}  Gõ /select <số> hoặc /select <model_id> để chọn model khác.{C.RESET}")
    print()

    # ─── Chat Loop ───
    while True:
        try:
            # Prompt
            model_label = client.selected_model or "no-model"
            prompt_str = f"{C.BOLD}{C.GREEN}[{model_label}]{C.RESET} {C.BOLD}>{C.RESET} "
            user_input = input(prompt_str).strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{C.GREEN}[+] Bye! 👋{C.RESET}")
            break

        if not user_input:
            continue

        # ─── Handle Commands ───
        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "/exit" or cmd == "/quit":
                print(f"{C.GREEN}[+] Bye! 👋{C.RESET}")
                break

            elif cmd == "/models":
                client.display_models()

            elif cmd == "/select":
                if not arg:
                    print(f"{C.YELLOW}[!] Dùng: /select <số> hoặc /select <model_id>{C.RESET}")
                    print(f"{C.DIM}    VD: /select 1  hoặc  /select gpt-4o{C.RESET}")
                else:
                    client.select_model(arg.strip())

            elif cmd == "/info":
                client.display_model_info()

            elif cmd == "/clear":
                client.clear_history()

            elif cmd == "/history":
                client.display_history()

            elif cmd == "/system":
                sub = arg.strip().lower()
                if sub == "reset":
                    client.reset_system_prompt()
                elif sub.startswith("set"):
                    # Cho phép nhập multi-line system prompt
                    inline = sub[3:].strip()
                    if inline:
                        # /system set Bạn là trợ lý...
                        client.set_system_prompt(arg[3:].strip())
                    else:
                        print(f"{C.YELLOW}Nhập system prompt mới (gõ dòng trống để kết thúc):{C.RESET}")
                        lines = []
                        while True:
                            try:
                                line = input(f"{C.DIM}  | {C.RESET}")
                                if line == "":
                                    break
                                lines.append(line)
                            except (KeyboardInterrupt, EOFError):
                                print()
                                break
                        if lines:
                            client.set_system_prompt("\n".join(lines))
                        else:
                            print(f"{C.YELLOW}[!] Không có nội dung, giữ nguyên system prompt.{C.RESET}")
                else:
                    client.display_system_prompt()

            elif cmd == "/help":
                display_help()

            elif cmd == "/token":
                try:
                    new_token = input(f"{C.YELLOW}GitHub Token mới: {C.RESET}").strip()
                    if new_token:
                        client.set_github_token(new_token)
                        if client.fetch_copilot_token():
                            client.fetch_models()
                except (KeyboardInterrupt, EOFError):
                    print()

            elif cmd == "/refresh":
                client.fetch_copilot_token()

            elif cmd == "/mcp":
                sub = arg.strip().lower()
                if sub.startswith("add"):
                    # /mcp add /path/to/dir
                    dir_path = arg[3:].strip() if len(arg) > 3 else ""
                    if not dir_path:
                        try:
                            dir_path = input(f"{C.YELLOW}Đường dẫn thư mục: {C.RESET}").strip()
                        except (KeyboardInterrupt, EOFError):
                            print()
                            continue
                    if not dir_path:
                        print(f"{C.YELLOW}[!] Cần nhập đường dẫn thư mục.{C.RESET}")
                    elif not client.mcp_manager:
                        print(f"{C.RED}[!] MCP module chưa được cài đặt (thiếu mcp_client.py).{C.RESET}")
                    else:
                        # Dừng server cũ nếu có
                        if "filesystem" in client.mcp_manager.servers:
                            try:
                                client.mcp_manager._run_async(
                                    client.mcp_manager._disconnect_server(
                                        client.mcp_manager.servers["filesystem"]
                                    )
                                )
                            except Exception:
                                pass
                            del client.mcp_manager.servers["filesystem"]
                            client.mcp_manager.tool_map = {
                                k: v for k, v in client.mcp_manager.tool_map.items()
                                if v != "filesystem"
                            }
                        print(f"{C.BOLD}[MCP] Đang khởi động Filesystem Server...{C.RESET}")
                        dirs = [d.strip() for d in dir_path.split(",")]
                        if client.mcp_manager.add_filesystem_server(dirs):
                            n_tools = len([
                                t for t in client.mcp_manager.tool_map
                                if client.mcp_manager.tool_map[t] == "filesystem"
                            ])
                            print(f"{C.GREEN}[+] MCP Filesystem Server đã kết nối! ({n_tools} tools){C.RESET}")
                            client.mcp_manager.display_tools()
                        else:
                            print(f"{C.RED}[!] Không thể khởi động MCP Filesystem Server.{C.RESET}")

                elif sub == "fetch":
                    if not client.mcp_manager:
                        print(f"{C.RED}[!] MCP module chưa được cài đặt.{C.RESET}")
                    elif "fetch" in client.mcp_manager.servers:
                        print(f"{C.YELLOW}[!] Fetch Server đã đang chạy.{C.RESET}")
                    else:
                        print(f"{C.BOLD}[MCP] Đang khởi động Fetch Server...{C.RESET}")
                        if client.mcp_manager.add_fetch_server():
                            print(f"{C.GREEN}[+] MCP Fetch Server đã kết nối!{C.RESET}")
                        else:
                            print(f"{C.RED}[!] Không thể khởi động MCP Fetch Server.{C.RESET}")
                            print(f"{C.DIM}    Cài: pip install mcp-server-fetch{C.RESET}")

                elif sub == "shell":
                    if not client.mcp_manager:
                        print(f"{C.RED}[!] MCP module chưa được cài đặt.{C.RESET}")
                    elif "shell" in client.mcp_manager.servers:
                        print(f"{C.YELLOW}[!] Shell Server đã đang chạy.{C.RESET}")
                    else:
                        print(f"{C.BOLD}[MCP] Đang khởi động Shell Server...{C.RESET}")
                        if client.mcp_manager.add_shell_server():
                            print(f"{C.GREEN}[+] MCP Shell Server đã kết nối!{C.RESET}")
                        else:
                            print(f"{C.RED}[!] Không thể khởi động MCP Shell Server.{C.RESET}")
                            print(f"{C.DIM}    Cài: pip install mcp-server-shell{C.RESET}")

                elif sub == "auto":
                    if not client.mcp_manager:
                        print(f"{C.RED}[!] MCP module chưa được cài đặt.{C.RESET}")
                    else:
                        cwd = os.getcwd()
                        print(f"{C.BOLD}[MCP] Đang khởi động tất cả servers...{C.RESET}")
                        # Filesystem
                        if "filesystem" not in client.mcp_manager.servers:
                            print(f"  📁 Filesystem ({cwd})...", end=" ", flush=True)
                            if client.mcp_manager.add_filesystem_server([cwd]):
                                print(f"{C.GREEN}OK{C.RESET}")
                            else:
                                print(f"{C.RED}FAIL{C.RESET}")
                        # Fetch
                        if "fetch" not in client.mcp_manager.servers:
                            print(f"  🌐 Fetch...", end=" ", flush=True)
                            if client.mcp_manager.add_fetch_server():
                                print(f"{C.GREEN}OK{C.RESET}")
                            else:
                                print(f"{C.RED}FAIL{C.RESET}")
                        # Shell
                        if "shell" not in client.mcp_manager.servers:
                            print(f"  💻 Shell...", end=" ", flush=True)
                            if client.mcp_manager.add_shell_server():
                                print(f"{C.GREEN}OK{C.RESET}")
                            else:
                                print(f"{C.RED}FAIL{C.RESET}")
                        print()
                        client.mcp_manager.display_tools()
                        n = len(client.mcp_manager.get_openai_tools())
                        print(f"\n  {C.GREEN}Tổng cộng {n} tools sẵn sàng.{C.RESET}\n")

                elif sub == "stop":
                    if client.mcp_manager:
                        client.mcp_manager.stop_all()
                        print(f"{C.GREEN}[+] Đã dừng tất cả MCP servers.{C.RESET}")
                    else:
                        print(f"{C.YELLOW}[!] MCP module chưa được cài đặt.{C.RESET}")

                else:
                    # /mcp → hiển thị tools
                    print()
                    print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
                    print(f"  {C.BOLD}🔌 MCP SERVERS & TOOLS{C.RESET}")
                    print(f"{C.CYAN}{'─' * 60}{C.RESET}")
                    if client.mcp_manager:
                        client.mcp_manager.display_tools()
                    else:
                        print(f"  {C.YELLOW}MCP module chưa được cài đặt.{C.RESET}")
                    print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
                    print(f"  {C.DIM}/mcp add <dir>  - Filesystem Server{C.RESET}")
                    print(f"  {C.DIM}/mcp fetch      - Fetch Server (tải web){C.RESET}")
                    print(f"  {C.DIM}/mcp shell      - Shell Server (terminal){C.RESET}")
                    print(f"  {C.DIM}/mcp auto       - Tất cả servers{C.RESET}")
                    print()

            else:
                print(f"{C.YELLOW}[!] Lệnh không hợp lệ: {cmd}{C.RESET}")
                print(f"{C.DIM}    Gõ /help để xem danh sách lệnh.{C.RESET}")

            continue

        # ─── Chat ───
        if not client.selected_model:
            print(f"{C.YELLOW}[!] Chưa chọn model. Dùng /models để xem và /select <số> để chọn.{C.RESET}")
            continue

        print()
        print(f"{C.BLUE}🤖 Copilot:{C.RESET}")
        client.chat(user_input)
        print()

    # Cleanup MCP servers
    if client.mcp_manager:
        client.mcp_manager.stop_all()


if __name__ == "__main__":
    main()
