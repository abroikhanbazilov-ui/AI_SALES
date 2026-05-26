from __future__ import annotations

import argparse
import os
from textwrap import dedent

from ssh_utils import connect_ssh_with_retry, run_remote_python_script


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run short production runtime checks over SSH.")
    parser.add_argument("check", choices=["prompt", "interest-reply", "sync-prompt", "project-settings", "logs"])
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
    parser.add_argument("--message", default="Интересно")
    parser.add_argument("--prefix-chars", type=int, default=140)
    parser.add_argument("--log-category", default="", help="Optional event_logs category filter for the 'logs' check.")
    parser.add_argument("--log-limit", type=int, default=10, help="Maximum number of event log rows to return for the 'logs' check.")
    args = parser.parse_args()
    if args.password_env:
        args.password = os.environ.get(args.password_env, "")
    if not args.password:
        parser.error("Provide --password or --password-env with a non-empty environment variable")
    return args


def build_remote_script(args: argparse.Namespace) -> str:
    message = repr(args.message)
    return dedent(
        f"""
        import json
        import sys
        import traceback

        sys.path.insert(0, "/opt/holodka-bot/app")

        from app.main import SECOND_PROJECT_AI_PROMPT, db_conn, get_project, get_settings, interested_reply_text, use_project

        CHECK = {args.check!r}
        PROJECT_ID = {args.project_id}
        MESSAGE = {message}
        PREFIX_CHARS = {args.prefix_chars}
        LOG_CATEGORY = {args.log_category!r}
        LOG_LIMIT = {args.log_limit}

        try:
            project = get_project(PROJECT_ID)
            settings = get_settings(PROJECT_ID)
            prompt = settings.get("ai_system_prompt", "")
            result = {{
                "check": CHECK,
                "project_id": PROJECT_ID,
                "workflow_type": project.get("workflow_type"),
                "prompt_prefix": prompt[:PREFIX_CHARS],
                "prompt_length": len(prompt),
            }}
            if CHECK == "sync-prompt":
                if PROJECT_ID != 2:
                    raise RuntimeError("sync-prompt currently supports only project 2")
                with db_conn() as conn:
                    conn.execute(
                        "UPDATE projects SET ai_system_prompt = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (SECOND_PROJECT_AI_PROMPT, PROJECT_ID),
                    )
                project = get_project(PROJECT_ID)
                settings = get_settings(PROJECT_ID)
                prompt = settings.get("ai_system_prompt", "")
                result["prompt_prefix"] = prompt[:PREFIX_CHARS]
                result["prompt_length"] = len(prompt)
                result["exact_match_to_default"] = prompt == SECOND_PROJECT_AI_PROMPT
            elif CHECK == "prompt":
                result["exact_match_to_default"] = prompt == SECOND_PROJECT_AI_PROMPT if PROJECT_ID == 2 else None
            elif CHECK == "project-settings":
                result["project"] = {{
                    "id": project.get("id"),
                    "slug": project.get("slug"),
                    "name": project.get("name"),
                    "product_name": project.get("product_name"),
                    "workflow_type": project.get("workflow_type"),
                    "proposal_filename": project.get("proposal_filename"),
                    "updated_at": project.get("updated_at"),
                }}
                result["settings_subset"] = {{
                    "current_project_id": settings.get("current_project_id"),
                    "ai_provider": settings.get("ai_provider"),
                    "ai_enabled": settings.get("ai_enabled"),
                    "krisha_use_browser": settings.get("krisha_use_browser"),
                    "krisha_browser_engine": settings.get("krisha_browser_engine"),
                    "krisha_headless": settings.get("krisha_headless"),
                    "krisha_chrome_executable_path": settings.get("krisha_chrome_executable_path"),
                    "handoff_enabled": settings.get("handoff_enabled"),
                }}
            elif CHECK == "logs":
                limit = LOG_LIMIT if LOG_LIMIT > 0 else 10
                with db_conn() as conn:
                    if LOG_CATEGORY:
                        rows = conn.execute(
                            "SELECT id, created_at, category, level, message "
                            "FROM event_logs "
                            "WHERE project_id = ? AND category = ? "
                            "ORDER BY id DESC "
                            "LIMIT ?",
                            (PROJECT_ID, LOG_CATEGORY, limit),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT id, created_at, category, level, message "
                            "FROM event_logs "
                            "WHERE project_id = ? "
                            "ORDER BY id DESC "
                            "LIMIT ?",
                            (PROJECT_ID, limit),
                        ).fetchall()
                result["log_category"] = LOG_CATEGORY or None
                result["log_limit"] = limit
                result["rows"] = [dict(row) for row in rows]
            else:
                with use_project(PROJECT_ID):
                    reply = interested_reply_text({{}}, MESSAGE)
                result["message"] = MESSAGE
                result["interested_reply"] = reply
                result["contains_budget_check_question"] = "чек 35 млн" in reply.lower()
            print(json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            print(
                json.dumps(
                    {{
                        "check": CHECK,
                        "project_id": PROJECT_ID,
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    }},
                    ensure_ascii=False,
                )
            )
            raise
        """
    ).strip()


def main() -> int:
    args = parse_args()
    remote_script_path = "/tmp/holodka_check_production_runtime.py"
    client = None
    try:
        client = connect_ssh_with_retry(
            hostname=args.host,
            username=args.user,
            password=args.password,
            port=args.port,
        )
        script_text = build_remote_script(args)
        print(f"Executing runtime check '{args.check}' on remote server...")
        exit_code, _output, _error = run_remote_python_script(
            client,
            script_text,
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
