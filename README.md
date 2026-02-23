# 🛠️ Hướng dẫn cài đặt GitHub Copilot Chat CLI (`ghcpl`)

## Yêu cầu hệ thống

| Thành phần | Phiên bản tối thiểu | Ghi chú |
|------------|---------------------|---------|
| **Python** | 3.10+ | Khuyến nghị 3.11+ |
| **Node.js** | 18+ | Cần cho Filesystem Server & Playwright |
| **npm** | 9+ | Đi kèm Node.js |

---

## 1. Cài đặt Python dependencies

```bash
pip install -r requirements.txt
```

Hoặc cài thủ công:

```bash
# Core (bắt buộc)
pip install requests mcp

# MCP Servers
pip install mcp-server-fetch mcp-server-shell

# Web Search (DuckDuckGo built-in)
pip install ddgs

# Optional (cải thiện fetch)
pip install readabilipy
```

---

## 2. Cài đặt MCP Servers qua npm

### 📁 Filesystem Server (đọc/ghi file)

```bash
npm install -g @modelcontextprotocol/server-filesystem
```

> Tool dùng lệnh này khi bạn gõ `/mcp add <dir>` hoặc tự động mount thư mục hiện tại khi khởi động.
> Nếu chưa cài global, tool sẽ fallback sang `npx -y` (tải tạm mỗi lần chạy, chậm hơn).

### 🎭 Playwright Server (tương tác trình duyệt)

```bash
# Cài MCP server
npm install -g @playwright/mcp

# Cài trình duyệt Chromium cho Playwright
npx playwright install chromium
```

> Dùng qua lệnh `/mcp playwright` (headless) hoặc `/mcp playwright headed` (hiện trình duyệt).
> Đây là **optional** — nếu chưa cài, tool sẽ bỏ qua (SKIP) khi chạy `/mcp auto` hoặc `/mcp web`.

---

## 3. Thiết lập GitHub Token

Tạo file `token.txt` cùng thư mục với `copilot_chat.py`:

```bash
echo "gho_your_token_here" > token.txt
```

Token lấy từ GitHub Copilot extension (VS Code) hoặc OAuth flow.

---

## 4. Cài đặt thành CLI tool (`ghcpl`)

```bash
# Tạo thư mục chứa tool
mkdir -p ~/.local/share/ghcpl

# Copy các file cần thiết
cp copilot_chat.py mcp_client.py token.txt requirements.txt ~/.local/share/ghcpl/

# Cấp quyền thực thi
chmod +x ~/.local/share/ghcpl/copilot_chat.py

# Tạo symlink vào PATH
ln -sf ~/.local/share/ghcpl/copilot_chat.py ~/.local/bin/ghcpl
```

> Đảm bảo `~/.local/bin` có trong `$PATH`. Kiểm tra: `echo $PATH | tr ':' '\n' | grep local/bin`

### Kiểm tra

```bash
which ghcpl      # → ~/.local/bin/ghcpl
ghcpl            # Khởi động tool
```

---

## 5. Sử dụng

```bash
# Chạy tại bất kỳ thư mục nào
cd ~/my-project
ghcpl
```

Tool sẽ tự động:
1. Load token từ `token.txt`
2. Lấy Copilot token từ GitHub API
3. Fetch danh sách models
4. Chọn model mặc định
5. Mount thư mục hiện tại vào MCP Filesystem Server

### Các lệnh chính

| Lệnh | Chức năng |
|-------|-----------|
| `/models` | Xem danh sách models |
| `/select <số\|id>` | Chọn model |
| `/info` | Thông tin model đang dùng |
| `/help` | Xem tất cả lệnh |

### Quản lý phiên chat

| Lệnh | Chức năng |
|-------|-----------|
| `/save [tên]` | Lưu phiên chat (tên tự động nếu bỏ trống) |
| `/load [số\|tên]` | Load phiên chat đã lưu |
| `/sessions` | Liệt kê tất cả phiên |
| `/sessions rename <số> <tên>` | Đổi tên phiên |
| `/sessions delete <số>` | Xóa phiên |
| `/history` | Xem lịch sử chat hiện tại |
| `/clear` | Xóa lịch sử chat |

### MCP Servers

| Lệnh | Chức năng |
|-------|-----------|
| `/mcp` | Xem tools đang kết nối |
| `/mcp add <dir>` | Mount thư mục vào Filesystem Server |
| `/mcp fetch` | Bật Fetch Server (tải web) |
| `/mcp shell` | Bật Shell Server (chạy terminal) |
| `/mcp search` | Bật Web Search (DuckDuckGo) |
| `/mcp playwright` | Bật Playwright headless |
| `/mcp playwright headed` | Bật Playwright có giao diện |
| `/mcp web` | Bật tất cả web servers |
| `/mcp auto` | Bật tất cả servers |
| `/mcp stop` | Dừng tất cả servers |

---

## Tổng quan dependencies

```
ghcpl
├── Python (pip)
│   ├── requests          ← HTTP client (bắt buộc)
│   ├── mcp               ← MCP SDK (bắt buộc)
│   ├── mcp-server-fetch  ← /mcp fetch
│   ├── mcp-server-shell  ← /mcp shell
│   ├── ddgs              ← /mcp search (DuckDuckGo)
│   └── readabilipy       ← cải thiện fetch (optional)
│
└── Node.js (npm)
    ├── @modelcontextprotocol/server-filesystem  ← /mcp add, auto-mount
    └── @playwright/mcp                          ← /mcp playwright (optional)
        └── chromium (npx playwright install)
```

---

## Cài đặt nhanh (one-liner)

```bash
# Python deps
pip install requests mcp mcp-server-fetch mcp-server-shell ddgs readabilipy

# Node.js deps
npm install -g @modelcontextprotocol/server-filesystem @playwright/mcp && npx playwright install chromium

# Setup CLI
mkdir -p ~/.local/share/ghcpl && cp copilot_chat.py mcp_client.py token.txt requirements.txt ~/.local/share/ghcpl/ && chmod +x ~/.local/share/ghcpl/copilot_chat.py && ln -sf ~/.local/share/ghcpl/copilot_chat.py ~/.local/bin/ghcpl
```
