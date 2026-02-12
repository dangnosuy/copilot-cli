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
  /select <id>  - Chọn model theo ID (vd: /select gpt-4o)
  /info         - Xem thông tin model đang dùng
  /system       - Xem/chỉnh sửa system prompt
  /clear        - Xóa lịch sử hội thoại
  /history      - Xem lịch sử hội thoại
  /help         - Xem hướng dẫn
  /exit         - Thoát
"""

import json
import sys
import os
import time
import textwrap
import io

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
    "You are an AI programming assistant.\n"
    "When asked for your name, you must respond with \"GitHub Copilot\".\n"
    "Follow the user's requirements carefully & to the letter.\n"
    "Follow Microsoft content policies.\n"
    "Avoid content that violates copyrights.\n"
    "If you are asked to generate content that is harmful, hateful, racist, sexist, lewd, or violent, "
    "only respond with \"Sorry, I can't assist with that.\"\n"
    "Keep your answers short and impersonal.\n"
    "You can answer general programming questions and perform the following tasks:\n"
    "* Ask a question about the files in your current workspace\n"
    "* Explain how the code in your active editor works\n"
    "* Make changes to existing code\n"
    "* Generate unit tests for the selected code\n"
    "* Propose a fix for the problems in the selected code\n"
    "* Scaffold code for a new file or project in a workspace\n"
    "Use Markdown formatting in your answers.\n"
    "The user is working on a Linux machine.\n"
    "You can only give one reply for each conversation turn."
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
            return True

        except requests.exceptions.RequestException as e:
            print(f"{C.RED}[!] Lỗi kết nối: {e}{C.RESET}")
            return False

    def display_models(self):
        """Hiển thị danh sách models đẹp."""
        if not self.models:
            if not self.fetch_models():
                return

        # Lọc chỉ lấy chat models có trong model_picker
        chat_models = [
            m for m in self.models
            if m.get("model_picker_enabled", False)
            and m.get("capabilities", {}).get("type") == "chat"
        ]

        if not chat_models:
            print(f"{C.YELLOW}[!] Không tìm thấy model nào.{C.RESET}")
            return

        # Nhóm theo category
        categories = {}
        for m in chat_models:
            cat = m.get("model_picker_category", "other")
            categories.setdefault(cat, []).append(m)

        cat_order = ["lightweight", "versatile", "powerful"]
        cat_labels = {
            "lightweight": "⚡ Lightweight (Nhanh)",
            "versatile":   "🔄 Versatile (Đa năng)",
            "powerful":    "🚀 Powerful (Mạnh mẽ)",
        }

        print()
        print(f"{C.BOLD}{C.CYAN}{'═' * 80}{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}  📋 DANH SÁCH MODELS CÓ SẴN{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}{'═' * 80}{C.RESET}")

        for cat in cat_order:
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
                supports_vision = m.get("capabilities", {}).get("supports", {}).get("vision", False)
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
                if supports_vision:
                    tags.append("👁️")
                if supports_thinking:
                    tags.append("🧠")

                tag_str = " ".join(tags)

                # Context size in K
                ctx_k = f"{max_ctx // 1000}K" if max_ctx else "?"
                out_k = f"{max_out // 1000}K" if max_out else "?"

                # Marker cho model đang chọn
                marker = f"{C.GREEN}► " if self.selected_model and self.selected_model == model_id else "  "

                print(f"  {marker}{C.BOLD}{C.WHITE}{model_id}{C.RESET}")
                print(f"      {C.DIM}{name} | {vendor} | ctx:{ctx_k} out:{out_k}{C.RESET}  {tag_str}")

        # Others
        if "other" in categories:
            print()
            print(f"  {C.BOLD}{C.YELLOW}📦 Other{C.RESET}")
            print(f"  {'─' * 76}")
            for m in categories["other"]:
                model_id = m.get("id", "")
                name = m.get("name", "")
                print(f"    {C.WHITE}{model_id}{C.RESET} - {C.DIM}{name}{C.RESET}")

        print()
        print(f"{C.BOLD}{C.CYAN}{'═' * 80}{C.RESET}")
        print(f"  {C.DIM}Dùng /select <model_id> để chọn model. VD: /select gpt-4o{C.RESET}")
        print()

    def select_model(self, model_id: str) -> bool:
        """Chọn model theo ID."""
        if not self.models:
            self.fetch_models()

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
            print(f"{C.YELLOW}[!] Chưa chọn model. Dùng /select <model_id>{C.RESET}")
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
        """Gửi tin nhắn và nhận phản hồi (streaming)."""
        if not self.ensure_token():
            return "[Lỗi] Token không hợp lệ."

        if not self.selected_model:
            return "[Lỗi] Chưa chọn model. Dùng /select <model_id>"

        # Thêm message của user
        self.messages.append({"role": "user", "content": user_message})

        # Build request body
        all_messages = [{"role": "system", "content": self.system_prompt}] + self.messages

        body = {
            "messages": all_messages,
            "model": self.selected_model,
            "temperature": 0.1,
            "top_p": 1,
            "max_tokens": 64000,
            "n": 1,
            "stream": True,
        }

        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.copilot_token}",
            "X-Request-Id": f"chat-{int(time.time())}",
            "X-Interaction-Type": "conversation-panel",
            "OpenAI-Intent": "conversation-panel",
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
                return ""

            # Force UTF-8 encoding để tránh mojibake tiếng Việt
            resp.encoding = "utf-8"

            # Stream response - dùng iter_content + tự tách line
            # để xử lý UTF-8 multi-byte characters đúng cách
            full_content = ""
            reasoning_text = ""
            showed_reasoning_header = False
            buffer = ""

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

            # Lưu vào history
            if full_content:
                self.messages.append({"role": "assistant", "content": full_content})

            return full_content

        except requests.exceptions.RequestException as e:
            print(f"{C.RED}[!] Lỗi kết nối: {e}{C.RESET}")
            self.messages.pop()
            return ""

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
  {C.YELLOW}/select <id>{C.RESET}     Chọn model (VD: /select gpt-4o)
  {C.YELLOW}/info{C.RESET}            Xem thông tin model đang dùng
  {C.YELLOW}/system{C.RESET}          Xem system prompt hiện tại
  {C.YELLOW}/system set{C.RESET}      Thay đổi system prompt (nhập multi-line)
  {C.YELLOW}/system reset{C.RESET}    Reset system prompt về mặc định
  {C.YELLOW}/clear{C.RESET}           Xóa lịch sử hội thoại
  {C.YELLOW}/history{C.RESET}         Xem lịch sử hội thoại
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
    print(f"{C.DIM}  Gõ /select <model_id> để chọn model khác.{C.RESET}")
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
                    print(f"{C.YELLOW}[!] Dùng: /select <model_id>{C.RESET}")
                    print(f"{C.DIM}    VD: /select gpt-4o{C.RESET}")
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

            else:
                print(f"{C.YELLOW}[!] Lệnh không hợp lệ: {cmd}{C.RESET}")
                print(f"{C.DIM}    Gõ /help để xem danh sách lệnh.{C.RESET}")

            continue

        # ─── Chat ───
        if not client.selected_model:
            print(f"{C.YELLOW}[!] Chưa chọn model. Dùng /models để xem và /select <id> để chọn.{C.RESET}")
            continue

        print()
        print(f"{C.BLUE}🤖 Copilot:{C.RESET}")
        client.chat(user_input)
        print()


if __name__ == "__main__":
    main()
