from __future__ import annotations

import argparse
import os
import shlex
import sys

from ssh_utils import connect_ssh_with_retry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a command over SSH and print stdout/stderr.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--password-env", default="", help="Read SSH password from this environment variable instead of --password.")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("command", nargs="+", help="Remote shell command to run")
    args = parser.parse_args()
    if args.password_env:
        args.password = os.environ.get(args.password_env, "")
    if not args.password:
        parser.error("Provide --password or --password-env with a non-empty environment variable")
    return args


def main() -> int:
    args = parse_args()
    client = None
    try:
        client = connect_ssh_with_retry(
            hostname=args.host,
            username=args.user,
            password=args.password,
            port=args.port,
        )
        command = " ".join(args.command)
        stdin, stdout, stderr = client.exec_command(f"bash -lc {shlex.quote(command)}")
        exit_code = stdout.channel.recv_exit_status()
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        if output:
            print(output, end="")
        if error:
            print(error, end="", file=sys.stderr)
        return exit_code
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
