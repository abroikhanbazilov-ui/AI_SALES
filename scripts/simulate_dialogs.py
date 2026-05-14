from __future__ import annotations

import asyncio
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.main as main


def inbound_body(phone: str, text: str) -> dict[str, object]:
    return {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": f"sim-{uuid.uuid4().hex}",
        "senderData": {
            "chatId": f"{phone}@c.us",
            "sender": f"{phone}@c.us",
            "senderName": "Тест",
        },
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {"textMessage": text},
        },
    }


class SimulationHarness:
    def __init__(self) -> None:
        self.transcript: list[tuple[str, str, str]] = []
        self.counter = 0

    def log_bot(self, contact: dict[str, object], text: str) -> None:
        fresh = main.get_contact(contact["id"]) or contact
        self.counter += 1
        self.transcript.append((str(fresh["phone"]), "BOT", text))
        main.save_message(fresh["id"], fresh["chat_id"], "out", text, f"sim-out-{self.counter}", {"simulated": True})
        main.update_contact_fields(fresh["id"], last_outbound_at=main.now_iso(), last_error=None)

    def log_user(self, contact: dict[str, object], text: str) -> None:
        self.transcript.append((str(contact["phone"]), "USER", text))

    async def send_and_log(self, contact: dict[str, object], text: str) -> dict[str, object]:
        self.log_bot(contact, text)
        return {"idMessage": f"sim-out-{self.counter}"}

    async def send_proposal(self, contact: dict[str, object]) -> None:
        fresh = main.get_contact(contact["id"]) or contact
        caption = "[SIMULATED КП] Короткое КП по мобильному приложению автоскоринга для автодилера."
        self.log_bot(fresh, caption)
        main.update_contact_fields(
            fresh["id"],
            proposal_sent=1,
            status="proposal_sent",
            stage="proposal_sent",
            last_outbound_at=main.now_iso(),
            last_error=None,
        )


async def fake_check_and_mark_whatsapp(contact: dict[str, object]) -> bool:
    main.update_contact_fields(contact["id"], whatsapp_exists=1)
    return True


def setup_temp_db(temp_db: Path, original_settings: dict[str, str], original_project: dict[str, object]) -> None:
    main.DB_PATH = temp_db
    main.init_db()
    main.update_settings(
        {
            "bot_name": original_settings.get("bot_name") or "Медет",
            "ai_enabled": original_settings.get("ai_enabled") or "true",
            "ai_provider": original_settings.get("ai_provider") or "openai",
            "openai_api_key": original_settings.get("openai_api_key") or "",
            "openai_base_url": original_settings.get("openai_base_url") or "https://api.openai.com/v1",
            "openai_model": original_settings.get("openai_model") or "gpt-5.2",
            "deepseek_api_key": original_settings.get("deepseek_api_key") or "",
            "deepseek_base_url": original_settings.get("deepseek_base_url") or "https://api.deepseek.com",
            "deepseek_model": original_settings.get("deepseek_model") or "deepseek-v4-flash",
            "ai_temperature": original_settings.get("ai_temperature") or "0.35",
            "default_country_code": original_settings.get("default_country_code") or "7",
            "handoff_enabled": "false",
            "proposal_filename": original_settings.get("proposal_filename") or "Коммерческое Предложение.html",
        }
    )
    with main.db_conn() as conn:
        conn.execute(
            """
            UPDATE projects
            SET name = ?, product_name = ?, workflow_type = ?, proposal_filename = ?, ai_system_prompt = ?
            WHERE id = ?
            """,
            (
                original_project.get("name"),
                original_project.get("product_name"),
                original_project.get("workflow_type"),
                original_project.get("proposal_filename"),
                original_project.get("ai_system_prompt"),
                original_project.get("id"),
            ),
        )
        conn.commit()


def create_lead(phone: str, company: str, *, meta: dict[str, object] | None = None) -> dict[str, object]:
    return main.create_or_update_contact(
        phone=phone,
        kind="lead",
        source="simulation",
        company=company,
        status="replied",
        stage="replied",
        meta=meta,
    )


def print_summary(title: str, harness: SimulationHarness) -> None:
    print("=" * 88)
    print(title)
    print("-" * 88)
    for phone, role, text in harness.transcript:
        print(f"[{phone}] {role}: {text}")
    with main.db_conn() as conn:
        rows = conn.execute(
            "SELECT phone, kind, name, status, stage, proposal_sent, owner_contact_id FROM contacts ORDER BY id"
        ).fetchall()
    print("STATE:")
    for row in rows:
        print(
            {
                "phone": row["phone"],
                "kind": row["kind"],
                "name": row["name"],
                "status": row["status"],
                "stage": row["stage"],
                "proposal_sent": row["proposal_sent"],
                "owner_contact_id": row["owner_contact_id"],
            }
        )
    print()


async def run_steps(lead: dict[str, object], steps: list[str], harness: SimulationHarness) -> dict[str, object]:
    with (
        patch.object(main, "send_and_log", harness.send_and_log),
        patch.object(main, "send_proposal", harness.send_proposal),
        patch.object(main, "check_and_mark_whatsapp", fake_check_and_mark_whatsapp),
    ):
        for text in steps:
            harness.log_user(lead, text)
            await main.process_notification_body(inbound_body(str(lead["phone"]), text), allow_outbound=True)
            lead = main.get_contact(lead["id"]) or lead
    return lead


def seed_campaign_touch(lead: dict[str, object], harness: SimulationHarness) -> dict[str, object]:
    harness.log_bot(lead, main.build_campaign_greeting(lead))
    next_stage = "warmup_permission" if main.uses_autoscore_warmup_flow() else "waiting_reply"
    main.update_contact_fields(lead["id"], status="sent", stage=next_stage)
    return main.get_contact(lead["id"]) or lead


def build_scenarios() -> list[dict[str, object]]:
    return [
        {"title": "permission ack -> credit yes -> reveal", "phone": "77010000001", "company": "Royal Auto", "steps": ["Здравствуйте", "Да, есть"]},
        {"title": "greeting reply -> credit yes -> reveal", "phone": "77010000002", "company": "Drive Motors", "steps": ["Добрый день", "Да, оформляем"]},
        {"title": "question about reason -> credit yes", "phone": "77010000003", "company": "Prime Auto", "steps": ["По какому вопросу?", "Да, есть автокредит"]},
        {"title": "listening reply -> credit yes", "phone": "77010000004", "company": "Orbit Auto", "steps": ["Слушаю", "Да, занимаемся"]},
        {"title": "decline on permission step", "phone": "77010000005", "company": "North Cars", "steps": ["Нет"]},
        {"title": "opt-out on permission step", "phone": "77010000006", "company": "Vector Auto", "steps": ["Стоп"]},
        {"title": "no fit on credit question", "phone": "77010000007", "company": "Delta Cars", "steps": ["Здравствуйте", "Нет, не занимаемся"]},
        {"title": "self-lpr -> explicit proposal consent", "phone": "77010000008", "company": "Aspan Auto", "steps": ["Здравствуйте", "Да, этим занимаюсь я", "Да, пришлите сюда"]},
        {"title": "self-lpr -> short ok consent", "phone": "77010000009", "company": "Merit Cars", "steps": ["Можно", "Да, я занимаюсь", "Ок"]},
        {"title": "self-lpr -> imperative consent", "phone": "77010000010", "company": "Summit Auto", "steps": ["Слушаю", "Да, можете мне писать", "Отправляй!"]},
        {"title": "detail request after reveal", "phone": "77010000011", "company": "Zeta Motors", "steps": ["Здравствуйте", "Да, оформляем", "Уточните подробнее"]},
        {"title": "budget objection after reveal", "phone": "77010000012", "company": "Gamma Auto", "steps": ["Здравствуйте", "Да, оформляем", "Сейчас нет бюджета"]},
        {"title": "existing crm objection after reveal", "phone": "77010000013", "company": "Alem Cars", "steps": ["Здравствуйте", "Да, оформляем", "У нас уже есть своя CRM"]},
        {"title": "meeting interest after reveal", "phone": "77010000014", "company": "Nova Auto", "steps": ["Здравствуйте", "Да, оформляем", "Можно короткий созвон завтра?"]},
        {"title": "action request micro-cta", "phone": "77010000015", "company": "Silver Auto", "steps": ["Здравствуйте", "Да, оформляем", "Ок. Что требуется от меня?"]},
        {"title": "transfer offer branch", "phone": "77010000016", "company": "Titan Auto", "steps": ["Здравствуйте", "Да, оформляем", "Я свяжу вас с менеджером отдела продаж"]},
        {"title": "buyer confusion branch", "phone": "77010000017", "company": "Union Auto", "steps": ["Здравствуйте", "Да, оформляем", "Вы хотите получить автокредитование?"]},
        {"title": "referred contact with phone", "phone": "77010000018", "company": "Axis Auto", "steps": ["Здравствуйте", "Да, оформляем", "Вот контакт Ержан 77001234567"]},
        {"title": "referred contact without name", "phone": "77010000019", "company": "Pilot Auto", "steps": ["Здравствуйте", "Да, оформляем", "Вот контакт 77001234568", "Незнаю"]},
        {"title": "referred contact owner ack", "phone": "77010000020", "company": "Ultra Auto", "steps": ["Здравствуйте", "Да, оформляем", "Вот контакт 77001234569", "Ок"]},
        {"title": "email request after reveal", "phone": "77010000021", "company": "Vector Motors", "steps": ["Здравствуйте", "Да, оформляем", "Скиньте КП на почту test@example.com"]},
        {"title": "generic interest after reveal", "phone": "77010000022", "company": "Matrix Auto", "steps": ["Здравствуйте", "Да, оформляем", "Да, интересно"]},
        {"title": "identity question on first reply", "phone": "77010000023", "company": "R-Line Auto", "steps": ["А вы кто вообще?"]},
        {"title": "repeated greeting mid-dialog", "phone": "77010000024", "company": "Orion Auto", "steps": ["Здравствуйте", "Да, оформляем", "Здравствуйте"]},
        {"title": "soft negative after reveal", "phone": "77010000025", "company": "Plaza Cars", "steps": ["Здравствуйте", "Да, оформляем", "Пока не актуально"]},
        {"title": "self-lpr asks for email proposal", "phone": "77010000026", "company": "Quartz Auto", "steps": ["Здравствуйте", "Да, этим занимаюсь я", "Скиньте КП на почту boss@example.com"]},
        {"title": "self-lpr asks details before proposal", "phone": "77010000027", "company": "Rocket Motors", "steps": ["Здравствуйте", "Да, этим занимаюсь я", "Расскажите подробнее"]},
        {"title": "not my area after reveal", "phone": "77010000028", "company": "Sector Auto", "steps": ["Здравствуйте", "Да, оформляем", "Это не ко мне"]},
        {"title": "interest then discovery", "phone": "77010000029", "company": "Trinity Auto", "steps": ["Здравствуйте", "Да, есть", "Да, интересно", "Для нас важнее быстрее обработка анкет"]},
        {"title": "interest then scheduling", "phone": "77010000030", "company": "Unity Motors", "steps": ["Здравствуйте", "Да, есть", "Да, интересно", "Можно короткий созвон завтра?"]},
    ]


async def run_campaign_scenario(index: int, scenario: dict[str, object]) -> None:
    harness = SimulationHarness()
    lead = create_lead(
        str(scenario["phone"]),
        str(scenario["company"]),
        meta=scenario.get("meta") if isinstance(scenario.get("meta"), dict) else None,
    )
    lead = seed_campaign_touch(lead, harness)
    await run_steps(lead, list(scenario["steps"]), harness)
    print_summary(f"SCENARIO {index}: {scenario['title']}", harness)


async def main_async() -> None:
    original_db_path = main.DB_PATH
    original_settings = main.get_settings()
    original_project = main.get_project()
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            setup_temp_db(Path(temp_dir) / "simulate.sqlite3", original_settings, original_project)
            scenarios = build_scenarios()
            for index, scenario in enumerate(scenarios, start=1):
                await run_campaign_scenario(index, scenario)
            print(f"TOTAL SCENARIOS: {len(scenarios)}")
    finally:
        main.DB_PATH = original_db_path


if __name__ == "__main__":
    asyncio.run(main_async())