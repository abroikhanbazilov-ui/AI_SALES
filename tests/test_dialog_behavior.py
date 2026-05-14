from __future__ import annotations

import sys
import tempfile
import types
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app.main as main


class TempDbMixin:
    def setUp(self) -> None:
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = main.DB_PATH
        main.DB_PATH = Path(self.temp_dir.name) / "test.sqlite3"
        main.init_db()
        main.update_settings({"bot_name": "Медет", "ai_enabled": "true"})

    def tearDown(self) -> None:
        main.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()
        super().tearDown()

    def create_lead(self, phone: str = "77000000001") -> dict[str, object]:
        return main.create_or_update_contact(
            phone=phone,
            kind="lead",
            source="test",
            company="Test Dealer",
            status="replied",
            stage="replied",
        )

    @staticmethod
    def inbound_body(phone: str, text: str) -> dict[str, object]:
        return {
            "typeWebhook": "incomingMessageReceived",
            "idMessage": f"msg-{uuid.uuid4().hex}",
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

    @staticmethod
    def image_body(phone: str) -> dict[str, object]:
        return {
            "typeWebhook": "incomingMessageReceived",
            "idMessage": f"msg-{uuid.uuid4().hex}",
            "senderData": {
                "chatId": f"{phone}@c.us",
                "sender": f"{phone}@c.us",
                "senderName": "Тест",
            },
            "messageData": {
                "typeMessage": "imageMessage",
                "journalData": {
                    "timestamp": 1778311722,
                    "idInstance": 7700282474,
                    "downloadUrl": "https://example.test/file.jpg",
                    "caption": "",
                },
            },
        }

    @staticmethod
    def quoted_body(phone: str, text: str, quoted_text: str = "Подскажите, у вас есть продажи авто через автокредит?") -> dict[str, object]:
        return {
            "typeWebhook": "incomingMessageReceived",
            "idMessage": f"msg-{uuid.uuid4().hex}",
            "senderData": {
                "chatId": f"{phone}@c.us",
                "sender": f"{phone}@c.us",
                "senderName": "Тест",
            },
            "messageData": {
                "typeMessage": "quotedMessage",
                "extendedTextMessageData": {"text": text, "stanzaId": "quoted-id", "participant": f"{phone}@c.us"},
                "quotedMessage": {"typeMessage": "textMessage", "textMessage": quoted_text},
            },
        }

    @staticmethod
    def buttons_body(phone: str) -> dict[str, object]:
        return {
            "typeWebhook": "incomingMessageReceived",
            "idMessage": f"msg-{uuid.uuid4().hex}",
            "senderData": {
                "chatId": f"{phone}@c.us",
                "sender": f"{phone}@c.us",
                "senderName": "Тест",
            },
            "messageData": {
                "typeMessage": "buttonsMessage",
                "buttonsMessage": {
                    "contentText": "Подскажите, Вам на каком языке удобно продолжить диалог?",
                    "buttons": [{"buttonText": "Қазақша"}, {"buttonText": "Русский"}],
                },
            },
        }

    @staticmethod
    def audio_body(phone: str) -> dict[str, object]:
        return {
            "typeWebhook": "incomingMessageReceived",
            "idMessage": f"msg-{uuid.uuid4().hex}",
            "senderData": {
                "chatId": f"{phone}@c.us",
                "sender": f"{phone}@c.us",
                "senderName": "Тест",
            },
            "messageData": {
                "typeMessage": "audioMessage",
                "fileMessageData": {
                    "downloadUrl": "https://example.test/voice.oga",
                    "fileName": "voice.oga",
                    "mimeType": "audio/ogg; codecs=opus",
                },
            },
        }


class DialogHelperTests(TempDbMixin, unittest.TestCase):
    def test_clean_person_name_rejects_unknown_name(self) -> None:
        self.assertIsNone(main.clean_person_name("Незнаю"))
        self.assertEqual(main.clean_person_name("Агыбай"), "Агыбай")

    def test_fallback_identity_reply_uses_bot_name(self) -> None:
        lead = self.create_lead()
        action = main.fallback_reply(lead, "А вы кто вообще?")
        self.assertEqual(action["stage"], "continue")
        self.assertIn("Меня зовут Медет", action["reply"])

    def test_fallback_owner_ack_does_not_reask_lpr(self) -> None:
        lead = self.create_lead()
        main.create_or_update_contact(
            phone="77000000002",
            kind="lpr",
            source="test",
            company="Test Dealer",
            owner_contact_id=lead["id"],
            status="proposal_sent",
            stage="proposal_sent",
        )
        action = main.fallback_reply(lead, "Ок")
        self.assertEqual(action["stage"], "continue")
        self.assertNotIn("кто у вас", action["reply"].lower())

    def test_polite_owner_follow_up_after_proposal_is_silent(self) -> None:
        lead = self.create_lead("77000000018")
        main.create_or_update_contact(
            phone="77000000019",
            kind="lpr",
            source="test",
            company="Test Dealer",
            owner_contact_id=lead["id"],
            status="proposal_sent",
            stage="proposal_sent",
        )
        main.update_contact_fields(lead["id"], proposal_sent=1, status="proposal_sent", stage="proposal_sent")

        self.assertIsNone(main.polite_owner_follow_up(lead, "Буду иметь в виду"))

    def test_strip_repeated_greeting_in_existing_dialog(self) -> None:
        lead = self.create_lead()
        main.save_message(lead["id"], lead["chat_id"], "out", "Добрый день. У меня короткий вопрос.")
        stripped = main.strip_repeated_greeting("Здравствуйте! Подскажите, кто отвечает за это направление?", lead["id"])
        self.assertFalse(stripped.lower().startswith("здравствуйте"))
        self.assertIn("кто отвечает", stripped.lower())

    def test_direct_self_reply_detected_after_decision_maker_question(self) -> None:
        lead = self.create_lead()
        main.save_message(
            lead["id"],
            lead["chat_id"],
            "out",
            "Подскажите, пожалуйста, кто у вас отвечает за это направление?",
        )
        self.assertTrue(main.is_direct_self_reply(lead["id"], "я"))

    def test_strip_redundant_self_intro_removes_repeated_bot_name(self) -> None:
        lead = self.create_lead("77000000020")
        main.save_message(
            lead["id"],
            lead["chat_id"],
            "out",
            "Здравствуйте! Меня зовут Медет. Пишу по коммерческому вопросу.",
        )

        cleaned = main.strip_redundant_self_intro(
            "Медет, пишу по white-label приложению для автокредитов под бренд дилера.",
            lead,
            "Здравствуйте. По какому вопросу?",
        )

        self.assertFalse(cleaned.startswith("Медет"))
        self.assertTrue(cleaned.startswith("Пишу"))

    def test_autoscore_campaign_greeting_uses_permission_opener(self) -> None:
        lead = main.create_or_update_contact(
            phone="77000000009",
            kind="lead",
            source="test",
            company="Test Dealer",
            status="ready",
            stage="imported",
            meta={
                "sales_signal": "Рубрика: автосалон",
                "sales_angle": "У вас похоже есть дилерское направление, поэтому пишу по цифровым заявкам и автоскорингу.",
            },
        )
        text = main.build_campaign_greeting(lead)
        self.assertIn("автокредитному направлению", text)
        self.assertNotIn("Медет", text)
        self.assertNotIn("автоскоринг", text.lower())

    def test_fallback_budget_objection_uses_planning_angle(self) -> None:
        lead = self.create_lead()
        action = main.fallback_reply(lead, "Сейчас нет бюджета")
        self.assertEqual(action["stage"], "budget_objection")
        self.assertIn("3-6 месяцев", action["reply"])

    def test_fallback_existing_solution_uses_laer_explore_question(self) -> None:
        lead = self.create_lead()
        action = main.fallback_reply(lead, "У нас уже есть своя CRM")
        self.assertEqual(action["stage"], "existing_solution_objection")
        self.assertIn("заявках", action["reply"])

    def test_media_json_numbers_are_not_lpr_candidates(self) -> None:
        _, message_data = main.extract_inbound_text(self.image_body("77000000011"))
        candidates = main.extract_lpr_candidates("", message_data)
        self.assertEqual(candidates, [])
        self.assertIsNone(main.normalize_phone("1778311722"))

    def test_extract_inbound_text_reads_quoted_reply_text(self) -> None:
        text, message_data = main.extract_inbound_text(self.quoted_body("77000000012", "Да есть"))
        self.assertEqual(text, "Да есть")
        self.assertIn("автокредит", main.quoted_message_context(message_data))

    def test_extract_inbound_text_reads_buttons_content(self) -> None:
        text, _ = main.extract_inbound_text(self.buttons_body("77000000013"))
        self.assertIn("каком языке", text)
        self.assertIn("Русский", text)

    def test_backfill_message_text_from_payload_restores_quoted_text(self) -> None:
        lead = self.create_lead("77000000014")
        body = self.quoted_body(str(lead["phone"]), "На этот номер напишите")
        main.save_message(lead["id"], lead["chat_id"], "in", "", body["idMessage"], body)
        with main.db_conn() as conn:
            updated = main.backfill_message_text_from_payloads(conn)
            row = conn.execute("SELECT text FROM messages WHERE contact_id = ?", (lead["id"],)).fetchone()
        self.assertEqual(updated, 1)
        self.assertEqual(row["text"], "На этот номер напишите")

    def test_name_near_phone_rejects_instruction_phrase(self) -> None:
        text = "Щас скину номер можете ему написать +77064162091"
        self.assertIsNone(main.extract_name_near_phone(text, "77064162091"))

    def test_name_near_phone_extracts_declared_name(self) -> None:
        text = "Его зовут Агыбай +77064162091"
        self.assertEqual(main.extract_name_near_phone(text, "77064162091"), "Агыбай")

    def test_user_message_sanitizer_replaces_lpr_term(self) -> None:
        text = main.sanitize_user_message_text("Если подскажете контакт ЛПР, напишу ЛПР напрямую.")
        self.assertNotIn("ЛПР", text)
        self.assertIn("ответственного", text)

    def test_parse_handoff_phones_accepts_multiple_recipients(self) -> None:
        phones = main.parse_handoff_phones("+77759419359, +77015001995")
        self.assertEqual(phones, ["77759419359", "77015001995"])

    def test_password_hash_verification(self) -> None:
        stored = main.hash_password("secure-password")
        self.assertTrue(main.verify_password("secure-password", stored))
        self.assertFalse(main.verify_password("wrong-password", stored))

    def test_same_phone_can_exist_in_different_projects(self) -> None:
        with main.use_project(1):
            first = self.create_lead("77000000015")
        with main.use_project(2):
            second = self.create_lead("77000000015")

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["project_id"], 1)
        self.assertEqual(second["project_id"], 2)

    def test_project_settings_are_isolated_by_project(self) -> None:
        with main.use_project(1):
            main.update_settings(
                {
                    "green_id_instance": "111111",
                    "green_api_token": "autoscore-token",
                    "ai_provider": "openai",
                }
            )
        with main.use_project(2):
            main.update_settings(
                {
                    "green_id_instance": "222222",
                    "green_api_token": "keramo-token",
                    "ai_provider": "deepseek",
                }
            )

        with main.use_project(1):
            autoscore_settings = main.get_settings()
        with main.use_project(2):
            keramo_settings = main.get_settings()

        self.assertEqual(autoscore_settings["green_id_instance"], "111111")
        self.assertEqual(autoscore_settings["green_api_token"], "autoscore-token")
        self.assertEqual(autoscore_settings["ai_provider"], "openai")
        self.assertEqual(keramo_settings["green_id_instance"], "222222")
        self.assertEqual(keramo_settings["green_api_token"], "keramo-token")
        self.assertEqual(keramo_settings["ai_provider"], "deepseek")

    def test_auto_work_settings_are_isolated_by_project(self) -> None:
        with main.use_project(1):
            main.update_settings({"auto_campaign_enabled": "true", "auto_campaign_time": "09:30"})
        with main.use_project(2):
            main.update_settings({"auto_campaign_enabled": "false", "auto_campaign_time": "15:45"})

        with main.use_project(1):
            autoscore_settings = main.get_settings()
        with main.use_project(2):
            keramo_settings = main.get_settings()

        self.assertEqual(autoscore_settings["auto_campaign_enabled"], "true")
        self.assertEqual(autoscore_settings["auto_campaign_time"], "09:30")
        self.assertEqual(keramo_settings["auto_campaign_enabled"], "false")
        self.assertEqual(keramo_settings["auto_campaign_time"], "15:45")

    def test_auto_campaign_due_respects_timezone_and_last_run(self) -> None:
        settings = main.DEFAULT_SETTINGS.copy()
        settings.update(
            {
                "auto_campaign_time": "10:00",
                "auto_campaign_timezone": "Asia/Qyzylorda",
                "auto_campaign_last_run_date": "",
            }
        )

        due_before, date_key, _, reason_before = main.auto_campaign_due(
            settings,
            datetime(2026, 5, 14, 4, 59, tzinfo=timezone.utc),
        )
        due_after, _, _, reason_after = main.auto_campaign_due(
            settings,
            datetime(2026, 5, 14, 5, 0, tzinfo=timezone.utc),
        )
        settings["auto_campaign_last_run_date"] = date_key
        due_again, _, _, reason_again = main.auto_campaign_due(
            settings,
            datetime(2026, 5, 14, 6, 0, tzinfo=timezone.utc),
        )

        self.assertFalse(due_before)
        self.assertEqual(reason_before, "too_early")
        self.assertTrue(due_after)
        self.assertEqual(reason_after, "due")
        self.assertFalse(due_again)
        self.assertEqual(reason_again, "already_ran")

    def test_project_is_resolved_from_greenapi_instance_id(self) -> None:
        with main.use_project(1):
            main.update_settings({"green_id_instance": "111111"})
        with main.use_project(2):
            main.update_settings({"green_id_instance": "222222"})
        main.set_setting("current_project_id", "1")

        body = self.inbound_body("77000000017", "Здравствуйте")
        body["instanceData"] = {"idInstance": "222222"}

        self.assertEqual(main.project_id_from_notification_body(body), 2)

    def test_reset_sales_data_only_clears_current_project(self) -> None:
        with main.use_project(1):
            first = self.create_lead("77000000016")
        with main.use_project(2):
            second = self.create_lead("77000000017")
            result = main.reset_sales_data()

        self.assertEqual(result["deleted_contacts"], 1)
        with main.use_project(1):
            self.assertIsNotNone(main.get_contact(first["id"]))
        with main.use_project(2):
            self.assertIsNone(main.get_contact(second["id"]))

    def test_reset_sales_data_clears_test_run_data_and_preserves_settings(self) -> None:
        lead = self.create_lead()
        lpr = main.create_or_update_contact(
            phone="77000000010",
            kind="lpr",
            source="test",
            owner_contact_id=lead["id"],
            status="lpr_ready",
            stage="new_lpr",
        )
        main.save_message(lead["id"], lead["chat_id"], "out", "Тест")
        main.log_event("contact", "Тестовый лог", contact_id=lead["id"])
        with main.db_conn() as conn:
            conn.execute(
                """
                INSERT INTO campaigns(status, target_kind, max_messages, delay_min_seconds, delay_max_seconds, created_at, updated_at)
                VALUES ('stopped', 'lead', 1, 1, 2, ?, ?)
                """,
                (main.now_iso(), main.now_iso()),
            )

        result = main.reset_sales_data()

        self.assertEqual(result["deleted_contacts"], 2)
        self.assertEqual(result["deleted_messages"], 1)
        self.assertEqual(result["deleted_campaigns"], 1)
        self.assertEqual(main.get_settings()["bot_name"], "Медет")
        self.assertIsNone(main.get_contact(lead["id"]))
        self.assertIsNone(main.get_contact(lpr["id"]))
        with main.db_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0], 0)


class KeramoProjectTests(TempDbMixin, unittest.TestCase):
    def test_second_project_is_keramo_build(self) -> None:
        project = main.get_project(main.KERAMO_PROJECT_ID)

        self.assertEqual(project["name"], "KERAMO BUILD")
        self.assertEqual(project["workflow_type"], main.KERAMO_WORKFLOW_TYPE)
        self.assertEqual(project["proposal_filename"], main.KERAMO_PROPOSAL_FILENAME)

    def test_keramo_campaign_greeting_uses_investor_context(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            lead = main.create_or_update_contact(
                phone="77000000055",
                kind="lead",
                source="krisha",
                company="Коммерческое помещение, Астана",
                status="ready",
                stage="imported",
                meta={
                    "sales_angle": "Вижу связь с коммерческой недвижимостью, поэтому аккуратно пишу по инвестиционной возможности KERAMO BUILD.",
                },
            )
            text = main.build_campaign_greeting(lead)

        self.assertIn("KERAMO BUILD", text)
        self.assertTrue("инвест" in text.lower() or "партнер" in text.lower())
        self.assertNotIn("автоскор", text.lower())

    def test_krisha_parser_extracts_filtered_commercial_lead(self) -> None:
        payload = main.KrishaImportRequest(
            source_text="",
            city="Астана",
            property_type="commercial",
            min_area=100,
            max_price=100_000_000,
        )
        sample = """
        Коммерческое помещение в Астана, бизнес-центр
        Площадь 120 м²
        Цена 65 000 000 ₸
        Собственник: +7 701 000 00 55
        https://krisha.kz/a/show/123456
        """

        leads = main.extract_krisha_leads_from_text(sample, payload)

        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["phone"], "77010000055")
        self.assertEqual(leads[0]["area"], 120)
        self.assertEqual(leads[0]["price"], 65_000_000)

    def test_krisha_parser_respects_max_contacts(self) -> None:
        payload = main.KrishaImportRequest(property_type="any", max_contacts=1)
        sample = """
        Commercial property owner: +7 701 000 00 55
        Another commercial property owner: +7 702 000 00 66
        """

        leads = main.extract_krisha_leads_from_text(sample, payload)

        self.assertEqual([lead["phone"] for lead in leads], ["77010000055"])

    def test_krisha_payload_from_settings_reads_max_contacts(self) -> None:
        settings = main.DEFAULT_SETTINGS.copy()
        settings["krisha_max_contacts"] = "12"

        payload = main.krisha_payload_from_settings(settings)

        self.assertEqual(payload.max_contacts, 12)

    def test_krisha_search_url_is_built_from_filters(self) -> None:
        payload = main.KrishaImportRequest(city="Астана", property_type="retail")

        urls = main.krisha_search_urls_from_payload(payload)

        self.assertEqual(urls, ["https://krisha.kz/prodazha/kommercheskaya-nedvizhimost/typi-magaziny_i_butiki/astana/"])

    def test_krisha_captcha_detector_ignores_footer_policy(self) -> None:
        html = """
        <p class="g-recaptcha-policy">Этот сайт защищен сервисом reCAPTCHA</p>
        <textarea name="g-recaptcha-response" style="display:none"></textarea>
        """

        self.assertFalse(main.is_captcha_page(html))


class DialogRuntimeTests(TempDbMixin, unittest.IsolatedAsyncioTestCase):
    async def test_krisha_browser_launch_failure_falls_back_to_http(self) -> None:
        payload = main.KrishaImportRequest(city="Астана", property_type="commercial")
        settings = main.DEFAULT_SETTINGS.copy()
        settings["krisha_use_browser"] = "true"
        settings["krisha_headless"] = "true"

        class FailingChromium:
            async def launch(self, *, headless: bool) -> None:
                raise RuntimeError("BrowserType.launch: Executable doesn't exist at /missing/chromium")

        class FakePlaywrightContext:
            async def __aenter__(self) -> object:
                return types.SimpleNamespace(chromium=FailingChromium())

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: FakePlaywrightContext()
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.async_api = fake_async_api

        with (
            patch.dict(sys.modules, {"playwright": fake_playwright, "playwright.async_api": fake_async_api}),
            patch.object(main, "collect_krisha_sources_httpx", AsyncMock(return_value=([{"source": "http", "text": "ok"}], []))) as http_mock,
        ):
            collected, errors = await main.collect_krisha_source_texts(payload, settings)

        self.assertEqual(collected, [{"source": "http", "text": "ok"}])
        self.assertTrue(any("Playwright Chromium не установлен" in error for error in errors))
        http_mock.assert_awaited_once()

    async def test_auto_work_creates_campaign_for_configured_project(self) -> None:
        main.runtime_tasks["campaign"] = None
        with main.use_project(1):
            main.update_settings(
                {
                    "green_id_instance": "111111",
                    "green_api_token": "token-1",
                    "auto_campaign_enabled": "true",
                    "auto_campaign_time": "00:00",
                    "auto_campaign_timezone": "UTC",
                    "auto_campaign_max_messages": "3",
                    "auto_campaign_delay_min_seconds": "1",
                    "auto_campaign_delay_max_seconds": "2",
                }
            )
            lead = main.create_or_update_contact(
                phone="77000000050",
                kind="lead",
                source="test",
                company="Autoscore",
                status="new",
                stage="new",
            )
            main.update_contact_fields(lead["id"], whatsapp_exists=1, status="ready")
        with main.use_project(2):
            main.update_settings(
                {
                    "green_id_instance": "222222",
                    "green_api_token": "token-2",
                    "auto_campaign_enabled": "false",
                }
            )
            other = main.create_or_update_contact(
                phone="77000000051",
                kind="lead",
                source="test",
                company="Keramo",
                status="new",
                stage="new",
            )
            main.update_contact_fields(other["id"], whatsapp_exists=1, status="ready")

        result = await main.run_auto_campaign_for_project(
            1,
            now_utc=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
            start_worker=False,
        )

        self.assertTrue(result["started"])
        with main.db_conn() as conn:
            campaigns = conn.execute("SELECT * FROM campaigns ORDER BY id").fetchall()
        self.assertEqual(len(campaigns), 1)
        self.assertEqual(int(campaigns[0]["project_id"]), 1)
        self.assertEqual(int(campaigns[0]["max_messages"]), 3)
        with main.use_project(1):
            self.assertEqual(main.get_settings()["auto_campaign_last_run_date"], "2026-05-14")
        with main.use_project(2):
            self.assertEqual(main.get_settings()["auto_campaign_last_run_date"], "")

    async def test_auto_work_waits_for_pending_whatsapp_check(self) -> None:
        main.runtime_tasks["campaign"] = None
        with main.use_project(1):
            main.update_settings(
                {
                    "green_id_instance": "111111",
                    "green_api_token": "token-1",
                    "auto_campaign_enabled": "true",
                    "auto_campaign_time": "00:00",
                    "auto_campaign_timezone": "UTC",
                }
            )
            main.create_or_update_contact(
                phone="77000000052",
                kind="lead",
                source="test",
                company="Pending WA",
                status="new",
                stage="new",
            )

        result = await main.run_auto_campaign_for_project(
            1,
            now_utc=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
            start_worker=False,
        )

        self.assertFalse(result["started"])
        self.assertEqual(result["reason"], "waiting_whatsapp_check")
        with main.db_conn() as conn:
            campaigns = conn.execute("SELECT * FROM campaigns").fetchall()
        self.assertEqual(len(campaigns), 0)
        with main.use_project(1):
            self.assertEqual(main.get_settings()["auto_campaign_last_run_date"], "")

    async def test_process_notification_handles_transfer_offer_without_generic_repeat(self) -> None:
        lead = self.create_lead("77000000024")

        with patch.object(
            main,
            "call_ai",
            AsyncMock(
                return_value={
                    "reply": "AI should not run here",
                    "stage": "continue",
                    "send_proposal": False,
                    "is_lpr": False,
                    "interested": False,
                    "lpr_phone": "",
                    "lpr_name": "",
                    "stop": False,
                }
            ),
        ) as ai_mock, patch.object(main, "send_and_log", AsyncMock()) as send_mock:
            await main.process_notification_body(
                self.inbound_body(
                    str(lead["phone"]),
                    "Здравствуйте, вы хотите получить автокредитование? Я могу вас связать с менеджером из отдела продаж",
                ),
                allow_outbound=True,
            )

        ai_mock.assert_not_called()
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("не оформляем автокредит", reply)
        self.assertTrue("свяж" in reply or "контакт" in reply or "номер" in reply)
        self.assertNotIn("кто у вас отвечает", reply)

    async def test_process_notification_answers_action_request_with_micro_cta(self) -> None:
        lead = self.create_lead("77000000025")
        main.save_message(
            lead["id"],
            lead["chat_id"],
            "out",
            "Если удобно, свяжите, пожалуйста, с менеджером или руководителем, кто отвечает за это направление.",
        )

        with patch.object(
            main,
            "call_ai",
            AsyncMock(
                return_value={
                    "reply": "AI should not run here",
                    "stage": "continue",
                    "send_proposal": False,
                    "is_lpr": False,
                    "interested": False,
                    "lpr_phone": "",
                    "lpr_name": "",
                    "stop": False,
                }
            ),
        ) as ai_mock, patch.object(main, "send_and_log", AsyncMock()) as send_mock:
            await main.process_notification_body(
                self.inbound_body(str(lead["phone"]), "Ок. Что требуется от меня?"),
                allow_outbound=True,
            )

        ai_mock.assert_not_called()
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertTrue("контакт" in reply or "номер" in reply or "перешл" in reply)
        self.assertNotIn("кто у вас отвечает", reply)

    async def test_process_notification_handles_future_transfer_phrase(self) -> None:
        lead = self.create_lead("77000000038")
        main.update_contact_fields(lead["id"], status="replied", stage="warmup_intro")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(
                self.inbound_body(str(lead["phone"]), "Я свяжу вас с менеджером отдела продаж"),
                allow_outbound=True,
            )

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("не оформляем автокредит", reply)
        self.assertTrue("свяж" in reply or "контакт" in reply or "номер" in reply)
        ai_mock.assert_not_called()

    async def test_process_notification_generic_interest_keeps_dialog_open(self) -> None:
        lead = self.create_lead("77000000026")

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "notify_handoff", AsyncMock(return_value=True)) as handoff_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Да, интересно"), allow_outbound=True)

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertTrue("что для вас" in reply or "что важнее" in reply or "как удобнее" in reply)
        proposal_mock.assert_not_awaited()
        handoff_mock.assert_awaited_once()

    async def test_notify_handoff_sends_to_all_configured_numbers(self) -> None:
        lead = self.create_lead("77000000040")
        main.update_settings(
            {
                "handoff_enabled": "true",
                "handoff_phone": "+77759419359, +77015001995",
            }
        )
        fake_green = type("FakeGreen", (), {})()
        fake_green.configured = True
        fake_green.send_message = AsyncMock(side_effect=[{"idMessage": "h1"}, {"idMessage": "h2"}])

        with patch.object(main, "GreenApiClient", return_value=fake_green):
            sent = await main.notify_handoff(lead, "Да, интересно", "test")

        self.assertTrue(sent)
        self.assertEqual(fake_green.send_message.await_count, 2)
        chat_ids = [call.args[0] for call in fake_green.send_message.await_args_list]
        self.assertEqual(chat_ids, ["77759419359@c.us", "77015001995@c.us"])

        updated = main.get_contact(lead["id"])
        meta = main.contact_meta(updated)
        self.assertEqual(meta["handoff_phones"], ["77759419359", "77015001995"])
        with main.db_conn() as conn:
            saved = conn.execute(
                "SELECT chat_id FROM messages WHERE contact_id IS NULL AND direction = 'out' ORDER BY id"
            ).fetchall()
        self.assertEqual([row["chat_id"] for row in saved], ["77759419359@c.us", "77015001995@c.us"])

    async def test_process_notification_warmup_first_reply_asks_autocredit_question(self) -> None:
        lead = self.create_lead("77000000031")
        main.update_contact_fields(lead["id"], status="sent", stage="warmup_permission")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Здравствуйте"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["stage"], "warmup_credit_check")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("есть продажи авто через автокредит", reply)
        self.assertNotIn("спасибо", reply)
        ai_mock.assert_not_called()

    async def test_process_notification_warmup_yes_path_reveals_offer_after_second_yes(self) -> None:
        lead = self.create_lead("77000000032")
        main.update_contact_fields(lead["id"], status="replied", stage="warmup_credit_check")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Да, есть"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["stage"], "warmup_intro")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertFalse(reply.lower().startswith("спасибо"))
        self.assertIn("Меня зовут Медет", reply)
        self.assertIn("цифровая заявка на автокредит", reply)
        self.assertIn("лучше обсудить с коллегой", reply)
        ai_mock.assert_not_called()

    async def test_process_notification_warmup_yes_detects_reversed_self_responsible_phrase(self) -> None:
        lead = self.create_lead("77000000039")
        main.update_contact_fields(lead["id"], status="replied", stage="warmup_credit_check")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Да, этим занимаюсь я"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["kind"], "lpr")
        self.assertEqual(updated["status"], "lpr_self")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("пришлю короткое КП сюда", reply)
        self.assertNotIn("лучше обсудить с коллегой", reply)
        ai_mock.assert_not_called()

    async def test_process_notification_warmup_no_path_closes_without_pitch(self) -> None:
        lead = self.create_lead("77000000033")
        main.update_contact_fields(lead["id"], status="replied", stage="warmup_credit_check")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Нет, не занимаемся"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["status"], "not_interested")
        self.assertEqual(updated["stage"], "closed_no_interest")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("не буду отвлекать", reply)
        self.assertNotIn("цифровая заявка", reply)
        ai_mock.assert_not_called()

    async def test_contact_lpr_starts_dialog_before_sending_proposal(self) -> None:
        lpr = main.create_or_update_contact(
            phone="77000000027",
            kind="lpr",
            source="test",
            company="Test Dealer",
            status="lpr_ready",
            stage="new_lpr",
        )

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "send_proposal", AsyncMock()) as proposal_mock:
            await main.contact_lpr(lpr)

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("ваш контакт передали", reply)
        self.assertTrue("это ваш контур" in reply or "это вы смотрите" in reply or "лучше обсудить" in reply)
        proposal_mock.assert_not_awaited()

    async def test_contact_lpr_does_not_send_duplicate_intro(self) -> None:
        lpr = main.create_or_update_contact(
            phone="77000000044",
            kind="lpr",
            source="test",
            company="Test Dealer",
            name="Агыбай",
            status="lpr_ready",
            stage="lpr_intro",
        )
        main.save_message(lpr["id"], lpr["chat_id"], "out", main.referred_lpr_intro_text(lpr))

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock:
            await main.contact_lpr(lpr)

        send_mock.assert_not_called()

    def test_latest_lpr_ready_for_owner_skips_already_contacted_lpr(self) -> None:
        lead = self.create_lead("77000000045")
        lpr = main.create_or_update_contact(
            phone="77000000046",
            kind="lpr",
            source="test",
            company="Test Dealer",
            owner_contact_id=lead["id"],
            status="lpr_ready",
            stage="new_lpr",
        )
        self.assertEqual(main.latest_lpr_ready_for_owner(lead["id"])["id"], lpr["id"])

        main.save_message(lpr["id"], lpr["chat_id"], "out", main.referred_lpr_intro_text(lpr))

        self.assertIsNone(main.latest_lpr_ready_for_owner(lead["id"]))

    async def test_process_notification_handles_unknown_lpr_name_gracefully(self) -> None:
        lead = self.create_lead()
        lpr = main.create_or_update_contact(
            phone="77000000003",
            kind="lpr",
            source="test",
            company="Test Dealer",
            owner_contact_id=lead["id"],
            status="lpr_needs_name",
            stage="new_lpr",
        )

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "contact_lpr", AsyncMock()) as contact_lpr_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Незнаю"), allow_outbound=True)

        updated_lpr = main.get_contact(lpr["id"])
        self.assertIsNotNone(updated_lpr)
        self.assertEqual(updated_lpr["status"], "lpr_ready")
        self.assertIsNone(updated_lpr["name"])
        send_mock.assert_awaited_once()
        self.assertIn("без имени", send_mock.await_args.args[1])
        contact_lpr_mock.assert_awaited_once()

    async def test_process_notification_owner_ack_after_name_request_contacts_lpr_without_reply(self) -> None:
        lead = self.create_lead("77000000013")
        lpr = main.create_or_update_contact(
            phone="77000000014",
            kind="lpr",
            source="test",
            company="Test Dealer",
            owner_contact_id=lead["id"],
            status="lpr_needs_name",
            stage="new_lpr",
        )

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "contact_lpr", AsyncMock()) as contact_lpr_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Ок, обратитесь к нему"), allow_outbound=True)

        updated_lpr = main.get_contact(lpr["id"])
        self.assertEqual(updated_lpr["status"], "lpr_ready")
        send_mock.assert_not_called()
        contact_lpr_mock.assert_awaited_once()

    async def test_owner_name_after_contact_card_does_not_recontact_lpr(self) -> None:
        lead = self.create_lead("77000000047")
        main.update_contact_fields(lead["id"], status="replied", stage="lpr_contact_shared")
        lpr = main.create_or_update_contact(
            phone="77000000048",
            kind="lpr",
            source="test",
            company="Test Dealer",
            name="Агыбай",
            owner_contact_id=lead["id"],
            status="lpr_ready",
            stage="lpr_intro",
        )
        main.save_message(lpr["id"], lpr["chat_id"], "out", main.referred_lpr_intro_text(lpr))

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "contact_lpr", AsyncMock()) as contact_lpr_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Агыбай"), allow_outbound=True)

        send_mock.assert_not_called()
        contact_lpr_mock.assert_not_called()
        ai_mock.assert_not_called()

    async def test_robot_question_after_lpr_contact_is_answered_without_recontact(self) -> None:
        lead = self.create_lead("77000000049")
        main.update_contact_fields(lead["id"], status="replied", stage="lpr_contact_shared")
        lpr = main.create_or_update_contact(
            phone="77000000050",
            kind="lpr",
            source="test",
            company="Test Dealer",
            name="Агыбай",
            owner_contact_id=lead["id"],
            status="lpr_ready",
            stage="lpr_intro",
        )
        main.save_message(lpr["id"], lpr["chat_id"], "out", main.referred_lpr_intro_text(lpr))

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "contact_lpr", AsyncMock()) as contact_lpr_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Вы робот?"), allow_outbound=True)

        send_mock.assert_awaited_once()
        self.assertIn("Меня зовут Медет", send_mock.await_args.args[1])
        contact_lpr_mock.assert_not_called()
        ai_mock.assert_not_called()

    async def test_resolve_person_name_can_use_ai_to_validate_name(self) -> None:
        main.update_settings({"ai_provider": "deepseek", "deepseek_api_key": "test-key"})

        with patch.object(main, "call_ai_json", AsyncMock(return_value={"is_name": False, "name": ""})) as ai_mock:
            name = await main.resolve_person_name("можете ему написать", context="test")

        self.assertIsNone(name)
        ai_mock.assert_awaited_once()

        with patch.object(main, "call_ai_json", AsyncMock(return_value={"is_name": True, "name": "Агыбай"})):
            name = await main.resolve_person_name("его зовут агыбай", context="test")

        self.assertEqual(name, "Агыбай")

    async def test_process_notification_answers_identity_before_ai(self) -> None:
        lead = self.create_lead("77000000004")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "А вы кто вообще?"), allow_outbound=True)

        send_mock.assert_awaited_once()
        self.assertIn("Меня зовут Медет", send_mock.await_args.args[1])
        ai_mock.assert_not_called()

    async def test_process_notification_asks_for_email_before_ai(self) -> None:
        lead = self.create_lead("77000000005")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Скиньте КП на почту"), allow_outbound=True)

        send_mock.assert_awaited_once()
        self.assertIn("почту", send_mock.await_args.args[1].lower())
        ai_mock.assert_not_called()

    async def test_process_notification_clarifies_confusion_politely_before_ai(self) -> None:
        lead = self.create_lead("77000000021")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Не понял"), allow_outbound=True)

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("Коротко:", reply)
        self.assertNotIn("поясню проще", reply.lower())
        ai_mock.assert_not_called()

    async def test_process_notification_self_lpr_waits_for_proposal_consent(self) -> None:
        lead = self.create_lead("77000000022")
        main.save_message(
            lead["id"],
            lead["chat_id"],
            "out",
            "Подскажите, пожалуйста, кто у вас отвечает за это направление?",
        )

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Ну я"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["kind"], "lpr")
        self.assertEqual(updated["status"], "lpr_self")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("пришлю короткое КП сюда", reply)
        self.assertNotIn("почт", reply.lower())
        proposal_mock.assert_not_awaited()
        ai_mock.assert_not_called()

    async def test_process_notification_self_lpr_phrase_beats_generic_interest(self) -> None:
        lead = self.create_lead("77000000025")

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Я этим занимаюсь"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["kind"], "lpr")
        self.assertEqual(updated["status"], "lpr_self")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("пришлю короткое КП сюда", reply)
        self.assertNotIn("что для вас сейчас важнее", reply.lower())
        proposal_mock.assert_not_awaited()
        ai_mock.assert_not_called()

    async def test_process_notification_meeting_interest_after_discovery_moves_to_scheduling(self) -> None:
        lead = self.create_lead("77000000026")
        main.update_contact_fields(lead["id"], status="interested_pending", stage="interest_dialog")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Можно короткий созвон завтра?"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["status"], "interested")
        self.assertEqual(updated["stage"], "interest_dialog")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("до обеда", reply.lower())
        self.assertNotIn("что для вас сейчас важнее", reply.lower())
        ai_mock.assert_not_called()

    async def test_process_notification_self_lpr_yes_after_offer_sends_proposal(self) -> None:
        lead = self.create_lead("77000000027")
        lpr = main.mark_contact_as_lpr(lead, "test_self_lpr_followup")
        main.save_message(lpr["id"], lpr["chat_id"], "out", main.proposal_offer_reply())
        ai_action = main.normalize_ai_action({
            "reply": "Отлично, прикреплю короткое КП. После ознакомления можем обсудить, насколько такой формат вам подходит.",
            "stage": "send_proposal",
            "send_proposal": True,
            "is_lpr": True,
        })

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock(return_value=ai_action)) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lpr["phone"]), "Да, пришлите сюда"), allow_outbound=True)

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("прикреплю короткое КП", reply)
        proposal_mock.assert_awaited_once()
        ai_mock.assert_not_called()

    async def test_process_notification_self_lpr_ok_after_offer_sends_proposal(self) -> None:
        lead = self.create_lead("77000000029")
        lpr = main.mark_contact_as_lpr(lead, "test_self_lpr_ok_followup")
        main.save_message(lpr["id"], lpr["chat_id"], "out", main.proposal_offer_reply())
        ai_action = main.normalize_ai_action({
            "reply": "Отлично, прикреплю короткое КП. После ознакомления можем обсудить, насколько такой формат вам подходит.",
            "stage": "send_proposal",
            "send_proposal": True,
            "is_lpr": True,
        })

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock(return_value=ai_action)) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lpr["phone"]), "Ок"), allow_outbound=True)

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("прикреплю короткое КП", reply)
        proposal_mock.assert_awaited_once()
        ai_mock.assert_not_called()

    async def test_process_notification_uses_ai_intent_for_proposal_consent_when_available(self) -> None:
        main.update_settings({"deepseek_api_key": "test-key"})
        lead = self.create_lead("77000000036")
        lpr = main.mark_contact_as_lpr(lead, "test_ai_intent_consent")
        main.save_message(lpr["id"], lpr["chat_id"], "out", main.proposal_offer_reply())

        with (
            patch.object(main, "call_ai_json", AsyncMock(return_value={"proposal_consent": True})) as intent_mock,
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lpr["phone"]), "Ок"), allow_outbound=True)

        intent_mock.assert_awaited()
        send_mock.assert_awaited_once()
        self.assertIn("прикреплю короткое КП", send_mock.await_args.args[1])
        proposal_mock.assert_awaited_once()
        ai_mock.assert_not_called()

    async def test_process_notification_self_lpr_imperative_after_offer_sends_proposal(self) -> None:
        lead = self.create_lead("77000000030")
        lpr = main.mark_contact_as_lpr(lead, "test_self_lpr_imperative_followup")
        main.save_message(lpr["id"], lpr["chat_id"], "out", main.proposal_offer_reply())
        ai_action = main.normalize_ai_action({
            "reply": "Отлично, прикреплю короткое КП. После ознакомления можем обсудить, насколько такой формат вам подходит.",
            "stage": "send_proposal",
            "send_proposal": True,
            "is_lpr": True,
        })

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock(return_value=ai_action)) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lpr["phone"]), "Отправляй!"), allow_outbound=True)

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("прикреплю короткое КП", reply)
        proposal_mock.assert_awaited_once()
        ai_mock.assert_not_called()

    async def test_process_notification_self_lpr_acknowledgement_after_offer_sends_proposal(self) -> None:
        lead = self.create_lead("77000000031")
        lpr = main.mark_contact_as_lpr(lead, "test_self_lpr_ack_followup")
        main.save_message(lpr["id"], lpr["chat_id"], "out", main.proposal_offer_reply())
        ai_action = main.normalize_ai_action({
            "reply": "Отлично, прикреплю короткое КП. После ознакомления можем обсудить, насколько такой формат вам подходит.",
            "stage": "send_proposal",
            "send_proposal": True,
            "is_lpr": True,
        })

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock(return_value=ai_action)) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lpr["phone"]), "Принял, спасибо"), allow_outbound=True)

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("прикреплю короткое КП", reply)
        proposal_mock.assert_awaited_once()
        ai_mock.assert_not_called()

    async def test_process_notification_detail_request_does_not_end_dialog(self) -> None:
        lead = self.create_lead("77000000028")
        main.update_contact_fields(lead["id"], status="interested", stage="handoff")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Уточните подробнее"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["status"], "interested")
        self.assertEqual(updated["stage"], "interest_dialog")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertTrue("коротко:" in reply or "white-label" in reply or "предварительный скоринг" in reply)
        self.assertTrue("что для вас" in reply or "что для вас сейчас" in reply)
        ai_mock.assert_not_called()

    async def test_process_notification_qualification_answer_moves_forward_without_repeating_question(self) -> None:
        lead = self.create_lead("77000000040")
        main.update_contact_fields(lead["id"], status="replied", stage="clarified_offer", attempts=5)
        main.save_message(
            lead["id"],
            lead["chat_id"],
            "out",
            "Коротко: это white-label приложение автодилера. Что для вас сейчас важнее: больше заявок или быстрее обработка анкет?",
        )

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "быстрее обработка"), allow_outbound=True)

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("быстрее", reply)
        self.assertIn("кто у вас отвечает", reply)
        self.assertNotIn("что для вас сейчас важнее", reply)
        proposal_mock.assert_not_awaited()
        ai_mock.assert_not_called()

    async def test_process_notification_strips_repeated_name_from_ai_followup(self) -> None:
        lead = self.create_lead("77000000023")
        main.save_message(
            lead["id"],
            lead["chat_id"],
            "out",
            "Здравствуйте! Меня зовут Медет. Пишу по коммерческому вопросу для Test Dealer.",
        )
        action = {
            "reply": "Медет, пишу по white-label приложению для автокредитов под бренд дилера. Подскажите, этим у вас занимается коммерция?",
            "stage": "continue",
            "send_proposal": False,
            "is_lpr": False,
            "interested": False,
            "lpr_phone": "",
            "lpr_name": "",
            "stop": False,
        }

        with patch.object(main, "call_ai", AsyncMock(return_value=action)), patch.object(main, "send_and_log", AsyncMock()) as send_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Здравствуйте. По какому вопросу?"), allow_outbound=True)

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertFalse(reply.startswith("Медет"))
        self.assertNotIn("Меня зовут Медет", reply)

    async def test_process_notification_soft_negative_is_not_hard_opt_out(self) -> None:
        lead = self.create_lead("77000000006")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Пока не актуально"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["status"], "not_interested")
        self.assertEqual(updated["stage"], "closed_no_interest")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertNotIn("вернуться к вопросу", reply.lower())
        self.assertNotIn("на связи", reply.lower())

    async def test_process_notification_email_with_address_stores_email_and_asks_focus(self) -> None:
        lead = self.create_lead("77000000007")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(
                self.inbound_body(str(lead["phone"]), "Пришлите КП на почту test@example.com"),
                allow_outbound=True,
            )

        updated = main.get_contact(lead["id"])
        meta = main.contact_meta(updated)
        self.assertEqual(meta["requested_email"], "test@example.com")
        self.assertIn("рост заявок", send_mock.await_args.args[1])
        ai_mock.assert_not_called()

    async def test_process_notification_ignores_image_without_caption(self) -> None:
        lead = self.create_lead("77000000008")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(self.image_body(str(lead["phone"])), allow_outbound=True)

        send_mock.assert_not_called()
        ai_mock.assert_not_called()

    async def test_process_notification_does_not_promise_second_proposal(self) -> None:
        lpr = main.create_or_update_contact(
            phone="77000000012",
            kind="lpr",
            source="test",
            company="Test Dealer",
            status="proposal_sent",
            stage="proposal_sent",
        )
        main.update_contact_fields(lpr["id"], proposal_sent=1)
        action = {
            "reply": "Понял, тогда отправлю короткое КП.",
            "stage": "send_proposal",
            "send_proposal": True,
            "is_lpr": False,
            "interested": False,
            "lpr_phone": "",
            "lpr_name": "",
            "stop": False,
        }

        with (
            patch.object(main, "call_ai", AsyncMock(return_value=action)) as ai_mock,
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lpr["phone"]), "Ок, прочитаю"), allow_outbound=True)

        ai_mock.assert_not_called()
        send_mock.assert_not_called()
        proposal_mock.assert_not_called()

    async def test_process_notification_after_proposal_yes_is_silent(self) -> None:
        lpr = main.create_or_update_contact(
            phone="77000000034",
            kind="lpr",
            source="test",
            company="Test Dealer",
            status="proposal_sent",
            stage="proposal_sent",
        )
        main.update_contact_fields(lpr["id"], proposal_sent=1)

        with (
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lpr["phone"]), "Да"), allow_outbound=True)

        ai_mock.assert_not_called()
        send_mock.assert_not_called()

    async def test_process_notification_after_proposal_answers_already_sent_question(self) -> None:
        lead = self.create_lead("77000000037")
        main.update_contact_fields(lead["id"], proposal_sent=1, status="interested", stage="handoff")

        with (
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Вы же уже отправили?"), allow_outbound=True)

        ai_mock.assert_not_called()
        proposal_mock.assert_not_called()
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("КП уже отправил", reply)
        self.assertNotIn("что для вас сейчас важнее", reply.lower())

    async def test_process_notification_after_proposal_benefit_question_does_not_repeat_focus(self) -> None:
        lead = self.create_lead("77000000041")
        main.update_contact_fields(lead["id"], proposal_sent=1, status="interested", stage="handoff")

        with (
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "а что это нам даст?"), allow_outbound=True)

        ai_mock.assert_not_called()
        proposal_mock.assert_not_called()
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("меньше ручного", reply)
        self.assertNotIn("что для вас сейчас важнее", reply)
        self.assertNotIn("больше заявок или быстрее обработка", reply)

    async def test_process_notification_after_proposal_credit_matching_question_is_answered_directly(self) -> None:
        lead = self.create_lead("77000000042")
        main.update_contact_fields(lead["id"], proposal_sent=1, status="interested", stage="handoff")

        with (
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
        ):
            await main.process_notification_body(
                self.inbound_body(str(lead["phone"]), "а система сама подбирает автокредит клиенту?"),
                allow_outbound=True,
            )

        ai_mock.assert_not_called()
        proposal_mock.assert_not_called()
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("предварительную оценку", reply)
        self.assertIn("финальное решение", reply)
        self.assertNotIn("что для вас сейчас важнее", reply)

    async def test_process_notification_after_proposal_forward_offer_and_ok_sends_summary(self) -> None:
        lead = self.create_lead("77000000043")
        main.update_contact_fields(lead["id"], proposal_sent=1, status="interested", stage="handoff")

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(
                self.inbound_body(str(lead["phone"]), "я сам передам руководителю"),
                allow_outbound=True,
            )

        ai_mock.assert_not_called()
        send_mock.assert_awaited_once()
        offer = send_mock.await_args.args[1].lower()
        self.assertIn("текст для руководителя", offer)
        self.assertNotIn("спасибо", offer)

        main.save_message(lead["id"], lead["chat_id"], "out", send_mock.await_args.args[1])
        with (
            patch.object(main, "send_and_log", AsyncMock()) as summary_mock,
            patch.object(main, "notify_handoff", AsyncMock(return_value=True)) as handoff_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "ок"), allow_outbound=True)

        ai_mock.assert_not_called()
        proposal_mock.assert_not_called()
        summary_mock.assert_awaited_once()
        summary = summary_mock.await_args.args[1].lower()
        self.assertIn("для руководителя", summary)
        self.assertIn("предварительный скоринг", summary)
        handoff_mock.assert_awaited_once()

    async def test_process_notification_rewrites_ai_reoffer_after_proposal(self) -> None:
        lpr = main.create_or_update_contact(
            phone="77000000035",
            kind="lpr",
            source="test",
            company="Test Dealer",
            status="proposal_sent",
            stage="proposal_sent",
        )
        main.update_contact_fields(lpr["id"], proposal_sent=1)
        ai_action = main.normalize_ai_action(
            {
                "reply": "Отлично. Могу отправить короткое КП сюда — вам важнее рост заявок на автокредит или скорость обработки анкет?",
                "stage": "continue",
                "send_proposal": False,
            }
        )

        with (
            patch.object(main, "call_ai", AsyncMock(return_value=ai_action)) as ai_mock,
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lpr["phone"]), "Что там главное?"), allow_outbound=True)

        ai_mock.assert_awaited_once()
        proposal_mock.assert_not_awaited()
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("КП уже отправил", reply)
        self.assertNotIn("Могу отправить короткое КП", reply)

    async def test_process_notification_quoted_warmup_yes_advances(self) -> None:
        lead = self.create_lead("77000000044")
        main.update_contact_fields(lead["id"], status="replied", stage="warmup_credit_check")

        with (
            patch.object(main, "call_ai_json", AsyncMock(return_value=None)),
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
        ):
            await main.process_notification_body(self.quoted_body(str(lead["phone"]), "Да есть"), allow_outbound=True)

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("пишу по решению", reply)

    async def test_process_notification_buttons_language_prompt_replies_in_russian(self) -> None:
        lead = self.create_lead("77000000045")
        main.update_contact_fields(lead["id"], status="sent", stage="warmup_permission")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock:
            await main.process_notification_body(self.buttons_body(str(lead["phone"])), allow_outbound=True)

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("на русском", reply)
        self.assertNotIn("автокредит", reply)

    async def test_process_notification_audio_transcription_is_used(self) -> None:
        lead = self.create_lead("77000000046")
        main.update_contact_fields(lead["id"], status="replied", stage="warmup_credit_check")

        with (
            patch.object(main, "transcribe_audio_message", AsyncMock(return_value="Да, есть")) as transcribe_mock,
            patch.object(main, "call_ai_json", AsyncMock(return_value=None)),
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
        ):
            await main.process_notification_body(self.audio_body(str(lead["phone"])), allow_outbound=True)

        transcribe_mock.assert_awaited_once()
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("пишу по решению", reply)

    async def test_process_notification_request_our_contact_does_not_loop(self) -> None:
        lead = self.create_lead("77000000047")
        main.update_contact_fields(lead["id"], status="replied", stage="warmup_intro")

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "notify_handoff", AsyncMock(return_value=True)) as handoff_mock,
        ):
            await main.process_notification_body(
                self.inbound_body(str(lead["phone"]), "Оставьте ваши контакты, менеджер сам с вами свяжется"),
                allow_outbound=True,
            )

        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("+77759419359", reply)
        self.assertNotIn("что для вас сейчас важнее", reply.lower())
        handoff_mock.assert_awaited_once()

    async def test_process_notification_after_proposal_specialist_offer_does_not_schedule(self) -> None:
        lead = self.create_lead("77000000048")
        main.update_contact_fields(lead["id"], proposal_sent=1, status="proposal_sent", stage="proposal_sent")

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "notify_handoff", AsyncMock()) as handoff_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(
                self.inbound_body(
                    str(lead["phone"]),
                    "Я не могу принимать коммерческие предложения, но могу предоставить контакты специалиста.",
                ),
                allow_outbound=True,
            )

        ai_mock.assert_not_awaited()
        handoff_mock.assert_not_awaited()
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("номер специалиста", reply)
        self.assertNotIn("слот", reply)
