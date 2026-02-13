#!/usr/bin/env python3
"""
VS Code Copilot Token Interceptor
====================================
Bắt GitHub OAuth token (gho_...) bằng cách MITM proxy VS Code.

Cơ chế:
  1. Tự tạo CA certificate (self-signed)
  2. Khởi động HTTPS MITM proxy trên 127.0.0.1:8080
  3. Mở VS Code qua proxy đó (NODE_EXTRA_CA_CERTS + HTTP_PROXY)
  4. Bắt request tới api.github.com chứa header "Authorization: token gho_..."
  5. In token ra và thoát

Ưu điểm:
  - Không cần Electron, không cần decrypt keyring/DPAPI
  - Cross-platform: Linux, Windows, macOS
  - Chỉ cần Python 3.10+ (stdlib + cryptography)
  - Zero dependency ngoài cryptography (thường có sẵn)

Sử dụng:
  python3 intercept_token.py                # Chạy và đợi bắt token
  python3 intercept_token.py --port 9090    # Dùng port khác
  python3 intercept_token.py --save         # Lưu token vào token.txt
  python3 intercept_token.py --timeout 60   # Timeout 60s (mặc định 30s)
"""

import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# COLORS (disable on Windows nếu không hỗ trợ)
# ═══════════════════════════════════════════════════════════════
if sys.platform == "win32":
    try:
        os.system("")  # Enable ANSI on Windows 10+
    except Exception:
        pass


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
# CA CERTIFICATE GENERATION
# ═══════════════════════════════════════════════════════════════
def generate_ca_cert(cert_dir: Path) -> tuple[Path, Path]:
    """Tạo self-signed CA certificate cho MITM proxy.

    Returns: (ca_cert_path, ca_key_path)
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    # Generate RSA key
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Build CA certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "VSCode Token Interceptor CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Token Interceptor"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    # Save
    cert_path = cert_dir / "ca.pem"
    key_path = cert_dir / "ca-key.pem"

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    return cert_path, key_path


def generate_host_cert(
    ca_cert_path: Path, ca_key_path: Path, hostname: str, cert_dir: Path
) -> tuple[Path, Path]:
    """Tạo certificate cho một hostname cụ thể, ký bởi CA."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    # Load CA
    ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)

    # Generate host key
    host_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(host_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(hostname),
                x509.DNSName(f"*.{hostname}"),
            ]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    cert_path = cert_dir / f"{hostname}.pem"
    key_path = cert_dir / f"{hostname}-key.pem"

    # Cert chain: host cert + CA cert
    cert_path.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    key_path.write_bytes(
        host_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    return cert_path, key_path


# ═══════════════════════════════════════════════════════════════
# MITM PROXY
# ═══════════════════════════════════════════════════════════════
class TokenInterceptor:
    """HTTPS MITM Proxy tối giản — chỉ bắt Authorization header."""

    # Domains cần MITM để bắt token
    TARGET_DOMAINS = {
        "api.github.com",
        "api.individual.githubcopilot.com",
        "copilot-proxy.githubusercontent.com",
    }

    def __init__(self, port: int = 8080, timeout: int = 30, verbose: bool = False):
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.token: str | None = None
        self.copilot_token: str | None = None
        self.cert_dir: Path | None = None
        self.ca_cert_path: Path | None = None
        self.ca_key_path: Path | None = None
        self.server_socket: socket.socket | None = None
        self.running = False
        self._host_cert_cache: dict[str, tuple[Path, Path]] = {}
        self._lock = threading.Lock()

    def _get_host_cert(self, hostname: str) -> tuple[Path, Path]:
        """Lấy hoặc tạo cert cho hostname (có cache)."""
        if hostname not in self._host_cert_cache:
            cert, key = generate_host_cert(
                self.ca_cert_path, self.ca_key_path, hostname, self.cert_dir
            )
            self._host_cert_cache[hostname] = (cert, key)
        return self._host_cert_cache[hostname]

    def _log(self, msg: str):
        """Print nếu verbose mode."""
        if self.verbose:
            print(f"  {C.DIM}[DBG] {msg}{C.RESET}", flush=True)

    def _check_authorization(self, raw_data: bytes, source: str = "") -> bool:
        """Kiểm tra data có chứa gho_ token không."""
        text = raw_data.decode("utf-8", errors="replace")

        # Log all Authorization headers in verbose mode
        if self.verbose:
            for line in text.split("\r\n"):
                if line.lower().startswith("authorization:"):
                    self._log(f"[{source}] {line[:120]}")

        # Tìm GitHub OAuth token (gho_...)
        match = re.search(r"(gho_[A-Za-z0-9_]{20,})", text)
        if match:
            with self._lock:
                self.token = match.group(1)
            print(f"\n  {C.GREEN}{C.BOLD}🎯 BẮT ĐƯỢC GITHUB TOKEN!{C.RESET}")
            print(f"  {C.CYAN}{self.token}{C.RESET}")
            return True

        # Tìm Copilot bearer token (tid=...)
        match = re.search(r"Authorization:\s*Bearer\s+(tid=[^\r\n]+)", text, re.IGNORECASE)
        if match:
            with self._lock:
                if not self.copilot_token:
                    self.copilot_token = match.group(1).strip()
                    print(f"\n  {C.BLUE}📋 Copilot session token (bonus):{C.RESET}")
                    print(f"  {C.DIM}{self.copilot_token[:80]}...{C.RESET}")

        return False

    def _tunnel_data(self, src: socket.socket, dst: socket.socket, label: str) -> bool:
        """Chuyển dữ liệu giữa 2 socket, kiểm tra token trên đường đi.
        Returns True nếu tìm thấy token."""
        try:
            while self.running:
                try:
                    data = src.recv(8192)
                except (ConnectionResetError, ssl.SSLError, OSError):
                    break
                if not data:
                    break
                if self._check_authorization(data, label):
                    # Vẫn forward data để VS Code không bị lỗi
                    try:
                        dst.sendall(data)
                    except Exception:
                        pass
                    return True
                try:
                    dst.sendall(data)
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break
        except Exception:
            pass
        return False

    def _handle_connect(self, client_sock: socket.socket, hostname: str, port: int):
        """Xử lý HTTPS CONNECT — MITM nếu là target domain."""
        is_target = any(hostname.endswith(d) for d in self.TARGET_DOMAINS)

        self._log(f"CONNECT {hostname}:{port} (MITM={is_target})")

        if is_target:
            # MITM mode: tạo cert giả, decrypt traffic
            self._handle_mitm(client_sock, hostname, port)
        else:
            # Tunnel mode: chỉ forward, không decrypt
            self._handle_tunnel(client_sock, hostname, port)

    def _handle_mitm(self, client_sock: socket.socket, hostname: str, port: int):
        """MITM: decrypt HTTPS traffic để đọc headers."""
        try:
            # Kết nối tới server thật
            remote_sock = socket.create_connection((hostname, port), timeout=10)
            remote_ctx = ssl.create_default_context()
            remote_ssl = remote_ctx.wrap_socket(remote_sock, server_hostname=hostname)

            # Tạo cert giả cho hostname
            cert_path, key_path = self._get_host_cert(hostname)

            # Wrap client connection với cert giả
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            client_ctx.load_cert_chain(str(cert_path), str(key_path))
            client_ssl = client_ctx.wrap_socket(client_sock, server_side=True)

            # Bắt đầu relay data, kiểm tra token
            found = [False]

            def relay(src, dst, label):
                if self._tunnel_data(src, dst, label):
                    found[0] = True

            t1 = threading.Thread(target=relay, args=(client_ssl, remote_ssl, "→"), daemon=True)
            t2 = threading.Thread(target=relay, args=(remote_ssl, client_ssl, "←"), daemon=True)
            t1.start()
            t2.start()
            t1.join(timeout=self.timeout)
            t2.join(timeout=2)

            try:
                client_ssl.close()
            except Exception:
                pass
            try:
                remote_ssl.close()
            except Exception:
                pass

        except Exception as e:
            err = str(e)
            if "alert" not in err.lower() and "eof" not in err.lower():
                pass  # Bỏ qua SSL errors bình thường

    def _handle_tunnel(self, client_sock: socket.socket, hostname: str, port: int):
        """Pure tunnel: forward encrypted data mà không decrypt."""
        try:
            remote_sock = socket.create_connection((hostname, port), timeout=10)

            def relay(src, dst):
                try:
                    while self.running:
                        data = src.recv(8192)
                        if not data:
                            break
                        dst.sendall(data)
                except Exception:
                    pass

            t1 = threading.Thread(target=relay, args=(client_sock, remote_sock), daemon=True)
            t2 = threading.Thread(target=relay, args=(remote_sock, client_sock), daemon=True)
            t1.start()
            t2.start()
            t1.join(timeout=self.timeout)
            t2.join(timeout=2)

            try:
                client_sock.close()
            except Exception:
                pass
            try:
                remote_sock.close()
            except Exception:
                pass

        except Exception:
            pass

    def _handle_client(self, client_sock: socket.socket, addr):
        """Xử lý một client connection."""
        try:
            client_sock.settimeout(10)
            data = client_sock.recv(8192)
            if not data:
                client_sock.close()
                return

            request_line = data.split(b"\r\n")[0].decode("utf-8", errors="replace")

            if request_line.startswith("CONNECT"):
                # HTTPS: CONNECT hostname:port HTTP/1.1
                target = request_line.split()[1]
                if ":" in target:
                    hostname, port_str = target.rsplit(":", 1)
                    port = int(port_str)
                else:
                    hostname = target
                    port = 443

                # Gửi 200 Connection Established
                client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

                # Handle CONNECT
                self._handle_connect(client_sock, hostname, port)

            else:
                # HTTP request (không encrypt) — kiểm tra luôn
                self._check_authorization(data, "HTTP")
                client_sock.close()

        except Exception:
            try:
                client_sock.close()
            except Exception:
                pass

    def start(self) -> str | None:
        """Khởi động proxy, mở VS Code, đợi bắt token.

        Returns: token string hoặc None nếu timeout
        """
        # Tạo temp dir cho certs
        self._tmpdir = tempfile.mkdtemp(prefix="interceptor_")
        self.cert_dir = Path(self._tmpdir)

        print(f"  {C.BOLD}[1/4]{C.RESET} Tạo CA certificate...", end=" ", flush=True)
        self.ca_cert_path, self.ca_key_path = generate_ca_cert(self.cert_dir)
        print(f"{C.GREEN}OK{C.RESET}")

        # Khởi động proxy server
        print(f"  {C.BOLD}[2/4]{C.RESET} Khởi động MITM proxy (:{self.port})...", end=" ", flush=True)
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.server_socket.bind(("127.0.0.1", self.port))
        except OSError as e:
            print(f"{C.RED}LỖI{C.RESET}")
            print(f"\n  {C.RED}❌ Port {self.port} đang bị chiếm: {e}{C.RESET}")
            print(f"  {C.DIM}   Thử: python3 intercept_token.py --port 9090{C.RESET}")
            return None

        self.server_socket.listen(10)
        self.server_socket.settimeout(1)
        self.running = True
        print(f"{C.GREEN}OK{C.RESET}")

        # Mở VS Code
        print(f"  {C.BOLD}[3/4]{C.RESET} Mở VS Code qua proxy...", end=" ", flush=True)
        vscode_proc = self._launch_vscode()
        if not vscode_proc:
            print(f"{C.RED}LỖI{C.RESET}")
            self.running = False
            return None
        print(f"{C.GREEN}OK{C.RESET}")

        # Đợi bắt token
        print(f"  {C.BOLD}[4/4]{C.RESET} Đang chờ bắt token (timeout {self.timeout}s)...", flush=True)
        print(f"  {C.DIM}   VS Code đang khởi động và gửi auth request...{C.RESET}")

        start_time = time.time()
        try:
            while self.running and (time.time() - start_time) < self.timeout:
                try:
                    client_sock, addr = self.server_socket.accept()
                    t = threading.Thread(
                        target=self._handle_client,
                        args=(client_sock, addr),
                        daemon=True,
                    )
                    t.start()
                except socket.timeout:
                    pass

                # Kiểm tra đã bắt được token chưa
                with self._lock:
                    if self.token:
                        # Đợi thêm 2s để bắt copilot token nếu có
                        time.sleep(2)
                        break

                # Progress indicator
                elapsed = int(time.time() - start_time)
                if elapsed > 0 and elapsed % 5 == 0:
                    remaining = self.timeout - elapsed
                    print(f"  {C.DIM}   ⏳ {remaining}s còn lại...{C.RESET}", flush=True)

        except KeyboardInterrupt:
            print(f"\n  {C.YELLOW}⚠️  Interrupted{C.RESET}")

        # Cleanup
        self.running = False
        self._cleanup(vscode_proc)

        return self.token

    def _launch_vscode(self) -> subprocess.Popen | None:
        """Mở VS Code với proxy environment."""
        # Tìm VS Code binary
        if sys.platform == "win32":
            code_cmd = shutil.which("code.cmd") or shutil.which("code")
        else:
            code_cmd = shutil.which("code")

        if not code_cmd:
            print(f"\n  {C.RED}❌ Không tìm thấy 'code' command.{C.RESET}")
            return None

        env = os.environ.copy()
        env["HTTP_PROXY"] = f"http://127.0.0.1:{self.port}"
        env["HTTPS_PROXY"] = f"http://127.0.0.1:{self.port}"
        env["NO_PROXY"] = "169.254.169.254,localhost,127.0.0.1"
        env["NODE_EXTRA_CA_CERTS"] = str(self.ca_cert_path)
        # Bỏ NODE_TLS_REJECT_UNAUTHORIZED nếu có từ trước
        env.pop("NODE_TLS_REJECT_UNAUTHORIZED", None)

        try:
            # Mở VS Code ở temp folder (tránh load workspace nặng)
            tmpwork = tempfile.mkdtemp(prefix="vscode_interceptor_ws_")
            proc = subprocess.Popen(
                [code_cmd, "--new-window", tmpwork],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return proc
        except Exception as e:
            print(f"\n  {C.RED}❌ Không mở được VS Code: {e}{C.RESET}")
            return None

    def _cleanup(self, vscode_proc: subprocess.Popen | None):
        """Dọn dẹp."""
        # Đóng proxy server
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass

        # Kill VS Code process (chỉ process ta mở)
        if vscode_proc:
            try:
                vscode_proc.terminate()
                vscode_proc.wait(timeout=5)
            except Exception:
                try:
                    vscode_proc.kill()
                except Exception:
                    pass

        # Xóa temp certs
        if self._tmpdir and os.path.exists(self._tmpdir):
            try:
                import shutil as sh
                sh.rmtree(self._tmpdir, ignore_errors=True)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Bắt GitHub OAuth token từ VS Code qua MITM proxy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Cơ chế hoạt động:
  1. Tạo CA certificate tạm thời
  2. Khởi động HTTPS proxy trên 127.0.0.1:8080
  3. Mở VS Code mới với HTTP_PROXY + NODE_EXTRA_CA_CERTS
  4. VS Code gửi auth request → proxy bắt token → hiển thị

Ví dụ:
  python3 intercept_token.py                # Mặc định port 8080, timeout 30s
  python3 intercept_token.py --port 9090    # Port khác
  python3 intercept_token.py --timeout 60   # Đợi lâu hơn
  python3 intercept_token.py --save         # Lưu vào token.txt
        """,
    )
    parser.add_argument("--port", type=int, default=8080, help="Proxy port (mặc định: 8080)")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout giây (mặc định: 30)")
    parser.add_argument("--save", action="store_true", help="Lưu token vào token.txt")
    parser.add_argument("--quiet", "-q", action="store_true", help="Chỉ in token")
    parser.add_argument("--verbose", "-v", action="store_true", help="Log chi tiết")
    args = parser.parse_args()

    if args.quiet:
        # Quiet mode — chạy headless
        interceptor = TokenInterceptor(port=args.port, timeout=args.timeout)

        # Suppress all prints in quiet mode
        original_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        token = interceptor.start()
        sys.stdout = original_stdout

        if token:
            print(token)
            sys.exit(0)
        else:
            sys.exit(1)

    # Normal mode
    print()
    print(f"  {C.BOLD}{C.CYAN}╔══════════════════════════════════════════════╗{C.RESET}")
    print(f"  {C.BOLD}{C.CYAN}║  🔍 VS Code Token Interceptor (MITM Proxy) ║{C.RESET}")
    print(f"  {C.BOLD}{C.CYAN}╚══════════════════════════════════════════════╝{C.RESET}")
    print()

    interceptor = TokenInterceptor(port=args.port, timeout=args.timeout, verbose=args.verbose)
    token = interceptor.start()

    if not token:
        print(f"\n  {C.RED}❌ Không bắt được token trong {args.timeout}s.{C.RESET}")
        print(f"  {C.DIM}   Thử tăng timeout: --timeout 60{C.RESET}")
        print(f"  {C.DIM}   Hoặc kiểm tra VS Code đã đăng nhập GitHub chưa.{C.RESET}")
        sys.exit(1)

    # Hiển thị kết quả
    print()
    print(f"  {C.BOLD}{C.GREEN}{'═' * 48}{C.RESET}")
    masked = f"{token[:10]}...{token[-4:]}"
    print(f"  {C.BOLD}GitHub Token:{C.RESET} {C.CYAN}{masked}{C.RESET}")
    print(f"  {C.DIM}Full: {token}{C.RESET}")

    if interceptor.copilot_token:
        print(f"\n  {C.BOLD}Copilot Token:{C.RESET}")
        print(f"  {C.DIM}{interceptor.copilot_token[:100]}...{C.RESET}")

    print(f"  {C.BOLD}{C.GREEN}{'═' * 48}{C.RESET}")

    # Lưu nếu yêu cầu
    if args.save:
        token_file = Path(__file__).parent / "token.txt"
        token_file.write_text(token + "\n")
        print(f"\n  {C.GREEN}💾 Đã lưu vào: {token_file}{C.RESET}")

    print()


if __name__ == "__main__":
    main()
