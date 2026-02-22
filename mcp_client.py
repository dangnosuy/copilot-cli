#!/usr/bin/env python3
"""
MCP Client Module (dùng official MCP Python SDK)
==================================================
Module quản lý kết nối tới các MCP Servers.
Hỗ trợ:
  - filesystem: đọc/ghi file, quản lý thư mục
  - fetch: tải nội dung web
  - shell: thực thi lệnh terminal
  - playwright: tương tác trình duyệt (click, navigate, screenshot, ...)
  - web_search: tìm kiếm web qua DuckDuckGo HTML (built-in, không cần API key)
"""

import asyncio
import json
import os
import re
import sys
import threading
import shutil
import urllib.parse


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

    def _run_async(self, coro, timeout=120):
        """Chạy coroutine trong background event loop và chờ kết quả."""
        self._ensure_event_loop()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

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

    def add_web_search(self) -> bool:
        """Thêm built-in web_search tool (DuckDuckGo HTML, không cần API key).
        
        Tool này KHÔNG dùng MCP server riêng, mà đăng ký trực tiếp 
        như một virtual tool trong MCPManager.
        """
        if "web_search" in self.servers:
            return True

        tool_def = {
            "name": "web_search",
            "description": (
                "Search the web using DuckDuckGo. Returns a list of results with "
                "title, URL, and snippet. Use this when you need to find current "
                "information, look up facts, or research topics on the internet."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query string",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 5, max 20)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        }

        # Đăng ký virtual server (không có MCP session)
        self.servers["web_search"] = {
            "name": "web_search",
            "session": None,  # built-in, không cần session
            "session_ctx": None,
            "stdio_ctx": None,
            "tools": [tool_def],
            "serverInfo": {"name": "Built-in Web Search", "version": "1.0"},
        }
        self.tool_map["web_search"] = "web_search"
        return True

    @staticmethod
    def _ddg_search(query: str, max_results: int = 5) -> list:
        """Tìm kiếm DuckDuckGo (không cần API key).
        
        Dùng thư viện duckduckgo_search (pure Python).
        Trả về list[dict] với keys: title, url, snippet.
        """
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
            except ImportError:
                return [{"error": "Cần cài: pip install ddgs"}]

        try:
            with DDGS() as ddgs:
                raw_results = list(ddgs.text(query, max_results=max_results))
        except Exception as e:
            # Fallback: thử qua lite HTML endpoint
            return MCPManager._ddg_search_html_fallback(query, max_results, str(e))

        if not raw_results:
            return [{"error": "No results found", "query": query}]

        results = []
        for r in raw_results:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            })
        return results

    @staticmethod
    def _ddg_search_html_fallback(query: str, max_results: int = 5, primary_error: str = "") -> list:
        """Fallback: search DuckDuckGo qua HTML lite endpoint."""
        import requests as _requests

        url = "https://lite.duckduckgo.com/lite/"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        data = {"q": query}

        try:
            resp = _requests.post(url, headers=headers, data=data, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            return [{"error": f"Both search methods failed. Primary: {primary_error}. Fallback: {e}"}]

        html = resp.text
        results = []

        # DuckDuckGo Lite format:
        # Links: <a rel="nofollow" href="URL" class='result-link'>Title</a>
        # Snippet: <td class="result-snippet">text</td>
        links = re.findall(
            r"class='result-link'[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",
            html,
            re.DOTALL,
        )
        snippets = re.findall(
            r'class="result-snippet"[^>]*>(.*?)</td>',
            html,
            re.DOTALL,
        )

        for i, (href, title_raw) in enumerate(links[:max_results]):
            title = re.sub(r'<[^>]+>', '', title_raw).strip()
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()

            # Decode DDG redirect URL
            link = href.strip()
            if "uddg=" in link:
                uddg_match = re.search(r'uddg=([^&]+)', link)
                if uddg_match:
                    link = urllib.parse.unquote(uddg_match.group(1))

            # Skip ads
            if "duckduckgo.com/y.js" in link or "ad_provider" in link:
                continue

            if title or link:
                results.append({
                    "title": title,
                    "url": link,
                    "snippet": snippet,
                })

        if not results:
            return [{"error": f"No results found. Primary error: {primary_error}", "query": query}]

        return results

        if not results:
            return [{"error": "No results found", "query": query}]

        return results

    def add_playwright_server(self, headless: bool = True) -> bool:
        """Thêm Playwright MCP Server (tương tác trình duyệt).
        
        Hỗ trợ: navigate, click, screenshot, fill, evaluate, v.v.
        Cài: npx @playwright/mcp
        
        Args:
            headless: True = chạy ẩn (mặc định), False = hiện trình duyệt lên màn hình.
        """
        mcp_bin = shutil.which("mcp-server-playwright")
        if mcp_bin:
            command = mcp_bin
            args = [] if not headless else ["--headless"]
        else:
            command = "npx"
            args = ["-y", "@playwright/mcp"]
            if headless:
                args.append("--headless")

        try:
            handle = self._run_async(
                self._connect_server("playwright", command, args)
            )
            if handle:
                self.servers["playwright"] = handle
                for tool in handle["tools"]:
                    self.tool_map[tool["name"]] = "playwright"
                return True
            return False
        except Exception as e:
            print(f"[MCP] Lỗi kết nối Playwright server: {e}", file=sys.stderr)
            return False

    async def _connect_server(self, name: str, command: str, args: list, env: dict = None) -> dict:
        """Kết nối tới MCP server (async)."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        # Merge env variables
        server_env = None
        if env:
            server_env = dict(os.environ)
            server_env.update(env)

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=server_env,
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
        Filesystem: 14 tools → 5 tools (bỏ deprecated, redundant, ít dùng)
        Các server web (playwright, web_search): cho phép tất cả tools.
        """
        # Whitelist tools cho filesystem (quá nhiều tools thừa)
        FILESYSTEM_ESSENTIAL = {
            "read_text_file", "write_file", "edit_file", 
            "list_directory", "search_files",
        }

        # Server-level whitelist: cho phép tất cả tools từ các server này
        ALLOW_ALL_SERVERS = {"playwright", "web_search", "fetch", "shell"}

        openai_tools = []

        for server_name, handle in self.servers.items():
            for tool in handle["tools"]:
                tool_name = tool["name"]
                
                # Filter logic
                if server_name == "filesystem":
                    if tool_name not in FILESYSTEM_ESSENTIAL:
                        continue
                elif server_name not in ALLOW_ALL_SERVERS:
                    # Server không được biết -> skip
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

        # Built-in web_search — xử lý trực tiếp, không qua MCP session
        if tool_name == "web_search":
            try:
                query = arguments.get("query", "")
                max_results = int(arguments.get("max_results", 5))
                max_results = min(max(max_results, 1), 20)
                results = self._ddg_search(query, max_results)
                return json.dumps(results, ensure_ascii=False, indent=2)
            except Exception as e:
                return f"[Lỗi web_search] {e}"

        try:
            # Shell commands cần timeout dài hơn vì tool có thể chạy nmap, nuclei, etc.
            tool_timeout = 300 if server_name == "shell" else 120
            result = self._run_async(
                handle["session"].call_tool(tool_name, arguments),
                timeout=tool_timeout,
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
            # Thêm hướng dẫn cụ thể cho timeout errors
            if isinstance(e, TimeoutError) or "timeout" in err_msg.lower():
                tool_timeout_used = 300 if server_name == "shell" else 120
                args_hint = json.dumps(arguments, ensure_ascii=False)[:200]
                return (
                    f"[Tool Error] Command timed out after {tool_timeout_used}s. "
                    f"The command took too long to complete. "
                    f"Tips: 1) Add timeout flags to your command (e.g. --connect-timeout 5 --max-time 15 for curl, "
                    f"-timeout 10 for ffuf, --timeout=15 for sqlmap). "
                    f"2) For long-running tools (nmap, nuclei, subfinder), limit scope: "
                    f"fewer targets, specific ports (-p 80,443), rate limits (-rl 50). "
                    f"3) Skip unresponsive hosts and move to the next target. "
                    f"Args were: {args_hint}"
                )
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
                # Skip virtual servers (built-in, không có session)
                if handle.get("session") is None:
                    continue
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
