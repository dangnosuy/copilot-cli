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
  - sqlite3 CLI
  - GNOME Keyring (hoặc tương đương) đang unlocked

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
# PATHS
# ═══════════════════════════════════════════════════════════════
HOME = Path.home()
VSCODE_STATE_DB = HOME / ".config" / "Code" / "User" / "globalStorage" / "state.vscdb"
ELECTRON_APP_JS = '''
const { app, safeStorage } = require('electron');
const { execSync } = require('child_process');
const path = require('path');
const os = require('os');
const fs = require('fs');

// Match VS Code's app name → uses same keyring encryption key
app.setName('Code');
app.disableHardwareAcceleration();
app.on('window-all-closed', () => app.quit());

app.whenReady().then(() => {
  try {
    const dbPath = process.env.VSCODE_DB_PATH || path.join(os.homedir(), '.config', 'Code', 'User', 'globalStorage', 'state.vscdb');

    if (!fs.existsSync(dbPath)) {
      process.stderr.write(JSON.stringify({error: 'state.vscdb not found', path: dbPath}) + '\\n');
      app.exit(1);
      return;
    }

    if (!safeStorage.isEncryptionAvailable()) {
      process.stderr.write(JSON.stringify({error: 'Encryption not available (keyring locked?)'}) + '\\n');
      app.exit(1);
      return;
    }

    // Read encrypted blob from SQLite
    const result = execSync(
      `sqlite3 "${dbPath}" "SELECT value FROM ItemTable WHERE key LIKE 'secret://%github.auth%'"`,
      { encoding: 'utf-8', timeout: 5000 }
    ).trim();

    if (!result) {
      process.stderr.write(JSON.stringify({error: 'No github auth entry in database'}) + '\\n');
      app.exit(1);
      return;
    }

    const data = JSON.parse(result);
    const encrypted = Buffer.from(data.data);
    const decrypted = safeStorage.decryptString(encrypted);

    // Output as JSON
    process.stdout.write(decrypted + '\\n');
    app.exit(0);
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
    """Tìm Electron binary."""
    # 1. Local node_modules (cùng thư mục script)
    script_dir = Path(__file__).parent
    local = script_dir / "node_modules" / ".bin" / "electron"
    if local.exists():
        return str(local)

    # 2. Các đường dẫn khác
    candidates = [
        Path("/tmp/node_modules/.bin/electron"),
        Path.home() / "node_modules" / ".bin" / "electron",
    ]
    for c in candidates:
        if c.exists():
            return str(c)

    # 3. Global electron
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


def extract_token_via_electron(electron_path: str) -> dict | None:
    """Chạy Electron app để decrypt token.

    Returns:
        dict với keys: tokens (list of str), sessions (raw parsed JSON)
        hoặc None nếu lỗi
    """
    # Tạo temp dir cho Electron app
    with tempfile.TemporaryDirectory(prefix="vscode_token_") as tmpdir:
        # Viết main.js
        main_js = Path(tmpdir) / "main.js"
        main_js.write_text(ELECTRON_APP_JS)

        # Viết package.json
        pkg = Path(tmpdir) / "package.json"
        pkg.write_text('{"name":"token-extractor","main":"main.js"}')

        # Chạy Electron
        try:
            env = os.environ.copy()
            env["VSCODE_DB_PATH"] = str(VSCODE_STATE_DB)
            result = subprocess.run(
                [
                    electron_path,
                    "--no-sandbox",
                    "--disable-gpu",
                    tmpdir,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
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
