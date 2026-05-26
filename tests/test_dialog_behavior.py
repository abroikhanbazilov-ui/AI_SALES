from __future__ import annotations

import json
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
        main.campaign_tasks.clear()
        main.poller_tasks.clear()
        main.ai_sync_tasks.clear()
        main.ai_states.clear()
        main.runtime_tasks["campaign"] = None
        main.runtime_tasks["poller"] = None
        main.runtime_tasks["ai_sync"] = None
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

    def test_newer_inbound_message_detector_marks_stale_message(self) -> None:
        lead = self.create_lead("77000000072")
        main.save_message(lead["id"], lead["chat_id"], "in", "Ассалаумагаликем", "old-msg")
        main.save_message(lead["id"], lead["chat_id"], "in", "Қандай кредит?", "new-msg")

        self.assertTrue(main.has_newer_inbound_message(lead["id"], "old-msg"))
        self.assertFalse(main.has_newer_inbound_message(lead["id"], "new-msg"))

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
                "auto_campaign_timezone": "Asia/Almaty",
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

    def test_keramo_interested_reply_offers_proposal_before_budget_check(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            reply = main.interested_reply_text({}, "Интересно")

        self.assertIn("пришлю короткое КП", reply)
        self.assertNotIn("чек 35 млн", reply)

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

    def test_krisha_detail_url_limit_tracks_contact_target(self) -> None:
        self.assertEqual(main.krisha_detail_url_limit(main.KrishaImportRequest(max_contacts=3)), 10)
        self.assertEqual(main.krisha_detail_url_limit(main.KrishaImportRequest(max_contacts=20)), 40)
        self.assertEqual(main.krisha_detail_url_limit(main.KrishaImportRequest()), 80)

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

    def test_krisha_captcha_detector_catches_phone_challenge(self) -> None:
        html = "Чтобы&nbsp;увидеть номер телефона, нажмите на&nbsp;кнопку «Я&nbsp;не&nbsp;робот»"

        self.assertTrue(main.is_captcha_page(html))

    def test_krisha_search_page_empty_results_detector(self) -> None:
        html = "<div>По вашему запросу ничего не найдено</div>"

        self.assertTrue(main.krisha_search_page_has_empty_results(html))

    def test_krisha_sanitized_storage_state_filters_external_origins(self) -> None:
        storage_state_path = Path(self.temp_dir.name) / "krisha_storage_state.json"
        storage_state_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "krisha", "value": "1", "domain": ".krisha.kz", "path": "/"},
                        {"name": "google", "value": "1", "domain": ".google.com", "path": "/"},
                    ],
                    "origins": [
                        {"origin": "https://krisha.kz", "localStorage": [{"name": "token", "value": "1"}]},
                        {"origin": "https://www.google.com", "localStorage": [{"name": "noise", "value": "1"}]},
                        {"origin": "https://id.kolesa.kz", "localStorage": [{"name": "idk", "value": "1"}]},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        sanitized = main.krisha_sanitized_storage_state(storage_state_path)

        self.assertIsNotNone(sanitized)
        self.assertEqual([item["domain"] for item in sanitized["cookies"]], [".krisha.kz"])
        self.assertEqual(
            [item["origin"] for item in sanitized["origins"]],
            ["https://krisha.kz", "https://id.kolesa.kz"],
        )

    def test_krisha_reimport_rechecks_stale_whatsapp_status(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            payload = main.KrishaImportRequest(city="Астана", property_type="commercial", import_contacts=True)
            stale = main.create_or_update_contact(
                phone="77000000066",
                kind="lead",
                source="krisha",
                company="Old",
                status="krisha_stale",
                stage="krisha_stale",
            )
            main.update_contact_fields(stale["id"], whatsapp_exists=0)

            imported, updated = main.save_krisha_leads(
                [
                    {
                        "phone": "77000000066",
                        "phone_raw": "+7 700 000 00 66",
                        "title": "Коммерческое помещение",
                        "city": "Астана",
                        "source_url": "https://krisha.kz/a/show/1",
                    }
                ],
                payload,
                main.KERAMO_PROJECT_ID,
            )

            refreshed = main.get_contact(stale["id"])

        self.assertEqual((imported, updated), (0, 1))
        self.assertEqual(refreshed["status"], "new")
        self.assertEqual(refreshed["stage"], "imported")
        self.assertIsNone(refreshed["whatsapp_exists"])

    def test_krisha_reimport_preserves_already_sent_contact(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            payload = main.KrishaImportRequest(city="Астана", property_type="commercial", import_contacts=True)
            sent = main.create_or_update_contact(
                phone="77000000067",
                kind="lead",
                source="krisha",
                company="Already sent",
                status="sent",
                stage="waiting_reply",
            )
            main.update_contact_fields(sent["id"], whatsapp_exists=1)

            imported, updated = main.save_krisha_leads(
                [
                    {
                        "phone": "77000000067",
                        "phone_raw": "+7 700 000 00 67",
                        "title": "Коммерческое помещение",
                        "city": "Астана",
                        "source_url": "https://krisha.kz/a/show/2",
                    }
                ],
                payload,
                main.KERAMO_PROJECT_ID,
            )

            refreshed = main.get_contact(sent["id"])

        self.assertEqual((imported, updated), (0, 1))
        self.assertEqual(refreshed["status"], "sent")
        self.assertEqual(refreshed["stage"], "waiting_reply")
        self.assertEqual(refreshed["whatsapp_exists"], 1)


class DialogRuntimeTests(TempDbMixin, unittest.IsolatedAsyncioTestCase):
    @patch("app.main.krisha_solve_recaptcha_audio", new_callable=AsyncMock)
    async def test_krisha_wait_for_manual_captcha_saves_state_after_resolution(self, mock_audio_solve: AsyncMock) -> None:
        mock_audio_solve.return_value = False
        class FakePage:
            def __init__(self) -> None:
                self.url = "https://krisha.kz/a/show/1009057062"
                self._attempt = 0

            async def content(self) -> str:
                self._attempt += 1
                if self._attempt < 3:
                    return "Чтобы увидеть номер телефона, нажмите на кнопку Я не робот"
                return "<html><body>+7 777 000 00 01</body></html>"

            async def wait_for_timeout(self, _ms: int) -> None:
                return None

        page = FakePage()
        context = types.SimpleNamespace(storage_state=AsyncMock())

        resolved = await main.krisha_wait_for_manual_captcha(
            page,
            context,
            Path("krisha_state.json"),
            stage="after_phone_click",
            timeout_seconds=5,
            settings={"krisha_headless": "false"},
        )

        self.assertTrue(resolved)
        context.storage_state.assert_awaited_once_with(path="krisha_state.json")

    @patch("app.main.krisha_solve_recaptcha_audio", new_callable=AsyncMock)
    async def test_krisha_wait_for_manual_captcha_times_out_when_challenge_persists(self, mock_audio_solve: AsyncMock) -> None:
        mock_audio_solve.return_value = False
        class FakePage:
            url = "https://krisha.kz/a/show/1009057062"

            async def content(self) -> str:
                return "Чтобы увидеть номер телефона, нажмите на кнопку Я не робот"

            async def wait_for_timeout(self, _ms: int) -> None:
                return None

        context = types.SimpleNamespace(storage_state=AsyncMock())

        resolved = await main.krisha_wait_for_manual_captcha(
            FakePage(),
            context,
            Path("krisha_state.json"),
            stage="after_phone_click",
            timeout_seconds=3,
            settings={"krisha_headless": "false"},
        )

        self.assertFalse(resolved)
        context.storage_state.assert_not_awaited()

    @patch("app.main.krisha_solve_recaptcha_audio", new_callable=AsyncMock)
    async def test_krisha_wait_for_manual_captcha_audio_solve_success(self, mock_audio_solve: AsyncMock) -> None:
        mock_audio_solve.return_value = True
        class FakePage:
            url = "https://krisha.kz/a/show/1009057062"
            async def content(self) -> str:
                return "Чтобы увидеть номер телефона, нажмите на кнопку Я не робот"

        context = types.SimpleNamespace(storage_state=AsyncMock())

        resolved = await main.krisha_wait_for_manual_captcha(
            FakePage(),
            context,
            Path("krisha_state.json"),
            stage="after_phone_click",
            timeout_seconds=3,
        )

        self.assertTrue(resolved)
        context.storage_state.assert_awaited_once_with(path="krisha_state.json")

    def test_normalize_recaptcha_audio_answer_strips_stt_noise(self) -> None:
        self.assertEqual(
            main.normalize_recaptcha_audio_answer(" If foreclosure radar disappear. "),
            "if foreclosure radar disappear",
        )
        self.assertEqual(
            main.normalize_recaptcha_audio_answer("&quot;Seven, nine.&quot;"),
            "seven nine",
        )

    async def test_krisha_click_recaptcha_reload_returns_false_when_disabled(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.waits = 0

            async def wait_for_timeout(self, _ms: int) -> None:
                self.waits += 1

        class FakeReloadLocator:
            @property
            def first(self) -> "FakeReloadLocator":
                return self

            async def count(self) -> int:
                return 1

            async def wait_for(self, state: str = "visible", timeout: int = 0) -> None:
                return None

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return False

            async def click(self, timeout: int | None = None) -> None:
                raise AssertionError("disabled reload button must not be clicked")

        class FakeChallengeFrame:
            def locator(self, selector: str) -> FakeReloadLocator:
                self.last_selector = selector
                return FakeReloadLocator()

        page = FakePage()
        clicked = await main.krisha_click_recaptcha_reload(page, FakeChallengeFrame(), stage="after_phone_click")

        self.assertFalse(clicked)
        self.assertEqual(page.waits, 3)

    async def test_krisha_find_recaptcha_challenge_frame_falls_back_to_bframe_without_audio_button(self) -> None:
        class FakeCountLocator:
            def __init__(self, count: int) -> None:
                self._count = count

            async def count(self) -> int:
                return self._count

        class FakeInnerLocator:
            async def count(self) -> int:
                return 0

        class FakeFrame:
            def locator(self, selector: str) -> FakeInnerLocator:
                self.last_selector = selector
                return FakeInnerLocator()

        class FakeFrameLocator:
            def __init__(self, frame: FakeFrame) -> None:
                self.frame = frame

            def nth(self, index: int) -> FakeFrame:
                self.last_index = index
                return self.frame

        class FakePage:
            def __init__(self) -> None:
                self.frame = FakeFrame()

            def locator(self, selector: str) -> FakeCountLocator:
                return FakeCountLocator(1 if "recaptcha/api2/bframe" in selector else 0)

            def frame_locator(self, selector: str) -> FakeFrameLocator:
                self.last_frame_selector = selector
                return FakeFrameLocator(self.frame)

        page = FakePage()
        frame, selector = await main.krisha_find_recaptcha_challenge_frame(page)

        self.assertIs(frame, page.frame)
        self.assertEqual(selector, "iframe[src*='recaptcha/api2/bframe'] nth(0)")

    async def test_krisha_solve_recaptcha_audio_retries_when_challenge_frame_is_not_ready(self) -> None:
        class FakeCountLocator:
            async def count(self) -> int:
                return 0

            def nth(self, index: int) -> "FakeCountLocator":
                return self

            async def get_attribute(self, name: str, timeout: int | None = None) -> str:
                return ""

        class FakeFrameLocator:
            def nth(self, index: int) -> "FakeFrameLocator":
                return self

            def locator(self, selector: str) -> FakeCountLocator:
                return FakeCountLocator()

        class FakePage:
            def __init__(self) -> None:
                self.waits: list[int] = []

            def locator(self, selector: str) -> FakeCountLocator:
                return FakeCountLocator()

            def frame_locator(self, selector: str) -> FakeFrameLocator:
                return FakeFrameLocator()

            async def wait_for_timeout(self, ms: int) -> None:
                self.waits.append(ms)

            async def content(self) -> str:
                return "captcha"

        page = FakePage()
        with (
            patch.object(main, "krisha_find_recaptcha_challenge_frame", AsyncMock(return_value=(None, None))) as challenge_mock,
            patch.object(main, "is_captcha_page", side_effect=lambda content: content == "captcha"),
        ):
            solved = await main.krisha_solve_recaptcha_audio(
                page,
                object(),
                {"groq_api_key": "test-key"},
                stage="after_phone_click",
                max_retries=2,
            )

        self.assertFalse(solved)
        self.assertEqual(challenge_mock.await_count, 2)
        self.assertGreaterEqual(page.waits.count(3000), 2)

    async def test_krisha_solve_recaptcha_audio_reuses_active_audio_challenge_on_retry(self) -> None:
        class FakeIframeElement:
            def __init__(self, src: str, title: str, name: str) -> None:
                self.attrs = {"src": src, "title": title, "name": name}

            async def get_attribute(self, name: str, timeout: int | None = None) -> str:
                return self.attrs.get(name, "")

        class FakeIframeList:
            def __init__(self) -> None:
                self.items = [
                    FakeIframeElement("https://www.google.com/recaptcha/api2/anchor", "reCAPTCHA", "anchor"),
                    FakeIframeElement("https://www.google.com/recaptcha/api2/bframe", "reCAPTCHA challenge", "challenge"),
                ]

            async def count(self) -> int:
                return len(self.items)

            def nth(self, index: int) -> FakeIframeElement:
                return self.items[index]

        class BaseLocator:
            def __init__(self, *, visible: bool = True) -> None:
                self.visible = visible
                self.click_count = 0

            @property
            def first(self) -> "BaseLocator":
                return self

            async def count(self) -> int:
                return 1

            async def wait_for(self, state: str = "visible", timeout: int = 0) -> None:
                return None

            async def is_visible(self) -> bool:
                return self.visible

            async def click(self) -> None:
                self.click_count += 1

            async def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
                return None

            async def text_content(self) -> str:
                return ""

            async def fill(self, value: str) -> None:
                return None

            async def type(self, value: str, delay: int = 0) -> None:
                return None

        class FakeCheckboxLocator(BaseLocator):
            async def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
                if name == "aria-checked":
                    return "false"
                return await super().get_attribute(name, timeout=timeout)

        class FakeAudioButtonLocator(BaseLocator):
            def __init__(self, frame: "FakeChallengeFrame") -> None:
                super().__init__()
                self.frame = frame

            async def click(self) -> None:
                self.click_count += 1
                if self.frame.audio_mode_active:
                    raise AssertionError("audio button was clicked after audio mode was already active")
                self.frame.audio_mode_active = True

        class FakeAudioSourceLocator(BaseLocator):
            def __init__(self, frame: "FakeChallengeFrame") -> None:
                super().__init__()
                self.frame = frame

            async def count(self) -> int:
                return 1 if self.frame.audio_mode_active else 0

            async def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
                if name == "src" and self.frame.audio_mode_active:
                    return f"https://google.test/audio-{self.frame.reload_clicks}.mp3"
                return await super().get_attribute(name, timeout=timeout)

        class FakeResponseInputLocator(BaseLocator):
            def __init__(self, frame: "FakeChallengeFrame") -> None:
                super().__init__()
                self.frame = frame
                self.typed_values: list[str] = []

            async def count(self) -> int:
                return 1 if self.frame.audio_mode_active else 0

            async def fill(self, value: str) -> None:
                return None

            async def type(self, value: str, delay: int = 0) -> None:
                self.typed_values.append(value)

        class FakeReloadLocator(BaseLocator):
            def __init__(self, frame: "FakeChallengeFrame") -> None:
                super().__init__()
                self.frame = frame

            async def count(self) -> int:
                return 1 if self.frame.audio_mode_active else 0

            async def click(self) -> None:
                self.click_count += 1
                self.frame.reload_clicks += 1

        class FakeEmptyLocator(BaseLocator):
            def __init__(self) -> None:
                super().__init__(visible=False)

            async def count(self) -> int:
                return 0

            async def is_visible(self) -> bool:
                return False

        class FakeAnchorFrame:
            def __init__(self) -> None:
                self.checkbox = FakeCheckboxLocator()

            def locator(self, selector: str) -> BaseLocator:
                self.last_selector = selector
                return self.checkbox

        class FakeChallengeFrame:
            def __init__(self) -> None:
                self.audio_mode_active = False
                self.reload_clicks = 0
                self.audio_button = FakeAudioButtonLocator(self)
                self.audio_source = FakeAudioSourceLocator(self)
                self.response_input = FakeResponseInputLocator(self)
                self.verify_button = BaseLocator()
                self.reload_button = FakeReloadLocator(self)
                self.empty = FakeEmptyLocator()

            def locator(self, selector: str) -> BaseLocator:
                if selector == "#recaptcha-audio-button, button.rc-button-audio":
                    return self.audio_button
                if selector == "#audio-source, audio#audio-source":
                    return self.audio_source
                if selector == "#audio-response, input#audio-response":
                    return self.response_input
                if selector == "#recaptcha-verify-button, button#recaptcha-verify-button":
                    return self.verify_button
                if selector == "#recaptcha-reload-button":
                    return self.reload_button
                if selector == ".rc-audiochallenge-error-message, :has-text('automated queries'), :has-text('компьютер или сеть')":
                    return self.empty
                raise AssertionError(f"Unexpected selector: {selector}")

        class FakePage:
            def __init__(self) -> None:
                self.contents = iter(["captcha", "clear"])

            def locator(self, selector: str) -> FakeIframeList:
                self.last_selector = selector
                return FakeIframeList()

            async def wait_for_timeout(self, _ms: int) -> None:
                return None

            async def content(self) -> str:
                return next(self.contents)

        class FakeHttpResponse:
            def __init__(self, *, content: bytes = b"", json_payload: dict[str, str] | None = None) -> None:
                self.content = content
                self._json_payload = json_payload or {}

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, str]:
                return self._json_payload

        class FakeAsyncClient:
            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def get(self, url: str) -> FakeHttpResponse:
                return FakeHttpResponse(content=b"audio-bytes")

            async def post(self, url: str, data: dict[str, str], files: dict[str, tuple[str, bytes, str]], headers: dict[str, str]) -> FakeHttpResponse:
                return FakeHttpResponse(json_payload={"text": "weekend"})

        anchor_frame = FakeAnchorFrame()
        challenge_frame = FakeChallengeFrame()
        page = FakePage()

        with (
            patch.object(
                main,
                "krisha_find_frame_locator",
                AsyncMock(
                    side_effect=[
                        (anchor_frame, "iframe[src*='recaptcha/api2/anchor'] nth(0)"),
                        (challenge_frame, "iframe[src*='recaptcha/api2/bframe']"),
                        (challenge_frame, "iframe[src*='recaptcha/api2/bframe']"),
                    ]
                ),
            ),
            patch.object(main, "is_captcha_page", side_effect=lambda content: content == "captcha"),
            patch.object(main.httpx, "AsyncClient", return_value=FakeAsyncClient()),
        ):
            solved = await main.krisha_solve_recaptcha_audio(
                page,
                object(),
                {"groq_api_key": "test-key"},
                stage="after_phone_click",
                max_retries=2,
            )

        self.assertTrue(solved)
        self.assertEqual(challenge_frame.audio_button.click_count, 1)
        self.assertEqual(challenge_frame.reload_button.click_count, 1)
        self.assertEqual(challenge_frame.verify_button.click_count, 2)
        self.assertEqual(challenge_frame.response_input.typed_values, ["weekend", "weekend"])

    async def test_krisha_wait_for_search_results_retries_until_detail_urls_appear(self) -> None:
        payload = main.KrishaImportRequest(city="Астана", property_type="commercial", max_contacts=3)

        class FakePage:
            def __init__(self) -> None:
                self.calls = 0

            async def content(self) -> str:
                self.calls += 1
                if self.calls < 3:
                    return "<html><body>Loading search results...</body></html>"
                return '<a href="/a/show/123456">Объявление</a>'

            async def wait_for_timeout(self, _ms: int) -> None:
                return None

        content, detail_urls, empty_results = await main.krisha_wait_for_search_results(FakePage(), payload, timeout_ms=3_000)

        self.assertFalse(empty_results)
        self.assertEqual(detail_urls, ["https://krisha.kz/a/show/123456"])
        self.assertIn("/a/show/123456", content)

    async def test_krisha_wait_for_search_results_retries_navigation_content_error(self) -> None:
        payload = main.KrishaImportRequest(city="Астана", property_type="commercial", max_contacts=3)

        class FakePage:
            def __init__(self) -> None:
                self.calls = 0

            async def content(self) -> str:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("Page.content: Unable to retrieve content because the page is navigating and changing the content")
                return '<a href="/a/show/123456">Объявление</a>'

            async def wait_for_load_state(self, _state: str, timeout: int = 0) -> None:
                return None

            async def wait_for_timeout(self, _ms: int) -> None:
                return None

        content, detail_urls, empty_results = await main.krisha_wait_for_search_results(FakePage(), payload, timeout_ms=3_000)

        self.assertFalse(empty_results)
        self.assertEqual(detail_urls, ["https://krisha.kz/a/show/123456"])
        self.assertIn("/a/show/123456", content)

    async def test_krisha_wait_for_phone_reveal_reads_visible_text(self) -> None:
        payload = main.KrishaImportRequest(property_type="any", max_contacts=1)

        class FakeBodyLocator:
            @property
            def first(self) -> "FakeBodyLocator":
                return self

            async def inner_text(self, timeout: int = 0) -> str:
                return "Коммерческое помещение. Телефон собственника +7 701 000 00 55"

        class FakePage:
            async def content(self) -> str:
                return "<html><body>Показ телефона</body></html>"

            def locator(self, _selector: str) -> FakeBodyLocator:
                return FakeBodyLocator()

            async def evaluate(self, _script: str) -> str:
                return ""

            async def wait_for_timeout(self, _ms: int) -> None:
                return None

        snapshot, revealed, captcha = await main.krisha_wait_for_phone_reveal(
            FakePage(),
            payload,
            "7",
            "https://krisha.kz/a/show/123456",
        )

        self.assertTrue(revealed)
        self.assertFalse(captcha)
        self.assertIn("+7 701 000 00 55", snapshot)

    async def test_krisha_wait_for_phone_reveal_detects_captcha_widget(self) -> None:
        payload = main.KrishaImportRequest(property_type="any", max_contacts=1)

        class FakeBodyLocator:
            @property
            def first(self) -> "FakeBodyLocator":
                return self

            async def inner_text(self, timeout: int = 0) -> str:
                return "Показ телефона"

        class FakePage:
            async def content(self) -> str:
                return "<html><body>Показ телефона</body></html>"

            def locator(self, _selector: str) -> FakeBodyLocator:
                return FakeBodyLocator()

            async def evaluate(self, script: str) -> object:
                if "selectors.some" in script:
                    return True
                return ""

            async def wait_for_timeout(self, _ms: int) -> None:
                return None

        _snapshot, revealed, captcha = await main.krisha_wait_for_phone_reveal(
            FakePage(),
            payload,
            "7",
            "https://krisha.kz/a/show/123456",
        )

        self.assertFalse(revealed)
        self.assertTrue(captcha)

    async def test_krisha_wait_for_login_result_detects_invalid_credentials(self) -> None:
        class FakePage:
            url = "https://id.kolesa.kz/login/?destination=https%3A%2F%2Fkrisha.kz%2Fmy"

            async def content(self) -> str:
                return "<html><body>Неверно указан логин или пароль, попробуйте еще раз.</body></html>"

            async def wait_for_timeout(self, _ms: int) -> None:
                return None

        errors: list[str] = []
        context = types.SimpleNamespace(storage_state=AsyncMock())

        resolved = await main.krisha_wait_for_login_result(
            FakePage(),
            context,
            Path("krisha_state.json"),
            errors,
            headless=True,
        )

        self.assertFalse(resolved)
        self.assertEqual(errors, ["login: invalid_credentials"])
        context.storage_state.assert_not_awaited()

    async def test_krisha_wait_for_login_result_retries_while_page_is_navigating(self) -> None:
        class EmptyLocator:
            async def count(self) -> int:
                return 0

        class FakePage:
            def __init__(self) -> None:
                self.url = "https://id.kolesa.kz/redirect?redirectUrl=https%3A%2F%2Fkrisha.kz%2Fmy"
                self.content_calls = 0

            async def content(self) -> str:
                self.content_calls += 1
                if self.content_calls == 1:
                    raise RuntimeError(
                        "Page.content: Unable to retrieve content because the page is navigating and changing the content."
                    )
                self.url = "https://krisha.kz/my"
                return "<html><body>ok</body></html>"

            def locator(self, _selector: str) -> EmptyLocator:
                return EmptyLocator()

            async def wait_for_load_state(self, _state: str, timeout: int = 0) -> None:
                return None

            async def wait_for_timeout(self, _ms: int) -> None:
                return None

        context = types.SimpleNamespace(storage_state=AsyncMock())
        errors: list[str] = []

        resolved = await main.krisha_wait_for_login_result(
            FakePage(),
            context,
            Path("krisha_state.json"),
            errors,
            headless=True,
        )

        self.assertTrue(resolved)
        self.assertEqual(errors, [])
        context.storage_state.assert_awaited_once_with(path="krisha_state.json")

    async def test_switch_project_starts_ai_for_enabled_target(self) -> None:
        with main.use_project(1):
            main.update_settings({"ai_enabled": "true"})
        with main.use_project(2):
            main.update_settings({"ai_enabled": "false"})
        main.set_setting("current_project_id", "2")

        with patch.object(main, "start_ai_resume_task") as start_ai_resume_task:
            response = await main.api_switch_project(main.ProjectSwitchPayload(project_id=1))

        self.assertTrue(response["ok"])
        self.assertEqual(main.current_project_id(), 1)
        start_ai_resume_task.assert_called_once_with(1)

    async def test_switch_project_keeps_other_project_ai_running_when_target_disabled(self) -> None:
        class FakeTask:
            def __init__(self) -> None:
                self.cancelled = False

            def done(self) -> bool:
                return False

            def cancel(self) -> None:
                self.cancelled = True

        with main.use_project(1):
            main.update_settings({"ai_enabled": "true"})
        with main.use_project(2):
            main.update_settings({"ai_enabled": "false"})
        main.set_setting("current_project_id", "1")

        old_poller = main.runtime_tasks["poller"]
        old_ai_sync = main.runtime_tasks["ai_sync"]
        old_ai_state = dict(main.runtime_state["ai"])
        poller_task = FakeTask()
        ai_sync_task = FakeTask()
        main.poller_tasks[1] = poller_task
        main.ai_sync_tasks[1] = ai_sync_task
        main.runtime_tasks["poller"] = poller_task
        main.runtime_tasks["ai_sync"] = ai_sync_task
        main.ai_states[1] = {"status": "running", "processed": 3, "last_error": None, "project_id": 1}
        main.runtime_state["ai"] = dict(main.ai_states[1])

        try:
            with patch.object(main, "start_ai_resume_task") as start_ai_resume_task:
                response = await main.api_switch_project(main.ProjectSwitchPayload(project_id=2))
        finally:
            main.poller_tasks.clear()
            main.ai_sync_tasks.clear()
            main.runtime_tasks["poller"] = old_poller
            main.runtime_tasks["ai_sync"] = old_ai_sync
            main.runtime_state["ai"] = old_ai_state

        self.assertTrue(response["ok"])
        self.assertEqual(main.current_project_id(), 2)
        self.assertFalse(poller_task.cancelled)
        self.assertFalse(ai_sync_task.cancelled)
        start_ai_resume_task.assert_not_called()

    async def test_krisha_browser_launch_failure_falls_back_to_http(self) -> None:
        payload = main.KrishaImportRequest(city="Астана", property_type="commercial")
        settings = main.DEFAULT_SETTINGS.copy()
        settings["krisha_use_browser"] = "true"
        settings["krisha_browser_engine"] = "playwright"
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

    async def test_krisha_bad_storage_state_retries_with_empty_context(self) -> None:
        payload = main.KrishaImportRequest(city="Астана", property_type="commercial", max_pages=1)
        settings = main.DEFAULT_SETTINGS.copy()
        settings["krisha_use_browser"] = "true"
        settings["krisha_browser_engine"] = "playwright"
        settings["krisha_headless"] = "true"

        temp_data_dir = Path(self.temp_dir.name) / "data"
        temp_data_dir.mkdir(parents=True, exist_ok=True)
        (temp_data_dir / "krisha_storage_state.json").write_text(
            json.dumps(
                {
                    "cookies": [{"name": "sid", "value": "1", "domain": ".krisha.kz", "path": "/"}],
                    "origins": [
                        {"origin": "https://krisha.kz", "localStorage": [{"name": "token", "value": "1"}]},
                    ],
                }
            ),
            encoding="utf-8",
        )

        class FakePage:
            url = "https://krisha.kz/prodazha/kommercheskaya-nedvizhimost/astana/"

            def set_default_timeout(self, _timeout: int) -> None:
                return None

            def set_default_navigation_timeout(self, _timeout: int) -> None:
                return None

            async def content(self) -> str:
                return "<html><body>Loading search results...</body></html>"

            async def wait_for_timeout(self, _timeout: int) -> None:
                return None

        class FakeContext:
            def __init__(self, page: FakePage) -> None:
                self.page = page
                self.storage_state = AsyncMock()

            async def route(self, *_args: object, **_kwargs: object) -> None:
                return None

            async def new_page(self) -> FakePage:
                return self.page

            async def close(self) -> None:
                return None

        class FakeBrowser:
            def __init__(self) -> None:
                self.page = FakePage()
                self.new_context_calls: list[dict[str, object]] = []

            async def new_context(self, **kwargs: object) -> FakeContext:
                self.new_context_calls.append(dict(kwargs))
                if len(self.new_context_calls) == 1 and "storage_state" in kwargs:
                    raise RuntimeError("Browser.new_context: Error setting storage state: net::ERR_ABORTED")
                return FakeContext(self.page)

            async def close(self) -> None:
                return None

        class FakeChromium:
            def __init__(self, browser: FakeBrowser) -> None:
                self.browser = browser

            async def launch(self, **_kwargs: object) -> FakeBrowser:
                return self.browser

        class FakePlaywrightContext:
            def __init__(self, browser: FakeBrowser) -> None:
                self.browser = browser

            async def __aenter__(self) -> object:
                return types.SimpleNamespace(chromium=FakeChromium(self.browser))

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

        browser = FakeBrowser()
        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: FakePlaywrightContext(browser)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.async_api = fake_async_api

        with (
            patch.dict(sys.modules, {"playwright": fake_playwright, "playwright.async_api": fake_async_api}),
            patch.object(main, "DATA_DIR", temp_data_dir),
            patch.object(main, "krisha_goto", AsyncMock(return_value=True)),
            patch.object(main, "close_krisha_popups", AsyncMock()),
            patch.object(main, "krisha_browser_auth_prompt_visible", AsyncMock(return_value=False)),
            patch.object(
                main,
                "krisha_wait_for_search_results",
                AsyncMock(return_value=("<div>По вашему запросу ничего не найдено</div>", [], True)),
            ),
        ):
            collected, errors = await main.collect_krisha_sources_browser(payload, [], settings)

        self.assertEqual(len(browser.new_context_calls), 2)
        self.assertIn("storage_state", browser.new_context_calls[0])
        self.assertNotIn("storage_state", browser.new_context_calls[1])
        self.assertEqual(len(collected), 1)
        self.assertEqual(errors, [])

    async def test_krisha_headless_is_forced_without_display(self) -> None:
        settings = main.DEFAULT_SETTINGS.copy()
        settings["krisha_headless"] = "false"

        with patch.object(main.os, "name", "posix"), patch.dict(main.os.environ, {}, clear=True):
            self.assertTrue(main.krisha_should_run_headless(settings))

    async def test_krisha_headed_mode_is_allowed_on_windows(self) -> None:
        settings = main.DEFAULT_SETTINGS.copy()
        settings["krisha_headless"] = "false"

        with patch.object(main.os, "name", "nt"), patch.dict(main.os.environ, {}, clear=True):
            self.assertFalse(main.krisha_should_run_headless(settings))

    async def test_krisha_browser_engine_selects_selenium_v2_aliases(self) -> None:
        self.assertEqual(main.krisha_browser_engine({"krisha_browser_engine": "selenium_undetected"}), "selenium_undetected")
        self.assertEqual(main.krisha_browser_engine({"krisha_browser_engine": "v2"}), "selenium_undetected")
        self.assertEqual(main.krisha_browser_engine({"krisha_browser_engine": "playwright"}), "playwright")

    def test_krisha_selenium_chrome_major_version_parses_chromium_output(self) -> None:
        completed = types.SimpleNamespace(stdout="Chromium 147.0.7727.15\n", stderr="")
        with patch.object(main.subprocess, "run", return_value=completed):
            self.assertEqual(main.krisha_selenium_chrome_major_version("/fake/chrome"), 147)

    def test_krisha_selenium_user_agent_matches_chrome_major(self) -> None:
        user_agent = main.krisha_selenium_user_agent(147)

        self.assertIn("X11; Linux x86_64", user_agent)
        self.assertIn("Chrome/147.0.0.0", user_agent)
        self.assertNotIn("HeadlessChrome", user_agent)

    def test_krisha_selenium_profile_directory_is_keyed_by_proxy(self) -> None:
        no_proxy = main.krisha_selenium_profile_directory({"krisha_proxy_server": ""})
        with_proxy = main.krisha_selenium_profile_directory({"krisha_proxy_server": "http://81.200.159.177:8000"})
        same_proxy = main.krisha_selenium_profile_directory({"krisha_proxy_server": "HTTP://81.200.159.177:8000"})

        self.assertEqual(no_proxy.name, "krisha_selenium_profile")
        self.assertTrue(with_proxy.name.startswith("krisha_selenium_profile_proxy_"))
        self.assertEqual(with_proxy, same_proxy)
        self.assertNotEqual(no_proxy, with_proxy)

    def test_krisha_httpx_proxy_url_embeds_credentials_when_needed(self) -> None:
        self.assertEqual(
            main.krisha_httpx_proxy_url({"server": "http://81.200.159.177:8000"}),
            "http://81.200.159.177:8000",
        )
        self.assertEqual(
            main.krisha_httpx_proxy_url(
                {
                    "server": "http://81.200.159.177:8000",
                    "username": "user name",
                    "password": "p@ss",
                }
            ),
            "http://user%20name:p%40ss@81.200.159.177:8000",
        )

    async def test_krisha_source_texts_dispatches_to_selenium_v2(self) -> None:
        payload = main.KrishaImportRequest(city="Астана", property_type="commercial")
        settings = main.DEFAULT_SETTINGS.copy()
        settings["krisha_use_browser"] = "true"
        settings["krisha_browser_engine"] = "selenium_undetected"

        with (
            patch.object(main, "collect_krisha_sources_selenium_undetected", AsyncMock(return_value=([{"source": "selenium", "text": "ok"}], []))) as selenium_mock,
            patch.object(main, "collect_krisha_sources_browser", AsyncMock()) as playwright_mock,
        ):
            collected, errors = await main.collect_krisha_source_texts(payload, settings)

        self.assertEqual(collected, [{"source": "selenium", "text": "ok"}])
        self.assertEqual(errors, [])
        selenium_mock.assert_awaited_once()
        playwright_mock.assert_not_awaited()

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

    async def test_auto_work_force_runs_before_scheduled_time(self) -> None:
        with main.use_project(1):
            main.update_settings(
                {
                    "green_id_instance": "111111",
                    "green_api_token": "token-1",
                    "auto_campaign_enabled": "false",
                    "auto_campaign_time": "23:00",
                    "auto_campaign_timezone": "Asia/Almaty",
                    "auto_campaign_max_messages": "1",
                    "auto_campaign_delay_min_seconds": "1",
                    "auto_campaign_delay_max_seconds": "1",
                }
            )
            lead = main.create_or_update_contact(
                phone="77000000053",
                kind="lead",
                source="test",
                company="Manual run",
                status="new",
                stage="new",
            )
            main.update_contact_fields(lead["id"], whatsapp_exists=1, status="ready")

        result = await main.run_auto_campaign_for_project(
            1,
            now_utc=datetime(2026, 5, 14, 6, 0, tzinfo=timezone.utc),
            start_worker=False,
            force=True,
        )

        self.assertTrue(result["started"])
        self.assertEqual(result["reason"], "started")
        with main.db_conn() as conn:
            campaign = conn.execute("SELECT * FROM campaigns WHERE project_id = 1").fetchone()
        self.assertIsNotNone(campaign)

    async def test_keramo_auto_work_uses_cached_ready_contacts_before_krisha(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            main.update_settings(
                {
                    "green_id_instance": "222222",
                    "green_api_token": "token-2",
                    "auto_campaign_enabled": "true",
                    "auto_campaign_time": "00:00",
                    "auto_campaign_timezone": "Asia/Almaty",
                    "auto_campaign_max_messages": "2",
                    "auto_campaign_delay_min_seconds": "1",
                    "auto_campaign_delay_max_seconds": "1",
                    "krisha_max_contacts": "1",
                }
            )
            lead = main.create_or_update_contact(
                phone="77000000054",
                kind="lead",
                source="krisha",
                company="Keramo",
                status="new",
                stage="new",
            )
            main.update_contact_fields(lead["id"], whatsapp_exists=1, status="ready")

        with patch.object(main, "run_krisha_import_cycle", AsyncMock()) as krisha_mock, patch.object(
            main, "check_whatsapp_worker", AsyncMock()
        ) as check_mock:
            result = await main.run_auto_campaign_for_project(
                main.KERAMO_PROJECT_ID,
                now_utc=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
                start_worker=False,
            )

        self.assertTrue(result["started"])
        krisha_mock.assert_not_awaited()
        check_mock.assert_not_awaited()
        with main.db_conn() as conn:
            campaign = conn.execute("SELECT * FROM campaigns WHERE project_id = ?", (main.KERAMO_PROJECT_ID,)).fetchone()
            old_lead = conn.execute("SELECT * FROM contacts WHERE id = ?", (lead["id"],)).fetchone()
        self.assertIsNotNone(campaign)
        self.assertEqual(old_lead["status"], "ready")

    async def test_keramo_auto_work_runs_krisha_only_after_cached_pool_empty(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            main.update_settings(
                {
                    "green_id_instance": "222222",
                    "green_api_token": "token-2",
                    "auto_campaign_enabled": "true",
                    "auto_campaign_time": "00:00",
                    "auto_campaign_timezone": "Asia/Almaty",
                    "auto_campaign_max_messages": "2",
                    "auto_campaign_delay_min_seconds": "1",
                    "auto_campaign_delay_max_seconds": "1",
                    "krisha_max_contacts": "1",
                }
            )

        captured: dict[str, object] = {}

        async def fake_krisha_import(payload: main.KrishaImportRequest, **kwargs: object) -> dict[str, object]:
            captured["payload"] = payload
            captured["kwargs"] = kwargs
            with main.use_project(main.KERAMO_PROJECT_ID):
                fresh = main.create_or_update_contact(
                    phone="77000000055",
                    kind="lead",
                    source="krisha",
                    company="Fresh Keramo",
                    status="new",
                    stage="new",
                )
                main.update_contact_fields(fresh["id"], whatsapp_exists=1, status="ready")
            return {"found": 1, "imported": 1, "updated": 0, "source_errors": []}

        with patch.object(main, "run_krisha_import_cycle", AsyncMock(side_effect=fake_krisha_import)) as krisha_mock, patch.object(
            main, "check_whatsapp_worker", AsyncMock()
        ) as check_mock:
            result = await main.run_auto_campaign_for_project(
                main.KERAMO_PROJECT_ID,
                now_utc=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
                start_worker=False,
            )

        self.assertTrue(result["started"])
        krisha_mock.assert_awaited_once()
        check_mock.assert_awaited_once()
        self.assertEqual(captured["payload"].max_contacts, 6)
        self.assertEqual(captured["kwargs"]["settings_override"]["krisha_use_browser"], "true")
        self.assertEqual(captured["kwargs"]["settings_override"]["krisha_browser_engine"], "selenium_undetected")
        with main.db_conn() as conn:
            campaign = conn.execute("SELECT * FROM campaigns WHERE project_id = ?", (main.KERAMO_PROJECT_ID,)).fetchone()
        self.assertIsNotNone(campaign)

    async def test_keramo_auto_work_starts_from_cached_ready_even_if_krisha_would_find_nothing(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            main.update_settings(
                {
                    "green_id_instance": "222222",
                    "green_api_token": "token-2",
                    "auto_campaign_enabled": "true",
                    "auto_campaign_time": "00:00",
                    "auto_campaign_timezone": "Asia/Almaty",
                    "auto_campaign_max_messages": "2",
                    "auto_campaign_delay_min_seconds": "1",
                    "auto_campaign_delay_max_seconds": "1",
                }
            )
            old = main.create_or_update_contact(
                phone="77000000056",
                kind="lead",
                source="krisha",
                company="Old Keramo",
                status="new",
                stage="new",
            )
            main.update_contact_fields(old["id"], whatsapp_exists=1, status="ready")

        with patch.object(
            main,
            "run_krisha_import_cycle",
            AsyncMock(return_value={"found": 0, "imported": 0, "updated": 0, "source_errors": []}),
        ) as krisha_mock, patch.object(main, "check_whatsapp_worker", AsyncMock()) as check_mock:
            result = await main.run_auto_campaign_for_project(
                main.KERAMO_PROJECT_ID,
                now_utc=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
                start_worker=False,
            )

        self.assertTrue(result["started"])
        self.assertEqual(result["reason"], "started")
        krisha_mock.assert_not_awaited()
        check_mock.assert_not_awaited()
        with main.db_conn() as conn:
            campaigns = conn.execute("SELECT * FROM campaigns WHERE project_id = ?", (main.KERAMO_PROJECT_ID,)).fetchall()
            old_lead = conn.execute("SELECT * FROM contacts WHERE id = ?", (old["id"],)).fetchone()
        self.assertEqual(len(campaigns), 1)
        self.assertEqual(old_lead["status"], "ready")

    async def test_keramo_auto_work_reactivates_stale_cached_contacts_before_krisha(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            main.update_settings(
                {
                    "green_id_instance": "222222",
                    "green_api_token": "token-2",
                    "auto_campaign_enabled": "true",
                    "auto_campaign_time": "00:00",
                    "auto_campaign_timezone": "Asia/Almaty",
                    "auto_campaign_max_messages": "2",
                    "auto_campaign_delay_min_seconds": "1",
                    "auto_campaign_delay_max_seconds": "1",
                }
            )
            cached = main.create_or_update_contact(
                phone="77000000057",
                kind="lead",
                source="krisha",
                company="Cached Keramo",
                status="krisha_stale",
                stage="krisha_stale",
            )
            main.update_contact_fields(cached["id"], whatsapp_exists=1)

        with patch.object(main, "run_krisha_import_cycle", AsyncMock()) as krisha_mock:
            result = await main.run_auto_campaign_for_project(
                main.KERAMO_PROJECT_ID,
                now_utc=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
                start_worker=False,
            )

        self.assertTrue(result["started"])
        krisha_mock.assert_not_awaited()
        with main.db_conn() as conn:
            cached_row = conn.execute("SELECT * FROM contacts WHERE id = ?", (cached["id"],)).fetchone()
        self.assertEqual(cached_row["status"], "ready")

    async def test_keramo_auto_work_checks_cached_pending_contacts_before_krisha(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            main.update_settings(
                {
                    "green_id_instance": "222222",
                    "green_api_token": "token-2",
                    "auto_campaign_enabled": "true",
                    "auto_campaign_time": "00:00",
                    "auto_campaign_timezone": "Asia/Almaty",
                    "auto_campaign_max_messages": "2",
                    "auto_campaign_delay_min_seconds": "1",
                    "auto_campaign_delay_max_seconds": "1",
                }
            )
            cached = main.create_or_update_contact(
                phone="77000000058",
                kind="lead",
                source="krisha",
                company="Pending Keramo",
                status="new",
                stage="imported",
            )

        async def fake_check_whatsapp() -> None:
            main.update_contact_fields(cached["id"], whatsapp_exists=1, status="ready")

        with patch.object(main, "run_krisha_import_cycle", AsyncMock()) as krisha_mock, patch.object(
            main, "check_whatsapp_worker", AsyncMock(side_effect=fake_check_whatsapp)
        ) as check_mock:
            result = await main.run_auto_campaign_for_project(
                main.KERAMO_PROJECT_ID,
                now_utc=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
                start_worker=False,
            )

        self.assertTrue(result["started"])
        check_mock.assert_awaited_once()
        krisha_mock.assert_not_awaited()
        with main.db_conn() as conn:
            campaign = conn.execute("SELECT * FROM campaigns WHERE project_id = ?", (main.KERAMO_PROJECT_ID,)).fetchone()
        self.assertIsNotNone(campaign)

    async def test_manual_keramo_campaign_reactivates_cached_contacts_without_krisha(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            main.update_settings({"green_id_instance": "222222", "green_api_token": "token-2"})
            cached = main.create_or_update_contact(
                phone="77000000059",
                kind="lead",
                source="krisha",
                company="Manual Cached Keramo",
                status="krisha_stale",
                stage="krisha_stale",
            )
            main.update_contact_fields(cached["id"], whatsapp_exists=1)

            with patch.object(main, "start_campaign_task") as start_campaign_task, patch.object(
                main, "run_krisha_import_cycle", AsyncMock()
            ) as krisha_mock:
                result = await main.api_campaign_start(
                    main.CampaignStart(max_messages=1, delay_min_seconds=1, delay_max_seconds=1, target_kind="lead")
                )

            self.assertTrue(result["ok"])
            start_campaign_task.assert_called_once()
            krisha_mock.assert_not_awaited()
            refreshed = main.get_contact(cached["id"])

        self.assertEqual(refreshed["status"], "ready")

    async def test_manual_keramo_campaign_checks_cached_pending_without_krisha(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            main.update_settings({"green_id_instance": "222222", "green_api_token": "token-2"})
            cached = main.create_or_update_contact(
                phone="77000000060",
                kind="lead",
                source="krisha",
                company="Manual Pending Keramo",
                status="new",
                stage="imported",
            )

            async def fake_check_whatsapp() -> None:
                main.update_contact_fields(cached["id"], whatsapp_exists=1, status="ready")

            with patch.object(main, "start_campaign_task") as start_campaign_task, patch.object(
                main, "check_whatsapp_worker", AsyncMock(side_effect=fake_check_whatsapp)
            ) as check_mock, patch.object(main, "run_krisha_import_cycle", AsyncMock()) as krisha_mock:
                result = await main.api_campaign_start(
                    main.CampaignStart(max_messages=1, delay_min_seconds=1, delay_max_seconds=1, target_kind="lead")
                )

            self.assertTrue(result["ok"])
            start_campaign_task.assert_called_once()
            check_mock.assert_awaited_once()
            krisha_mock.assert_not_awaited()
            refreshed = main.get_contact(cached["id"])

        self.assertEqual(refreshed["status"], "ready")

    async def test_keramo_auto_work_source_error_does_not_mark_day_as_done(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            main.update_settings(
                {
                    "green_id_instance": "222222",
                    "green_api_token": "token-2",
                    "auto_campaign_enabled": "true",
                    "auto_campaign_time": "00:00",
                    "auto_campaign_timezone": "Asia/Almaty",
                    "auto_campaign_max_messages": "2",
                    "auto_campaign_delay_min_seconds": "1",
                    "auto_campaign_delay_max_seconds": "1",
                }
            )

        with patch.object(
            main,
            "run_krisha_import_cycle",
            AsyncMock(
                return_value={
                    "found": 0,
                    "imported": 0,
                    "updated": 0,
                    "source_errors": ["login: Page.content: Unable to retrieve content because the page is navigating"],
                }
            ),
        ) as krisha_mock, patch.object(main, "check_whatsapp_worker", AsyncMock()) as check_mock:
            result = await main.run_auto_campaign_for_project(
                main.KERAMO_PROJECT_ID,
                now_utc=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
                start_worker=False,
            )

        self.assertFalse(result["started"])
        self.assertEqual(result["reason"], "krisha_import_error")
        krisha_mock.assert_awaited_once()
        check_mock.assert_not_awaited()
        with main.use_project(main.KERAMO_PROJECT_ID):
            settings = main.get_settings()
        self.assertEqual(settings["auto_campaign_last_run_date"], "")
        self.assertEqual(settings["auto_campaign_last_error_date"], "2026-05-14")

    async def test_auto_work_skips_repeated_run_after_same_day_krisha_error(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            main.update_settings(
                {
                    "green_id_instance": "222222",
                    "green_api_token": "token-2",
                    "auto_campaign_enabled": "true",
                    "auto_campaign_time": "00:00",
                    "auto_campaign_timezone": "Asia/Almaty",
                    "auto_campaign_last_error_date": "2026-05-14",
                    "auto_campaign_last_error": "Krisha login failed",
                }
            )

        with patch.object(main, "run_krisha_import_cycle", AsyncMock()) as krisha_mock:
            result = await main.run_auto_campaign_for_project(
                main.KERAMO_PROJECT_ID,
                now_utc=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
                start_worker=False,
            )

        self.assertFalse(result["started"])
        self.assertEqual(result["reason"], "last_error_today")
        krisha_mock.assert_not_awaited()

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

    async def test_history_sync_deferred_permission_message_does_not_skip_credit_question(self) -> None:
        lead = self.create_lead("77000000073")
        main.update_contact_fields(lead["id"], status="sent", stage="warmup_permission")

        with patch.object(main, "send_and_log", AsyncMock()) as first_send:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Не понял"), allow_outbound=False)

        self.assertEqual(main.get_contact(lead["id"])["stage"], "warmup_permission")
        first_send.assert_not_called()

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Задавайте"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["stage"], "warmup_credit_check")
        send_mock.assert_awaited_once()
        self.assertIn("есть продажи авто через автокредит", send_mock.await_args.args[1].lower())
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

    async def test_process_notification_buyer_confusion_does_not_send_forced_proposal(self) -> None:
        lead = self.create_lead("77000000068")
        main.update_contact_fields(lead["id"], status="replied", stage="warmup_intro", attempts=5)

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Мне не нужен кредит"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["stage"], "clarified_offer")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("сами не оформляем автокредит", reply)
        self.assertNotIn("отправлю короткое кп", reply)
        proposal_mock.assert_not_awaited()
        ai_mock.assert_not_called()

    async def test_process_notification_action_question_does_not_send_forced_proposal(self) -> None:
        lead = self.create_lead("77000000072")
        main.update_contact_fields(lead["id"], status="replied", stage="warmup_intro", attempts=5)

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Что надо?"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["stage"], "awaiting_intro")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("пришлите номер", reply)
        self.assertNotIn("отправлю короткое кп", reply)
        proposal_mock.assert_not_awaited()
        ai_mock.assert_not_called()

    async def test_process_notification_short_credit_question_does_not_send_forced_proposal(self) -> None:
        lead = self.create_lead("77000000073")
        main.update_contact_fields(lead["id"], status="replied", stage="warmup_intro", attempts=5)

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "В кредит?"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["stage"], "clarified_offer")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("не выдаем кредит", reply)
        self.assertNotIn("отправлю короткое кп", reply)
        proposal_mock.assert_not_awaited()
        ai_mock.assert_not_called()

    async def test_process_notification_responsible_exists_short_reply_asks_for_number(self) -> None:
        lead = self.create_lead("77000000074")
        main.update_contact_fields(lead["id"], status="replied", stage="warmup_intro", attempts=5)
        main.save_message(
            lead["id"],
            lead["chat_id"],
            "out",
            "Кто у вас отвечает за кредитные продажи или цифровые продукты?",
        )

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Бар иә"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["stage"], "awaiting_responsible_contact")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("whatsapp", reply)
        self.assertNotIn("отправлю короткое кп", reply)
        proposal_mock.assert_not_awaited()
        ai_mock.assert_not_called()

    async def test_process_notification_mid_dialog_greeting_does_not_force_proposal(self) -> None:
        lead = self.create_lead("77000000075")
        main.update_contact_fields(lead["id"], status="replied", stage="ask_lpr", attempts=5)
        ai_action = main.normalize_ai_action(
            {
                "reply": "Здравствуйте. Ранее писал по цифровым заявкам и автоскорингу для автодилера.",
                "stage": "continue",
                "send_proposal": False,
            }
        )

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock(return_value=ai_action)) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Привет"), allow_outbound=True)

        send_mock.assert_awaited_once()
        self.assertNotIn("отправлю короткое кп", send_mock.await_args.args[1].lower())
        proposal_mock.assert_not_awaited()
        ai_mock.assert_awaited_once()

    async def test_process_notification_short_car_model_reply_closes_as_wrong_address(self) -> None:
        lead = self.create_lead("77000000069")
        main.update_contact_fields(lead["id"], status="replied", stage="warmup_intro", attempts=5)

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Камри 40"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["status"], "not_interested")
        self.assertEqual(updated["stage"], "closed_retail_customer")
        send_mock.assert_awaited_once()
        self.assertIn("не продаем авто", send_mock.await_args.args[1].lower())
        proposal_mock.assert_not_awaited()
        ai_mock.assert_not_called()

    async def test_process_notification_closed_contact_reopens_when_offering_responsible_number(self) -> None:
        lead = self.create_lead("77000000070")
        main.update_contact_fields(lead["id"], status="not_interested", stage="closed_no_interest")

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(
                self.inbound_body(str(lead["phone"]), "Но я могу дать номер который продает машины"),
                allow_outbound=True,
            )

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["status"], "replied")
        self.assertEqual(updated["stage"], "awaiting_specialist_contact")
        send_mock.assert_awaited_once()
        self.assertIn("номер специалиста", send_mock.await_args.args[1].lower())
        ai_mock.assert_not_called()

    async def test_process_notification_closed_project_stops_warmup(self) -> None:
        lead = self.create_lead("77000000057")
        main.update_contact_fields(lead["id"], status="sent", stage="warmup_permission")

        with patch.object(main, "send_and_log", AsyncMock()) as send_mock, patch.object(main, "call_ai", AsyncMock()) as ai_mock:
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Проект уже закрыт, не работаем"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["status"], "not_interested")
        self.assertEqual(updated["stage"], "closed_no_interest")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("не буду отвлекать", reply)
        self.assertNotIn("автокредит", reply)
        ai_mock.assert_not_called()

    async def test_process_notification_self_lpr_to_me_phrase_offers_proposal(self) -> None:
        lead = self.create_lead("77000000058")
        main.save_message(lead["id"], lead["chat_id"], "out", "Это вы смотрите или лучше обсудить с коллегой?")

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Это ко мне"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["kind"], "lpr")
        self.assertEqual(updated["status"], "lpr_self")
        send_mock.assert_awaited_once()
        self.assertIn("пришлю короткое КП сюда", send_mock.await_args.args[1])
        proposal_mock.assert_not_awaited()
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

    async def test_process_notification_uses_recent_name_hint_before_lpr_phone(self) -> None:
        lead = self.create_lead("77000000059")
        main.save_message(lead["id"], lead["chat_id"], "in", "Менеджер Айболат")

        with (
            patch.object(main, "check_and_mark_whatsapp", AsyncMock(return_value=True)),
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "contact_lpr", AsyncMock()) as contact_lpr_mock,
        ):
            await main.process_notification_body(
                self.inbound_body(str(lead["phone"]), "Напишите ему +77000000060"),
                allow_outbound=True,
            )

        lpr = main.get_contact_by_chat("77000000060@c.us")
        self.assertIsNotNone(lpr)
        self.assertEqual(lpr["name"], "Айболат")
        self.assertEqual(lpr["status"], "lpr_ready")
        send_mock.assert_awaited_once()
        self.assertNotIn("имя", send_mock.await_args.args[1].lower())
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

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "notify_handoff", AsyncMock(return_value=True)) as handoff_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Можно короткий созвон завтра?"), allow_outbound=True)

        updated = main.get_contact(lead["id"])
        self.assertEqual(updated["status"], "interested")
        self.assertEqual(updated["stage"], "interest_dialog")
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1]
        self.assertIn("завтра", reply.lower())
        self.assertNotIn("что для вас сейчас важнее", reply.lower())
        handoff_mock.assert_awaited_once()
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

    async def test_process_notification_live_callback_time_confirms_without_reasking_slot(self) -> None:
        lead = self.create_lead("77000000061")
        main.update_contact_fields(lead["id"], status="interested", stage="awaiting_callback")

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "notify_handoff", AsyncMock(return_value=True)) as handoff_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
        ):
            await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Сегодня после обеда"), allow_outbound=True)

        ai_mock.assert_not_called()
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("сегодня после обеда", reply)
        self.assertNotIn("завтра", reply)
        self.assertNotIn("какой слот", reply)
        handoff_mock.assert_awaited_once()

    async def test_process_notification_after_proposal_custom_development_goes_to_handoff(self) -> None:
        lead = self.create_lead("77000000062")
        main.update_contact_fields(lead["id"], proposal_sent=1, status="proposal_sent", stage="proposal_sent")

        with (
            patch.object(main, "send_and_log", AsyncMock()) as send_mock,
            patch.object(main, "notify_handoff", AsyncMock(return_value=True)) as handoff_mock,
            patch.object(main, "call_ai", AsyncMock()) as ai_mock,
            patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
        ):
            await main.process_notification_body(
                self.inbound_body(str(lead["phone"]), "Другое приложение сможете сделать если я вам детально все объясню?"),
                allow_outbound=True,
            )

        ai_mock.assert_not_called()
        proposal_mock.assert_not_called()
        send_mock.assert_awaited_once()
        reply = send_mock.await_args.args[1].lower()
        self.assertIn("отдельную разработку", reply)
        handoff_mock.assert_awaited_once()

    async def test_keramo_ambiguous_reaction_does_not_send_proposal(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            lead = self.create_lead("77000000063")
            main.update_contact_fields(lead["id"], status="sent", stage="waiting_reply")

            with (
                patch.object(main, "send_and_log", AsyncMock()) as send_mock,
                patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
                patch.object(main, "call_ai", AsyncMock()) as ai_mock,
            ):
                await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Оу"), allow_outbound=True)

            updated = main.get_contact(lead["id"])
            self.assertEqual(updated["stage"], "awaiting_interest_confirmation")
            send_mock.assert_awaited_once()
            self.assertIn("могу отправить короткое КП", send_mock.await_args.args[1])
            proposal_mock.assert_not_awaited()
            ai_mock.assert_not_called()

    async def test_keramo_interest_dialog_yes_after_proposal_offer_sends_proposal(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            lead = self.create_lead("77000000064")
            main.update_contact_fields(lead["id"], status="interested_pending", stage="interest_dialog")
            main.save_message(
                lead["id"],
                lead["chat_id"],
                "out",
                main.interested_reply_text(lead, "Интересно"),
            )

            with (
                patch.object(main, "send_and_log", AsyncMock()) as send_mock,
                patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
                patch.object(main, "call_ai", AsyncMock()) as ai_mock,
            ):
                await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Да"), allow_outbound=True)

            updated = main.get_contact(lead["id"])
            self.assertEqual(updated["kind"], "lpr")
            send_mock.assert_awaited_once()
            self.assertIn("прикреплю короткое КП", send_mock.await_args.args[1])
            proposal_mock.assert_awaited_once()
            ai_mock.assert_not_called()

    async def test_keramo_contextual_typo_request_after_proposal_offer_sends_proposal(self) -> None:
        with main.use_project(main.KERAMO_PROJECT_ID):
            lead = self.create_lead("77000000071")
            main.update_contact_fields(lead["id"], status="replied", stage="awaiting_interest_confirmation")
            main.save_message(
                lead["id"],
                lead["chat_id"],
                "out",
                "Если интересно, могу отправить короткое КП с цифрами.",
            )

            with (
                patch.object(main, "send_and_log", AsyncMock()) as send_mock,
                patch.object(main, "send_proposal", AsyncMock()) as proposal_mock,
                patch.object(main, "call_ai", AsyncMock()) as ai_mock,
            ):
                await main.process_notification_body(self.inbound_body(str(lead["phone"]), "Отпроавьте"), allow_outbound=True)

            updated = main.get_contact(lead["id"])
            self.assertEqual(updated["kind"], "lpr")
            send_mock.assert_awaited_once()
            self.assertIn("прикреплю короткое КП", send_mock.await_args.args[1])
            proposal_mock.assert_awaited_once()
            ai_mock.assert_not_called()

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
