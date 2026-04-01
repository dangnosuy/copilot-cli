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
  /save [tên]   - Lưu phiên chat hiện tại
  /load [số|tên]- Load phiên chat đã lưu
  /sessions     - Xem danh sách phiên đã lưu
  /sessions rename <số> <tên mới> - Đổi tên phiên
  /sessions delete <số|tên>       - Xóa phiên
  /mcp          - Xem danh sách MCP tools
  /mcp add <dir>- Thêm thư mục cho MCP Filesystem
  /mcp fetch    - Thêm Fetch Server (tải web)
  /mcp shell    - Thêm Shell Server (chạy terminal)
  /mcp search   - Thêm Web Search (DuckDuckGo, không cần API)
  /mcp playwright - Thêm Playwright (trình duyệt, headless)
  /mcp playwright headed - Playwright hiện trình duyệt lên màn hình
  /mcp web      - Thêm tất cả Web servers
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

try:
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.spinner import Spinner
    from rich.syntax import Syntax
    from rich.text import Text
    from rich.table import Table
    from rich.rule import Rule
    import difflib
    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    console = None


# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════
GITHUB_API = "https://api.github.com"
COPILOT_TOKEN_ENDPOINT = "/copilot_internal/v2/token"
GITHUB_API_VERSION = "2025-04-01"
COPILOT_API_VERSION = "2025-07-16"
USER_AGENT = "GitHubCopilotChat/0.31.5"

# Models sử dụng Responses API (POST /responses) thay vì Chat Completions API (POST /chat/completions)
RESPONSES_API_MODELS = {"oswe-vscode-prime"}

# GPT Codex models — cũng dùng Responses API nhưng với max_output_tokens lớn hơn
GPT_CODEX_RESPONSES_MODELS = {
    "gpt-5.1-codex-mini",
    "gpt-5.1-codex",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5.1-codex-max",
    "gpt-5.4",
}

# Sessions directory
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "sessions")

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

DANGEROUS_TOOLS = {"execute_command", "write_file", "edit_file"}

# /yolo mode — auto-approve all tool calls without permission prompts
yolo_mode = False


# ═══════════════════════════════════════════════════════════════
# SMART INPUT — Inline autocomplete cho / commands
# ═══════════════════════════════════════════════════════════════
import tty
import termios
import select as _select

# Commands cho autocomplete
SLASH_COMMANDS = [
    "/models", "/select", "/info", "/system", "/system set", "/system reset",
    "/clear", "/history",
    "/save", "/load", "/sessions", "/sessions rename", "/sessions delete",
    "/mcp", "/mcp add", "/mcp fetch", "/mcp shell",
    "/mcp auto", "/mcp stop", "/mcp search", "/mcp playwright",
    "/mcp playwright headed", "/mcp playwright headless",
    "/mcp web", "/token", "/refresh", "/yolo", "/help", "/exit",
]

# Model IDs — cập nhật runtime khi fetch_models
_model_ids_for_complete: list[str] = []


def _get_suggestions(text: str) -> list[str]:
    """Trả về danh sách gợi ý dựa trên text đang gõ."""
    if not text.startswith("/"):
        return []

    # /select <arg> → gợi ý số hoặc tên model
    if text.startswith("/select "):
        arg = text[8:]
        suggestions = []
        for i, mid in enumerate(_model_ids_for_complete, 1):
            if not arg:
                suggestions.append(f"/select {i}  {C.DIM}({mid}){C.RESET}")
            elif arg.isdigit() and str(i).startswith(arg):
                suggestions.append(f"/select {i}  {C.DIM}({mid}){C.RESET}")
            elif mid.lower().startswith(arg.lower()):
                suggestions.append(f"/select {mid}")
        return suggestions[:8]  # Max 8 gợi ý

    # / commands
    matches = [cmd for cmd in SLASH_COMMANDS if cmd.startswith(text)]
    # Chỉ hiện tối đa 10 gợi ý
    return matches[:10]


def _smart_input(prompt: str) -> str:
    """Input với inline autocomplete popup cho / commands.

    - Gõ / → hiện gợi ý bên dưới
    - Gõ thêm chữ → thu hẹp gợi ý
    - Tab → chọn gợi ý đầu tiên
    - ↑/↓ → duyệt history
    - Enter → submit
    - Backspace → xóa
    - Ctrl+C → raise KeyboardInterrupt
    """
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    buf = []        # Ký tự đang gõ
    cursor = 0      # Vị trí con trỏ trong buf
    prev_suggestion_lines = 0  # Số dòng gợi ý đang hiển thị
    prev_total_lines = 0       # Tổng số dòng vật lý (prompt+text) lần vẽ trước

    # History
    if not hasattr(_smart_input, "_history"):
        _smart_input._history = []
    history = _smart_input._history
    hist_idx = len(history)  # Bắt đầu ở cuối (dòng mới)
    saved_buf = None  # Lưu dòng đang gõ khi duyệt history

    import re as _re

    def _visible_len(s: str) -> int:
        """Tính độ dài hiển thị (bỏ ANSI escape codes)."""
        return len(_re.sub(r'\033\[[^m]*m', '', s))

    def _get_term_width() -> int:
        try:
            return os.get_terminal_size().columns
        except OSError:
            return 80

    # Track the physical line the cursor is on (0-based from the first prompt line)
    # After _redraw(), this is updated to reflect cursor's actual physical line.
    _cursor_phys_line = [0]

    def _redraw():
        nonlocal prev_suggestion_lines, prev_total_lines
        text = "".join(buf)
        term_w = _get_term_width()
        prompt_vis = _visible_len(prompt)

        # Tính số dòng vật lý mà prompt+text chiếm
        total_visible = prompt_vis + len(text)
        cur_lines = max(1, (total_visible + term_w - 1) // term_w)

        # === Bước 1: Di chuyển con trỏ về dòng đầu tiên của prompt ===
        up_from_cursor = _cursor_phys_line[0]
        if up_from_cursor > 0:
            sys.stdout.write(f"\033[{up_from_cursor}A")

        # === Bước 2: Xóa từ đầu dòng prompt đến hết màn hình ===
        sys.stdout.write("\r\033[J")

        # === Bước 3: Vẽ lại prompt + text ===
        sys.stdout.write(prompt)
        sys.stdout.write(text)

        prev_total_lines = cur_lines
        prev_suggestion_lines = 0

        # === Bước 4: Tính vị trí cursor trong text ===
        cursor_pos = prompt_vis + cursor
        cursor_line = cursor_pos // term_w
        cursor_col = cursor_pos % term_w

        # Tính end_line (dòng vật lý cuối cùng sau khi viết text)
        if total_visible == 0:
            end_line = 0
        elif total_visible % term_w == 0:
            end_line = total_visible // term_w
        else:
            end_line = (total_visible - 1) // term_w

        # === Bước 5: Hiển thị gợi ý (trước khi di chuyển cursor về vị trí đúng) ===
        # Lúc này cursor đang ở cuối text (end_line).
        # Viết suggestions xuống phía dưới, rồi tự di chuyển lên cursor_line.
        suggestions = _get_suggestions(text) if text.startswith("/") else []
        n_sugg = len(suggestions)
        prev_suggestion_lines = n_sugg

        if suggestions:
            for s in suggestions:
                sys.stdout.write(f"\r\n  {C.DIM}{s}{C.RESET}")
            # Bây giờ cursor ở dòng end_line + n_sugg
            # Cần quay về cursor_line
            total_up = (end_line - cursor_line) + n_sugg
            if total_up > 0:
                sys.stdout.write(f"\033[{total_up}A")
        else:
            # Không có suggestion, chỉ cần di chuyển từ end_line về cursor_line
            lines_up = end_line - cursor_line
            if lines_up > 0:
                sys.stdout.write(f"\033[{lines_up}A")
            elif lines_up < 0:
                sys.stdout.write(f"\033[{-lines_up}B")

        # Di chuyển về cột đúng
        if cursor_col > 0:
            sys.stdout.write(f"\r\033[{cursor_col}C")
        else:
            sys.stdout.write("\r")

        _cursor_phys_line[0] = cursor_line

        sys.stdout.flush()

    try:
        tty.setraw(fd)

        # In prompt ban đầu
        sys.stdout.write(prompt)
        sys.stdout.flush()

        while True:
            # Đọc 1 byte
            ch = os.read(fd, 1)

            if ch == b'\r' or ch == b'\n':
                # Enter → submit
                # Di chuyển con trỏ xuống cuối text, xóa hết phía dưới
                term_w = _get_term_width()
                prompt_vis = _visible_len(prompt)
                total_vis = prompt_vis + len(buf)
                if total_vis == 0:
                    end_line = 0
                elif total_vis % term_w == 0:
                    end_line = total_vis // term_w
                else:
                    end_line = (total_vis - 1) // term_w
                down = end_line - _cursor_phys_line[0]
                if down > 0:
                    sys.stdout.write(f"\033[{down}B")
                sys.stdout.write("\033[J\r\n")  # xóa từ cursor đến hết màn hình + xuống dòng
                sys.stdout.flush()
                result = "".join(buf)
                if result.strip():
                    history.append(result)
                return result

            elif ch == b'\x03':
                # Ctrl+C
                term_w = _get_term_width()
                prompt_vis = _visible_len(prompt)
                total_vis = prompt_vis + len(buf)
                if total_vis == 0:
                    end_line = 0
                elif total_vis % term_w == 0:
                    end_line = total_vis // term_w
                else:
                    end_line = (total_vis - 1) // term_w
                down = end_line - _cursor_phys_line[0]
                if down > 0:
                    sys.stdout.write(f"\033[{down}B")
                sys.stdout.write("\033[J\r\n")
                sys.stdout.flush()
                raise KeyboardInterrupt

            elif ch == b'\x04':
                # Ctrl+D (EOF)
                term_w = _get_term_width()
                prompt_vis = _visible_len(prompt)
                total_vis = prompt_vis + len(buf)
                if total_vis == 0:
                    end_line = 0
                elif total_vis % term_w == 0:
                    end_line = total_vis // term_w
                else:
                    end_line = (total_vis - 1) // term_w
                down = end_line - _cursor_phys_line[0]
                if down > 0:
                    sys.stdout.write(f"\033[{down}B")
                sys.stdout.write("\033[J\r\n")
                sys.stdout.flush()
                raise EOFError

            elif ch == b'\x7f' or ch == b'\x08':
                # Backspace
                if cursor > 0:
                    buf.pop(cursor - 1)
                    cursor -= 1
                    _redraw()

            elif ch == b'\t':
                # Tab → chọn gợi ý đầu tiên
                text = "".join(buf)
                suggestions = _get_suggestions(text) if text.startswith("/") else []
                if suggestions:
                    # Lấy text thật từ suggestion (bỏ ANSI + phần mô tả)
                    import re
                    raw = re.sub(r'\033\[[^m]*m', '', suggestions[0])
                    # Nếu có phần "  (model_id)" thì chỉ lấy phần trước
                    if "  (" in raw:
                        raw = raw[:raw.index("  (")]
                    buf = list(raw)
                    cursor = len(buf)
                    _redraw()

            elif ch == b'\x1b':
                # Escape sequence (arrows, etc.)
                # Đọc thêm 2 byte
                if _select.select([fd], [], [], 0.05)[0]:
                    seq1 = os.read(fd, 1)
                    if seq1 == b'[' and _select.select([fd], [], [], 0.05)[0]:
                        seq2 = os.read(fd, 1)
                        if seq2 == b'A':
                            # ↑ Arrow Up — history previous
                            if hist_idx > 0:
                                if hist_idx == len(history):
                                    saved_buf = list(buf)
                                hist_idx -= 1
                                buf = list(history[hist_idx])
                                cursor = len(buf)
                                _redraw()
                        elif seq2 == b'B':
                            # ↓ Arrow Down — history next
                            if hist_idx < len(history):
                                hist_idx += 1
                                if hist_idx == len(history):
                                    buf = saved_buf if saved_buf is not None else []
                                else:
                                    buf = list(history[hist_idx])
                                cursor = len(buf)
                                _redraw()
                        elif seq2 == b'C':
                            # → Arrow Right
                            if cursor < len(buf):
                                cursor += 1
                                _redraw()
                        elif seq2 == b'D':
                            # ← Arrow Left
                            if cursor > 0:
                                cursor -= 1
                                _redraw()
                        elif seq2 == b'3':
                            # Delete key (ESC [ 3 ~)
                            if _select.select([fd], [], [], 0.05)[0]:
                                os.read(fd, 1)  # consume '~'
                            if cursor < len(buf):
                                buf.pop(cursor)
                                _redraw()
                        elif seq2 == b'H':
                            # Home
                            cursor = 0
                            _redraw()
                        elif seq2 == b'F':
                            # End
                            cursor = len(buf)
                            _redraw()
                    else:
                        pass  # Unknown escape
                else:
                    pass  # Single ESC

            elif ch >= b' ':
                # Printable character (bao gồm UTF-8 multi-byte)
                # Xử lý UTF-8
                byte = ch[0]
                if byte < 0x80:
                    char = ch.decode('utf-8')
                elif byte < 0xE0:
                    char = (ch + os.read(fd, 1)).decode('utf-8', errors='replace')
                elif byte < 0xF0:
                    char = (ch + os.read(fd, 2)).decode('utf-8', errors='replace')
                else:
                    char = (ch + os.read(fd, 3)).decode('utf-8', errors='replace')

                buf.insert(cursor, char)
                cursor += 1
                _redraw()

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


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


# ═════════════════════════════════════════���═════════════════════
# RICH UI HELPERS — Claude Code style display
# ═══════════════════════════════════════════════════════════════

def _format_tool_summary(tool_name: str, func_args: dict) -> str:
    """Trả về mô tả 1 dòng dễ đọc cho tool call."""
    if tool_name == "read_text_file":
        path = func_args.get("path", func_args.get("file_path", "?"))
        return f"Reading {path}"
    elif tool_name == "write_file":
        path = func_args.get("path", func_args.get("file_path", "?"))
        return f"Writing {path}"
    elif tool_name == "edit_file":
        path = func_args.get("path", func_args.get("file_path", "?"))
        return f"Editing {path}"
    elif tool_name == "list_directory":
        path = func_args.get("path", func_args.get("directory", "."))
        return f"Listing {path}"
    elif tool_name == "search_files":
        pattern = func_args.get("pattern", func_args.get("query", "?"))
        return f"Searching: {pattern}"
    elif tool_name == "execute_command":
        cmd = func_args.get("command", "?")
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        return f"Running: {cmd}"
    elif tool_name == "fetch":
        url = func_args.get("url", "?")
        if len(url) > 60:
            url = url[:57] + "..."
        return f"Fetching {url}"
    elif tool_name == "web_search":
        query = func_args.get("query", "?")
        return f"Searching web: {query}"
    else:
        return f"Calling {tool_name}"


def _ask_permission(tool_name: str, func_args: dict) -> bool:
    """Hiện cảnh báo tool nguy hiểm, hỏi user cho phép. Bỏ qua nếu yolo_mode."""
    global yolo_mode
    if yolo_mode:
        return True
    if not HAS_RICH:
        return True

    summary = _format_tool_summary(tool_name, func_args)

    # Show compact summary — không dump raw JSON
    console.print(f"  [bold yellow]{summary}[/]")
    try:
        answer = input(f"  {C.YELLOW}Allow? (Y/n): {C.RESET}").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return False

    return answer in ("", "y", "yes")


def _generate_diff(old_content: str, new_content: str, filename: str = "file") -> str:
    """Tạo unified diff string."""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{filename}", tofile=f"b/{filename}")
    return "".join(diff)


def _render_tool_result(tool_name: str, func_args: dict, result_text: str, old_content: str = None) -> None:
    """Hiện kết quả tool compact — Claude Code style.

    Nguyên tắc: chỉ hiện summary 1 dòng. Chỉ show details khi lỗi hoặc diff.
    """
    if not HAS_RICH:
        # Fallback to raw print
        preview = result_text[:200] + "..." if len(result_text) > 200 else result_text
        print(f"     {C.DIM}→ {preview}{C.RESET}")
        return

    is_error = "[Tool Error]" in result_text or "[Lỗi]" in result_text
    summary = _format_tool_summary(tool_name, func_args)

    # ── Error → show panel with details ──
    if is_error:
        lines = result_text.split("\n")
        truncated = "\n".join(lines[:15]) if len(lines) > 15 else result_text
        console.print(Panel(
            Text(truncated, style="red"),
            title=f"[bold red] Error: {summary} [/]",
            border_style="red",
            padding=(0, 1),
        ))
        return

    # ── Diff display cho write/edit ──
    if old_content is not None and tool_name in ("write_file", "edit_file"):
        filename = func_args.get("path", func_args.get("file_path", "file"))
        new_content = func_args.get("content", result_text)
        diff_text = _generate_diff(old_content, new_content, os.path.basename(filename))
        if diff_text.strip():
            # Count additions/deletions
            added = sum(1 for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in diff_text.splitlines() if l.startswith("-") and not l.startswith("---"))
            console.print(Panel(
                Syntax(diff_text, "diff", theme="monokai", line_numbers=False),
                title=f"[bold cyan] {filename} [/]",
                subtitle=f"[dim]+{added} -{removed}[/]",
                border_style="cyan",
                padding=(0, 1),
            ))
        else:
            console.print(f"  [dim green]✓ {filename} (no changes)[/]")
        return

    # ── Write file (new) → 1 line ──
    if tool_name == "write_file" and old_content is None:
        filepath = func_args.get("path", func_args.get("file_path", "file"))
        console.print(f"  [bold green]✓ Created {filepath}[/]")
        return

    # ── Read file → compact summary (không dump nội dung) ──
    if tool_name == "read_text_file":
        filepath = func_args.get("path", func_args.get("file_path", "file"))
        line_count = result_text.count("\n") + 1
        char_count = len(result_text)
        console.print(f"  [dim]⎯ Read {filepath} ({line_count} lines, {char_count:,} chars)[/]")
        return

    # ── Execute command → show exit status + compact output ──
    if tool_name == "execute_command":
        cmd = func_args.get("command", "")
        lines = result_text.strip().split("\n")
        if len(lines) <= 5:
            # Short output → show inline
            output_preview = "\n".join(lines)
            console.print(Panel(
                Text(output_preview),
                title=f"[bold yellow] $ {cmd[:80]} [/]",
                border_style="dim",
                padding=(0, 1),
            ))
        else:
            # Long output → show first/last few lines
            console.print(f"  [dim]⎯ $ {cmd[:80]}  ({len(lines)} lines)[/]")
        return

    # ── Web search → show compact results ──
    if tool_name == "web_search":
        query = func_args.get("query", "?")
        # Try parse JSON results
        try:
            results = json.loads(result_text)
            if isinstance(results, list):
                table = Table(show_header=False, box=None, padding=(0, 1))
                table.add_column("", style="bold")
                table.add_column("", style="dim")
                for r in results[:5]:
                    title = r.get("title", "")
                    url = r.get("url", r.get("href", ""))
                    table.add_row(title, url)
                console.print(Panel(
                    table,
                    title=f"[bold blue] Search: {query} [/]",
                    border_style="blue",
                    padding=(0, 1),
                ))
                return
        except (json.JSONDecodeError, TypeError):
            pass
        # Fallback: show truncated text
        console.print(f"  [dim]⎯ Search: {query} ({len(result_text):,} chars)[/]")
        return

    # ── Fetch URL → compact ──
    if tool_name == "fetch":
        url = func_args.get("url", "?")
        char_count = len(result_text)
        console.print(f"  [dim]⎯ Fetched {url[:60]} ({char_count:,} chars)[/]")
        return

    # ── Default: 1-line summary ──
    char_count = len(result_text)
    console.print(f"  [dim]⎯ {summary} ({char_count:,} chars)[/]")


class StreamingMarkdown:
    """Rich streaming markdown renderer with thinking spinner — Claude Code style."""

    def __init__(self):
        self._buffer = ""
        self._live = None
        self._started = False
        self._thinking = False
        self._first_token = False

    def start(self):
        """Bắt đầu Live với spinner 'Thinking...'"""
        if not HAS_RICH:
            return
        self._live = Live(
            Spinner("dots", text="[dim]Thinking...[/]"),
            console=console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()
        self._started = True
        self._thinking = True

    def show_thinking(self):
        """Hiển thị spinner thinking."""
        if not HAS_RICH or not self._live:
            print(f"\n{C.DIM}💭 Thinking...{C.RESET}")
            return
        if self._started and not self._thinking:
            self._thinking = True
            self._live.update(Spinner("dots", text="[dim italic]Thinking...[/]"))

    def feed(self, text: str):
        """Thêm text vào buffer, re-render Markdown."""
        if not HAS_RICH or not self._live:
            sys.stdout.write(text)
            sys.stdout.flush()
            return

        if self._thinking:
            self._thinking = False
            self._first_token = True

        self._buffer += text
        try:
            self._live.update(Markdown(self._buffer))
        except Exception:
            # Fallback nếu Markdown render lỗi
            self._live.update(Text(self._buffer))

    def stop(self) -> str:
        """Dừng Live, trả về full text."""
        if not HAS_RICH or not self._live:
            print()
            return self._buffer

        try:
            # Final render
            if self._buffer:
                self._live.update(Markdown(self._buffer))
            self._live.stop()
        except Exception:
            try:
                self._live.stop()
            except Exception:
                pass
        self._started = False
        return self._buffer


# ═══════════════════════════════════════════════════════════════
# SESSION MANAGER — Lưu / Load / Quản lý phiên chat
# ═══════════════════════════════════════════════════════════════
import re as _re_mod
import glob as _glob_mod
from datetime import datetime as _datetime


class SessionManager:
    """Quản lý các phiên chat (lưu/load/rename/delete)."""

    def __init__(self, sessions_dir: str = SESSIONS_DIR):
        self.sessions_dir = sessions_dir
        os.makedirs(self.sessions_dir, exist_ok=True)

    def _sanitize_name(self, name: str) -> str:
        """Chuẩn hóa tên phiên thành tên file an toàn."""
        # Giữ unicode nhưng loại ký tự đặc biệt filesystem
        name = name.strip()
        name = _re_mod.sub(r'[\\/:*?"<>|]', '_', name)
        name = _re_mod.sub(r'\s+', '_', name)
        return name[:100] or "unnamed"

    def _gen_session_id(self) -> str:
        """Tạo ID duy nhất cho phiên."""
        return _datetime.now().strftime("%Y%m%d_%H%M%S")

    def _list_session_files(self) -> list:
        """Liệt kê tất cả file phiên, sắp xếp theo thời gian chỉnh sửa (mới nhất trước)."""
        pattern = os.path.join(self.sessions_dir, "*.json")
        files = _glob_mod.glob(pattern)
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        return files

    def _load_session_meta(self, filepath: str) -> dict:
        """Load metadata của phiên (không load toàn bộ messages để nhanh)."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "file": filepath,
                "name": data.get("name", "unnamed"),
                "model": data.get("model", "?"),
                "cwd": data.get("cwd", "?"),
                "created_at": data.get("created_at", "?"),
                "updated_at": data.get("updated_at", "?"),
                "message_count": len(data.get("messages", [])),
                "system_prompt_custom": data.get("system_prompt") != SYSTEM_PROMPT if data.get("system_prompt") else False,
            }
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, client, name: str = None) -> str:
        """Lưu phiên chat hiện tại.

        Returns: đường dẫn file đã lưu, hoặc None nếu lỗi.
        """
        if not client.messages:
            print(f"{C.YELLOW}[!] Không có lịch sử chat để lưu.{C.RESET}")
            return None

        now = _datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        # user_named = True nếu user chủ động truyền tên
        user_named = name is not None and name.strip() != ""

        # Tạo tên tự động nếu chưa có
        if not name:
            # Lấy câu hỏi đầu tiên của user làm tên
            first_user = ""
            for m in client.messages:
                if m.get("role") == "user":
                    first_user = (m.get("content") or "")[:80]
                    break
            if first_user:
                # Cắt dòng đầu, bỏ ký tự thừa
                name = first_user.split("\n")[0].strip()
            if not name:
                name = f"session_{now.strftime('%Y%m%d_%H%M%S')}"

        safe_name = self._sanitize_name(name)
        session_id = self._gen_session_id()

        # Kiểm tra xem đã có file với tên này chưa
        # Nếu đang update phiên đã load → ghi đè
        existing_file = getattr(client, '_loaded_session_file', None)
        if existing_file and os.path.isfile(existing_file):
            filepath = existing_file
            # Giữ tên cũ nếu user không chủ động đặt tên mới
            if not user_named:
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        old_data = json.load(f)
                    name = old_data.get("name", name)
                except (json.JSONDecodeError, OSError):
                    pass
        else:
            # Tạo file mới
            filename = f"{session_id}_{safe_name}.json"
            filepath = os.path.join(self.sessions_dir, filename)

        session_data = {
            "name": name,
            "model": client.selected_model,
            "cwd": os.getcwd(),
            "system_prompt": client.system_prompt,
            "created_at": getattr(client, '_session_created_at', now_str),
            "updated_at": now_str,
            "messages": client.messages,
        }

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(session_data, f, ensure_ascii=False, indent=2)

            # Đánh dấu file đang dùng (cho lần save tiếp theo ghi đè)
            client._loaded_session_file = filepath
            client._session_created_at = session_data["created_at"]

            msg_count = len(client.messages)
            print(f"{C.GREEN}[+] Đã lưu phiên: {C.BOLD}{name}{C.RESET}")
            print(f"    {C.DIM}{msg_count} messages | Model: {client.selected_model}{C.RESET}")
            print(f"    {C.DIM}File: {filepath}{C.RESET}")
            return filepath

        except OSError as e:
            print(f"{C.RED}[!] Lỗi lưu phiên: {e}{C.RESET}")
            return None

    def load(self, client, identifier: str = None) -> bool:
        """Load phiên chat từ file.

        identifier: số thứ tự (1-based) hoặc tên phiên (fuzzy match).
        Nếu None → hiện danh sách để chọn.
        """
        files = self._list_session_files()
        if not files:
            print(f"{C.YELLOW}[!] Chưa có phiên nào được lưu.{C.RESET}")
            return False

        # Nếu không có identifier → hiện danh sách và cho chọn
        if not identifier:
            self.display_sessions()
            try:
                choice = input(f"\n{C.YELLOW}Chọn phiên (số): {C.RESET}").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                return False
            if not choice:
                return False
            identifier = choice

        # Tìm file phiên
        filepath = None

        # Thử parse số
        if identifier.isdigit():
            idx = int(identifier) - 1
            if 0 <= idx < len(files):
                filepath = files[idx]
            else:
                print(f"{C.RED}[!] Số phiên không hợp lệ (1-{len(files)}).{C.RESET}")
                return False
        else:
            # Fuzzy match theo tên
            identifier_lower = identifier.lower()
            for f in files:
                meta = self._load_session_meta(f)
                if meta and identifier_lower in meta["name"].lower():
                    filepath = f
                    break

            if not filepath:
                print(f"{C.RED}[!] Không tìm thấy phiên: {identifier}{C.RESET}")
                return False

        # Load dữ liệu
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"{C.RED}[!] Lỗi đọc file phiên: {e}{C.RESET}")
            return False

        session_name = data.get("name", "unnamed")
        session_cwd = data.get("cwd", "")
        session_model = data.get("model", "")
        session_messages = data.get("messages", [])
        session_system = data.get("system_prompt", "")
        created_at = data.get("created_at", "?")
        updated_at = data.get("updated_at", "?")

        # Hỏi có muốn chuyển thư mục không (nếu khác cwd hiện tại)
        current_cwd = os.getcwd()
        if session_cwd and session_cwd != current_cwd and os.path.isdir(session_cwd):
            print(f"\n{C.YELLOW}  Phiên này được tạo tại: {C.BOLD}{session_cwd}{C.RESET}")
            print(f"{C.YELLOW}  Thư mục hiện tại:       {C.BOLD}{current_cwd}{C.RESET}")
            try:
                switch = input(f"{C.YELLOW}  Chuyển sang thư mục cũ? [Y/n]: {C.RESET}").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print()
                switch = "n"

            if switch != "n":
                os.chdir(session_cwd)
                print(f"{C.GREEN}  [+] Đã chuyển sang: {session_cwd}{C.RESET}")
                # Re-init filesystem MCP cho thư mục mới
                if client.mcp_manager and "filesystem" in client.mcp_manager.servers:
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
                    if client.mcp_manager.add_filesystem_server([session_cwd]):
                        print(f"{C.GREEN}  [+] MCP Filesystem → {session_cwd}{C.RESET}")

        # Restore messages & system prompt
        client.messages = session_messages
        if session_system:
            client.system_prompt = session_system

        # Restore model (nếu có trong danh sách)
        if session_model and client.models:
            for m in client.models:
                if m.get("id") == session_model:
                    client.selected_model = session_model
                    break

        # Đánh dấu file đang dùng
        client._loaded_session_file = filepath
        client._session_created_at = created_at

        # Hiển thị summary
        n_user = sum(1 for m in session_messages if m.get("role") == "user")
        n_assistant = sum(1 for m in session_messages if m.get("role") == "assistant")
        n_tool = sum(1 for m in session_messages if m.get("role") == "tool")

        print()
        print(f"{C.GREEN}[+] Đã load phiên: {C.BOLD}{session_name}{C.RESET}")
        print(f"    {C.DIM}Model: {session_model} | CWD: {session_cwd}{C.RESET}")
        print(f"    {C.DIM}Tạo: {created_at} | Cập nhật: {updated_at}{C.RESET}")
        print(f"    {C.DIM}{len(session_messages)} messages ({n_user} user, {n_assistant} assistant, {n_tool} tool){C.RESET}")
        print(f"    {C.DIM}Dùng /history để xem nội dung, /save để lưu tiếp.{C.RESET}")
        print()
        return True

    def display_sessions(self):
        """Hiển thị danh sách các phiên đã lưu."""
        files = self._list_session_files()
        if not files:
            print(f"{C.YELLOW}[!] Chưa có phiên nào được lưu.{C.RESET}")
            print(f"{C.DIM}    Dùng /save [tên] để lưu phiên hiện tại.{C.RESET}")
            return

        print()
        print(f"{C.BOLD}{C.CYAN}{'═' * 80}{C.RESET}")
        print(f"  {C.BOLD}💾 CÁC PHIÊN CHAT ĐÃ LƯU ({len(files)} phiên){C.RESET}")
        print(f"{C.BOLD}{C.CYAN}{'═' * 80}{C.RESET}")

        for i, filepath in enumerate(files, 1):
            meta = self._load_session_meta(filepath)
            if not meta:
                continue

            name = meta["name"]
            model = meta["model"]
            cwd = meta["cwd"]
            updated = meta["updated_at"]
            msg_count = meta["message_count"]
            custom_sys = meta["system_prompt_custom"]

            # Tags
            tags = []
            if custom_sys:
                tags.append(f"{C.MAGENTA}[custom-prompt]{C.RESET}")

            tag_str = " ".join(tags)

            print(f"\n  {C.DIM}{i:>3}.{C.RESET} {C.BOLD}{C.WHITE}{name}{C.RESET} {tag_str}")
            print(f"       {C.DIM}Model: {model} | {msg_count} msgs | {updated}{C.RESET}")
            print(f"       {C.DIM}📁 {cwd}{C.RESET}")

        print(f"\n{C.BOLD}{C.CYAN}{'═' * 80}{C.RESET}")
        print(f"  {C.DIM}/load <số>                   - Load phiên{C.RESET}")
        print(f"  {C.DIM}/save [tên]                  - Lưu phiên hiện tại{C.RESET}")
        print(f"  {C.DIM}/sessions rename <số> <tên>  - Đổi tên phiên{C.RESET}")
        print(f"  {C.DIM}/sessions delete <số>        - Xóa phiên{C.RESET}")
        print()

    def rename(self, identifier: str, new_name: str) -> bool:
        """Đổi tên phiên."""
        files = self._list_session_files()
        if not files:
            print(f"{C.YELLOW}[!] Chưa có phiên nào.{C.RESET}")
            return False

        if not identifier.isdigit():
            print(f"{C.RED}[!] Dùng số thứ tự: /sessions rename <số> <tên mới>{C.RESET}")
            return False

        idx = int(identifier) - 1
        if idx < 0 or idx >= len(files):
            print(f"{C.RED}[!] Số phiên không hợp lệ (1-{len(files)}).{C.RESET}")
            return False

        filepath = files[idx]

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            old_name = data.get("name", "unnamed")
            data["name"] = new_name.strip()

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            print(f"{C.GREEN}[+] Đổi tên: {C.DIM}{old_name}{C.RESET} → {C.BOLD}{new_name}{C.RESET}")
            return True

        except (json.JSONDecodeError, OSError) as e:
            print(f"{C.RED}[!] Lỗi: {e}{C.RESET}")
            return False

    def delete(self, identifier: str) -> bool:
        """Xóa phiên."""
        files = self._list_session_files()
        if not files:
            print(f"{C.YELLOW}[!] Chưa có phiên nào.{C.RESET}")
            return False

        filepath = None

        if identifier.isdigit():
            idx = int(identifier) - 1
            if 0 <= idx < len(files):
                filepath = files[idx]
            else:
                print(f"{C.RED}[!] Số phiên không hợp lệ (1-{len(files)}).{C.RESET}")
                return False
        else:
            # Fuzzy match
            for f in files:
                meta = self._load_session_meta(f)
                if meta and identifier.lower() in meta["name"].lower():
                    filepath = f
                    break

        if not filepath:
            print(f"{C.RED}[!] Không tìm thấy phiên: {identifier}{C.RESET}")
            return False

        # Load tên để confirm
        meta = self._load_session_meta(filepath)
        name = meta["name"] if meta else os.path.basename(filepath)

        try:
            confirm = input(f"{C.YELLOW}Xóa phiên \"{name}\"? [y/N]: {C.RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return False

        if confirm != "y":
            print(f"{C.DIM}  Đã hủy.{C.RESET}")
            return False

        try:
            os.remove(filepath)
            print(f"{C.GREEN}[+] Đã xóa phiên: {name}{C.RESET}")
            return True
        except OSError as e:
            print(f"{C.RED}[!] Lỗi xóa: {e}{C.RESET}")
            return False


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

        # Tool calling config
        self.max_tool_rounds = 30
        self.max_consecutive_errors = 3  # 0 = unlimited

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
            # Cập nhật cho autocomplete
            global _model_ids_for_complete
            _model_ids_for_complete = [m.get("id", "") for m in ordered]

            return True

        except requests.exceptions.RequestException as e:
            print(f"{C.RED}[!] Lỗi kết nối: {e}{C.RESET}")
            return False

    def _get_chat_models_ordered(self) -> list[dict]:
        """Lấy toàn bộ models theo thứ tự hiển thị (lightweight → versatile → powerful → other)."""
        # Hiện tất cả models, không lọc theo model_picker_enabled hay type
        all_models = list(self.models)
        categories = {}
        for m in all_models:
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
            "lightweight": "Lightweight (Nhanh)",
            "versatile":   "Versatile (Da nang)",
            "powerful":    "Powerful (Manh me)",
            "other":       "Other",
        }

        if HAS_RICH:
            table = Table(show_header=True, header_style="bold cyan", border_style="cyan", padding=(0, 1))
            table.add_column("#", style="dim", width=4, justify="right")
            table.add_column("Model ID", style="bold white", min_width=30)
            table.add_column("Vendor", style="dim", width=12)
            table.add_column("Context", justify="right", width=8)
            table.add_column("Output", justify="right", width=8)
            table.add_column("Tags", width=25)

            idx = 1
            for cat in ["lightweight", "versatile", "powerful", "other"]:
                if cat not in categories:
                    continue
                table.add_row("", f"[bold yellow]{cat_labels.get(cat, cat)}[/]", "", "", "", "", end_section=True)

                for m in categories[cat]:
                    model_id = m.get("id", "")
                    vendor = m.get("vendor", "")
                    is_premium = m.get("billing", {}).get("is_premium", False)
                    multiplier = m.get("billing", {}).get("multiplier", 0)
                    is_preview = m.get("preview", False)
                    is_default = m.get("is_chat_default", False)
                    supports_thinking = m.get("capabilities", {}).get("supports", {}).get("adaptive_thinking", False) or \
                                        m.get("capabilities", {}).get("supports", {}).get("max_thinking_budget", 0) > 0
                    max_ctx = m.get("capabilities", {}).get("limits", {}).get("max_context_window_tokens", 0)
                    max_out = m.get("capabilities", {}).get("limits", {}).get("max_output_tokens", 0)

                    tags = []
                    if is_default:
                        tags.append("[green]DEFAULT[/]")
                    if is_preview:
                        tags.append("[magenta]PREVIEW[/]")
                    if is_premium:
                        tags.append(f"[yellow]PREMIUM x{multiplier}[/]")
                    else:
                        tags.append("[green]FREE[/]")
                    if supports_thinking:
                        tags.append("[cyan]THINK[/]")

                    ctx_k = f"{max_ctx // 1000}K" if max_ctx else "?"
                    out_k = f"{max_out // 1000}K" if max_out else "?"

                    marker = "[green]>[/] " if self.selected_model and self.selected_model == model_id else "  "
                    model_display = f"{marker}{model_id}"

                    table.add_row(str(idx), model_display, vendor, ctx_k, out_k, " ".join(tags))
                    idx += 1

            console.print()
            console.print(Panel(table, title="[bold cyan] DANH SACH MODELS [/]", border_style="cyan"))
            console.print("  [dim]Dung /select <so> hoac /select <model_id> de chon model.[/]")
            console.print()
            return

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

        # Cảnh báo nếu không phải chat model (nhưng vẫn cho chọn)
        model_type = found.get("capabilities", {}).get("type", "unknown")
        if model_type != "chat":
            print(f"{C.YELLOW}[!] Cảnh báo: Model '{model_id}' có type='{model_type}', có thể không hỗ trợ chat tốt.{C.RESET}")

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

        if HAS_RICH:
            info_table = Table(show_header=False, border_style="cyan", padding=(0, 2), box=None)
            info_table.add_column("Property", style="bold", width=18)
            info_table.add_column("Value")
            info_table.add_row("ID", found.get("id", ""))
            info_table.add_row("Vendor", found.get("vendor", ""))
            info_table.add_row("Version", found.get("version", ""))
            info_table.add_row("Preview", str(found.get("preview", False)))
            info_table.add_row("Premium", f"{billing.get('is_premium', False)} (x{billing.get('multiplier', 0)})")
            ctx_tokens = limits.get("max_context_window_tokens", "?")
            out_tokens = limits.get("max_output_tokens", "?")
            prompt_tokens = limits.get("max_prompt_tokens", "?")
            info_table.add_row("Context Window", f"{ctx_tokens:,} tokens" if isinstance(ctx_tokens, int) else str(ctx_tokens))
            info_table.add_row("Max Output", f"{out_tokens:,} tokens" if isinstance(out_tokens, int) else str(out_tokens))
            info_table.add_row("Max Prompt", f"{prompt_tokens:,} tokens" if isinstance(prompt_tokens, int) else str(prompt_tokens))
            info_table.add_row("Vision", str(supports.get("vision", False)))
            info_table.add_row("Tool Calls", str(supports.get("tool_calls", False)))
            info_table.add_row("Streaming", str(supports.get("streaming", False)))
            info_table.add_row("Thinking", str(supports.get("max_thinking_budget", 0) > 0))
            console.print()
            console.print(Panel(info_table, title=f"[bold cyan] {found.get('name', '')} [/]", border_style="cyan"))
            console.print()
        else:
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
        max_tool_rounds = self.max_tool_rounds
        consecutive_errors = 0
        max_consecutive_errors = self.max_consecutive_errors
        for _round in range(max_tool_rounds):
            # Auto-retry khi gặp lỗi kết nối (RemoteDisconnected, timeout, etc.)
            result = None
            for _attempt in range(3):
                result = self._send_chat_request(
                    request_id=interaction_request_id,
                    interaction_id=interaction_id,
                    round_number=_round,
                )
                if result is not None:
                    break
                # Lỗi kết nối — retry sau delay
                delay = 2 ** _attempt  # 1s, 2s, 4s
                print(f"{C.YELLOW}[↻] Retrying in {delay}s... (attempt {_attempt + 2}/3){C.RESET}")
                time.sleep(delay)
                # Refresh token phòng trường hợp hết hạn
                self.ensure_token()

            if result is None:
                print(f"{C.RED}[!] Thất bại sau 3 lần retry.{C.RESET}")
                return ""

            full_content, tool_calls = result

            # Nếu không có tool calls, đã xong
            if not tool_calls:
                if full_content:
                    self.messages.append({"role": "assistant", "content": full_content})
                return full_content

            # Có tool calls → thực thi và gửi lại
            # Validate: lọc bỏ tool calls có name hoặc id rỗng (model hallucinate)
            valid_tool_calls = []
            for tc in tool_calls:
                _tc_id = tc.get("id", "")
                _tc_name = tc.get("function", {}).get("name", "")
                if not _tc_id or not _tc_name:
                    print(f"     {C.RED}[!] Bỏ qua tool call không hợp lệ (id={repr(_tc_id)}, name={repr(_tc_name)}){C.RESET}")
                    continue
                valid_tool_calls.append(tc)
            tool_calls = valid_tool_calls

            # Nếu sau khi lọc không còn tool call hợp lệ → trả content luôn
            if not tool_calls:
                if full_content:
                    self.messages.append({"role": "assistant", "content": full_content})
                return full_content

            # Thêm assistant message với tool_calls (chỉ các tool calls hợp lệ)
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

                summary = _format_tool_summary(func_name, func_args)

                # Permission check cho dangerous tools
                if func_name in DANGEROUS_TOOLS:
                    if not _ask_permission(func_name, func_args):
                        tool_result = "[Permission denied by user]"
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": tool_result,
                        })
                        continue

                # Đọc file hiện tại trước nếu write/edit (để tạo diff sau)
                old_content = None
                if func_name in ("write_file", "edit_file") and self.mcp_manager:
                    filepath = func_args.get("path", func_args.get("file_path", ""))
                    if filepath:
                        try:
                            old_content = self.mcp_manager.execute_tool("read_text_file", {"path": filepath})
                            if "[Tool Error]" in old_content or "[Lỗi]" in old_content:
                                old_content = None  # File chưa tồn tại
                        except Exception:
                            old_content = None

                # Gọi MCP tool với spinner
                if HAS_RICH:
                    with console.status(f"[bold cyan]{summary}[/]", spinner="dots"):
                        if self.mcp_manager:
                            tool_result = self.mcp_manager.execute_tool(func_name, func_args)
                        else:
                            tool_result = "[Lỗi] MCP Manager chưa được khởi tạo"
                else:
                    print(f"\n  {C.YELLOW}🔧 {summary}{C.RESET}")
                    if self.mcp_manager:
                        tool_result = self.mcp_manager.execute_tool(func_name, func_args)
                    else:
                        tool_result = "[Lỗi] MCP Manager chưa được khởi tạo"

                # Hiển thị kết quả
                _render_tool_result(func_name, func_args, tool_result, old_content=old_content)

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
                if max_consecutive_errors > 0 and consecutive_errors >= max_consecutive_errors:
                    print(f"\n{C.RED}[!] Dừng: {consecutive_errors} lần tool call liên tiếp bị lỗi.{C.RESET}")
                    self.messages.append({"role": "assistant", "content": full_content or "[Tool calling failed repeatedly]"})
                    return full_content or ""
            else:
                consecutive_errors = 0

            # Tiếp tục vòng lặp để AI xử lý kết quả tool
            if HAS_RICH:
                console.print(Rule(style="dim"))
            else:
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

    def _is_responses_api(self) -> bool:
        """Kiểm tra model hiện tại có dùng Responses API không."""
        if self.selected_model in RESPONSES_API_MODELS or self.selected_model in GPT_CODEX_RESPONSES_MODELS:
            return True
        # Prefix matching cho các model có date suffix (vd: gpt-5.4-2026-03-05)
        for prefix in GPT_CODEX_RESPONSES_MODELS:
            if self.selected_model.startswith(prefix):
                return True
        return False

    def _is_gpt_codex_model(self) -> bool:
        """Kiểm tra model có phải GPT Codex (max_output_tokens lớn hơn) không."""
        if self.selected_model in GPT_CODEX_RESPONSES_MODELS:
            return True
        for prefix in GPT_CODEX_RESPONSES_MODELS:
            if self.selected_model.startswith(prefix):
                return True
        return False

    def _send_chat_request(self, request_id=None, interaction_id=None, round_number=0):
        """Gửi một request chat và trả về (content, tool_calls) hoặc None nếu lỗi."""
        # Delegate sang Responses API nếu model yêu cầu
        if self._is_responses_api():
            return self._send_responses_request(
                request_id=request_id,
                interaction_id=interaction_id,
                round_number=round_number,
            )

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
                "fetch": "Fetch main content from a URL. Useful for summarizing or analyzing web pages or calling APIs. Use web_search first to find URLs, then fetch to read their content",
                "execute_command": "Run shell commands (bash, python, curl, etc.)",
                "web_search": "Search the web using DuckDuckGo (no API key needed). Returns results with title, URL, snippet. Use this to find current information, look up facts, or research topics",
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
            "X-Initiator": "agent",
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
                # Xóa messages bị lỗi: nếu đang giữa tool loop thì phải rollback
                # tất cả tool results + assistant message cho đến user message gốc
                while self.messages and self.messages[-1].get("role") in ("tool", "assistant"):
                    removed = self.messages.pop()
                    if removed.get("role") == "assistant" and removed.get("tool_calls"):
                        break  # Đã xóa hết 1 round (assistant + tools)
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
            md = StreamingMarkdown()
            md.start()

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
                            md.show_thinking()
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
                            # Không có id → continuation chunk
                            # Opus/Claude đôi khi gửi chunks không có id
                            # → append vào tool call cuối cùng thay vì tạo mới
                            if idx in tool_calls_acc:
                                pass  # Append vào entry hiện tại theo index
                            elif tool_calls_acc:
                                # Fallback: append vào entry cuối cùng
                                idx = max(tool_calls_acc.keys())
                            else:
                                # Chưa có entry nào → tạo mới (sẽ bị filter sau)
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
                        md.feed(content)

                    # Finish reason
                    finish = choices[0].get("finish_reason")
                    if finish:
                        buffer = ""
                        break

            md.stop()  # Dừng streaming markdown

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
                        # Arguments rỗng → fallback "{}"
                        # Nhiều MCP tools (browser_install, etc.) không cần args
                        if not args_str or not args_str.strip() or len(args_str.strip()) <= 2:
                            tc["function"]["arguments"] = "{}"
                            tool_calls.append(tc)
                        else:
                            # Detect concatenated JSON objects: {"cmd":"a"}{"url":"b"}
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
                                # Fallback "{}" nếu args có nội dung nhưng JSON bị lỗi
                                tc["function"]["arguments"] = "{}"
                                tool_calls.append(tc)

            return (full_content, tool_calls)

        except requests.exceptions.RequestException as e:
            try:
                md.stop()
            except Exception:
                pass
            print(f"{C.RED}[!] Lỗi kết nối: {e}{C.RESET}")
            # Chỉ pop nếu message cuối là user message (round đầu tiên)
            # Nếu đang giữa tool loop, không pop để retry giữ nguyên context
            if self.messages and self.messages[-1].get("role") == "user" and not any(
                m.get("role") == "tool" for m in self.messages[-3:]
            ):
                self.messages.pop()
            return None

    # ─── Responses API (oswe-vscode-prime / GPT Codex) ─────
    def _build_responses_input(self, effective_system: str) -> list:
        """Chuyển đổi self.messages (Chat format) sang Responses API input format.

        Chat Completions format:
            {"role": "system", "content": "..."}
            {"role": "user", "content": "..."}
            {"role": "assistant", "content": "...", "tool_calls": [...]}
            {"role": "tool", "tool_call_id": "...", "content": "..."}

        Responses API format:
            {"role": "system", "content": [{"type": "input_text", "text": "..."}]}
            {"role": "user", "content": [{"type": "input_text", "text": "..."}]}
            {"role": "assistant", "content": [{"type": "output_text", "text": "..."}]}
            {"type": "function_call", "name": "...", "arguments": "...", "call_id": "..."}
            {"type": "function_call_output", "call_id": "...", "output": "..."}
        """
        input_items = []

        # System message
        input_items.append({
            "role": "system",
            "content": [{"type": "input_text", "text": effective_system}],
        })

        trimmed = self._trim_messages_for_context(self.messages)

        for msg in trimmed:
            role = msg.get("role", "")
            content = msg.get("content") or ""

            if role == "user":
                input_items.append({
                    "role": "user",
                    "content": [{"type": "input_text", "text": content}],
                })

            elif role == "assistant":
                # Assistant message có thể có content và/hoặc tool_calls
                if content:
                    input_items.append({
                        "role": "assistant",
                        "type": "message",
                        "content": [{"type": "output_text", "text": content}],
                    })

                # Tool calls → function_call items
                tool_calls = msg.get("tool_calls", [])
                for tc in tool_calls:
                    func = tc.get("function", {})
                    input_items.append({
                        "type": "function_call",
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", "{}"),
                        "call_id": tc.get("id", ""),
                    })

            elif role == "tool":
                # Tool result → function_call_output
                input_items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": content,
                })

        return input_items

    def _send_responses_request(self, request_id=None, interaction_id=None, round_number=0):
        """Gửi request sử dụng Responses API (POST /responses) và trả về (content, tool_calls)."""
        # Build system prompt
        effective_system = self.system_prompt

        if self.mcp_manager and self.mcp_manager.servers:
            tool_summary = {
                "read_text_file": "Read file contents from disk",
                "write_file": "Create or overwrite a file with content",
                "edit_file": "Edit an existing file (partial changes)",
                "list_directory": "List files/folders in a directory",
                "search_files": "Search for files matching a pattern",
                "fetch": "Fetch main content from a URL. Useful for summarizing or analyzing web pages or calling APIs. Use web_search first to find URLs, then fetch to read their content",
                "execute_command": "Run shell commands (bash, python, curl, etc.)",
                "web_search": "Search the web using DuckDuckGo (no API key needed). Returns results with title, URL, snippet. Use this to find current information, look up facts, or research topics",
            }
            tool_lines = []
            for handle in self.mcp_manager.servers.values():
                for tool in handle["tools"]:
                    t_name = tool.get("name", "")
                    if t_name in (self.mcp_manager.tool_map or {}):
                        desc = tool_summary.get(t_name, "")
                        tool_lines.append(f"- **{t_name}**: {desc}" if desc else f"- {t_name}")
            if tool_lines:
                effective_system += "\n\n## YOUR TOOLS\n" + "\n".join(tool_lines)

        # Build input (Responses API format)
        input_items = self._build_responses_input(effective_system)

        body = {
            "model": self.selected_model,
            "input": input_items,
            "stream": True,
            "top_p": 1,
            "max_output_tokens": 128000 if self._is_gpt_codex_model() else 64000,
            "store": False,
            "truncation": "disabled",
            "reasoning": {"summary": "detailed"},
            "include": ["reasoning.encrypted_content"],
        }

        # Thêm tools nếu có MCP — convert sang Responses API flat format
        if self.mcp_manager and self.mcp_manager.servers:
            chat_tools = self.mcp_manager.get_openai_tools()
            if chat_tools:
                # Convert: {"type":"function","function":{"name":"x","description":"y","parameters":{...}}}
                #      →   {"type":"function","name":"x","description":"y","parameters":{...},"strict":false}
                responses_tools = []
                for ct in chat_tools:
                    func = ct.get("function", {})
                    responses_tools.append({
                        "type": "function",
                        "name": func.get("name", ""),
                        "description": func.get("description", ""),
                        "parameters": func.get("parameters", {"type": "object", "properties": {}}),
                        "strict": False,
                    })
                body["tools"] = responses_tools
                body["tool_choice"] = "auto"

        url = f"{self.api_base}/responses"
        headers = {
            "Authorization": f"Bearer {self.copilot_token}",
            "X-Request-Id": request_id or str(uuid.uuid4()),
            "X-Interaction-Type": "conversation-agent",
            "OpenAI-Intent": "conversation-agent",
            "X-Interaction-Id": interaction_id or str(uuid.uuid4()),
            "X-Initiator": "agent",
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
                print(f"{C.RED}[!] Responses API thất bại (HTTP {resp.status_code}): {err_text}{C.RESET}")
                self.messages.pop()
                return None

            resp.encoding = "utf-8"

            # Parse SSE events từ Responses API
            full_content = ""
            showed_reasoning_header = False
            buffer = ""
            tool_calls = []  # list of {id, type, function: {name, arguments}}
            md = StreamingMarkdown()
            md.start()

            # Accumulator cho function_call streaming
            # Responses API gửi function_call dưới dạng output_item
            current_function_calls = {}  # output_index -> {call_id, name, arguments}

            for chunk_bytes in resp.iter_content(chunk_size=None):
                if not chunk_bytes:
                    continue

                buffer += chunk_bytes.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()

                    if not line:
                        continue

                    # Responses API dùng "event:" và "data:" lines
                    if line.startswith("event:"):
                        continue  # Event type — xử lý qua data

                    if not line.startswith("data:"):
                        continue

                    data_str = line[5:].strip()  # Bỏ "data:" (có thể "data: " hoặc "data:")

                    if not data_str:
                        continue

                    try:
                        event_data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event_type = event_data.get("type", "")

                    # ─── Text content delta ───
                    if event_type == "response.output_text.delta":
                        delta_text = event_data.get("delta", "")
                        if delta_text:
                            full_content += delta_text
                            md.feed(delta_text)

                    # ─── Reasoning (thinking) ───
                    elif event_type == "response.reasoning.delta":
                        if not showed_reasoning_header:
                            md.show_thinking()
                            showed_reasoning_header = True

                    # ─── Output item added (new message, function_call, reasoning) ───
                    elif event_type == "response.output_item.added":
                        item = event_data.get("item", {})
                        item_type = item.get("type", "")
                        output_idx = event_data.get("output_index", 0)

                        if item_type == "function_call":
                            # Bắt đầu function call mới
                            current_function_calls[output_idx] = {
                                "call_id": item.get("call_id", ""),
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", ""),
                            }

                        elif item_type == "reasoning":
                            if not showed_reasoning_header:
                                md.show_thinking()
                                showed_reasoning_header = True

                    # ─── Function call argument delta ───
                    elif event_type == "response.function_call_arguments.delta":
                        output_idx = event_data.get("output_index", 0)
                        delta_args = event_data.get("delta", "")
                        if output_idx in current_function_calls and delta_args:
                            current_function_calls[output_idx]["arguments"] += delta_args

                    # ─── Function call done ───
                    elif event_type == "response.function_call_arguments.done":
                        output_idx = event_data.get("output_index", 0)
                        if output_idx in current_function_calls:
                            fc = current_function_calls[output_idx]
                            # Finalize arguments if provided in done event
                            final_args = event_data.get("arguments")
                            if final_args is not None:
                                fc["arguments"] = final_args

                    # ─── Output item done (finalize) ───
                    elif event_type == "response.output_item.done":
                        item = event_data.get("item", {})
                        item_type = item.get("type", "")
                        output_idx = event_data.get("output_index", 0)

                        if item_type == "function_call":
                            # Có thể item chứa thông tin đầy đủ
                            call_id = item.get("call_id", "")
                            name = item.get("name", "")
                            arguments = item.get("arguments", "")

                            # Merge với accumulated data
                            if output_idx in current_function_calls:
                                fc = current_function_calls[output_idx]
                                if not call_id:
                                    call_id = fc.get("call_id", "")
                                if not name:
                                    name = fc.get("name", "")
                                if not arguments:
                                    arguments = fc.get("arguments", "")

                            tool_calls.append({
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": arguments,
                                },
                            })

                        elif item_type == "message":
                            # Final message text — có thể extract full content
                            msg_content = item.get("content", [])
                            if msg_content and not full_content:
                                for part in msg_content:
                                    if part.get("type") == "output_text":
                                        full_content = part.get("text", "")

                    # ─── Response completed ───
                    elif event_type == "response.completed":
                        resp_data = event_data.get("response", {})
                        # Extract full output nếu chưa có
                        if not full_content and not tool_calls:
                            for output_item in resp_data.get("output", []):
                                if output_item.get("type") == "message":
                                    for part in output_item.get("content", []):
                                        if part.get("type") == "output_text":
                                            text = part.get("text", "")
                                            if text:
                                                full_content = text
                                                md.feed(text)
                        break

            md.stop()  # Dừng streaming markdown

            # Validate tool calls
            validated_tool_calls = []
            for tc in tool_calls:
                args_str = tc["function"]["arguments"]
                try:
                    json.loads(args_str)
                    validated_tool_calls.append(tc)
                except (json.JSONDecodeError, TypeError):
                    # Arguments rỗng → fallback "{}"
                    if not args_str or not args_str.strip() or len(args_str.strip()) <= 2:
                        tc["function"]["arguments"] = "{}"
                        validated_tool_calls.append(tc)
                    else:
                        # Try split concatenated JSON
                        split_objects = self._split_concat_json(args_str)
                        if len(split_objects) > 1:
                            print(f"     {C.YELLOW}[!] Tách {len(split_objects)} tool calls bị merge{C.RESET}")
                            for i, obj_str in enumerate(split_objects):
                                try:
                                    json.loads(obj_str)
                                    validated_tool_calls.append({
                                        "id": tc["id"] + f"_split{i}" if i > 0 else tc["id"],
                                        "type": "function",
                                        "function": {
                                            "name": tc["function"]["name"],
                                            "arguments": obj_str,
                                        },
                                    })
                                except json.JSONDecodeError:
                                    pass
                        else:
                            print(f"     {C.RED}[!] Tool '{tc['function']['name']}' invalid JSON args: {repr(args_str[:300])}{C.RESET}")
                            tc["function"]["arguments"] = "{}"
                            validated_tool_calls.append(tc)

            return (full_content, validated_tool_calls)

        except requests.exceptions.RequestException as e:
            try:
                md.stop()
            except Exception:
                pass
            print(f"{C.RED}[!] Lỗi kết nối: {e}{C.RESET}")
            if self.messages and self.messages[-1].get("role") == "user" and not any(
                m.get("role") == "tool" for m in self.messages[-3:]
            ):
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
        if HAS_RICH:
            console.print()
            console.print(Panel(
                Markdown(self.system_prompt),
                title="[bold cyan] SYSTEM PROMPT [/]",
                border_style="cyan",
                padding=(1, 2),
            ))
            console.print("  [dim]Dung /system set <noi dung> de thay doi[/]")
            console.print("  [dim]Dung /system reset de reset ve mac dinh[/]")
            console.print()
        else:
            print()
            print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
            print(f"  {C.BOLD}SYSTEM PROMPT HIEN TAI{C.RESET}")
            print(f"{C.CYAN}{'─' * 60}{C.RESET}")
            for line in self.system_prompt.split("\n"):
                print(f"  {C.DIM}{line}{C.RESET}")
            print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
            print()

    def display_history(self):
        """Hiển thị lịch sử hội thoại."""
        if not self.messages:
            print(f"{C.YELLOW}[!] Chưa có lịch sử hội thoại.{C.RESET}")
            return

        if HAS_RICH:
            console.print()
            console.print(Rule(f"[bold cyan]LICH SU HOI THOAI ({len(self.messages)} messages)[/]"))
            for i, msg in enumerate(self.messages):
                role = msg["role"]
                content = msg.get("content") or ""

                if role == "user":
                    if len(content) > 300:
                        content = content[:300] + "..."
                    console.print(Panel(content, title="[green] You [/]", border_style="green", padding=(0, 1)))
                elif role == "tool":
                    if len(content) > 300:
                        content = content[:300] + "..."
                    console.print(Panel(
                        Text(content, style="dim"),
                        title=f"[yellow] Tool [/]",
                        border_style="yellow",
                        padding=(0, 1),
                    ))
                else:
                    tool_calls_list = msg.get("tool_calls")
                    if tool_calls_list and not content:
                        names = [tc.get("function", {}).get("name", "?") for tc in tool_calls_list]
                        console.print(f"  [blue]Copilot -> goi tool: {', '.join(names)}[/]")
                        continue
                    if len(content) > 300:
                        content = content[:300] + "..."
                    console.print(Panel(
                        Markdown(content),
                        title="[blue] Copilot [/]",
                        border_style="blue",
                        padding=(0, 1),
                    ))
            console.print(Rule(style="cyan"))
            console.print()
        else:
            print()
            print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
            print(f"  {C.BOLD}LICH SU HOI THOAI ({len(self.messages)} messages){C.RESET}")
            print(f"{C.CYAN}{'═' * 60}{C.RESET}")

            for i, msg in enumerate(self.messages):
                role = msg["role"]
                content = msg.get("content") or ""

                if role == "user":
                    print(f"\n  {C.GREEN}You:{C.RESET}")
                elif role == "tool":
                    print(f"\n  {C.MAGENTA}Tool:{C.RESET}")
                else:
                    tool_calls_list = msg.get("tool_calls")
                    if tool_calls_list and not content:
                        names = [tc.get("function", {}).get("name", "?") for tc in tool_calls_list]
                        print(f"\n  {C.BLUE}Copilot -> goi tool: {', '.join(names)}{C.RESET}")
                        continue
                    print(f"\n  {C.BLUE}Copilot:{C.RESET}")

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
    if not HAS_RICH:
        print(f"""
{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}
  {C.BOLD}HUONG DAN SU DUNG{C.RESET}
{C.CYAN}{'═' * 60}{C.RESET}
  /models, /select, /info, /system, /clear, /history
  /save, /load, /sessions, /mcp, /token, /refresh, /help, /exit
{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}
""")
        return

    table = Table(show_header=True, header_style="bold cyan", border_style="cyan", padding=(0, 2))
    table.add_column("Command", style="yellow bold", min_width=28)
    table.add_column("Description", style="white")

    # Chat & Model
    table.add_row("[bold white]Chat & Model[/]", "", end_section=True)
    table.add_row("/models", "Xem danh sach models co san")
    table.add_row("/select <so|id>", "Chon model (VD: /select 1)")
    table.add_row("/info", "Xem thong tin model dang dung")
    table.add_row("/system", "Xem system prompt hien tai")
    table.add_row("/system set", "Thay doi system prompt")
    table.add_row("/system reset", "Reset system prompt ve mac dinh")
    table.add_row("/clear", "Xoa lich su hoi thoai")
    table.add_row("/history", "Xem lich su hoi thoai", end_section=True)

    # Session
    table.add_row("[bold white]Session Management[/]", "", end_section=True)
    table.add_row("/save [ten]", "Luu phien chat")
    table.add_row("/load [so|ten]", "Load phien chat da luu")
    table.add_row("/sessions", "Xem danh sach phien da luu")
    table.add_row("/sessions rename <so> <ten>", "Doi ten phien")
    table.add_row("/sessions delete <so>", "Xoa phien", end_section=True)

    # MCP
    table.add_row("[bold white]MCP Servers[/]", "", end_section=True)
    table.add_row("/mcp", "Xem danh sach MCP tools")
    table.add_row("/mcp add <dir>", "Them thu muc Filesystem")
    table.add_row("/mcp fetch", "Them Fetch Server")
    table.add_row("/mcp shell", "Them Shell Server")
    table.add_row("/mcp search", "Them Web Search (DuckDuckGo)")
    table.add_row("/mcp playwright", "Them Playwright (headless)")
    table.add_row("/mcp web", "Tat ca Web servers")
    table.add_row("/mcp auto", "Tat ca MCP servers")
    table.add_row("/mcp stop", "Dung tat ca MCP servers", end_section=True)

    # Other
    table.add_row("[bold white]Other[/]", "", end_section=True)
    table.add_row("/yolo", "Toggle YOLO mode (auto-approve tool calls)")
    table.add_row("/token", "Doi GitHub token")
    table.add_row("/refresh", "Refresh Copilot token")
    table.add_row("/help", "Xem huong dan nay")
    table.add_row("/exit", "Thoat chuong trinh")

    console.print()
    console.print(Panel(table, title="[bold cyan] HUONG DAN SU DUNG [/]", border_style="cyan", padding=(1, 1)))
    console.print("  [dim]Nhap bat ky noi dung nao khac de chat voi AI.[/]")
    console.print()


# ═══════════════════════════════════════════════════════════════
# BANNER
# ═══════════════════════════════════════════════════════════════
def display_banner():
    ascii_art = """  ██████╗  ██████╗ ██████╗ ██╗██╗      ██████╗ ████████╗
 ██╔════╝ ██╔═══██╗██╔══██╗██║██║     ██╔═══██╗╚══██╔══╝
 ██║      ██║   ██║██████╔╝██║██║     ██║   ██║   ██║
 ██║      ██║   ██║██╔═══╝ ██║██║     ██║   ██║   ██║
 ╚██████╗ ╚██████╔╝██║     ██║███████╗╚██████╔╝   ██║
  ╚═════╝  ╚═════╝ ╚═╝     ╚═╝╚══════╝ ╚═════╝    ╚═╝

     GitHub Copilot Chat CLI Tool v1.0"""
    if HAS_RICH:
        console.print(Panel(
            Text(ascii_art, style="bold cyan", justify="center"),
            border_style="cyan",
            padding=(1, 2),
        ))
    else:
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
    global yolo_mode
    display_banner()

    client = CopilotClient()
    session_mgr = SessionManager()

    # ─── Bước 1: Nhập GitHub Token ───
    token_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "token.txt")
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

    # ─── Bước 5: Khởi tạo MCP Filesystem Server ───
    if client.mcp_manager:
        cwd = os.getcwd()
        print()
        print(f"{C.BOLD}[Bước 5] Đang khởi tạo MCP Filesystem Server tại {cwd}...{C.RESET}")
        if client.mcp_manager.add_filesystem_server([cwd]):
            print(f"{C.GREEN}[+] Đã kết nối MCP Filesystem Server!{C.RESET}")
        else:
            print(f"{C.YELLOW}[!] Không thể khởi tạo MCP Filesystem Server.{C.RESET}")

    print()
    print(f"{C.DIM}  Gõ /help để xem hướng dẫn. Gõ /models để xem danh sách models.{C.RESET}")
    print(f"{C.DIM}  Gõ /select <số> hoặc /select <model_id> để chọn model khác.{C.RESET}")
    print(f"{C.DIM}  Gõ /sessions để xem phiên cũ, /load <số> để tiếp tục phiên.{C.RESET}")
    print()

    # ─── Chat Loop ───
    while True:
        try:
            # Prompt — Claude Code style with separator line
            model_label = client.selected_model or "no-model"
            if HAS_RICH:
                # Print decorative separator before input
                try:
                    term_w = os.get_terminal_size(0).columns
                except (OSError, ValueError):
                    term_w = 80
                yolo_tag = f" {C.RED}•YOLO{C.RESET}" if yolo_mode else ""
                line_char = "─"
                dots = " ▪▪▪ "
                left_len = term_w - len(dots) - 1
                if left_len < 10:
                    left_len = 10
                sep_line = f"{C.DIM}{line_char * left_len}{dots}{line_char}{C.RESET}"
                print(sep_line)
                # Show model label + yolo tag on separate line
                print(f"{C.DIM}{model_label}{C.RESET}{yolo_tag}")
                prompt_str = f"{C.BOLD}❯{C.RESET} "
            else:
                prompt_str = f"{C.BOLD}{C.GREEN}[{model_label}]{C.RESET} {C.BOLD}>{C.RESET} "
            user_input = _smart_input(prompt_str).strip()
        except KeyboardInterrupt:
            print(f"\n{C.DIM}(Ctrl+C lần nữa để thoát){C.RESET}")
            try:
                _smart_input("").strip()
            except (KeyboardInterrupt, EOFError):
                print(f"\n{C.GREEN}[+] Bye! 👋{C.RESET}")
                break
            continue
        except EOFError:
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

            elif cmd == "/save":
                name = arg.strip() if arg.strip() else None
                session_mgr.save(client, name)

            elif cmd == "/load":
                identifier = arg.strip() if arg.strip() else None
                session_mgr.load(client, identifier)

            elif cmd == "/sessions":
                sub = arg.strip()
                if sub.lower().startswith("rename"):
                    # /sessions rename <số> <tên mới>
                    rename_args = sub[6:].strip()
                    rename_parts = rename_args.split(maxsplit=1)
                    if len(rename_parts) < 2:
                        print(f"{C.YELLOW}[!] Dùng: /sessions rename <số> <tên mới>{C.RESET}")
                    else:
                        session_mgr.rename(rename_parts[0], rename_parts[1])
                elif sub.lower().startswith("delete") or sub.lower().startswith("del") or sub.lower().startswith("rm"):
                    # /sessions delete <số|tên>
                    del_arg = sub.split(maxsplit=1)
                    if len(del_arg) < 2:
                        print(f"{C.YELLOW}[!] Dùng: /sessions delete <số>{C.RESET}")
                    else:
                        session_mgr.delete(del_arg[1].strip())
                else:
                    session_mgr.display_sessions()

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

            elif cmd == "/yolo":
                yolo_mode = not yolo_mode
                status = "ON" if yolo_mode else "OFF"
                color = C.RED if yolo_mode else C.GREEN
                print(f"{color}[*] YOLO mode: {status}{C.RESET}")
                if yolo_mode:
                    print(f"{C.DIM}    Auto-approve tất cả tool calls (skip permission prompts){C.RESET}")

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

                elif sub in ("playwright", "playwright headed", "playwright headless"):
                    if not client.mcp_manager:
                        print(f"{C.RED}[!] MCP module chưa được cài đặt.{C.RESET}")
                    elif "playwright" in client.mcp_manager.servers:
                        print(f"{C.YELLOW}[!] Playwright Server đã đang chạy.{C.RESET}")
                        print(f"{C.DIM}    Dùng /mcp stop rồi chạy lại nếu muốn đổi chế độ.{C.RESET}")
                    else:
                        headless = sub != "playwright headed"
                        mode_label = "headless (ẩn)" if headless else "headed (hiện trình duyệt)"
                        print(f"{C.BOLD}[MCP] Đang khởi động Playwright Server ({mode_label})...{C.RESET}")
                        if client.mcp_manager.add_playwright_server(headless=headless):
                            print(f"{C.GREEN}[+] MCP Playwright Server đã kết nối! ({mode_label}){C.RESET}")
                        else:
                            print(f"{C.RED}[!] Không thể khởi động Playwright Server.{C.RESET}")
                            print(f"{C.DIM}    Cài: npm install -g @playwright/mcp{C.RESET}")
                            print(f"{C.DIM}    Và:  npx playwright install chromium{C.RESET}")

                elif sub in ("search", "ddg", "duckduckgo"):
                    if not client.mcp_manager:
                        print(f"{C.RED}[!] MCP module chưa được cài đặt.{C.RESET}")
                    elif "web_search" in client.mcp_manager.servers:
                        print(f"{C.YELLOW}[!] Web Search đã đang hoạt động.{C.RESET}")
                    else:
                        print(f"{C.BOLD}[MCP] Đang kích hoạt Web Search (DuckDuckGo)...{C.RESET}")
                        if client.mcp_manager.add_web_search():
                            print(f"{C.GREEN}[+] Web Search đã sẵn sàng! (built-in, không cần server){C.RESET}")
                        else:
                            print(f"{C.RED}[!] Không thể kích hoạt Web Search.{C.RESET}")

                elif sub == "web":
                    # Shortcut: khởi động tất cả web tools (fetch + search + playwright)
                    if not client.mcp_manager:
                        print(f"{C.RED}[!] MCP module chưa được cài đặt.{C.RESET}")
                    else:
                        print(f"{C.BOLD}[MCP] Đang khởi động tất cả Web servers...{C.RESET}")
                        # Web Search (built-in)
                        if "web_search" not in client.mcp_manager.servers:
                            print(f"  🔍 Web Search (DuckDuckGo)...", end=" ", flush=True)
                            if client.mcp_manager.add_web_search():
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
                        # Playwright
                        if "playwright" not in client.mcp_manager.servers:
                            print(f"  🎭 Playwright...", end=" ", flush=True)
                            if client.mcp_manager.add_playwright_server():
                                print(f"{C.GREEN}OK{C.RESET}")
                            else:
                                print(f"{C.YELLOW}SKIP (chưa cài){C.RESET}")
                        print()
                        client.mcp_manager.display_tools()
                        n = len(client.mcp_manager.get_openai_tools())
                        print(f"\n  {C.GREEN}Tổng cộng {n} web tools sẵn sàng.{C.RESET}\n")

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
                        # Web Search (built-in, luôn thành công)
                        if "web_search" not in client.mcp_manager.servers:
                            print(f"  🔍 Web Search (DuckDuckGo)...", end=" ", flush=True)
                            client.mcp_manager.add_web_search()
                            print(f"{C.GREEN}OK{C.RESET}")
                        # Playwright
                        if "playwright" not in client.mcp_manager.servers:
                            print(f"  🎭 Playwright...", end=" ", flush=True)
                            if client.mcp_manager.add_playwright_server():
                                print(f"{C.GREEN}OK{C.RESET}")
                            else:
                                print(f"{C.YELLOW}SKIP{C.RESET}")
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
                    if HAS_RICH:
                        console.print()
                        if client.mcp_manager:
                            # Build a table of MCP servers and their tools
                            mcp_table = Table(show_header=True, header_style="bold cyan", border_style="cyan", padding=(0, 1))
                            mcp_table.add_column("Server", style="bold", width=15)
                            mcp_table.add_column("Tools", style="white")
                            mcp_table.add_column("Status", width=8)
                            for name, handle in client.mcp_manager.servers.items():
                                tools = [t.get("name", "?") for t in handle.get("tools", [])]
                                mcp_table.add_row(name, ", ".join(tools), "[green]OK[/]")
                            if not client.mcp_manager.servers:
                                mcp_table.add_row("[dim]none[/]", "[dim]No servers connected[/]", "[yellow]--[/]")
                            console.print(Panel(mcp_table, title="[bold cyan] MCP SERVERS & TOOLS [/]", border_style="cyan"))
                        else:
                            console.print("[yellow]MCP module chua duoc cai dat.[/]")
                        console.print("  [dim]/mcp add <dir> | /mcp fetch | /mcp shell | /mcp search | /mcp auto[/]")
                        console.print()
                    else:
                        print()
                        print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
                        print(f"  {C.BOLD}MCP SERVERS & TOOLS{C.RESET}")
                        print(f"{C.CYAN}{'─' * 60}{C.RESET}")
                        if client.mcp_manager:
                            client.mcp_manager.display_tools()
                        else:
                            print(f"  {C.YELLOW}MCP module chua duoc cai dat.{C.RESET}")
                        print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
                        print(f"  {C.DIM}/mcp add <dir> | /mcp fetch | /mcp shell | /mcp auto{C.RESET}")
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
        if HAS_RICH:
            console.print(Rule("Copilot", style="blue"))
        else:
            print(f"{C.BLUE}Copilot:{C.RESET}")
        try:
            client.chat(user_input)
        except KeyboardInterrupt:
            print(f"\n{C.YELLOW}[⏹] Đã dừng response.{C.RESET}")
        print()

    # Auto-save phiên khi thoát (nếu có lịch sử chat)
    if client.messages:
        try:
            print(f"{C.DIM}[*] Tự động lưu phiên...{C.RESET}")
            session_mgr.save(client)
        except Exception:
            pass

    # Cleanup MCP servers
    if client.mcp_manager:
        client.mcp_manager.stop_all()


if __name__ == "__main__":
    main()
