from __future__ import annotations

import argparse
import os
import shlex
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import paramiko

from ssh_utils import SSH_RETRYABLE_ERRORS, connect_ssh_with_retry


EXCLUDED_PARTS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    ".ruff_cache",
    "data",
    "uploads",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
EXCLUDED_NAMES = {"deploy.tar.gz", "deploy.zip"}

REMOTE_APP_ROOT = "/opt/holodka-bot"
REMOTE_APP_DIR = f"{REMOTE_APP_ROOT}/app"
REMOTE_SHARED_DIR = f"{REMOTE_APP_ROOT}/shared"
REMOTE_VENV_DIR = f"{REMOTE_APP_ROOT}/venv"
REMOTE_PLAYWRIGHT_BROWSERS_DIR = f"{REMOTE_SHARED_DIR}/ms-playwright"
REMOTE_ARCHIVE = "/root/holodka-bot-deploy.tar.gz"
REMOTE_SERVICE_PATH = "/etc/systemd/system/holodka-bot.service"
REMOTE_NGINX_PATH = "/etc/nginx/sites-available/holodka-bot"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy HOLODKA_BOT to an Ubuntu server.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument(
        "--password-env",
        default="",
        help="Read SSH password from this environment variable instead of --password.",
    )
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument(
        "--source-dir",
        default=str(Path(__file__).resolve().parents[1]),
        help="Local project directory to package and upload.",
    )
    args = parser.parse_args()
    if args.password_env:
        args.password = os.environ.get(args.password_env, "")
    if not args.password:
        parser.error("Provide --password or --password-env with a non-empty environment variable")
    return args


def should_skip(path: Path) -> bool:
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return True
    if path.name in EXCLUDED_NAMES:
        return True
    return path.suffix.lower() in EXCLUDED_SUFFIXES


def build_archive(source_dir: Path) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="holodka-deploy-"))
    archive_path = temp_dir / "holodka-bot.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        for item in sorted(source_dir.rglob("*")):
            relative_path = item.relative_to(source_dir)
            if should_skip(relative_path):
                continue
            tar.add(item, arcname=str(relative_path))
    return archive_path


def connect_ssh(host: str, user: str, password: str, port: int) -> paramiko.SSHClient:
    return connect_ssh_with_retry(
        hostname=host,
        username=user,
        password=password,
        port=port,
    )


def run_step_with_client(
    host: str,
    user: str,
    password: str,
    port: int,
    action: Any,
    *,
    attempts: int = 3,
    delay_seconds: float = 5.0,
) -> None:
    for attempt in range(1, attempts + 1):
        client = None
        try:
            client = connect_ssh(host, user, password, port)
            action(client)
            return
        except (paramiko.AuthenticationException, paramiko.BadHostKeyException):
            raise
        except (RuntimeError, *SSH_RETRYABLE_ERRORS) as exc:
            if attempt == attempts:
                raise
            print(
                f"Step failed on attempt {attempt}/{attempts}: {exc}. Retrying in {delay_seconds:.0f}s...",
                file=sys.stderr,
            )
            time.sleep(delay_seconds)
        finally:
            if client is not None:
                client.close()


def run_remote(client: paramiko.SSHClient, command: str) -> str:
    wrapped = f"bash -lc {shlex.quote(command)}"
    stdin, stdout, stderr = client.exec_command(wrapped)
    exit_code = stdout.channel.recv_exit_status()
    output = stdout.read().decode("utf-8", errors="replace")
    error = stderr.read().decode("utf-8", errors="replace")
    if exit_code != 0:
        raise RuntimeError(f"Remote command failed ({exit_code}): {command}\nSTDOUT:\n{output}\nSTDERR:\n{error}")
    return (output + error).strip()


def upload_file(client: paramiko.SSHClient, local_path: Path, remote_path: str) -> None:
    with client.open_sftp() as sftp:
        sftp.put(str(local_path), remote_path)


def upload_text(client: paramiko.SSHClient, remote_path: str, content: str, mode: int) -> None:
    with client.open_sftp() as sftp:
        with sftp.file(remote_path, "w") as remote_file:
            remote_file.write(content)
        sftp.chmod(remote_path, mode)


def wait_for_http(client: paramiko.SSHClient, url: str, expected_codes: set[int], attempts: int = 20, delay_seconds: float = 2.0) -> int:
    expected = {str(code) for code in expected_codes}
    last_code = ""
    for _ in range(attempts):
        try:
            last_code = run_remote(client, f"curl -sS -o /dev/null -w '%{{http_code}}' {shlex.quote(url)} || true").strip()
        except RuntimeError:
            last_code = ""
        if last_code in expected:
            return int(last_code)
        time.sleep(delay_seconds)
    raise RuntimeError(f"Timed out waiting for {url}. Last HTTP code: {last_code or 'none'}")


def service_unit() -> str:
    return f"""[Unit]
Description=HOLODKA Bot FastAPI service
After=network.target

[Service]
Type=simple
User=holodka
Group=holodka
WorkingDirectory=/opt/holodka-bot/app
Environment=PYTHONUNBUFFERED=1
Environment=AUTH_COOKIE_SECURE=false
Environment=HOME=/opt/holodka-bot
Environment=PLAYWRIGHT_BROWSERS_PATH={REMOTE_PLAYWRIGHT_BROWSERS_DIR}
ExecStart=/opt/holodka-bot/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""


def nginx_config() -> str:
    return """server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    client_max_body_size 25m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
        proxy_connect_timeout 60;
    }
}
"""


def provision_remote(client: paramiko.SSHClient) -> None:
    print("[1/6] Installing system packages...")
    run_remote(
        client,
        "export DEBIAN_FRONTEND=noninteractive && "
        "for attempt in 1 2 3; do "
        "apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update && "
        "apt-get install -y -o DPkg::Lock::Timeout=60 python3 python3-venv python3-pip nginx ufw ca-certificates && exit 0; "
        "if [ \"$attempt\" -eq 3 ]; then exit 1; fi; "
        "echo \"apt-get attempt $attempt failed; retrying...\" >&2; "
        "sleep 5; "
        "done",
    )
    print("[2/6] Creating app directories and service user...")
    run_remote(
        client,
        "id -u holodka >/dev/null 2>&1 || useradd --system --home-dir /opt/holodka-bot "
        "--shell /usr/sbin/nologin --no-create-home holodka; "
        f"mkdir -p /opt/holodka-bot/app /opt/holodka-bot/shared/data /opt/holodka-bot/shared/uploads {REMOTE_PLAYWRIGHT_BROWSERS_DIR}",
    )


def deploy_archive(client: paramiko.SSHClient) -> None:
    print("[3/6] Extracting uploaded project and preserving data directories...")
    run_remote(
        client,
        f"mkdir -p /opt/holodka-bot/shared/data /opt/holodka-bot/shared/uploads /opt/holodka-bot/shared/backups {REMOTE_PLAYWRIGHT_BROWSERS_DIR} && "
        "if [ -f /opt/holodka-bot/shared/data/agent.sqlite3 ]; then "
        "cp -a /opt/holodka-bot/shared/data/agent.sqlite3 "
        "\"/opt/holodka-bot/shared/backups/agent.$(date +%Y%m%d-%H%M%S).sqlite3\"; "
        "fi && "
        "rm -rf /opt/holodka-bot/app && mkdir -p /opt/holodka-bot/app && "
        f"tar -xzf {shlex.quote(REMOTE_ARCHIVE)} -C /opt/holodka-bot/app && "
        "mkdir -p /opt/holodka-bot/shared/data /opt/holodka-bot/shared/uploads && "
        "rm -rf /opt/holodka-bot/app/data /opt/holodka-bot/app/uploads && "
        "ln -s /opt/holodka-bot/shared/data /opt/holodka-bot/app/data && "
        "ln -s /opt/holodka-bot/shared/uploads /opt/holodka-bot/app/uploads",
    )


def configure_app(client: paramiko.SSHClient) -> None:
    print("[4/6] Building Python environment and installing dependencies...")
    run_remote(
        client,
        "python3 -m venv /opt/holodka-bot/venv && "
        "/opt/holodka-bot/venv/bin/pip install --upgrade pip setuptools wheel && "
        "/opt/holodka-bot/venv/bin/pip install -r /opt/holodka-bot/app/requirements.txt && "
        f"PLAYWRIGHT_BROWSERS_PATH={shlex.quote(REMOTE_PLAYWRIGHT_BROWSERS_DIR)} /opt/holodka-bot/venv/bin/python -m playwright install --with-deps chromium && "
        "chown -R holodka:holodka /opt/holodka-bot",
    )
    print("[5/6] Writing systemd and nginx configuration...")
    upload_text(client, REMOTE_SERVICE_PATH, service_unit(), 0o644)
    upload_text(client, REMOTE_NGINX_PATH, nginx_config(), 0o644)
    run_remote(
        client,
        "ln -sf /etc/nginx/sites-available/holodka-bot /etc/nginx/sites-enabled/holodka-bot && "
        "rm -f /etc/nginx/sites-enabled/default && "
        "systemctl daemon-reload && systemctl enable holodka-bot && systemctl restart holodka-bot && "
        "nginx -t && systemctl enable --now nginx && systemctl restart nginx && "
        "ufw allow OpenSSH && ufw allow 'Nginx Full' && ufw --force enable && "
        f"rm -f {shlex.quote(REMOTE_ARCHIVE)}",
    )


def verify_remote(client: paramiko.SSHClient, host: str) -> None:
    print("[6/6] Verifying service health locally on the server...")
    service_status = run_remote(client, "systemctl is-active holodka-bot")
    nginx_status = run_remote(client, "systemctl is-active nginx")
    wait_for_http(client, "http://127.0.0.1/api/auth/status", {200})
    wait_for_http(client, f"http://{host}/login", {200})
    auth_status = run_remote(client, "curl -fsS http://127.0.0.1/api/auth/status")
    login_status = run_remote(client, "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1/login")
    public_status = run_remote(client, f"curl -sS -o /dev/null -w '%{{http_code}}' http://{shlex.quote(host)}/login")
    print(f"holodka-bot: {service_status}")
    print(f"nginx: {nginx_status}")
    print(f"auth status: {auth_status}")
    print(f"login via localhost: HTTP {login_status}")
    print(f"login via public IP: HTTP {public_status}")


def main() -> int:
    args = parse_args()
    source_dir = Path(args.source_dir).resolve()
    if not source_dir.exists():
        print(f"Source directory does not exist: {source_dir}", file=sys.stderr)
        return 1

    print("Building deployment archive...")
    archive_path = build_archive(source_dir)
    try:
        print(f"Connecting to {args.user}@{args.host}:{args.port}...")
        run_step_with_client(args.host, args.user, args.password, args.port, provision_remote)
        print("Uploading project archive...")
        run_step_with_client(
            args.host,
            args.user,
            args.password,
            args.port,
            lambda client: upload_file(client, archive_path, REMOTE_ARCHIVE),
        )
        run_step_with_client(args.host, args.user, args.password, args.port, deploy_archive)
        run_step_with_client(args.host, args.user, args.password, args.port, configure_app)
        run_step_with_client(
            args.host,
            args.user,
            args.password,
            args.port,
            lambda client: verify_remote(client, args.host),
        )
        print(f"Deployment completed. Open http://{args.host}/login")
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
