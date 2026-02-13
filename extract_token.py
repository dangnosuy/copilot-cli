#!/usr/bin/env python3
"""
VS Code Copilot GitHub Token Extractor
=======================================
Trích xuất GitHub OAuth token (gho_...) từ VS Code's encrypted storage.

Cơ chế:
  1. Đọc encrypted token từ ~/.config/Code/User/globalStorage/state.vscdb
  2. Dùng Electron's safeStorage API để giải mã (cần npm electron package)
  3. Parse JSON sessions → trích xuất accessToken

Yêu cầu:
  - VS Code đã đăng nhập GitHub (có GitHub Authentication)
  - Node.js + npm
  - Linux: GNOME Keyring / KWallet đang unlocked
  - Windows: cùng user account đã login VS Code
  - macOS: Keychain access

Hỗ trợ: Linux, Windows, macOS

Sử dụng:
  python3 extract_token.py              # Trích xuất và hiển thị token
  python3 extract_token.py --save       # Lưu vào token.txt
  python3 extract_token.py --json       # Output JSON
  python3 extract_token.py --quiet      # Chỉ in token, không gì khác
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# COLORS
# ═══════════════════════════════════════════════════════════════
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"


# ═══════════════════════════════════════════════════════════════
# PATHS (cross-platform)
# ═══════════════════════════════════════════════════════════════
HOME = Path.home()
PLATFORM = sys.platform  # 'linux', 'win32', 'darwin'

def get_vscode_state_db() -> Path:
    """Trả về path tới state.vscdb theo OS."""
    if PLATFORM == "win32":
        # Windows: %APPDATA%\Code\User\globalStorage\state.vscdb
        appdata = os.environ.get("APPDATA", str(HOME / "AppData" / "Roaming"))
        return Path(appdata) / "Code" / "User" / "globalStorage" / "state.vscdb"
    elif PLATFORM == "darwin":
        # macOS: ~/Library/Application Support/Code/User/globalStorage/state.vscdb
        return HOME / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "state.vscdb"
    else:
        # Linux: ~/.config/Code/User/globalStorage/state.vscdb
        return HOME / ".config" / "Code" / "User" / "globalStorage" / "state.vscdb"

VSCODE_STATE_DB = get_vscode_state_db()
ELECTRON_APP_JS = '''
const { app, safeStorage } = require('electron');
const os = require('os');

// Linux: must match VS Code's app name to use same keyring encryption key
// Windows: DPAPI doesn't care about app name
// macOS: Keychain uses app signature, setName helps
if (process.platform !== 'win32') {
  app.setName('Code');
}

app.disableHardwareAcceleration();
app.on('window-all-closed', () => app.quit());

app.whenReady().then(() => {
  try {
    if (!safeStorage.isEncryptionAvailable()) {
      process.stderr.write(JSON.stringify({error: 'Encryption not available. Keyring/DPAPI locked?'}) + '\\n');
      app.exit(1);
      return;
    }

    // Read encrypted buffer from stdin (sent by Python as JSON array of bytes)
    let inputData = '';
    process.stdin.setEncoding('utf-8');
    process.stdin.on('data', (chunk) => { inputData += chunk; });
    process.stdin.on('end', () => {
      try {
        const byteArray = JSON.parse(inputData);
        const encrypted = Buffer.from(byteArray);
        const decrypted = safeStorage.decryptString(encrypted);
        process.stdout.write(decrypted + '\\n');
        app.exit(0);
      } catch (e) {
        process.stderr.write(JSON.stringify({error: 'Decrypt failed: ' + e.message}) + '\\n');
        app.exit(1);
      }
    });
    process.stdin.resume();
  } catch (e) {
    process.stderr.write(JSON.stringify({error: e.message}) + '\\n');
    app.exit(1);
  }
});
'''.strip()


# ═══════════════════════════════════════════════════════════════
# ELECTRON SETUP
# ═══════════════════════════════════════════════════════════════
def find_electron() -> str | None:
    """Tìm Electron binary (cross-platform)."""
    script_dir = Path(__file__).parent

    # Binary name varies by OS
    if PLATFORM == "win32":
        bin_names = ["electron.cmd", "electron.exe", "electron"]
        bin_subdir = Path("node_modules") / ".bin"
    else:
        bin_names = ["electron"]
        bin_subdir = Path("node_modules") / ".bin"

    # 1. Local node_modules (cùng thư mục script)
    for name in bin_names:
        local = script_dir / bin_subdir / name
        if local.exists():
            return str(local)

    # 2. Temp location (from previous installs)
    if PLATFORM != "win32":
        for name in bin_names:
            tmp = Path("/tmp") / "node_modules" / ".bin" / name
            if tmp.exists():
                return str(tmp)

    # 3. Home directory
    for name in bin_names:
        home = HOME / "node_modules" / ".bin" / name
        if home.exists():
            return str(home)

    # 4. Global electron
    electron_path = shutil.which("electron")
    if electron_path:
        return electron_path

    return None


def install_electron() -> str | None:
    """Cài Electron npm package nếu chưa có."""
    script_dir = Path(__file__).parent
    target_dir = script_dir

    print(f"  {C.YELLOW}⏳ Đang cài đặt electron npm package...{C.RESET}")
    print(f"  {C.DIM}   (chỉ cần chạy 1 lần, ~30s){C.RESET}")

    try:
        result = subprocess.run(
            ["npm", "install", "electron", "--no-save", "--no-audit", "--no-fund"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            electron = target_dir / "node_modules" / ".bin" / "electron"
            if electron.exists():
                print(f"  {C.GREEN}✅ Đã cài electron thành công.{C.RESET}")
                return str(electron)

        print(f"  {C.RED}❌ Cài electron thất bại.{C.RESET}")
        if result.stderr:
            print(f"  {C.DIM}{result.stderr[:200]}{C.RESET}")
        return None

    except FileNotFoundError:
        print(f"  {C.RED}❌ npm không tìm thấy. Cần cài Node.js trước.{C.RESET}")
        return None
    except subprocess.TimeoutExpired:
        print(f"  {C.RED}❌ Timeout khi cài electron.{C.RESET}")
        return None


# ═══════════════════════════════════════════════════════════════
# TOKEN EXTRACTION
# ═══════════════════════════════════════════════════════════════
def check_vscode_db() -> bool:
    """Kiểm tra state.vscdb có tồn tại và có entry github auth."""
    if not VSCODE_STATE_DB.exists():
        return False

    try:
        conn = sqlite3.connect(str(VSCODE_STATE_DB))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM ItemTable WHERE key LIKE 'secret://%github.auth%'"
        )
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False


def read_encrypted_blob() -> list[int] | None:
    """Đọc encrypted buffer từ state.vscdb, trả về list of bytes."""
    try:
        conn = sqlite3.connect(str(VSCODE_STATE_DB))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT value FROM ItemTable WHERE key LIKE 'secret://%github.auth%'"
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        data = json.loads(row[0])
        # data = {"type": "Buffer", "data": [118, 49, 49, ...]}
        return data.get("data", [])
    except Exception:
        return None


def extract_token_via_electron(electron_path: str) -> dict | None:
    """Chạy Electron app để decrypt token.

    Flow: Python đọc SQLite → pipe encrypted bytes qua stdin → Electron decrypt → stdout

    Returns:
        dict với keys: tokens (list of str), sessions (raw parsed JSON)
        hoặc None nếu lỗi
    """
    # Đọc encrypted blob bằng Python sqlite3 (cross-platform, không cần sqlite3 CLI)
    encrypted_bytes = read_encrypted_blob()
    if not encrypted_bytes:
        raise RuntimeError("Không đọc được encrypted data từ state.vscdb")

    # Tạo temp dir cho Electron app
    with tempfile.TemporaryDirectory(prefix="vscode_token_") as tmpdir:
        # Viết main.js
        main_js = Path(tmpdir) / "main.js"
        main_js.write_text(ELECTRON_APP_JS)

        # Viết package.json
        pkg = Path(tmpdir) / "package.json"
        pkg.write_text('{"name":"token-extractor","main":"main.js"}')

        # Chạy Electron, pipe encrypted bytes qua stdin
        cmd = [electron_path, "--no-sandbox", "--disable-gpu", tmpdir]
        # Windows: không cần --no-sandbox
        if PLATFORM == "win32":
            cmd = [electron_path, "--disable-gpu", tmpdir]

        try:
            result = subprocess.run(
                cmd,
                input=json.dumps(encrypted_bytes),
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return None

        if result.returncode != 0:
            stderr = result.stderr.strip()
            if stderr:
                try:
                    err = json.loads(stderr)
                    raise RuntimeError(err.get("error", stderr))
                except json.JSONDecodeError:
                    raise RuntimeError(stderr)
            raise RuntimeError("Electron exited with non-zero code")

        raw = result.stdout.strip()
        if not raw:
            raise RuntimeError("Electron returned empty output")

        # Parse sessions JSON
        try:
            sessions = json.loads(raw)
        except json.JSONDecodeError:
            # Maybe it's just a raw token
            return {"tokens": [raw], "sessions": None}

        # Extract tokens
        tokens = []
        if isinstance(sessions, list):
            for s in sessions:
                if isinstance(s, dict) and s.get("accessToken"):
                    tokens.append(s["accessToken"])
        elif isinstance(sessions, dict) and sessions.get("accessToken"):
            tokens.append(sessions["accessToken"])

        return {"tokens": tokens, "sessions": sessions}


def extract_github_token(quiet: bool = False) -> str | None:
    """Main extraction function. Returns token string or None.

    Có thể import từ module khác:
        from extract_token import extract_github_token
        token = extract_github_token(quiet=True)
    """
    # Step 1: Check VS Code database
    if not check_vscode_db():
        if not quiet:
            print(f"{C.RED}❌ Không tìm thấy VS Code GitHub auth data.{C.RESET}")
            print(f"{C.DIM}   Kiểm tra: VS Code đã đăng nhập GitHub chưa?{C.RESET}")
            print(f"{C.DIM}   Path: {VSCODE_STATE_DB}{C.RESET}")
        return None

    # Step 2: Find or install Electron
    electron = find_electron()
    if not electron:
        if not quiet:
            print(f"{C.YELLOW}⚠️  Không tìm thấy Electron. Đang tự cài...{C.RESET}")
        electron = install_electron()
        if not electron:
            if not quiet:
                print(f"{C.RED}❌ Không thể cài Electron.{C.RESET}")
                print(f"{C.DIM}   Thử chạy: npm install electron{C.RESET}")
            return None

    # Step 3: Extract
    try:
        result = extract_token_via_electron(electron)
    except RuntimeError as e:
        if not quiet:
            print(f"{C.RED}❌ Lỗi giải mã: {e}{C.RESET}")
        return None

    if not result or not result["tokens"]:
        if not quiet:
            print(f"{C.RED}❌ Không tìm thấy token trong dữ liệu giải mã.{C.RESET}")
        return None

    # Return first token (usually the active one)
    return result["tokens"][0]


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Trích xuất GitHub OAuth token từ VS Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ví dụ:
  python3 extract_token.py              # Hiển thị token
  python3 extract_token.py --save       # Lưu vào token.txt
  python3 extract_token.py --quiet      # Chỉ in token (cho scripting)
  python3 extract_token.py --json       # Output JSON đầy đủ
        """,
    )
    parser.add_argument("--save", action="store_true", help="Lưu token vào token.txt")
    parser.add_argument("--quiet", "-q", action="store_true", help="Chỉ in token")
    parser.add_argument("--json", action="store_true", help="Output JSON đầy đủ")
    args = parser.parse_args()

    # Quiet mode: chỉ in token
    if args.quiet:
        token = extract_github_token(quiet=True)
        if token:
            print(token)
            sys.exit(0)
        else:
            sys.exit(1)

    # Normal mode
    print()
    print(f"  {C.BOLD}{C.CYAN}╔══════════════════════════════════════════╗{C.RESET}")
    print(f"  {C.BOLD}{C.CYAN}║  🔑 VS Code GitHub Token Extractor     ║{C.RESET}")
    print(f"  {C.BOLD}{C.CYAN}╚══════════════════════════════════════════╝{C.RESET}")
    print()

    # Step 1: Check DB
    print(f"  {C.BOLD}[1/3]{C.RESET} Kiểm tra VS Code database...", end=" ", flush=True)
    if not check_vscode_db():
        print(f"{C.RED}KHÔNG TÌM THẤY{C.RESET}")
        print(f"\n  {C.RED}❌ VS Code chưa đăng nhập GitHub.{C.RESET}")
        print(f"  {C.DIM}   Mở VS Code → Ctrl+Shift+P → 'GitHub: Sign In'{C.RESET}")
        sys.exit(1)
    print(f"{C.GREEN}OK{C.RESET}")
    print(f"  {C.DIM}   DB: {VSCODE_STATE_DB}{C.RESET}")

    # Step 2: Find Electron
    print(f"  {C.BOLD}[2/3]{C.RESET} Tìm Electron runtime...", end=" ", flush=True)
    electron = find_electron()
    if electron:
        print(f"{C.GREEN}OK{C.RESET}")
        print(f"  {C.DIM}   Path: {electron}{C.RESET}")
    else:
        print(f"{C.YELLOW}CHƯA CÀI{C.RESET}")
        electron = install_electron()
        if not electron:
            print(f"\n  {C.RED}❌ Không thể cài Electron. Thử: npm install electron{C.RESET}")
            sys.exit(1)

    # Step 3: Decrypt
    print(f"  {C.BOLD}[3/3]{C.RESET} Giải mã token...", end=" ", flush=True)
    try:
        result = extract_token_via_electron(electron)
    except RuntimeError as e:
        print(f"{C.RED}LỖI{C.RESET}")
        print(f"\n  {C.RED}❌ {e}{C.RESET}")
        sys.exit(1)

    if not result or not result["tokens"]:
        print(f"{C.RED}KHÔNG CÓ TOKEN{C.RESET}")
        print(f"\n  {C.RED}❌ Database có entry nhưng không chứa token.{C.RESET}")
        sys.exit(1)

    print(f"{C.GREEN}THÀNH CÔNG{C.RESET}")

    # JSON mode
    if args.json:
        output = {
            "tokens": result["tokens"],
            "count": len(result["tokens"]),
            "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if result["sessions"]:
            sessions = result["sessions"]
            if isinstance(sessions, list):
                output["sessions"] = [
                    {
                        "id": s.get("id", ""),
                        "scopes": s.get("scopes", []),
                        "account": s.get("account", {}).get("label", ""),
                    }
                    for s in sessions
                    if isinstance(s, dict)
                ]
        print(json.dumps(output, indent=2))
        sys.exit(0)

    # Display results
    print()
    print(f"  {C.BOLD}{C.GREEN}{'─' * 44}{C.RESET}")
    for i, token in enumerate(result["tokens"]):
        masked = f"{token[:10]}...{token[-4:]}"
        print(f"  {C.BOLD}Token {i + 1}:{C.RESET} {C.CYAN}{masked}{C.RESET}")
        print(f"  {C.DIM}Full:    {token}{C.RESET}")

        # Show session info if available
        if result["sessions"] and isinstance(result["sessions"], list):
            for s in result["sessions"]:
                if isinstance(s, dict) and s.get("accessToken") == token:
                    account = s.get("account", {})
                    scopes = s.get("scopes", [])
                    if account.get("label"):
                        print(f"  {C.DIM}Account: {account['label']}{C.RESET}")
                    if scopes:
                        scope_str = ", ".join(scopes) if isinstance(scopes, list) else str(scopes)
                        print(f"  {C.DIM}Scopes:  {scope_str}{C.RESET}")
        print()
    print(f"  {C.BOLD}{C.GREEN}{'─' * 44}{C.RESET}")

    # Save if requested
    if args.save:
        token_file = Path(__file__).parent / "token.txt"
        token_file.write_text(result["tokens"][0] + "\n")
        print(f"\n  {C.GREEN}💾 Đã lưu vào: {token_file}{C.RESET}")

    print()


if __name__ == "__main__":
    main()
