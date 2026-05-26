from __future__ import annotations

import shlex
import socket
import sys
import time
from typing import Any

import paramiko


SSH_RETRYABLE_ERRORS = (
    TimeoutError,
    socket.timeout,
    EOFError,
    OSError,
    paramiko.SSHException,
)


def stream_print(text: str, *, file: Any | None = None) -> None:
    target = file or sys.stdout
    try:
        print(text, end="", file=target, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(target, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe_text, end="", file=target, flush=True)


def connect_ssh_with_retry(
    *,
    hostname: str,
    username: str,
    password: str,
    port: int = 22,
    attempts: int = 4,
    delay_seconds: float = 5.0,
) -> paramiko.SSHClient:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=hostname,
                username=username,
                password=password,
                port=port,
                look_for_keys=False,
                allow_agent=False,
                timeout=120,
                auth_timeout=60,
                banner_timeout=120,
            )
            return client
        except (paramiko.AuthenticationException, paramiko.BadHostKeyException):
            client.close()
            raise
        except SSH_RETRYABLE_ERRORS as exc:
            client.close()
            last_error = exc
            if attempt == attempts:
                raise
            print(
                f"SSH connect attempt {attempt}/{attempts} failed: {exc}. Retrying in {delay_seconds:.0f}s...",
                file=sys.stderr,
            )
            time.sleep(delay_seconds)
    assert last_error is not None
    raise last_error


def upload_text(client: paramiko.SSHClient, remote_path: str, content: str) -> None:
    with client.open_sftp() as sftp:
        with sftp.file(remote_path, "w") as remote_file:
            remote_file.write(content)


def exec_command_streaming(
    client: paramiko.SSHClient,
    command: str,
    *,
    stream_output: bool = True,
    poll_interval_seconds: float = 0.2,
) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command)
    try:
        stdin.close()
    except Exception:
        pass

    channel = stdout.channel
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    while True:
        made_progress = False
        while channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            stdout_chunks.append(chunk)
            if stream_output and chunk:
                stream_print(chunk)
            made_progress = True
        while channel.recv_stderr_ready():
            chunk = channel.recv_stderr(4096).decode("utf-8", errors="replace")
            stderr_chunks.append(chunk)
            if stream_output and chunk:
                stream_print(chunk, file=sys.stderr)
            made_progress = True
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            break
        if not made_progress:
            time.sleep(poll_interval_seconds)

    exit_code = channel.recv_exit_status()
    return exit_code, "".join(stdout_chunks), "".join(stderr_chunks)


def run_remote_python_script(
    client: paramiko.SSHClient,
    script_text: str,
    *,
    remote_path: str,
    python_bin: str = "/opt/holodka-bot/venv/bin/python3",
    stream_output: bool = True,
) -> tuple[int, str, str]:
    upload_text(client, remote_path, script_text)
    try:
        command = f"{shlex.quote(python_bin)} {shlex.quote(remote_path)}"
        return exec_command_streaming(client, command, stream_output=stream_output)
    finally:
        try:
            client.exec_command(f"rm -f {shlex.quote(remote_path)}")
        except Exception:
            pass
