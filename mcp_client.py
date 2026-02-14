#!/usr/bin/env python3
"""
MCP Client Module (dùng official MCP Python SDK)
==================================================
Module quản lý kết nối tới các MCP Servers.
Hỗ trợ:
  - filesystem: đọc/ghi file, quản lý thư mục
  - fetch: tải nội dung web
  - shell: thực thi lệnh terminal
"""

import asyncio
import json
import os
import sys
import threading
import shutil


class MCPManager:
    """Quản lý nhiều MCP Servers và chuyển đổi tools thành OpenAI function format."""

    def __init__(self):
        self.servers = {}        # name -> MCPServerHandle
        self.tool_map = {}       # tool_name -> server_name
        self._loop = None
        self._thread = None
        self._started = False

    def _ensure_event_loop(self):
        """Đảm bảo có event loop chạy trong background thread."""
        if self._started:
            return

        self._loop = asyncio.new_event_loop()

        def run_loop():
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._thread = threading.Thread(target=run_loop, daemon=True)
        self._thread.start()
        self._started = True

    def _run_async(self, coro):
        """Chạy coroutine trong background event loop và chờ kết quả."""
        self._ensure_event_loop()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=60)

    def add_filesystem_server(self, allowed_dirs: list) -> bool:
        """Thêm MCP Filesystem Server."""
        # Validate dirs
        valid_dirs = []
        for d in allowed_dirs:
            abs_d = os.path.abspath(d)
            if os.path.isdir(abs_d):
                valid_dirs.append(abs_d)
            else:
                print(f"[MCP] Cảnh báo: Thư mục không tồn tại: {abs_d}")

        if not valid_dirs:
            print("[MCP] Không có thư mục hợp lệ nào!")
            return False

        # Tìm command
        mcp_bin = shutil.which("mcp-server-filesystem")
        if mcp_bin:
            command = mcp_bin
            args = valid_dirs
        else:
            command = "npx"
            args = ["-y", "@modelcontextprotocol/server-filesystem"] + valid_dirs

        try:
            handle = self._run_async(
                self._connect_server("filesystem", command, args)
            )
            if handle:
                self.servers["filesystem"] = handle
                for tool in handle["tools"]:
                    self.tool_map[tool["name"]] = "filesystem"
                return True
            return False
        except Exception as e:
            print(f"[MCP] Lỗi kết nối: {e}", file=sys.stderr)
            return False

    def add_fetch_server(self, ignore_robots=True) -> bool:
        """Thêm MCP Fetch Server (tải nội dung web)."""
        # Pre-install readabilipy node_modules để tránh npm output lẫn vào stdout
        self._ensure_readabilipy_deps()

        mcp_bin = shutil.which("mcp-server-fetch")
        if not mcp_bin:
            # Fallback sang python -m
            command = sys.executable
            args = ["-m", "mcp_server_fetch"]
        else:
            command = mcp_bin
            args = []

        if ignore_robots:
            args.append("--ignore-robots-txt")

        try:
            handle = self._run_async(
                self._connect_server("fetch", command, args)
            )
            if handle:
                self.servers["fetch"] = handle
                for tool in handle["tools"]:
                    self.tool_map[tool["name"]] = "fetch"
                return True
            return False
        except Exception as e:
            print(f"[MCP] Lỗi kết nối fetch server: {e}", file=sys.stderr)
            return False

    def _ensure_readabilipy_deps(self):
        """Pre-install readabilipy node dependencies để tránh npm output lẫn stdout."""
        try:
            import readabilipy
            import subprocess
            js_dir = os.path.join(os.path.dirname(readabilipy.__file__), "javascript")
            node_modules = os.path.join(js_dir, "node_modules")
            if not os.path.isdir(node_modules):
                pkg_json = os.path.join(js_dir, "package.json")
                if os.path.isfile(pkg_json) and shutil.which("npm"):
                    print("[MCP] Đang cài readabilipy dependencies...", end=" ", flush=True)
                    subprocess.run(
                        ["npm", "install"],
                        cwd=js_dir,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    print("OK")
        except Exception:
            pass

    def add_shell_server(self) -> bool:
        """Thêm MCP Shell Server (thực thi lệnh terminal)."""
        # mcp-server-shell binary thường bị lỗi, dùng python -m
        command = sys.executable
        args = ["-m", "mcp_server_shell"]

        try:
            handle = self._run_async(
                self._connect_server("shell", command, args)
            )
            if handle:
                self.servers["shell"] = handle
                for tool in handle["tools"]:
                    self.tool_map[tool["name"]] = "shell"
                return True
            return False
        except Exception as e:
            print(f"[MCP] Lỗi kết nối shell server: {e}", file=sys.stderr)
            return False

    async def _connect_server(self, name: str, command: str, args: list) -> dict:
        """Kết nối tới MCP server (async)."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server_params = StdioServerParameters(
            command=command,
            args=args,
        )

        # Tạo context managers và giữ chúng mở
        stdio_ctx = stdio_client(server_params)
        read_write = await stdio_ctx.__aenter__()
        read, write = read_write

        session_ctx = ClientSession(read, write)
        session = await session_ctx.__aenter__()

        # Initialize
        init_result = await session.initialize()

        # List tools
        tools_result = await session.list_tools()

        # Convert tools sang dict format
        tools = []
        for tool in tools_result.tools:
            tool_dict = {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema if hasattr(tool, 'inputSchema') else {},
            }
            tools.append(tool_dict)

        server_info = {}
        if hasattr(init_result, 'serverInfo') and init_result.serverInfo:
            server_info = {
                "name": getattr(init_result.serverInfo, 'name', name),
                "version": getattr(init_result.serverInfo, 'version', '?'),
            }

        return {
            "name": name,
            "session": session,
            "session_ctx": session_ctx,
            "stdio_ctx": stdio_ctx,
            "tools": tools,
            "serverInfo": server_info,
        }

    def get_openai_tools(self) -> list:
        """Chuyển đổi MCP tools sang OpenAI function calling format.
        
        Chỉ expose các tools thiết yếu để giảm token cost.
        Filesystem: 14 tools → 6 tools (bỏ deprecated, redundant, ít dùng)
        """
        # Tools cần giữ (tên tool -> giữ/bỏ)
        # Bỏ: read_file (deprecated, dùng read_text_file), read_media_file (ít dùng),
        #      read_multiple_files (dùng read_text_file nhiều lần), 
        #      list_directory_with_sizes (dùng list_directory),
        #      directory_tree (dùng list_directory), move_file (ít dùng),
        #      create_directory (write_file tự tạo), list_allowed_directories (internal),
        #      get_file_info (ít dùng)
        ESSENTIAL_TOOLS = {
            # Filesystem - chỉ giữ 5 tools chính
            "read_text_file", "write_file", "edit_file", 
            "list_directory", "search_files",
            # Fetch
            "fetch",
            # Shell
            "execute_command",
        }

        openai_tools = []

        for server_name, handle in self.servers.items():
            for tool in handle["tools"]:
                tool_name = tool["name"]
                
                # Chỉ gửi essential tools
                if tool_name not in ESSENTIAL_TOOLS:
                    continue

                # Rút gọn description để tiết kiệm tokens
                desc = tool.get("description", "")
                if len(desc) > 150:
                    desc = desc[:147] + "..."

                openai_tool = {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": desc,
                    },
                }

                input_schema = tool.get("inputSchema", {})
                if input_schema:
                    # Rút gọn schema — bỏ description dài trong properties
                    clean_schema = self._compact_schema(input_schema)
                    openai_tool["function"]["parameters"] = clean_schema
                else:
                    openai_tool["function"]["parameters"] = {
                        "type": "object",
                        "properties": {},
                    }

                openai_tools.append(openai_tool)

        return openai_tools

    def _compact_schema(self, schema: dict) -> dict:
        """Clean JSON schema cho OpenAI function calling format.
        
        - Xóa fields không thuộc OpenAI spec (title, description ở top-level, format hints)
        - Giữ lại: type, properties, required, items, enum, default
        - Truncate description dài trong properties
        """
        result = {}

        # Chỉ copy các fields cần thiết cho OpenAI function calling
        for key in ("type", "properties", "required", "items", "enum",
                     "anyOf", "oneOf", "allOf", "additionalProperties"):
            if key in schema:
                result[key] = schema[key]

        if "properties" in result:
            props = {}
            for k, v in result["properties"].items():
                clean_prop = {}
                # Giữ lại type, description (truncated), enum, default, items, required
                if "type" in v:
                    clean_prop["type"] = v["type"]
                if "description" in v:
                    desc = v["description"]
                    if len(desc) > 80:
                        desc = desc[:77] + "..."
                    clean_prop["description"] = desc
                if "enum" in v:
                    clean_prop["enum"] = v["enum"]
                if "default" in v:
                    clean_prop["default"] = v["default"]
                if "items" in v:
                    clean_prop["items"] = v["items"]
                props[k] = clean_prop
            result["properties"] = props

        return result

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        """Thực thi tool và trả về kết quả dạng text."""
        server_name = self.tool_map.get(tool_name)
        if not server_name:
            return f"[Lỗi] Không tìm thấy tool: {tool_name}"

        handle = self.servers.get(server_name)
        if not handle:
            return f"[Lỗi] Server '{server_name}' không hoạt động"

        try:
            result = self._run_async(
                handle["session"].call_tool(tool_name, arguments)
            )

            # Kiểm tra error flag
            is_error = getattr(result, 'isError', False)

            # Parse content
            texts = []
            for item in result.content:
                if hasattr(item, 'text'):
                    texts.append(item.text)
                elif hasattr(item, 'data'):
                    texts.append(f"[Binary data: {len(item.data)} bytes]")
                else:
                    texts.append(str(item))

            output = "\n".join(texts) if texts else "[Không có kết quả]"

            if is_error:
                return f"[Tool Error] {output}"
            return output

        except Exception as e:
            err_msg = str(e).strip()
            if not err_msg:
                err_msg = f"{type(e).__name__}"
            return f"[Lỗi tool] {err_msg}"

    def display_tools(self):
        """Hiển thị danh sách tools đã đăng ký."""
        if not self.servers:
            print("  [Chưa có MCP server nào được kết nối]")
            return

        for server_name, handle in self.servers.items():
            info = handle.get("serverInfo", {})
            s_name = info.get("name", server_name)
            s_ver = info.get("version", "?")
            print(f"\n  📦 {s_name} v{s_ver}")
            print(f"  {'─' * 56}")

            for tool in handle["tools"]:
                name = tool.get("name", "")
                desc = tool.get("description", "")
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                print(f"    🔧 {name}")
                print(f"       {desc}")

    def stop_all(self):
        """Dừng tất cả MCP servers."""
        if self._loop and self._started:
            for name, handle in list(self.servers.items()):
                try:
                    self._run_async(self._disconnect_server(handle))
                except Exception:
                    pass
        self.servers.clear()
        self.tool_map.clear()

    async def _disconnect_server(self, handle: dict):
        """Ngắt kết nối MCP server (async)."""
        try:
            session_ctx = handle.get("session_ctx")
            if session_ctx:
                await session_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        try:
            stdio_ctx = handle.get("stdio_ctx")
            if stdio_ctx:
                await stdio_ctx.__aexit__(None, None, None)
        except Exception:
            pass
