from __future__ import annotations

import argparse
import os
from textwrap import dedent

from ssh_utils import connect_ssh_with_retry, run_remote_python_script


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Krisha import cycle on production over SSH.")
    parser.add_argument("--host", default="38.107.234.161")
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument(
        "--password-env",
        default="",
        help="Read the SSH password from this environment variable instead of --password.",
    )
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--project-id", type=int, default=2)
    parser.add_argument("--max-contacts", type=int, default=None)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument(
        "--remote-timeout-seconds",
        type=int,
        default=0,
        help="Abort the remote Krisha run after N seconds and return a timeout result. 0 disables the timeout.",
    )
    parser.add_argument(
        "--import-contacts",
        choices=["true", "false"],
        default="true",
        help="Whether to persist imported contacts on the server.",
    )
    parser.add_argument(
        "--start-whatsapp-check",
        choices=["true", "false"],
        default="true",
        help="Whether to trigger WhatsApp checking after the import cycle.",
    )
    parser.add_argument(
        "--show-settings",
        action="store_true",
        help="Print the resolved production settings before running the cycle.",
    )
    args = parser.parse_args()
    if args.password_env:
        args.password = os.environ.get(args.password_env, "")
    if not args.password:
        parser.error("Provide --password or --password-env with a non-empty environment variable")
    return args


def build_remote_script(args: argparse.Namespace) -> str:
    max_contacts = "None" if args.max_contacts is None else str(args.max_contacts)
    max_pages = "None" if args.max_pages is None else str(args.max_pages)
    remote_timeout_seconds = str(args.remote_timeout_seconds)
    import_contacts = "True" if args.import_contacts == "true" else "False"
    start_whatsapp_check = "True" if args.start_whatsapp_check == "true" else "False"
    show_settings = "True" if args.show_settings else "False"
    return dedent(
        f"""
        import asyncio
        import json
        import sys

        sys.path.insert(0, "/opt/holodka-bot/app")

        from app.main import get_settings, krisha_payload_from_settings, run_krisha_import_cycle, use_project

        PROJECT_ID = {args.project_id}
        MAX_CONTACTS = {max_contacts}
        MAX_PAGES = {max_pages}
        REMOTE_TIMEOUT_SECONDS = {remote_timeout_seconds}
        IMPORT_CONTACTS = {import_contacts}
        START_WHATSAPP_CHECK = {start_whatsapp_check}
        SHOW_SETTINGS = {show_settings}


        async def run() -> None:
            with use_project(PROJECT_ID):
                settings = get_settings(PROJECT_ID)
                payload = krisha_payload_from_settings(settings)
                if MAX_CONTACTS is not None:
                    payload.max_contacts = MAX_CONTACTS
                if MAX_PAGES is not None:
                    payload.max_pages = MAX_PAGES
                payload.import_contacts = IMPORT_CONTACTS
                summary = {{
                    "project_id": PROJECT_ID,
                    "max_contacts": payload.max_contacts,
                    "max_pages": payload.max_pages,
                    "remote_timeout_seconds": REMOTE_TIMEOUT_SECONDS,
                    "import_contacts": payload.import_contacts,
                    "start_whatsapp_check": START_WHATSAPP_CHECK,
                }}
                print("RUN_CONFIG:", json.dumps(summary, ensure_ascii=False))
                if SHOW_SETTINGS:
                    print("SETTINGS:", json.dumps(settings, ensure_ascii=False))
                try:
                    if REMOTE_TIMEOUT_SECONDS > 0:
                        result = await asyncio.wait_for(
                            run_krisha_import_cycle(
                                payload,
                                project_id=PROJECT_ID,
                                start_whatsapp_check=START_WHATSAPP_CHECK,
                            ),
                            timeout=REMOTE_TIMEOUT_SECONDS,
                        )
                    else:
                        result = await run_krisha_import_cycle(
                            payload,
                            project_id=PROJECT_ID,
                            start_whatsapp_check=START_WHATSAPP_CHECK,
                        )
                except asyncio.TimeoutError:
                    result = {{
                        "timed_out": True,
                        "timeout_seconds": REMOTE_TIMEOUT_SECONDS,
                        "source_errors": ["remote_timeout:" + str(REMOTE_TIMEOUT_SECONDS)],
                    }}
                print("RESULT:", json.dumps(result, ensure_ascii=False))


        asyncio.run(run())
        """
    ).strip()


def main() -> int:
    args = parse_args()
    remote_script_path = "/tmp/holodka_run_production_import.py"
    client = None
    try:
        client = connect_ssh_with_retry(
            hostname=args.host,
            username=args.user,
            password=args.password,
            port=args.port,
        )
        remote_script = build_remote_script(args)
        print(f"Executing on remote server: /opt/holodka-bot/venv/bin/python3 {remote_script_path}")
        exit_code, _output, _error = run_remote_python_script(
            client,
            remote_script,
            remote_path=remote_script_path,
            stream_output=True,
        )
        print(f"\nEXIT CODE: {exit_code}")
        return exit_code
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
