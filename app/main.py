from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import random
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "app" / "static"
DB_PATH = DATA_DIR / "agent.sqlite3"
PROPOSAL_PATH = BASE_DIR / "Коммерческое Предложение.html"
AUTH_COOKIE_NAME = "wa_sales_admin"
AUTH_SESSION_DAYS = 14
PASSWORD_ITERATIONS = 260_000
DEFAULT_PROJECT_ID = 1
KERAMO_PROJECT_ID = 2
KERAMO_WORKFLOW_TYPE = "keramo_investor"
KERAMO_PROPOSAL_FILENAME = "KERAMO_BUILD_INVEST_PROPOSAL.html"
SECOND_PROJECT_AI_PROMPT = """Ты B2B sales assistant в WhatsApp для проекта KERAMO BUILD. Оффер: инвестиция в производство, нарезку и монтаж керамогранита в Астане. Сумма инвестиций 35 млн тенге, базовый срок возврата 25 месяцев, после возврата инвестор получает 30% прибыли. Цель диалога: коротко и без давления понять, интересна ли человеку инвестиционная возможность, является ли он собственником/инвестором или кто принимает такие решения, и довести до следующего шага: КП, созвон, встреча или ручная передача.

Стиль: живой B2B-мессенджер, 1-2 коротких предложения, один вопрос за сообщение. Не обещай гарантированную доходность, прибыль без риска или юридическую защиту без оговорок. Говори через базовый сценарий, прогноз и условия сделки. Не используй термин «ЛПР» в сообщениях клиенту; пиши «собственник», «инвестор», «руководитель» или «тот, кто смотрит инвестиционные вопросы». Не используй шаблонные завершающие фразы вроде «если захотите вернуться к вопросу» или «я на связи».

Правила поведения:
- Используй research-first и permission-based selling: если есть сигнал по коммерческой недвижимости, собственнику помещения или аренде, аккуратно свяжи его с инвестиционной темой.
- В первом контакте не отправляй длинный оффер. Сначала спроси, уместно ли коротко написать по инвестиционному предложению.
- Если собеседник заинтересован, задай один квалифицирующий вопрос: комфортен ли чек 35 млн тенге, интересна ли доля в операционном бизнесе или удобнее созвон.
- Если собеседник просит подробнее, дай короткую суть: производство керамогранита, 35 млн тенге, возврат по базовому плану 25 месяцев, затем 30% прибыли; добавь, что фактические результаты зависят от рынка.
- Если контакт ответственного уже передали, начни короткий диалог с ним и только после этого предлагай КП, созвон или встречу.

Возвращай только JSON:
{"reply":"текст ответа","stage":"ask_lpr|need_lpr_name|lpr_self|send_proposal|interested|handoff|stop|continue","send_proposal":false,"is_lpr":false,"interested":false,"lpr_phone":"","lpr_name":"","stop":false}
"""

project_context_id: ContextVar[int | None] = ContextVar("project_context_id", default=None)

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="WhatsApp AI Sales Agent")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


runtime_tasks: dict[str, asyncio.Task | None] = {
    "check": None,
    "campaign": None,
    "poller": None,
    "ai_sync": None,
    "krisha_parser": None,
    "auto_work": None,
}

runtime_state: dict[str, dict[str, Any]] = {
    "check": {"status": "idle", "processed": 0, "total": 0, "last_error": None},
    "campaign": {"status": "idle", "campaign_id": None, "last_error": None},
    "ai": {"status": "idle", "processed": 0, "last_error": None},
    "krisha_parser": {
        "status": "idle",
        "processed": 0,
        "found": 0,
        "imported": 0,
        "updated": 0,
        "max_contacts": None,
        "last_error": None,
        "last_run_at": None,
    },
    "auto_work": {
        "status": "idle",
        "checked_projects": 0,
        "last_check_at": None,
        "last_action": None,
        "last_error": None,
    },
}


DEFAULT_SETTINGS: dict[str, str] = {
    "green_api_url": "https://api.greenapi.com",
    "green_media_url": "https://media.green-api.com",
    "green_id_instance": "",
    "green_api_token": "",
    "default_country_code": "7",
    "ai_provider": "deepseek",
    "openai_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "openai_model": "gpt-5.2",
    "openai_transcription_model": "gpt-4o-mini-transcribe",
    "groq_api_key": "",
    "groq_base_url": "https://api.groq.com/openai/v1",
    "groq_transcription_model": "whisper-large-v3-turbo",
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-v4-flash",
    "ai_temperature": "0.35",
    "ai_enabled": "false",
    "audio_transcription_enabled": "true",
    "bot_name": "",
    "proposal_filename": "Коммерческое Предложение.html",
    "max_lpr_attempts": "4",
    "send_proposal_after_failed_attempts": "true",
    "typing_enabled": "true",
    "typing_min_seconds": "2",
    "typing_max_seconds": "6",
    "green_history_sync_minutes": "1440",
    "handoff_enabled": "true",
    "handoff_phone": "77759419359, 77015001995",
    "auto_campaign_enabled": "false",
    "auto_campaign_time": "10:00",
    "auto_campaign_timezone": "Asia/Qyzylorda",
    "auto_campaign_max_messages": "25",
    "auto_campaign_delay_min_seconds": "40",
    "auto_campaign_delay_max_seconds": "120",
    "auto_campaign_target_kind": "lead",
    "auto_campaign_last_run_date": "",
    "auto_campaign_last_wait_date": "",
    "ai_system_prompt": "",
    "current_project_id": str(DEFAULT_PROJECT_ID),
    "krisha_login": "",
    "krisha_password": "",
    "krisha_city": "Астана",
    "krisha_property_type": "commercial",
    "krisha_keywords": "",
    "krisha_min_area": "",
    "krisha_max_area": "",
    "krisha_min_price": "",
    "krisha_max_price": "",
    "krisha_max_pages": "1",
    "krisha_max_contacts": "",
    "krisha_interval_minutes": "120",
    "krisha_use_browser": "true",
    "krisha_headless": "true",
}

PROJECT_SETTING_KEYS = {"proposal_filename", "ai_system_prompt"}
GLOBAL_SETTING_KEYS = {"current_project_id"}
PROJECT_SCOPED_SETTING_KEYS = set(DEFAULT_SETTINGS) - PROJECT_SETTING_KEYS - GLOBAL_SETTING_KEYS
AUTH_SETTING_KEYS = {"admin_username", "admin_password_hash", "admin_session_secret"}


DEFAULT_AI_PROMPT = """Ты B2B sales assistant в WhatsApp. Компания предлагает white-label мобильное приложение автоскоринга для автодилеров: iOS/Android под бренд дилера, анкета клиента, предварительный скоринг, подбор банков и МФО, сравнение кредитных условий, каталог автомобилей, кабинет заявок и аналитика. Коммерческие условия: разработка 5-15 млн тенге, поддержка 200-500 тыс. тенге в месяц.

Цель диалога: аккуратно понять, кто отвечает за коммерческие вопросы, цифровые продукты, кредитные продажи или развитие автодилера. Если собеседник не этот человек, мягко попроси контакт или номер ответственного/руководителя. Если собеседник сам отвечает за вопрос, можно предложить отправить КП. Если собеседник дал номер или контакт ответственного, поблагодари; имя проси только после получения номера и только если это уместно. Если собеседник не знает имя, спокойно прими это и продолжай без давления. Если собеседник заинтересован в обсуждении, встрече, созвоне или следующих шагах, пометь это как interested/handoff. Если явно отказался или написал стоп, больше не продавай.

Стиль: дружелюбный, уважительный, спокойный, живой, как у сильного B2B sales-менеджера в мессенджере. Не сухо и не канцелярски. Одно сообщение - 1-2 коротких предложения, обычно до 220 символов. Не используй шаблонные завершающие фразы вроде «если захотите вернуться к вопросу» или «я на связи».

Правила поведения:
- В первом исходящем сообщении или когда тебя спросили, кто ты, коротко представься по имени из CRM-настроек, если имя задано.
- Используй research-first подход: если в CRM-контексте есть сигнал, угол атаки или данные из Excel, опирайся на них и объясняй, почему пишем именно сейчас. Если сигнала нет, честно держись общего контекста автодилера.
- Строй диалог по LAER: сначала считай смысл ответа клиента, коротко признай его позицию, затем задай один уточняющий вопрос и только потом отвечай/предлагай следующий шаг.
- Двигайся через permission-based selling и micro-CTA: если клиент помогает, предлагает связать, спрашивает что нужно или готов обсудить, давай один конкретный следующий шаг вместо общего повтора вопроса.
- Квалифицируй Authority, Need и Capacity мягко: кто принимает решение, есть ли боль в заявках/скоринге/кредитной воронке, когда имеет смысл обсуждать бюджет или пилот.
- Если клиент проявил общий интерес, не закрывай разговор преждевременно благодарностью, handoff или мгновенной отправкой КП. Сначала выясни один приоритет: рост заявок, скорость обработки анкет, текущий процесс или формат обсуждения.
- Если контакт ответственного передали, начни короткий релевантный диалог и только потом предлагай КП или созвон. Не отправляй КП автоматически в первом сообщении новому контакту.
- В возражениях не спорь. Если просят КП на почту, выясни один фокус: рост заявок на автокредит или скорость обработки анкет. Если говорят про бюджет, предложи оценить заранее для планирования.
- Если диалог уже начался, не начинай ответ новым приветствием и не дублируй приветствие клиента.
- Не используй в тексте для клиента термин «ЛПР». Пиши проще: «ответственный», «руководитель», «тот, кто отвечает за коммерческие вопросы».
- Не повторяй один и тот же вопрос про ответственного или его имя, если контакт уже дал максимум информации.
- После того как контакт ответственного уже получен или КП уже отправлено, не возвращай разговор назад к базовому вопросу «с кем обсудить».
- Если клиент просит уточнить, рассказать подробнее или продолжает разговор после предыдущего шага, не обрывай диалог молчанием: коротко поясни суть и задай один следующий вопрос.
- Если клиент спрашивает «кто вы?», ответь вежливо, коротко и по делу: имя, роль, причина сообщения.
- Если клиент явно просит больше не писать, пишет стоп или удалить контакт, прекрати продажу.
- Не обещай интеграции с конкретными банками и не выдумывай факты.

Возвращай только JSON:
{"reply":"текст ответа","stage":"ask_lpr|need_lpr_name|lpr_self|send_proposal|interested|handoff|stop|continue","send_proposal":false,"is_lpr":false,"interested":false,"lpr_phone":"","lpr_name":"","stop":false}
"""


LEGACY_AI_PROMPTS = {
    """Ты B2B sales assistant в WhatsApp. Компания предлагает white-label мобильное приложение автоскоринга для автодилеров: iOS/Android под бренд дилера, анкета клиента, предварительный скоринг, подбор банков и МФО, сравнение кредитных условий, каталог автомобилей, кабинет заявок и аналитика. Коммерческие условия: разработка 5-15 млн тенге, поддержка 200-500 тыс. тенге в месяц.

Цель диалога: аккуратно понять, кто является ЛПР по коммерческим вопросам, цифровым продуктам, кредитным продажам или развитию автодилера. Если собеседник не ЛПР, попроси контакт или номер ЛПР. Если собеседник сам ЛПР, можно предложить отправить КП. Если собеседник дал номер или контакт ЛПР, поблагодари и, если не хватает имени, попроси имя. Если собеседник заинтересован в обсуждении, встрече, созвоне или следующих шагах, пометь это как interested/handoff. Если явно отказался или написал стоп, больше не продавай.

Пиши коротко, по-человечески, без давления и без выдуманных фактов. Одно сообщение - 1-2 коротких предложения, обычно до 220 символов. Не обещай интеграции с конкретными банками. Возвращай только JSON:
{"reply":"текст ответа","stage":"ask_lpr|need_lpr_name|lpr_self|send_proposal|interested|handoff|stop|continue","send_proposal":false,"is_lpr":false,"interested":false,"lpr_phone":"","lpr_name":"","stop":false}
"""
}


GREETING_OPENERS = [
    "Добрый день.",
    "Здравствуйте.",
    "Добрый день!",
    "Здравствуйте!",
]

GREETING_CONTEXTS = [
    "Пишу по коммерческому вопросу для автодилера.",
    "Есть короткий вопрос по цифровым заявкам и автокредитам.",
    "Хотел уточнить по теме кредитных продаж и цифровой воронки.",
    "Пишу по решению для автодилеров: автоскоринг и заявки.",
    "Есть вопрос по инструментам для продаж авто в кредит.",
]

GREETING_QUESTIONS = [
    "С кем корректнее это обсудить?",
    "Кто у вас отвечает за такие решения?",
    "К кому лучше обратиться?",
    "Кто принимает решения по таким инструментам?",
    "С кем можно коротко переговорить по этой теме?",
]

AUTOSCORE_WARMUP_OPENERS = [
    "Здравствуйте. Можно коротко задать вопрос по автокредитному направлению?",
    "Добрый день. Можно коротко задать вопрос по автокредитному направлению?",
]

GREETING_PREFIX_RE = re.compile(
    r"^\s*(?:добрый\s+день|здравствуйте|доброе\s+утро|добрый\s+вечер|привет(?:ствую)?)\s*[!,.:;-]*\s*",
    re.IGNORECASE,
)
GREETING_ONLY_RE = re.compile(
    r"^\s*(?:добрый\s+день|здравствуйте|доброе\s+утро|добрый\s+вечер|привет(?:ствую)?)\s*[!.]?[\s!.,]*$",
    re.IGNORECASE,
)
UNKNOWN_NAME_RE = re.compile(
    r"\b(не\s*знаю|незнаю|не\s*помню|без\s*имени|имя\s*не\s*знаю|не\s*подскажу|не\s*уточнял(?:а)?|не\s*уточняли|не\s*сказали)\b",
    re.IGNORECASE,
)
IDENTITY_QUESTION_RE = re.compile(
    r"\b(а\s*вы\s*кто|вы\s*кто|кто\s*вы(?:\s*вообще)?|представь(?:тесь|ся)|как\s+вас\s+зовут|"
    r"с\s+кем\s+я\s+говорю|вы\s+робот|это\s+бот|с\s+ботом\s+говорю|живой\s+человек)\b",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
EMAIL_REQUEST_RE = re.compile(
    r"(скиньте|отправьте|пришлите).{0,40}(кп|коммерческ\w*|предложени\w*).{0,25}(на\s+почт\w*|email|e-mail)|(на\s+почт\w*|email|e-mail).{0,35}(кп|коммерческ\w*|предложени\w*)",
    re.IGNORECASE,
)
PROPOSAL_REQUEST_RE = re.compile(
    r"\b(пришлите|отправьте|скиньте|направьте|прикрепите|"
    r"отправля(?:й|йте)|присыла(?:й|йте)|скидыва(?:й|йте)|направля(?:й|йте)|прикрепля(?:й|йте)|"
    r"жду|давайте|можно).{0,30}(кп|коммерческ\w*|предложени\w*)"
    r"|\b(кп|коммерческ\w*\s+предложени\w*)\b",
    re.IGNORECASE,
)
PROPOSAL_CONSENT_RE = re.compile(
    r"^\s*(да|давайте|ок(?:ей)?|хорошо|конечно|можно|жду|принял(?:а)?|принято|"
    r"пришлите(?:\s+сюда)?|отправьте(?:\s+сюда)?|скиньте(?:\s+сюда)?|направьте(?:\s+сюда)?|прикрепите(?:\s+сюда)?|"
    r"отправля(?:й|йте)|присыла(?:й|йте)|скидыва(?:й|йте)|направля(?:й|йте)|прикрепля(?:й|йте))\b",
    re.IGNORECASE,
)
WARMUP_DECLINE_RE = re.compile(
    r"^\s*(нет|не\s+удобно|не\s+сейчас|не\s+нужно|не\s+интересно|не\s+актуально)\b",
    re.IGNORECASE,
)
WARMUP_AUTOCREDIT_YES_RE = re.compile(
    r"\b(да|есть|конечно|предоставля(?:ем|ю)|оформля(?:ем|ю)|занима(?:емся|юсь)|работа(?:ем|ю)|"
    r"есть\s+автокредит|есть\s+кредитн\w*\s+направлен\w*)\b",
    re.IGNORECASE,
)
WARMUP_AUTOCREDIT_NO_RE = re.compile(
    r"\b(нет|не\s+предоставля(?:ем|ю)|не\s+оформля(?:ем|ю)|не\s+занима(?:емся|юсь)|"
    r"не\s+работа(?:ем|ю)|нет\s+такого|автокредит\w*\s+нет)\b",
    re.IGNORECASE,
)
CONFUSION_RE = re.compile(
    r"\b(не\s+понял(?:а)?|не\s+совсем\s+понял(?:а)?|что\s+это|о\s+чем\s+речь|"
    r"в\s+чем\s+смысл|в\s+чем\s+вопрос|не\s+уловил(?:а)?|не\s+ясно)\b",
    re.IGNORECASE,
)
DETAIL_REQUEST_RE = re.compile(
    r"\b(подробнее|уточните(?:\s+подробнее)?|расскажите(?:\s+подробнее)?|можно\s+подробнее|"
    r"что\s+именно\s+вы\s+делаете|как\s+это\s+работает|в\s+чем\s+суть)\b",
    re.IGNORECASE,
)
BENEFIT_QUESTION_RE = re.compile(
    r"\b(что\s+(?:это\s+)?(?:нам|мне)\s+даст|что\s+даст|какая\s+польза|какой\s+эффект|зачем\s+это|в\s+чем\s+смысл)\b",
    re.IGNORECASE,
)
AUTOCREDIT_MATCHING_QUESTION_RE = re.compile(
    r"\b(сама|само|автомат\w*|подбира\w*|подбор\w*|банк\w*|финорганизац\w*|кредитн\w*\s+услов\w*)\b",
    re.IGNORECASE,
)
QUALIFICATION_FOCUS_RE = re.compile(
    r"\bчто\s+(?:для\s+вас\s+)?(?:сейчас\s+)?важнее\b.{0,140}\b(заявк\w*|обработк\w*|анкет\w*|скоринг\w*)\b",
    re.IGNORECASE,
)
QUALIFICATION_FOCUS_ANSWER_RE = re.compile(
    r"\b(быстр(?:ее|о)|скорост\w*|обработк\w*|анкет\w*|заявк\w*|лид\w*|скоринг\w*|оба|обе|и\s+то\s+и\s+то|все)\b",
    re.IGNORECASE,
)
TRANSFER_OFFER_RE = re.compile(
    r"\b(связ\w+|свяж\w+|соедин\w+|передам|переведу|познакомлю|сведу)\b.{0,90}\b(менеджер\w*|отдел\s+продаж|руководител\w*|коллег\w*|специалист\w*)\b",
    re.IGNORECASE,
)
BOT_OR_AUTO_REPLY_RE = re.compile(
    r"\b(виртуальн\w*\s+(?:менеджер|консультант)|онлайн[-\s]*консультант|администратор\s+торгового\s+зала|"
    r"выберите\s+пожалуйста|ответьте\s+цифр\w*|ваш\s+запрос\s+перенаправлен|мы\s+направили\s+ваши\s+контакты)\b",
    re.IGNORECASE,
)
REQUEST_OUR_CONTACT_RE = re.compile(
    r"\b(оставьте|укажите|напишите|пришлите|отправьте|вышлите)\b.{0,90}\b(контакт\w*|номер|телефон|почт\w*|email|e-mail|данные)\b|"
    r"\bс\s+вами\s+свяж\w+\b|\bменеджер\s+(?:сам\s+)?с\s+вами\s+свяж\w+\b",
    re.IGNORECASE,
)
SPECIALIST_CONTACT_OFFER_RE = re.compile(
    r"\b(?:могу|можем)\b.{0,90}\b(?:предоставить|дать|отправить|скинуть|направить)\b.{0,90}\b(?:контакт\w*|контактн\w*\s+данн\w*|номер|специалист\w*)\b|"
    r"\b(?:если|если\s+вам)\b.{0,80}\b(?:связаться|обсудить)\b.{0,80}\b(?:специалист\w*|ответственн\w*|менеджер\w*)\b",
    re.IGNORECASE,
)
CANNOT_ACCEPT_PROPOSAL_RE = re.compile(
    r"\bне\s+могу\b.{0,120}\b(?:принимать|принять|рассматривать|обсуждать)\b.{0,120}\b(?:коммерческ\w*|предложени\w*|документ\w*)\b",
    re.IGNORECASE,
)
FORWARDED_TO_INTERNAL_TEAM_RE = re.compile(
    r"\b(?:передам|передали|направим|направили|перенаправлен|перенаправили)\b.{0,120}\b(?:руководств\w*|отдел\w*|менеджер\w*|маркетинг\w*|ответственн\w*)\b",
    re.IGNORECASE,
)
FORWARD_TO_RESPONSIBLE_RE = re.compile(
    r"\b(?:сам(?:а)?\s+)?(?:передам|перешлю|покажу|отправлю)\b.{0,90}\b(руководител\w*|директор\w*|ответственн\w*|коллег\w*)\b",
    re.IGNORECASE,
)
ACTION_REQUEST_RE = re.compile(
    r"\b(что\s+(?:требуется|нужно|именно\s+нужно)\s+от\s+меня|что\s+вам\s+нужно|что\s+от\s+меня\s+нужно|что\s+требуется)\b",
    re.IGNORECASE,
)
BUYER_CONFUSION_RE = re.compile(
    r"\b(вы\s+хотите\s+получить\s+автокредит(?:ование)?|вам\s+нужен\s+автокредит(?:ование)?|"
    r"хотите\s+оформить\s+автокредит(?:ование)?|хотите\s+получить\s+кредит|оформить\s+кредит|"
    r"какую\s+(?:машину|модель|авто)\s+(?:хотели|ищете|подбираете)|какая\s+модель\s+вам\s+интересна)\b",
    re.IGNORECASE,
)
LANGUAGE_PROMPT_RE = re.compile(
    r"\b(на\s+каком\s+языке|каком\s+языке\s+удобно|удобно\s+продолжить\s+диалог|"
    r"қай\s+тілде|қазақша|русский)\b",
    re.IGNORECASE,
)
MEETING_INTEREST_RE = re.compile(
    r"\b(созвон|созвониться|встреч|демо|обсудим|обсудить|поговорить|связаться)\b",
    re.IGNORECASE,
)
ACK_RE = re.compile(
    r"^\s*(ок|окей|okay|хорошо|понял(?:а)?|принято|ясно|супер|отлично|спасибо|благодарю|сэнкю|thanks?|добрый\s+день|здравствуйте|до\s+свидания|досвидания|всего\s+доброго)\s*[!.]?[\s!.,]*$",
    re.IGNORECASE,
)
STEP_DONE_RE = re.compile(
    r"\b("
    r"ок(?:ей)?|хорошо|понял(?:а)?|принято|ясно|спасибо|благодарю|"
    r"прочита(?:ю|ем)|посмотр(?:ю|им)|изучу|ознаком(?:люсь|имся)|"
    r"обратитесь\s+к\s+нему|напишите\s+ему|свяжитесь\s+с\s+ним|пишите\s+ему|дальше\s+с\s+ним|"
    r"до\s+свидания|досвидания|всего\s+доброго"
    r")\b",
    re.IGNORECASE,
)
AFTER_PROPOSAL_ACK_RE = re.compile(
    r"^\s*(да|ок(?:ей)?|okay|понял(?:а)?|принял(?:а)?|принято|спасибо|благодарю|"
    r"получил(?:а)?|увидел(?:а)?)"
    r"(?:\s*[,!.]\s*(да|ок(?:ей)?|okay|понял(?:а)?|принял(?:а)?|принято|спасибо|благодарю|получил(?:а)?|увидел(?:а)?))?"
    r"\s*[!.]?\s*$",
    re.IGNORECASE,
)


OPT_OUT_RE = re.compile(
    r"\b(стоп|stop|отпис|не\s+пишите|не\s+надо\s+писать|больше\s+не\s+пишите|удалите|unsubscribe)\b",
    re.IGNORECASE,
)
SOFT_NEGATIVE_RE = re.compile(
    r"\b(неинтересно|не\s+интересно|не\s+актуально|неактуально|пока\s+не\s+(?:интересно|актуально|готов\w*)|не\s+нужно|не\s+надо)\b",
    re.IGNORECASE,
)
BUDGET_OBJECTION_RE = re.compile(
    r"\b(нет\s+бюджета|бюджет\w*\s+нет|не\s+заложен\w*\s+бюджет|дорого|цена\s+высок\w*|сейчас\s+без\s+бюджета)\b",
    re.IGNORECASE,
)
ALREADY_HAVE_RE = re.compile(
    r"\b(уже\s+есть|есть\s+(?:своя|свой|наша|наш)\s+(?:crm|црм|система|приложени\w*)|работаем\s+с\s+другими|есть\s+подрядчик|все\s+настроено)\b",
    re.IGNORECASE,
)
SELF_LPR_RE = re.compile(
    r"\b(я\s+(?:и\s+есть\s+)?лпр|сам\s+лпр|лпр\s+это\s+я|это\s+я|"
    r"я\s+(?:сам\s+)?(?:решаю|принимаю|директор|руководител\w*|собственник|владелец|учредител\w*|занимаюсь|отвечаю)|"
    r"я\s+(?:сам\s+)?(?:этим|этим\s+вопросом|этим\s+направлением)\s+(?:занимаюсь|отвечаю)|"
    r"(?:этим|этим\s+вопросом|этим\s+направлением)\s+(?:занимаюсь|отвечаю)\s+я|"
    r"со\s+мной\s+можно|пишите\s+мне|можете\s+мне)\b",
    re.IGNORECASE,
)
INTEREST_RE = re.compile(
    r"\b("
    r"интересно|заинтересовал\w*|актуально|"
    r"давайте\s+(?:обсудим|созвонимся|встретимся|поговорим)|"
    r"готов\w*\s+(?:обсудить|созвониться|встретиться|поговорить)|"
    r"хот(?:им|ел(?:и|а|ось)?|ел\s+бы|ела\s+бы)\s+(?:обсудить|созвониться|встретиться|поговорить)|"
    r"можем\s+(?:обсудить|созвониться|встретиться|поговорить)|"
    r"когда\s+(?:можно|удобно)\s+(?:созвониться|встретиться|обсудить)|"
    r"назнач(?:им|ьте)?\s+(?:встречу|созвон)|"
    r"встреч[ауы]|созвон|демо|презентац\w*|"
    r"условия\s+(?:подходят|интересны)|"
    r"что\s+дальше|следующ(?:ий|ие)\s+шаг"
    r")\b",
    re.IGNORECASE,
)
NEGATIVE_INTEREST_RE = re.compile(
    r"\b(не\s+(?:очень\s+|особо\s+|совсем\s+)?(?:интересно|актуально|заинтересовало)|"
    r"пока\s+не\s+(?:интересно|актуально|готов\w*)|без\s+интереса)\b",
    re.IGNORECASE,
)
LPR_ACTIVE_STATUSES = {"lpr_self", "lpr_ready", "lpr_needs_name"}
KRISHA_ALLOWED_HOST_RE = re.compile(r"(^|\.)krisha\.kz$", re.IGNORECASE)
KRISHA_LISTING_URL_RE = re.compile(r"https?://(?:www\.)?krisha\.kz/[^\s\"'<>]+|/(?:a/show|prodazha|arenda)/[^\s\"'<>]+", re.IGNORECASE)
KRISHA_DETAIL_URL_RE = re.compile(r"(?:https?://(?:www\.)?krisha\.kz)?/a/show/\d+", re.IGNORECASE)
KRISHA_CITY_SLUGS = {
    "астана": "astana",
    "нур-султан": "astana",
    "нурсултан": "astana",
    "алматы": "almaty",
    "шымкент": "shymkent",
    "караганда": "karaganda",
    "актобе": "aktobe",
    "атырау": "atyrau",
    "актау": "aktau",
    "павлодар": "pavlodar",
    "костанай": "kostanaj",
    "костанай": "kostanaj",
    "семей": "semej",
    "тараз": "taraz",
    "кызылорда": "kyzylorda",
    "кызылорда": "kyzylorda",
    "кокшетау": "kokshetau",
    "уральск": "uralsk",
    "орал": "uralsk",
    "петропавловск": "petropavlovsk",
    "талдыкорган": "taldykorgan",
    "туркестан": "turkestan",
    "экибастуз": "ekibastuz",
    "усть-каменогорск": "ust-kamenogorsk",
    "оскемен": "ust-kamenogorsk",
}
KRISHA_PROPERTY_PATHS = {
    "commercial": "prodazha/kommercheskaya-nedvizhimost",
    "any": "prodazha/kommercheskaya-nedvizhimost",
    "office": "prodazha/kommercheskaya-nedvizhimost/typi-ofisy",
    "retail": "prodazha/kommercheskaya-nedvizhimost/typi-magaziny_i_butiki",
    "warehouse": "prodazha/kommercheskaya-nedvizhimost/typi-sklady",
    "catering": "prodazha/kommercheskaya-nedvizhimost",
    "building": "prodazha/kommercheskaya-nedvizhimost",
}
KRISHA_PROPERTY_TYPES = {
    "any": [],
    "office": ["офис", "офисы", "офисное"],
    "retail": ["магазин", "бутик", "торгов", "ритейл", "street retail"],
    "warehouse": ["склад", "складское", "производств", "цех"],
    "catering": ["общепит", "кафе", "ресторан", "кофейн"],
    "building": ["здание", "особняк", "бизнес-центр", "бц"],
    "commercial": ["коммерчес", "помещен", "офис", "магазин", "склад", "здание", "общепит", "производств"],
}


class ImportRequest(BaseModel):
    upload_id: str
    phone_column: str | None = None
    phone_columns: list[str] | None = None
    company_column: str | None = None
    name_column: str | None = None
    signal_columns: list[str] | None = None


class KrishaImportRequest(BaseModel):
    source_text: str = Field(default="", max_length=250_000)
    city: str | None = None
    property_type: str | None = None
    keywords: str | None = None
    min_area: float | None = Field(default=None, ge=0)
    max_area: float | None = Field(default=None, ge=0)
    min_price: int | None = Field(default=None, ge=0)
    max_price: int | None = Field(default=None, ge=0)
    max_pages: int = Field(default=1, ge=1, le=10)
    max_contacts: int | None = Field(default=None, ge=1, le=10000)
    import_contacts: bool = True


class SettingsPayload(BaseModel):
    green_api_url: str | None = None
    green_media_url: str | None = None
    green_id_instance: str | None = None
    green_api_token: str | None = None
    default_country_code: str | None = None
    ai_provider: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_model: str | None = None
    openai_transcription_model: str | None = None
    groq_api_key: str | None = None
    groq_base_url: str | None = None
    groq_transcription_model: str | None = None
    deepseek_api_key: str | None = None
    deepseek_base_url: str | None = None
    deepseek_model: str | None = None
    ai_temperature: str | None = None
    audio_transcription_enabled: str | None = None
    bot_name: str | None = None
    ai_system_prompt: str | None = None
    max_lpr_attempts: str | None = None
    send_proposal_after_failed_attempts: str | None = None
    typing_enabled: str | None = None
    typing_min_seconds: str | None = None
    typing_max_seconds: str | None = None
    green_history_sync_minutes: str | None = None
    handoff_enabled: str | None = None
    handoff_phone: str | None = None
    auto_campaign_enabled: str | None = None
    auto_campaign_time: str | None = None
    auto_campaign_timezone: str | None = None
    auto_campaign_max_messages: str | None = None
    auto_campaign_delay_min_seconds: str | None = None
    auto_campaign_delay_max_seconds: str | None = None
    auto_campaign_target_kind: str | None = None
    krisha_login: str | None = None
    krisha_password: str | None = None
    krisha_city: str | None = None
    krisha_property_type: str | None = None
    krisha_keywords: str | None = None
    krisha_min_area: str | None = None
    krisha_max_area: str | None = None
    krisha_min_price: str | None = None
    krisha_max_price: str | None = None
    krisha_max_pages: str | None = None
    krisha_max_contacts: str | None = None
    krisha_interval_minutes: str | None = None
    krisha_use_browser: str | None = None
    krisha_headless: str | None = None


class CampaignStart(BaseModel):
    max_messages: int = Field(default=25, ge=1, le=10000)
    delay_min_seconds: int = Field(default=40, ge=1, le=86400)
    delay_max_seconds: int = Field(default=120, ge=1, le=86400)
    target_kind: str = "lead"


class AiToggle(BaseModel):
    enabled: bool


class AuthSetupPayload(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=200)


class LoginPayload(BaseModel):
    username: str
    password: str


class PasswordChangePayload(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


class ProjectPayload(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    product_name: str | None = None
    workflow_type: str = "generic_b2b"
    proposal_filename: str | None = None
    ai_system_prompt: str | None = None


class ProjectSwitchPayload(BaseModel):
    project_id: int


class ContactDeletePayload(BaseModel):
    contact_ids: list[int] = Field(default_factory=list)
    kind: str | None = None
    status: str | None = None
    q: str | None = None
    delete_all_matching: bool = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def add_column_if_missing(conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
    if column_name not in table_columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


def init_db() -> None:
    with db_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_settings (
                project_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(project_id, key),
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                product_name TEXT NOT NULL,
                workflow_type TEXT NOT NULL DEFAULT 'generic_b2b',
                proposal_filename TEXT NOT NULL,
                ai_system_prompt TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY,
                project_id INTEGER NOT NULL DEFAULT 1,
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                columns_json TEXT NOT NULL,
                preview_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL DEFAULT 1,
                phone_raw TEXT,
                phone TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'lead',
                source TEXT NOT NULL DEFAULT 'excel',
                company TEXT,
                name TEXT,
                whatsapp_exists INTEGER,
                status TEXT NOT NULL DEFAULT 'new',
                stage TEXT NOT NULL DEFAULT 'new',
                attempts INTEGER NOT NULL DEFAULT 0,
                proposal_sent INTEGER NOT NULL DEFAULT 0,
                owner_contact_id INTEGER,
                last_inbound_at TEXT,
                last_outbound_at TEXT,
                last_error TEXT,
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, phone, kind),
                FOREIGN KEY(project_id) REFERENCES projects(id),
                FOREIGN KEY(owner_contact_id) REFERENCES contacts(id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL DEFAULT 1,
                contact_id INTEGER,
                chat_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                text TEXT,
                channel_msg_id TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id),
                FOREIGN KEY(contact_id) REFERENCES contacts(id)
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                target_kind TEXT NOT NULL DEFAULT 'lead',
                max_messages INTEGER NOT NULL,
                delay_min_seconds INTEGER NOT NULL,
                delay_max_seconds INTEGER NOT NULL,
                sent_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                stopped_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS event_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL DEFAULT 1,
                level TEXT NOT NULL DEFAULT 'info',
                category TEXT NOT NULL,
                message TEXT NOT NULL,
                contact_id INTEGER,
                campaign_id INTEGER,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id),
                FOREIGN KEY(contact_id) REFERENCES contacts(id),
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
            );
            """
        )
        current_time = now_iso()
        conn.execute(
            """
            INSERT OR IGNORE INTO projects(
                id, slug, name, product_name, workflow_type, proposal_filename,
                ai_system_prompt, is_active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                DEFAULT_PROJECT_ID,
                "autoscore",
                "Автоскоринг",
                "White-label автоскоринг для автодилеров",
                "autoscore_responsible",
                DEFAULT_SETTINGS["proposal_filename"],
                DEFAULT_AI_PROMPT,
                current_time,
                current_time,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO projects(
                id, slug, name, product_name, workflow_type, proposal_filename,
                ai_system_prompt, is_active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                KERAMO_PROJECT_ID,
                "keramo-build",
                "KERAMO BUILD",
                "Инвестиции в производство керамогранита в Астане",
                KERAMO_WORKFLOW_TYPE,
                KERAMO_PROPOSAL_FILENAME,
                SECOND_PROJECT_AI_PROMPT,
                current_time,
                current_time,
            ),
        )
        conn.execute(
            """
            UPDATE projects
            SET slug = ?,
                name = ?,
                product_name = ?,
                workflow_type = ?,
                proposal_filename = ?,
                ai_system_prompt = ?,
                updated_at = ?
            WHERE id = ?
              AND (slug = 'second-product' OR name = 'Второй продукт' OR product_name = 'Новый продукт')
            """,
            (
                "keramo-build",
                "KERAMO BUILD",
                "Инвестиции в производство керамогранита в Астане",
                KERAMO_WORKFLOW_TYPE,
                KERAMO_PROPOSAL_FILENAME,
                SECOND_PROJECT_AI_PROMPT,
                current_time,
                KERAMO_PROJECT_ID,
            ),
        )
        for table_name in ("uploads", "contacts", "messages", "campaigns", "event_logs"):
            add_column_if_missing(conn, table_name, "project_id", "project_id INTEGER")
            conn.execute(
                f"UPDATE {table_name} SET project_id = ? WHERE project_id IS NULL",
                (DEFAULT_PROJECT_ID,),
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_project ON contacts(project_id, status, kind)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_project_phone_kind ON contacts(project_id, phone, kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project_id, contact_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_campaigns_project ON campaigns(project_id, status, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_logs_project ON event_logs(project_id, id)")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        conn.execute("DELETE FROM settings WHERE key = 'krisha_source_urls'")
        current_handoff = conn.execute("SELECT value FROM settings WHERE key = 'handoff_phone'").fetchone()
        if current_handoff and normalize_phone(current_handoff["value"]) == "77759419359":
            phones = parse_handoff_phones(current_handoff["value"])
            if "77015001995" not in phones:
                conn.execute(
                    "UPDATE settings SET value = ? WHERE key = 'handoff_phone'",
                    (", ".join([*phones, "77015001995"]),),
                )
        migrate_project_settings(conn)
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES ('admin_session_secret', ?)",
            (secrets.token_urlsafe(48),),
        )
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'ai_system_prompt' AND value = ''",
            (DEFAULT_AI_PROMPT,),
        )
        legacy_prompts = tuple(LEGACY_AI_PROMPTS)
        if legacy_prompts:
            conn.execute(
                f"UPDATE settings SET value = ? WHERE key = 'ai_system_prompt' AND value IN ({', '.join(['?'] * len(legacy_prompts))})",
                (DEFAULT_AI_PROMPT, *legacy_prompts),
            )
        backfill_message_text_from_payloads(conn)
    rebuild_contacts_unique_if_needed()


def contacts_table_sql(table_name: str = "contacts") -> str:
    return f"""
        CREATE TABLE {table_name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL DEFAULT 1,
            phone_raw TEXT,
            phone TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'lead',
            source TEXT NOT NULL DEFAULT 'excel',
            company TEXT,
            name TEXT,
            whatsapp_exists INTEGER,
            status TEXT NOT NULL DEFAULT 'new',
            stage TEXT NOT NULL DEFAULT 'new',
            attempts INTEGER NOT NULL DEFAULT 0,
            proposal_sent INTEGER NOT NULL DEFAULT 0,
            owner_contact_id INTEGER,
            last_inbound_at TEXT,
            last_outbound_at TEXT,
            last_error TEXT,
            meta_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, phone, kind),
            FOREIGN KEY(project_id) REFERENCES projects(id),
            FOREIGN KEY(owner_contact_id) REFERENCES contacts(id)
        )
    """


def contacts_has_legacy_global_unique(conn: sqlite3.Connection) -> bool:
    for index_row in conn.execute("PRAGMA index_list(contacts)").fetchall():
        if not int(index_row["unique"]):
            continue
        columns = [row["name"] for row in conn.execute(f"PRAGMA index_info({index_row['name']})").fetchall()]
        if columns == ["phone", "kind"]:
            return True
    return False


def rebuild_contacts_unique_if_needed() -> None:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        if not contacts_has_legacy_global_unique(conn):
            return
        current_time = now_iso()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.execute("ALTER TABLE contacts RENAME TO contacts_old_unique")
        conn.execute(contacts_table_sql("contacts"))
        conn.execute(
            """
            INSERT INTO contacts(
                id, project_id, phone_raw, phone, chat_id, kind, source, company, name,
                whatsapp_exists, status, stage, attempts, proposal_sent, owner_contact_id,
                last_inbound_at, last_outbound_at, last_error, meta_json, created_at, updated_at
            )
            SELECT
                id, COALESCE(project_id, 1), phone_raw, phone, chat_id, kind, source, company, name,
                whatsapp_exists, status, stage, attempts, proposal_sent, owner_contact_id,
                last_inbound_at, last_outbound_at, last_error, meta_json,
                COALESCE(created_at, ?), COALESCE(updated_at, ?)
            FROM contacts_old_unique
            """,
            (current_time, current_time),
        )
        conn.execute("DROP TABLE contacts_old_unique")
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_project ON contacts(project_id, status, kind)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_project_phone_kind ON contacts(project_id, phone, kind)")
    finally:
        conn.close()


def migrate_project_settings(conn: sqlite3.Connection) -> None:
    global_rows = conn.execute("SELECT key, value FROM settings").fetchall()
    global_values = {row["key"]: row["value"] for row in global_rows}
    project_rows = conn.execute("SELECT id FROM projects WHERE is_active = 1").fetchall()
    for project in project_rows:
        project_id = int(project["id"])
        for key in sorted(PROJECT_SCOPED_SETTING_KEYS):
            value = str(global_values.get(key, DEFAULT_SETTINGS.get(key, "")))
            conn.execute(
                """
                INSERT OR IGNORE INTO project_settings(project_id, key, value)
                VALUES (?, ?, ?)
                """,
                (project_id, key, value),
            )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_project_settings_key_value ON project_settings(key, value)")


def get_raw_settings(project_id: int | None = None) -> dict[str, str]:
    with db_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        project_rows = (
            conn.execute(
                "SELECT key, value FROM project_settings WHERE project_id = ?",
                (int(project_id),),
            ).fetchall()
            if project_id is not None
            else []
        )
    settings = DEFAULT_SETTINGS.copy()
    settings.update({row["key"]: row["value"] for row in rows})
    settings.update(
        {
            row["key"]: row["value"]
            for row in project_rows
            if row["key"] in PROJECT_SCOPED_SETTING_KEYS
        }
    )
    if not settings.get("ai_system_prompt"):
        settings["ai_system_prompt"] = DEFAULT_AI_PROMPT
    return settings


def get_setting(key: str, default: str = "") -> str:
    with db_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_setting(key: str, value: str) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def current_project_id() -> int:
    context_id = project_context_id.get()
    if context_id:
        return int(context_id)
    try:
        return int(get_setting("current_project_id", str(DEFAULT_PROJECT_ID)) or DEFAULT_PROJECT_ID)
    except ValueError:
        return DEFAULT_PROJECT_ID


@contextmanager
def use_project(project_id: int) -> Iterator[None]:
    token = project_context_id.set(int(project_id))
    try:
        yield
    finally:
        project_context_id.reset(token)


def get_project(project_id: int | None = None) -> dict[str, Any]:
    resolved_id = int(project_id or current_project_id())
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ? AND is_active = 1", (resolved_id,)).fetchone()
        if not row:
            row = conn.execute("SELECT * FROM projects WHERE id = ? AND is_active = 1", (DEFAULT_PROJECT_ID,)).fetchone()
    if not row:
        return {
            "id": DEFAULT_PROJECT_ID,
            "slug": "autoscore",
            "name": "Автоскоринг",
            "product_name": "White-label автоскоринг для автодилеров",
            "workflow_type": "autoscore_responsible",
            "proposal_filename": DEFAULT_SETTINGS["proposal_filename"],
            "ai_system_prompt": DEFAULT_AI_PROMPT,
        }
    return dict(row)


def list_projects() -> list[dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM projects WHERE is_active = 1 ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def project_id_for_green_instance(id_instance: Any) -> int | None:
    raw = safe_cell(id_instance)
    if not raw:
        return None
    current_id = current_project_id()
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.id
            FROM projects p
            JOIN project_settings ps ON ps.project_id = p.id
            WHERE p.is_active = 1
              AND ps.key = 'green_id_instance'
              AND ps.value = ?
            ORDER BY p.id
            """,
            (raw,),
        ).fetchall()
    ids = [int(row["id"]) for row in rows]
    if not ids:
        global_value = get_setting("green_id_instance", "")
        return current_id if global_value and global_value == raw else None
    if current_id in ids:
        return current_id
    return ids[0]


def project_id_from_notification_body(body: dict[str, Any]) -> int | None:
    if not isinstance(body, dict):
        return None
    message_data = body.get("messageData") or {}
    containers = [
        body.get("instanceData") or {},
        body,
        body.get("senderData") or {},
        message_data,
        message_data.get("journalData") or {},
        message_data.get("fileMessageData") or {},
        message_data.get("imageMessageData") or {},
        message_data.get("audioMessageData") or {},
    ]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("idInstance", "instanceId", "greenIdInstance"):
            project_id = project_id_for_green_instance(container.get(key))
            if project_id:
                return project_id
    return None


def get_settings(project_id: int | None = None) -> dict[str, str]:
    project = get_project(project_id)
    settings = get_raw_settings(int(project["id"]))
    settings["current_project_id"] = str(project["id"])
    settings["project_name"] = str(project.get("name") or "")
    settings["product_name"] = str(project.get("product_name") or "")
    settings["workflow_type"] = str(project.get("workflow_type") or "")
    settings["proposal_filename"] = str(project.get("proposal_filename") or DEFAULT_SETTINGS["proposal_filename"])
    settings["ai_system_prompt"] = str(project.get("ai_system_prompt") or settings.get("ai_system_prompt") or DEFAULT_AI_PROMPT)
    return settings


def update_settings(payload: dict[str, Any], project_id: int | None = None) -> dict[str, str]:
    allowed = set(DEFAULT_SETTINGS)
    project_updates: dict[str, Any] = {}
    target_project_id = int(project_id or current_project_id())
    with db_conn() as conn:
        for key, value in payload.items():
            if key not in allowed or value is None:
                continue
            cleaned_value = str(value).strip()
            if key in GLOBAL_SETTING_KEYS:
                conn.execute(
                    """
                    INSERT INTO settings(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, cleaned_value),
                )
                if key == "current_project_id":
                    try:
                        target_project_id = int(cleaned_value)
                    except ValueError:
                        target_project_id = DEFAULT_PROJECT_ID
                continue
            if key in PROJECT_SETTING_KEYS:
                project_updates[key] = cleaned_value
                continue
            if key not in PROJECT_SCOPED_SETTING_KEYS:
                continue
            conn.execute(
                """
                INSERT INTO project_settings(project_id, key, value) VALUES (?, ?, ?)
                ON CONFLICT(project_id, key) DO UPDATE SET value = excluded.value
                """,
                (target_project_id, key, cleaned_value),
            )
        if project_updates:
            pairs = []
            values: list[Any] = []
            for key, value in project_updates.items():
                pairs.append(f"{key} = ?")
                values.append(value)
            pairs.append("updated_at = ?")
            values.append(now_iso())
            values.append(target_project_id)
            conn.execute(f"UPDATE projects SET {', '.join(pairs)} WHERE id = ?", values)
    return get_settings(target_project_id)


def auth_configured() -> bool:
    return bool(get_setting("admin_username") and get_setting("admin_password_hash"))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def auth_secret() -> str:
    secret = get_setting("admin_session_secret")
    if not secret:
        secret = secrets.token_urlsafe(48)
        set_setting("admin_session_secret", secret)
    return secret


def create_session_cookie(username: str) -> str:
    expires = int((datetime.now(timezone.utc) + timedelta(days=AUTH_SESSION_DAYS)).timestamp())
    payload = f"{username}|{expires}|{secrets.token_urlsafe(16)}"
    signature = hmac.new(auth_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{signature}".encode("utf-8")).decode("ascii")


def validate_session_cookie(cookie_value: str | None) -> str | None:
    if not cookie_value:
        return None
    try:
        decoded = base64.urlsafe_b64decode(cookie_value.encode("ascii")).decode("utf-8")
        username, expires_raw, nonce, signature = decoded.split("|", 3)
        payload = f"{username}|{expires_raw}|{nonce}"
        expected = hmac.new(auth_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if int(expires_raw) < int(datetime.now(timezone.utc).timestamp()):
            return None
        if username != get_setting("admin_username"):
            return None
        return username
    except Exception:
        return None


def set_auth_cookie(response: JSONResponse | RedirectResponse, username: str) -> None:
    response.set_cookie(
        AUTH_COOKIE_NAME,
        create_session_cookie(username),
        httponly=True,
        secure=is_truthy(os.environ.get("AUTH_COOKIE_SECURE")),
        samesite="lax",
        max_age=AUTH_SESSION_DAYS * 86400,
        path="/",
    )


def clear_auth_cookie(response: JSONResponse | RedirectResponse) -> None:
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")


def current_admin(request: Request) -> str | None:
    return validate_session_cookie(request.cookies.get(AUTH_COOKIE_NAME))


def auth_public_path(path: str) -> bool:
    if path.startswith("/static/"):
        return True
    return path in {
        "/login",
        "/setup",
        "/api/auth/status",
        "/api/auth/login",
        "/api/auth/setup",
        "/api/webhook/greenapi",
    }


@app.middleware("http")
async def require_admin_auth(request: Request, call_next: Any) -> Any:
    path = request.url.path
    if auth_public_path(path):
        return await call_next(request)
    if not auth_configured():
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Требуется первичная настройка администратора", "setup_required": True}, status_code=401)
        return RedirectResponse("/setup")
    admin = current_admin(request)
    if admin:
        request.state.admin_username = admin
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Требуется вход в админку", "login_required": True}, status_code=401)
    return RedirectResponse("/login")


def log_event(
    category: str,
    message: str,
    *,
    level: str = "info",
    contact_id: int | None = None,
    campaign_id: int | None = None,
    project_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    resolved_project_id = int(project_id or current_project_id())
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO event_logs(project_id, level, category, message, contact_id, campaign_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_project_id,
                level,
                category,
                message,
                contact_id,
                campaign_id,
                json.dumps(payload or {}, ensure_ascii=False),
                now_iso(),
            ),
        )


def krisha_log(message: str, *, level: str = "info", payload: dict[str, Any] | None = None) -> None:
    log_event("krisha", message, level=level, payload=payload)


def masked_secret(value: str | None) -> str:
    raw = safe_cell(value)
    if not raw:
        return ""
    if len(raw) <= 4:
        return "*" * len(raw)
    return f"{raw[:2]}***{raw[-2:]}"


def is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "да"}


def optional_float(value: Any) -> float | None:
    raw = safe_cell(value)
    if not raw:
        return None
    try:
        return float(str(raw).replace(",", "."))
    except ValueError:
        return None


def optional_int(value: Any) -> int | None:
    raw = safe_cell(value)
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def is_interested_text(text: str) -> bool:
    return bool(INTEREST_RE.search(text or "") and not NEGATIVE_INTEREST_RE.search(text or ""))


def normalize_phone(value: Any, default_country_code: str = "7") -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, float) and value.is_integer():
        raw = str(int(value))
    else:
        raw = str(value).strip()
    if not raw or raw.lower() in {"nan", "none", "null"}:
        return None
    digits = re.sub(r"\D+", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10 and default_country_code:
        if default_country_code == "7" and digits[0] not in {"3", "4", "6", "7", "9"}:
            return None
        digits = f"{default_country_code}{digits}"
    if len(digits) == 11 and digits.startswith("8") and default_country_code == "7":
        digits = "7" + digits[1:]
    if 11 <= len(digits) <= 16:
        return digits
    return None


def extract_phones(value: Any, default_country_code: str = "7") -> list[str]:
    if value is None:
        return []
    if isinstance(value, float) and pd.isna(value):
        return []
    if isinstance(value, (int, float)):
        phone = normalize_phone(value, default_country_code)
        return [phone] if phone else []

    text = str(value)
    candidates = re.findall(r"\+?\d[\d\s().-]{8,}\d", text)
    if not candidates:
        candidates = [text]

    phones: list[str] = []
    for candidate in candidates:
        phone = normalize_phone(candidate, default_country_code)
        if phone and phone not in phones:
            phones.append(phone)
    return phones


def parse_handoff_phones(value: Any, default_country_code: str = "7") -> list[str]:
    raw = safe_cell(value)
    if not raw:
        return []
    phones: list[str] = []
    for part in re.split(r"[,;\n]+", raw):
        for phone in extract_phones(part, default_country_code):
            if phone not in phones:
                phones.append(phone)
    if not phones:
        phone = normalize_phone(raw, default_country_code)
        if phone:
            phones.append(phone)
    return phones


def chat_id_for_phone(phone: str) -> str:
    return f"{phone}@c.us"


def safe_cell(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def clip_text(text: str | None, limit: int = 120) -> str | None:
    value = safe_cell(text)
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1].rstrip()}…"


def row_signal_fields(row: Any, signal_columns: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for column in signal_columns:
        value = clip_text(row.get(column), limit=140)
        if value:
            fields[str(column)] = value
    return fields


def infer_sales_angle(company: str | None, signal_fields: dict[str, str]) -> str:
    joined = " ".join([company or "", *signal_fields.values()]).lower()
    if any(token in joined for token in ("автосалон", "авто салон", "автодил", "дилер", "trade", "трейд")):
        return "У вас похоже есть дилерское направление, поэтому пишу по цифровым заявкам и автоскорингу."
    if any(token in joined for token in ("кредит", "рассроч", "банк", "займ", "финанс")):
        return "Вижу связь с кредитными продажами, поэтому вопрос по цифровой анкете и предварительному скорингу."
    if any(token in joined for token in ("продаж", "отдел продаж", "crm", "црм", "заявк")):
        return "Похоже, у вас важна обработка заявок, поэтому пишу по цифровой воронке автокредитов."
    if company:
        return f"Пишу по коммерческому вопросу для {company}."
    return random.choice(GREETING_CONTEXTS)


def build_sales_context_meta(company: str | None, signal_fields: dict[str, str]) -> dict[str, Any]:
    if not signal_fields:
        return {}
    signal = "; ".join(f"{key}: {value}" for key, value in list(signal_fields.items())[:5])
    return {
        "sales_signal": compact_message(signal, limit=420),
        "sales_angle": infer_sales_angle(company, signal_fields),
        "signal_fields": signal_fields,
        "qualification_focus": "authority_need_capacity",
        "sales_framework": "research_first_laer_permission_based",
    }


def clean_krisha_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw or "")
    text = re.sub(r"(?i)<br\s*/?>|</(?:p|div|article|section|li|tr|td|h[1-6]|a)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def krisha_urls_from_source(source_text: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s\"'<>]+", source_text or "", flags=re.IGNORECASE):
        url = match.group(0).rstrip(").,;")
        parsed = urlparse(url)
        if parsed.netloc and KRISHA_ALLOWED_HOST_RE.search(parsed.netloc):
            if url not in urls:
                urls.append(url)
    return urls


def krisha_city_slug(city: str | None) -> str:
    raw = safe_cell(city).lower().replace("ё", "е")
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return "astana"
    if raw in KRISHA_CITY_SLUGS:
        return KRISHA_CITY_SLUGS[raw]
    ascii_slug = raw
    translit = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
        "и": "i", "й": "j", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
        "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch",
        "ш": "sh", "щ": "shh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
        "қ": "k", "ә": "a", "ң": "n", "ғ": "g", "ү": "u", "ұ": "u", "ө": "o", "һ": "h", "і": "i",
    }
    ascii_slug = "".join(translit.get(ch, ch) for ch in ascii_slug)
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", ascii_slug).strip("-")
    return ascii_slug or "astana"


def krisha_search_urls_from_payload(payload: KrishaImportRequest) -> list[str]:
    property_key = (payload.property_type or "commercial").strip().lower()
    path = KRISHA_PROPERTY_PATHS.get(property_key) or KRISHA_PROPERTY_PATHS["commercial"]
    city_slug = krisha_city_slug(payload.city)
    return [f"https://krisha.kz/{path}/{city_slug}/"]


def krisha_detail_urls_from_text(raw: str, limit: int = 120) -> list[str]:
    urls: list[str] = []
    for match in KRISHA_DETAIL_URL_RE.finditer(raw or ""):
        url = normalize_krisha_url(match.group(0).rstrip(").,;"))
        if url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def is_captcha_page(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        token in lowered
        for token in (
            "captcha-page",
            "form-captcha",
            "cf-chl",
            "подтвердите, что вы не робот",
            "подтвердите что вы не робот",
            "проверка безопасности",
            "are you a human",
            "i am not a robot",
        )
    )


def krisha_page_url(url: str, page: int) -> str:
    if page <= 1:
        return url
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunparse(parsed._replace(query=urlencode(query)))


def first_number_from_text(text: str, suffix_pattern: str) -> float | None:
    pattern = rf"(\d[\d\s.,]{{0,15}})\s*(?:{suffix_pattern})"
    match = re.search(pattern, text or "", flags=re.IGNORECASE)
    if not match:
        return None
    value = re.sub(r"[^\d.,]", "", match.group(1)).replace(",", ".")
    if value.count(".") > 1:
        value = value.replace(".", "")
    try:
        return float(value)
    except ValueError:
        return None


def krisha_price_from_text(text: str) -> int | None:
    value = first_number_from_text(text, r"₸|тг\.?|тенге")
    return int(value) if value and value >= 1000 else None


def krisha_area_from_text(text: str) -> float | None:
    value = first_number_from_text(text, r"м²|м2|кв\.?\s*м")
    return value if value and value <= 100_000 else None


def krisha_title_from_snippet(snippet: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip(" -•|") for line in snippet.splitlines()]
    for line in lines:
        if not 8 <= len(line) <= 120:
            continue
        lowered = line.lower()
        if extract_phones(line) or re.fullmatch(r"[\d\s.,₸тгм²м2-]+", lowered):
            continue
        if any(token in lowered for token in KRISHA_PROPERTY_TYPES["commercial"] + ["астана", "алматы", "аренда", "продажа"]):
            return line
    for line in lines:
        if 8 <= len(line) <= 90 and not extract_phones(line):
            return line
    return "Владелец коммерческой недвижимости"


def normalize_krisha_url(url: str) -> str:
    if url.startswith("/"):
        return f"https://krisha.kz{url}"
    return url


def nearest_krisha_url(raw: str, snippet: str) -> str | None:
    snippet_urls = KRISHA_LISTING_URL_RE.findall(snippet or "")
    if snippet_urls:
        return normalize_krisha_url(snippet_urls[0].rstrip(").,;"))
    urls = KRISHA_LISTING_URL_RE.findall(raw or "")
    if len(urls) == 1:
        return normalize_krisha_url(urls[0].rstrip(").,;"))
    return None


def krisha_property_tokens(payload: KrishaImportRequest) -> list[str]:
    property_key = (payload.property_type or "").strip().lower()
    return KRISHA_PROPERTY_TYPES.get(property_key, [])


def krisha_keyword_tokens(payload: KrishaImportRequest) -> list[str]:
    if not payload.keywords:
        return []
    return [
        token.strip().lower()
        for token in re.split(r"[,;\n]+", payload.keywords)
        if token.strip()
    ]


def krisha_lead_matches_filters(lead: dict[str, Any], payload: KrishaImportRequest) -> bool:
    combined = " ".join(
        str(lead.get(key) or "")
        for key in ("title", "snippet", "source_url", "source_scope")
    ).lower()
    city = safe_cell(payload.city)
    if city and city.lower() not in combined:
        return False
    property_tokens = krisha_property_tokens(payload)
    if property_tokens and not any(token in combined for token in property_tokens):
        return False
    for token in krisha_keyword_tokens(payload):
        if token and token not in combined:
            return False
    area = lead.get("area")
    if area is not None:
        if payload.min_area is not None and float(area) < float(payload.min_area):
            return False
        if payload.max_area is not None and float(area) > float(payload.max_area):
            return False
    price = lead.get("price")
    if price is not None:
        if payload.min_price is not None and int(price) < int(payload.min_price):
            return False
        if payload.max_price is not None and int(price) > int(payload.max_price):
            return False
    return True


def extract_krisha_leads_from_text(raw: str, payload: KrishaImportRequest, default_country_code: str = "7") -> list[dict[str, Any]]:
    text = clean_krisha_text(raw)
    leads: list[dict[str, Any]] = []
    seen: set[str] = set()
    max_contacts = int(payload.max_contacts or 0)
    phone_pattern = r"(?:\+?7|8)[\s(.-]*7\d{2}[\s). -]*\d[\d\s().-]{5,}\d"
    for match in re.finditer(phone_pattern, text):
        phone = normalize_phone(match.group(0), default_country_code)
        if not phone or phone in seen:
            continue
        start = max(0, match.start() - 700)
        end = min(len(text), match.end() + 700)
        snippet = text[start:end].strip()
        lead = {
            "phone": phone,
            "phone_raw": match.group(0),
            "title": krisha_title_from_snippet(snippet),
            "source_url": nearest_krisha_url(raw, snippet),
            "area": krisha_area_from_text(snippet),
            "price": krisha_price_from_text(snippet),
            "city": safe_cell(payload.city),
            "snippet": compact_message(snippet, limit=700),
            "source_scope": compact_message(text, limit=3000),
        }
        if not krisha_lead_matches_filters(lead, payload):
            continue
        seen.add(phone)
        leads.append(lead)
        if max_contacts and len(leads) >= max_contacts:
            break
    return leads


def krisha_collection_target_reached(
    payload: KrishaImportRequest,
    sources: list[dict[str, str]],
    default_country_code: str = "7",
) -> bool:
    if not payload.max_contacts:
        return False
    seen: set[str] = set()
    for item in sources:
        for lead in extract_krisha_leads_from_text(item["text"], payload, default_country_code):
            seen.add(lead["phone"])
            if len(seen) >= payload.max_contacts:
                return True
    return False


def krisha_payload_from_settings(settings: dict[str, str] | None = None) -> KrishaImportRequest:
    resolved = settings or get_settings()
    max_contacts = optional_int(resolved.get("krisha_max_contacts"))
    return KrishaImportRequest(
        source_text="",
        city=safe_cell(resolved.get("krisha_city")),
        property_type=safe_cell(resolved.get("krisha_property_type")) or "commercial",
        keywords=safe_cell(resolved.get("krisha_keywords")),
        min_area=optional_float(resolved.get("krisha_min_area")),
        max_area=optional_float(resolved.get("krisha_max_area")),
        min_price=optional_int(resolved.get("krisha_min_price")),
        max_price=optional_int(resolved.get("krisha_max_price")),
        max_pages=max(1, min(optional_int(resolved.get("krisha_max_pages")) or 1, 10)),
        max_contacts=max(1, min(max_contacts, 10000)) if max_contacts else None,
        import_contacts=True,
    )


async def collect_krisha_sources_httpx(payload: KrishaImportRequest, urls: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    collected: list[dict[str, str]] = []
    errors: list[str] = []
    search_urls = urls or krisha_search_urls_from_payload(payload)
    seen_details: set[str] = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; HOLODKA_BOT/1.0; +local parser)",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    }
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
        for url in search_urls:
            for page in range(1, payload.max_pages + 1):
                page_url = krisha_page_url(url, page)
                try:
                    response = await client.get(page_url)
                    response.raise_for_status()
                    if is_captcha_page(response.text):
                        errors.append(f"{page_url}: captcha")
                        break
                    collected.append({"source": page_url, "text": response.text})
                    if krisha_collection_target_reached(payload, collected):
                        return collected, errors
                    for detail_url in krisha_detail_urls_from_text(response.text):
                        if detail_url in seen_details:
                            continue
                        seen_details.add(detail_url)
                        try:
                            detail_response = await client.get(detail_url)
                            detail_response.raise_for_status()
                            if is_captcha_page(detail_response.text):
                                errors.append(f"{detail_url}: captcha")
                                continue
                            collected.append({"source": detail_url, "text": detail_response.text})
                            if krisha_collection_target_reached(payload, collected):
                                return collected, errors
                        except Exception as exc:
                            errors.append(f"{detail_url}: {exc}")
                except Exception as exc:
                    errors.append(f"{page_url}: {exc}")
                    break
    return collected, errors


async def krisha_browser_auth_prompt_visible(page: Any) -> bool:
    return bool(await page.locator(".auth-modal, input[type='password']").count())


async def krisha_browser_is_login_finished(page: Any) -> bool:
    if is_captcha_page(await page.content()):
        return False
    if "id.kolesa.kz" in page.url or "/login" in page.url:
        return False
    if await page.locator("input[type='password'], input[name='password']").count():
        return False
    if await page.locator("input[name='code'], input[type='tel'][autocomplete='one-time-code']").count():
        return False
    return True


async def krisha_wait_for_login_result(page: Any, context: Any, storage_state_path: Path, errors: list[str], *, headless: bool) -> bool:
    wait_seconds = 35 if headless else 180
    krisha_log("Ожидаю завершения авторизации Krisha", payload={"url": page.url, "timeout_seconds": wait_seconds})
    for _ in range(wait_seconds):
        content = await page.content()
        if is_captcha_page(content):
            errors.append("login: captcha")
            krisha_log("Krisha запросила CAPTCHA на авторизации", level="warning", payload={"url": page.url})
            return False
        if await krisha_browser_is_login_finished(page):
            await context.storage_state(path=str(storage_state_path))
            krisha_log("Авторизация Krisha завершена, сессия сохранена", payload={"url": page.url, "storage_state": str(storage_state_path)})
            return True
        await page.wait_for_timeout(1000)
    errors.append("login: auth_timeout")
    krisha_log("Авторизация Krisha не завершилась за время ожидания", level="warning", payload={"url": page.url, "timeout_seconds": wait_seconds})
    return False


async def krisha_click_or_enter(page: Any, locator: Any, fallback_locator: Any | None = None) -> str:
    try:
        if await locator.count():
            await locator.first.click(timeout=5000)
            return "click"
    except Exception:
        pass
    try:
        if fallback_locator is not None and await fallback_locator.count():
            await fallback_locator.first.press("Enter")
        else:
            await page.keyboard.press("Enter")
        return "enter"
    except Exception:
        return "none"


KRISHA_POPUP_CLOSE_SELECTORS = [
    ".tutorial__close",
    ".modal__close",
    ".kr-modal__close",
    ".popup__close",
    ".close-button",
    ".close-btn",
    "[data-testid='modal-close']",
    "[aria-label='Закрыть']",
    "[title='Закрыть']",
]

KRISHA_POPUP_BUTTON_SELECTORS = [
    "button:has-text('Не сейчас')",
    "button:has-text('Позже')",
    "button:has-text('Понятно')",
    "button:has-text('Закрыть')",
    "button:has-text('Пропустить')",
    "button:has-text('Нет, спасибо')",
    "button:has-text('Отмена')",
]


async def close_krisha_popups(page: Any, *, reason: str = "") -> int:
    closed = 0
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(150)
    except Exception:
        pass

    auth_visible = False
    try:
        auth_visible = bool(await page.locator(".auth-modal, input[type='password']").count())
    except Exception:
        auth_visible = False

    selectors = list(KRISHA_POPUP_CLOSE_SELECTORS)
    if not auth_visible:
        selectors.append(".vue-modal__close-btn")
    selectors.extend(KRISHA_POPUP_BUTTON_SELECTORS)

    for _ in range(2):
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = min(await locator.count(), 4)
                for index in range(count):
                    item = locator.nth(index)
                    try:
                        if await item.is_visible():
                            await item.click(timeout=1200)
                            closed += 1
                            await page.wait_for_timeout(250)
                    except Exception:
                        continue
            except Exception:
                continue

    if closed:
        krisha_log("Закрыл всплывающие окна Krisha", payload={"count": closed, "reason": reason, "url": page.url})
    return closed


async def click_krisha_phone_button(page: Any, detail_url: str) -> bool:
    await close_krisha_popups(page, reason="before_phone_click")
    phone_button = page.locator("button.show-phones, button:has-text('Показать телефон')").first
    if not await phone_button.count():
        return False
    try:
        krisha_log("Нажимаю кнопку показа телефона", payload={"url": detail_url})
        await phone_button.click(timeout=6000)
        return True
    except Exception as exc:
        krisha_log(
            f"Обычный клик по телефону заблокирован, закрываю попапы и пробую еще раз: {exc}",
            level="warning",
            payload={"url": detail_url},
        )
        await close_krisha_popups(page, reason="phone_click_retry")
        phone_button = page.locator("button.show-phones, button:has-text('Показать телефон')").first
        if not await phone_button.count():
            return False
        try:
            await phone_button.click(timeout=6000, force=True)
            krisha_log("Кнопка показа телефона нажата повторно", payload={"url": detail_url})
            return True
        except Exception as retry_exc:
            krisha_log(f"Не удалось нажать кнопку показа телефона: {retry_exc}", level="error", payload={"url": detail_url})
            return False


async def krisha_browser_login(page: Any, context: Any, settings: dict[str, str], storage_state_path: Path, errors: list[str]) -> bool:
    login = safe_cell(settings.get("krisha_login"))
    password = safe_cell(settings.get("krisha_password"))
    headless = is_truthy(settings.get("krisha_headless"))
    if not login or not password:
        return True

    try:
        krisha_log("Открываю Krisha для авторизации", payload={"login": masked_secret(login), "headless": headless})
        await page.goto("https://krisha.kz/my", wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(800)
        krisha_log("Страница авторизации/кабинета открыта", payload={"url": page.url})
        if is_captcha_page(await page.content()):
            errors.append("login: captcha")
            krisha_log("Krisha запросила CAPTCHA до ввода логина", level="warning", payload={"url": page.url})
            return False

        login_inputs = page.locator(
            "input[type='tel'], input[name='login'], input[name='phone'], input[name='email'], input[type='email'], input[type='text']"
        )
        password_inputs = page.locator("input[type='password'], input[name='password']")
        if not await login_inputs.count() and not await password_inputs.count() and "login" not in page.url:
            await context.storage_state(path=str(storage_state_path))
            krisha_log("Сессия Krisha уже активна", payload={"url": page.url})
            return True

        if not await login_inputs.count():
            krisha_log("Форма логина не найдена, открываю страницу id.kolesa.kz", payload={"url": page.url})
            await page.goto(
                "https://id.kolesa.kz/login/?destination=https%3A%2F%2Fkrisha.kz%2Fmy",
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            await page.wait_for_timeout(800)
            login_inputs = page.locator(
                "input[type='tel'], input[name='login'], input[name='phone'], input[name='email'], input[type='email'], input[type='text']"
            )
            password_inputs = page.locator("input[type='password'], input[name='password']")

        if is_captcha_page(await page.content()):
            errors.append("login: captcha")
            krisha_log("Krisha запросила CAPTCHA на странице id.kolesa.kz", level="warning", payload={"url": page.url})
            return False

        if await login_inputs.count():
            await login_inputs.first.fill(login)
            krisha_log("Логин Krisha введен, отправляю первый шаг", payload={"url": page.url, "login": masked_secret(login)})
            submit = page.locator("button[type='submit'], input[type='submit'], button:has-text('Продолжить'), button:has-text('Войти'), button:has-text('Далее'), button:has-text('Кіру')").first
            method = await krisha_click_or_enter(page, submit, login_inputs)
            krisha_log("Первый шаг авторизации отправлен", payload={"url": page.url, "method": method})
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=20_000)
            except Exception:
                pass
            try:
                await page.wait_for_selector("input[type='password'], input[name='password']", timeout=12_000)
                krisha_log("Поле пароля появилось", payload={"url": page.url})
            except Exception:
                krisha_log("Поле пароля не появилось после первого шага", level="warning", payload={"url": page.url})
            await page.wait_for_timeout(600)

        password_inputs = page.locator("input[type='password'], input[name='password']")
        if await password_inputs.count():
            await password_inputs.first.fill(password)
            krisha_log("Пароль Krisha введен, отправляю вход", payload={"url": page.url})
            submit = page.locator("button[type='submit'], input[type='submit'], button:has-text('Войти'), button:has-text('Продолжить'), button:has-text('Далее'), button:has-text('Кіру')").first
            method = await krisha_click_or_enter(page, submit, password_inputs)
            krisha_log("Шаг пароля отправлен", payload={"url": page.url, "method": method})
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception:
                pass
            await page.wait_for_timeout(1200)
        else:
            krisha_log("Поле пароля не найдено, перехожу к ожиданию результата авторизации", level="warning", payload={"url": page.url})

        content = await page.content()
        if is_captcha_page(content):
            errors.append("login: captcha")
            krisha_log("Krisha запросила CAPTCHA после отправки пароля", level="warning", payload={"url": page.url})
            return False
        return await krisha_wait_for_login_result(page, context, storage_state_path, errors, headless=headless)
    except Exception as exc:
        errors.append(f"login: {exc}")
        krisha_log(f"Ошибка авторизации Krisha: {exc}", level="error", payload={"url": getattr(page, "url", "")})
        return False


async def collect_krisha_sources_browser(
    payload: KrishaImportRequest,
    urls: list[str],
    settings: dict[str, str],
) -> tuple[list[dict[str, str]], list[str]]:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        return [], [f"Playwright недоступен: {exc}. Установите браузер: python -m playwright install chromium"]

    collected: list[dict[str, str]] = []
    errors: list[str] = []
    storage_state_path = DATA_DIR / "krisha_storage_state.json"
    search_urls = urls or krisha_search_urls_from_payload(payload)
    seen_details: set[str] = set()
    default_country_code = settings.get("default_country_code", "7")

    async with async_playwright() as p:
        headless = is_truthy(settings.get("krisha_headless"))
        krisha_log("Запускаю браузер Krisha", payload={"headless": headless, "search_urls": search_urls, "max_pages": payload.max_pages})
        browser = await p.chromium.launch(headless=headless)
        context_kwargs: dict[str, Any] = {
            "locale": "ru-RU",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        }
        if storage_state_path.exists():
            context_kwargs["storage_state"] = str(storage_state_path)
            krisha_log("Загружаю сохраненную сессию Krisha", payload={"storage_state": str(storage_state_path)})
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        try:
            if safe_cell(settings.get("krisha_login")) and safe_cell(settings.get("krisha_password")):
                logged_in = await krisha_browser_login(page, context, settings, storage_state_path, errors)
                if not logged_in:
                    krisha_log("Авторизация Krisha не завершена, парсинг остановлен", level="warning", payload={"errors": errors[-5:]})
                    return collected, errors

            for url in search_urls:
                for page_num in range(1, payload.max_pages + 1):
                    page_url = krisha_page_url(url, page_num)
                    try:
                        krisha_log("Открываю страницу поиска Krisha", payload={"url": page_url, "page": page_num})
                        await page.goto(page_url, wait_until="domcontentloaded", timeout=45_000)
                        await page.wait_for_timeout(1200)
                        await close_krisha_popups(page, reason="search_page_loaded")
                        content = await page.content()
                        if is_captcha_page(content):
                            errors.append(f"{page_url}: captcha")
                            krisha_log("CAPTCHA на странице поиска Krisha", level="warning", payload={"url": page_url})
                            return collected, errors
                        collected.append({"source": page_url, "text": content})
                        if krisha_collection_target_reached(payload, collected, default_country_code):
                            krisha_log("Лимит номеров Krisha достигнут на странице поиска", payload={"max_contacts": payload.max_contacts, "url": page_url})
                            return collected, errors
                        detail_urls = krisha_detail_urls_from_text(content)
                        krisha_log("Страница поиска Krisha обработана", payload={"url": page_url, "detail_urls": len(detail_urls)})
                        for detail_url in detail_urls:
                            if detail_url in seen_details:
                                continue
                            seen_details.add(detail_url)
                            try:
                                krisha_log("Открываю объявление Krisha", payload={"url": detail_url})
                                await page.goto(detail_url, wait_until="domcontentloaded", timeout=45_000)
                                await page.wait_for_timeout(1000)
                                await close_krisha_popups(page, reason="detail_page_loaded")
                                if is_captcha_page(await page.content()):
                                    errors.append(f"{detail_url}: captcha")
                                    krisha_log("CAPTCHA в объявлении Krisha", level="warning", payload={"url": detail_url})
                                    return collected, errors

                                clicked_phone = await click_krisha_phone_button(page, detail_url)
                                if clicked_phone:
                                    await page.wait_for_timeout(2200)
                                    if await krisha_browser_auth_prompt_visible(page):
                                        if safe_cell(settings.get("krisha_login")) and safe_cell(settings.get("krisha_password")):
                                            krisha_log("Krisha запросила авторизацию при показе телефона", payload={"url": detail_url})
                                            logged_in = await krisha_browser_login(page, context, settings, storage_state_path, errors)
                                            if not logged_in:
                                                krisha_log("Авторизация на показе телефона не завершена", level="warning", payload={"url": detail_url, "errors": errors[-5:]})
                                                return collected, errors
                                            await page.goto(detail_url, wait_until="domcontentloaded", timeout=45_000)
                                            await page.wait_for_timeout(800)
                                            await close_krisha_popups(page, reason="after_auth_return_to_detail")
                                            if await click_krisha_phone_button(page, detail_url):
                                                await page.wait_for_timeout(2200)
                                        else:
                                            errors.append(f"{detail_url}: auth_required")
                                            krisha_log("Для показа телефона нужна авторизация Krisha", level="warning", payload={"url": detail_url})
                                else:
                                    krisha_log("Кнопка показа телефона не найдена в объявлении", level="warning", payload={"url": detail_url})

                                detail_content = await page.content()
                                if is_captcha_page(detail_content):
                                    errors.append(f"{detail_url}: captcha")
                                    krisha_log("CAPTCHA после показа телефона", level="warning", payload={"url": detail_url})
                                    return collected, errors
                                collected.append({"source": detail_url, "text": detail_content})
                                if krisha_collection_target_reached(payload, collected, default_country_code):
                                    krisha_log("Лимит номеров Krisha достигнут на объявлении", payload={"max_contacts": payload.max_contacts, "url": detail_url})
                                    return collected, errors
                            except Exception as exc:
                                errors.append(f"{detail_url}: {exc}")
                                krisha_log(f"Ошибка обработки объявления Krisha: {exc}", level="error", payload={"url": detail_url})
                    except Exception as exc:
                        errors.append(f"{page_url}: {exc}")
                        krisha_log(f"Ошибка страницы поиска Krisha: {exc}", level="error", payload={"url": page_url})
                        break
            await context.storage_state(path=str(storage_state_path))
            krisha_log("Сессия Krisha сохранена после парсинга", payload={"storage_state": str(storage_state_path), "sources": len(collected), "errors": len(errors)})
        finally:
            await context.close()
            await browser.close()
            krisha_log("Браузер Krisha закрыт", payload={"sources": len(collected), "errors": len(errors)})
    return collected, errors


async def collect_krisha_source_texts(
    payload: KrishaImportRequest,
    settings: dict[str, str] | None = None,
) -> tuple[list[dict[str, str]], list[str]]:
    resolved_settings = settings or get_settings()
    source_text = payload.source_text or ""
    urls = krisha_urls_from_source(source_text)
    collected: list[dict[str, str]] = []
    errors: list[str] = []
    should_fetch = bool(urls) or not source_text.strip()
    if should_fetch:
        if is_truthy(resolved_settings.get("krisha_use_browser")):
            collected, errors = await collect_krisha_sources_browser(payload, urls, resolved_settings)
            blocking_browser_error = any(
                "captcha" in error.lower()
                or "auth" in error.lower()
                or error.lower().startswith("login:")
                for error in errors
            )
            if not collected and not blocking_browser_error:
                http_collected, http_errors = await collect_krisha_sources_httpx(payload, urls)
                collected.extend(http_collected)
                errors.extend(http_errors)
        else:
            collected, errors = await collect_krisha_sources_httpx(payload, urls)
    if source_text.strip():
        collected.append({"source": "pasted", "text": source_text})
    return collected, errors


def krisha_contact_meta(lead: dict[str, Any], payload: KrishaImportRequest) -> dict[str, Any]:
    signal_fields = {
        "Источник": "Krisha",
        "Объект": str(lead.get("title") or ""),
    }
    if lead.get("city"):
        signal_fields["Город"] = str(lead["city"])
    if lead.get("area"):
        signal_fields["Площадь"] = f"{lead['area']} м²"
    if lead.get("price"):
        signal_fields["Цена"] = f"{lead['price']} ₸"
    if lead.get("source_url"):
        signal_fields["Ссылка"] = str(lead["source_url"])
    meta = build_sales_context_meta(str(lead.get("title") or ""), signal_fields)
    meta.update(
        {
            "lead_source": "krisha",
            "source_url": lead.get("source_url"),
            "krisha_area": lead.get("area"),
            "krisha_price": lead.get("price"),
            "krisha_city": lead.get("city") or safe_cell(payload.city),
            "krisha_snippet": lead.get("snippet"),
            "sales_angle": "Вижу связь с коммерческой недвижимостью, поэтому аккуратно пишу по инвестиционной возможности KERAMO BUILD в производственный бизнес.",
            "qualification_focus": "investment_fit_capacity_authority",
        }
    )
    return meta


def krisha_has_captcha_error(errors: list[str]) -> bool:
    return any("captcha" in str(error).lower() or "капч" in str(error).lower() for error in errors)


def krisha_has_auth_error(errors: list[str]) -> bool:
    return any(
        "auth" in str(error).lower()
        or str(error).lower().startswith("login:")
        for error in errors
    )


def save_krisha_leads(leads: list[dict[str, Any]], payload: KrishaImportRequest, project_id: int | None = None) -> tuple[int, int]:
    imported = 0
    updated = 0
    resolved_project_id = int(project_id or current_project_id())
    for lead in leads:
        with db_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM contacts WHERE project_id = ? AND phone = ?",
                (resolved_project_id, lead["phone"]),
            ).fetchone()
        create_or_update_contact(
            phone=lead["phone"],
            kind="lead",
            source="krisha",
            project_id=resolved_project_id,
            phone_raw=lead.get("phone_raw"),
            company=lead.get("title") or "Владелец коммерческой недвижимости",
            name=None,
            status="new",
            stage="imported",
            meta=krisha_contact_meta(lead, payload),
        )
        if existing:
            updated += 1
        else:
            imported += 1
    return imported, updated


async def run_krisha_import_cycle(
    payload: KrishaImportRequest,
    *,
    project_id: int | None = None,
    start_whatsapp_check: bool = True,
) -> dict[str, Any]:
    resolved_project_id = int(project_id or current_project_id())
    with use_project(resolved_project_id):
        settings = get_settings()
        sources, source_errors = await collect_krisha_source_texts(payload, settings)
        leads: list[dict[str, Any]] = []
        seen: set[str] = set()
        max_contacts = int(payload.max_contacts or 0)
        for item in sources:
            for lead in extract_krisha_leads_from_text(item["text"], payload, settings.get("default_country_code", "7")):
                if lead["phone"] in seen:
                    continue
                seen.add(lead["phone"])
                if not lead.get("source_url") and item["source"] != "pasted":
                    lead["source_url"] = item["source"]
                leads.append(lead)
                if max_contacts and len(leads) >= max_contacts:
                    break
            if max_contacts and len(leads) >= max_contacts:
                break

        imported = 0
        updated = 0
        if payload.import_contacts:
            imported, updated = save_krisha_leads(leads, payload, resolved_project_id)

        check_started = False
        if start_whatsapp_check and payload.import_contacts and leads:
            if GreenApiClient(settings).configured:
                start_check_task()
                check_started = True
            else:
                runtime_state["check"] = {
                    "status": "waiting_greenapi",
                    "processed": 0,
                    "total": len(leads),
                    "last_error": None,
                    "project_id": resolved_project_id,
                }
                log_event(
                    "whatsapp",
                    "Проверка WhatsApp для Krisha-импорта ожидает настройки GreenAPI",
                    level="warning",
                )

        log_event(
            "import",
            f"Krisha-импорт завершен: найдено {len(leads)}, новых {imported}, обновлено {updated}",
            level="warning" if source_errors else "info",
            payload={
                "found": len(leads),
                "imported": imported,
                "updated": updated,
                "source_errors": source_errors[:10],
                "filters": {key: value for key, value in payload.model_dump().items() if key != "source_text"},
            },
        )
        return {
            "ok": True,
            "found": len(leads),
            "imported": imported,
            "updated": updated,
            "check_started": check_started,
            "max_contacts": payload.max_contacts,
            "source_errors": source_errors[:10],
            "preview": [{key: value for key, value in lead.items() if key != "source_scope"} for lead in leads[:50]],
        }


async def krisha_parser_worker(project_id: int) -> None:
    with use_project(project_id):
        runtime_state["krisha_parser"] = {
            "status": "running",
            "processed": 0,
            "found": 0,
            "imported": 0,
            "updated": 0,
            "max_contacts": None,
            "last_error": None,
            "last_run_at": None,
            "project_id": project_id,
        }
        log_event("import", "Фоновый Krisha-парсер запущен")
        try:
            while True:
                settings = get_settings()
                payload = krisha_payload_from_settings(settings)
                runtime_state["krisha_parser"]["status"] = "running"
                runtime_state["krisha_parser"]["max_contacts"] = payload.max_contacts
                runtime_state["krisha_parser"]["last_error"] = None
                runtime_state["krisha_parser"]["last_run_at"] = now_iso()
                result = await run_krisha_import_cycle(payload, project_id=project_id, start_whatsapp_check=True)
                runtime_state["krisha_parser"]["processed"] = int(runtime_state["krisha_parser"].get("processed") or 0) + 1
                runtime_state["krisha_parser"]["found"] = result["found"]
                runtime_state["krisha_parser"]["imported"] = result["imported"]
                runtime_state["krisha_parser"]["updated"] = result["updated"]
                runtime_state["krisha_parser"]["source_errors"] = result["source_errors"]

                if krisha_has_captcha_error(result["source_errors"]):
                    runtime_state["krisha_parser"]["status"] = "captcha_required"
                    runtime_state["krisha_parser"]["last_error"] = "Krisha запросила CAPTCHA. Откройте браузерный режим вручную и повторите запуск."
                    log_event("import", "Krisha-парсер остановлен: требуется CAPTCHA", level="warning")
                    return
                if krisha_has_auth_error(result["source_errors"]):
                    runtime_state["krisha_parser"]["status"] = "auth_required"
                    runtime_state["krisha_parser"]["last_error"] = "Авторизация Krisha не завершена. Откройте браузерный режим, завершите вход и повторите запуск."
                    log_event("import", "Krisha-парсер остановлен: требуется завершить авторизацию", level="warning")
                    return

                interval_minutes = optional_int(settings.get("krisha_interval_minutes")) or 120
                runtime_state["krisha_parser"]["status"] = "sleeping"
                await asyncio.sleep(max(1, interval_minutes) * 60)
        except asyncio.CancelledError:
            runtime_state["krisha_parser"]["status"] = "cancelled"
            log_event("import", "Фоновый Krisha-парсер остановлен")
            raise
        except Exception as exc:
            runtime_state["krisha_parser"]["status"] = "error"
            runtime_state["krisha_parser"]["last_error"] = str(exc)
            log_event("import", f"Ошибка фонового Krisha-парсера: {exc}", level="error")


def contact_sales_context(contact: dict[str, Any]) -> dict[str, Any]:
    meta = contact_meta(contact)
    return {
        "sales_signal": meta.get("sales_signal") or "",
        "sales_angle": meta.get("sales_angle") or "",
        "signal_fields": meta.get("signal_fields") if isinstance(meta.get("signal_fields"), dict) else {},
        "qualification_focus": meta.get("qualification_focus") or "authority_need_capacity",
    }


def bot_name(settings: dict[str, str] | None = None) -> str | None:
    resolved_settings = settings or get_settings()
    return safe_cell(resolved_settings.get("bot_name"))


def uses_keramo_investor_flow(project: dict[str, Any] | None = None) -> bool:
    resolved_project = project or get_project()
    return str(resolved_project.get("workflow_type") or "") == KERAMO_WORKFLOW_TYPE


def project_topic_text(project: dict[str, Any] | None = None) -> str:
    resolved_project = project or get_project()
    if uses_keramo_investor_flow(resolved_project):
        return "инвестиционному предложению KERAMO BUILD: производство, нарезка и монтаж керамогранита в Астане"
    if str(resolved_project.get("workflow_type") or "") == "generic_b2b":
        product_name = clip_text(str(resolved_project.get("product_name") or "нашему продукту"), limit=110)
        return str(product_name or "нашему продукту")
    return "white-label приложению автоскоринга для автодилеров"


def agent_intro_text(settings: dict[str, str] | None = None) -> str:
    resolved_settings = settings or get_settings()
    name = bot_name(resolved_settings)
    if name:
        return f"Меня зовут {name}."
    project = get_project()
    if uses_keramo_investor_flow(project):
        return "Я из команды KERAMO BUILD."
    if project.get("workflow_type") == "generic_b2b":
        return f"Я из команды проекта по {project_topic_text(project)}."
    return "Я из команды проекта по автоскорингу для автодилеров."


def identity_reply_text(settings: dict[str, str] | None = None) -> str:
    project = get_project()
    if uses_keramo_investor_flow(project):
        return compact_message(
            f"{agent_intro_text(settings)} Пишу по инвестиционному предложению: производство керамогранита в Астане, 35 млн тенге, базовый возврат 25 месяцев и 30% прибыли после возврата.",
            limit=260,
        )
    if project.get("workflow_type") == "generic_b2b":
        return compact_message(
            f"{agent_intro_text(settings)} Пишу по короткому коммерческому вопросу: {project_topic_text(project)}.",
            limit=240,
        )
    return compact_message(
        f"{agent_intro_text(settings)} Пишу по короткому коммерческому вопросу: мы делаем white-label приложение для автодилеров с цифровыми заявками и автоскорингом.",
        limit=250,
    )


def clarify_offer_reply(contact: dict[str, Any]) -> str:
    project = get_project()
    product_name = clip_text(str(project.get("product_name") or ""), limit=90)
    if uses_keramo_investor_flow(project):
        return (
            "Коротко: KERAMO BUILD ищет инвестора в производство керамогранита в Астане. "
            "Сумма 35 млн тенге, базовый план возврата 25 месяцев, затем 30% прибыли. Вам это интересно как инвестиционная тема?"
        )
    if product_name and project.get("workflow_type") == "generic_b2b":
        return f"Коротко: речь о {product_name}. Если это не к вам, подскажите, пожалуйста, кто у вас смотрит такие вопросы?"
    return (
        "Коротко: это приложение для автодилера с онлайн-заявкой на автокредит и предварительной оценкой клиента. "
        "Если это не к вам, подскажите, пожалуйста, кто у вас это смотрит?"
    )


def proposal_offer_reply() -> str:
    return "Понял. Если удобно, пришлю короткое КП сюда в WhatsApp?"


def uses_autoscore_warmup_flow(project: dict[str, Any] | None = None) -> bool:
    resolved_project = project or get_project()
    return str(resolved_project.get("workflow_type") or "") == "autoscore_responsible"


def warmup_credit_check_reply(contact: dict[str, Any]) -> str:
    update_contact_fields(contact["id"], status="replied", stage="warmup_credit_check")
    return "Подскажите, у вас есть продажи авто через автокредит?"


def warmup_decline_reply(contact: dict[str, Any]) -> str:
    update_contact_fields(contact["id"], status="not_interested", stage="closed_no_interest")
    return "Понял, спасибо. Тогда не буду отвлекать."


def warmup_autocredit_positive_reply(
    contact: dict[str, Any],
    inbound_text: str,
    sender_name: str | None = None,
    *,
    self_responsible: bool = False,
) -> tuple[dict[str, Any], str]:
    settings = get_settings()
    intro = agent_intro_text(settings)
    if self_responsible or SELF_LPR_RE.search(inbound_text or "") or is_direct_self_reply(contact["id"], inbound_text):
        updated_contact = mark_contact_as_lpr(contact, "warmup_autocredit_positive", sender_name)
        text = compact_message(
            f"Понял. {intro} Пишу по решению для автодилеров: цифровая заявка на автокредит и предварительный скоринг. Если удобно, пришлю короткое КП сюда в WhatsApp?",
            limit=260,
        )
        return updated_contact, text

    update_contact_fields(contact["id"], status="replied", stage="warmup_intro")
    updated_contact = get_contact(contact["id"]) or contact
    text = compact_message(
        f"Понял. {intro} Пишу по решению для автодилеров: цифровая заявка на автокредит и предварительный скоринг. Это вы смотрите или лучше обсудить с коллегой?",
        limit=260,
    )
    return updated_contact, text


def transfer_offer_reply(contact: dict[str, Any]) -> str:
    update_contact_fields(contact["id"], status="replied", stage="awaiting_intro")
    if uses_keramo_investor_flow():
        return (
            "Понял. Я пишу не по аренде помещения, а по инвестиционному предложению KERAMO BUILD. "
            "Если у вас это смотрит другой человек, подскажите, пожалуйста, кому корректнее написать?"
        )
    return (
        "Понял. Мы сами не оформляем автокредит, пишу по решению для автодилеров. "
        "Если удобно, свяжите, пожалуйста, с менеджером или руководителем по кредитному направлению или партнерским инструментам."
    )


def action_request_reply(contact: dict[str, Any]) -> str:
    update_contact_fields(contact["id"], status="replied", stage="awaiting_intro")
    if uses_keramo_investor_flow():
        return (
            "Если удобно, просто пришлите WhatsApp человека, кто смотрит инвестиции или партнерства, либо разрешите написать ему напрямую. "
            "Я коротко объясню суть без лишней переписки."
        )
    return (
        "Если удобно, просто пришлите номер или WhatsApp ответственного, либо перешлите ему мой контакт. "
        "Я сам коротко объясню суть, без лишней переписки с вашей стороны."
    )


def buyer_confusion_reply(contact: dict[str, Any]) -> str:
    update_contact_fields(contact["id"], status="replied", stage="clarified_offer")
    if uses_keramo_investor_flow():
        return (
            "Нет, я не по покупке или аренде помещения. Речь об инвестиции в KERAMO BUILD: производство и монтаж керамогранита в Астане. "
            "Вы сами смотрите такие предложения?"
        )
    return (
        "Нет, мы сами не оформляем автокредит. Мы помогаем автодилерам принимать и предварительно оценивать заявки; "
        "подскажите, кто смотрит это направление, или свяжите с ответственным?"
    )


def detail_request_reply(contact: dict[str, Any]) -> str:
    project = get_project()
    if is_active_interest_contact(contact):
        update_contact_fields(contact["id"], status="interested", stage="interest_dialog")
    else:
        update_contact_fields(contact["id"], status="replied", stage="clarified_offer")
    if uses_keramo_investor_flow(project):
        return (
            "Суть такая: 35 млн тенге идут на запуск шоурума 80 м² и производства 75 м². "
            "По базовому плану возврат капитала 25 месяцев, после этого инвестор получает 30% прибыли; фактический результат зависит от рынка. Удобнее посмотреть КП или созвониться?"
        )
    if project.get("workflow_type") == "generic_b2b":
        product_name = clip_text(str(project.get("product_name") or "нашем продукте"), limit=90) or "нашем продукте"
        return (
            f"Коротко: речь о {product_name}. "
            "Если смотреть на практику, что для вас сейчас важнее: больше входящих заявок или быстрее обработка текущих обращений?"
        )
    return (
        "Коротко: это white-label приложение автодилера, где клиент оставляет заявку в вашем бренде, "
        "а менеджер сразу видит анкету и предварительный скоринг. Что для вас сейчас важнее: больше заявок или быстрее обработка анкет?"
    )


def interested_reply_text(contact: dict[str, Any], inbound_text: str) -> str:
    if uses_keramo_investor_flow():
        if MEETING_INTEREST_RE.search(inbound_text or ""):
            return "Да, можно. Чтобы предметно обсудить, какой формат удобнее: сначала короткое КП с цифрами или сразу 15-минутный созвон?"
        return "Отлично. Чтобы понять релевантность, вам комфортно рассматривать инвестиционный чек 35 млн тенге или лучше сначала отправить КП с моделью возврата?"
    if MEETING_INTEREST_RE.search(inbound_text or ""):
        return (
            "Отлично. Чтобы созвон был предметным, что для вас сейчас важнее: больше заявок на автокредит "
            "или быстрее обработка анкет?"
        )
    return (
        "Отлично. Чтобы разговор был предметным, что для вас сейчас важнее: больше заявок на автокредит "
        "или быстрее обработка анкет?"
    )


def is_active_interest_contact(contact: dict[str, Any]) -> bool:
    return contact.get("status") in {"interested", "interested_pending"} or contact.get("stage") in {"interest_dialog", "handoff"}


def meeting_schedule_reply(contact: dict[str, Any]) -> str:
    update_contact_fields(contact["id"], status="interested", stage="interest_dialog")
    if uses_keramo_investor_flow():
        return "Да, можно. Какой слот вам удобнее завтра: до обеда или после? На созвоне коротко пройдемся по 35 млн, возврату и доле 30%."
    return "Да, можно. Какой слот вам удобнее завтра: до обеда или после?"


def our_callback_contact_reply(contact: dict[str, Any]) -> str:
    settings = get_settings()
    phones = parse_handoff_phones(settings.get("handoff_phone"), settings.get("default_country_code", "7"))
    callback_phone = format_phone(phones[0] if phones else settings.get("handoff_phone") or "")
    update_contact_fields(contact["id"], status="interested", stage="awaiting_callback")
    phone_part = f", WhatsApp {callback_phone}" if callback_phone else ""
    if uses_keramo_investor_flow():
        return (
            f"Да, передайте, пожалуйста: {bot_name(settings) or 'менеджер KERAMO BUILD'}{phone_part}. "
            "Тема - инвестиции в производство керамогранита в Астане."
        )
    return (
        f"Да, передайте, пожалуйста: {bot_name(settings) or 'Медет'}{phone_part}. "
        "Тема — white-label приложение автоскоринга для дилера."
    )


def specialist_contact_request_reply(contact: dict[str, Any]) -> str:
    update_contact_fields(contact["id"], status="replied", stage="awaiting_specialist_contact")
    if uses_keramo_investor_flow():
        return "Да, подскажите, пожалуйста, WhatsApp или номер человека, кто смотрит инвестиции или партнерства. Я коротко напишу по KERAMO BUILD."
    return "Да, подскажите, пожалуйста, WhatsApp или номер специалиста, кто смотрит коммерческие решения по автокредитам."


def asks_to_leave_our_contact(text: str | None) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if EMAIL_REQUEST_RE.search(normalized) or PROPOSAL_REQUEST_RE.search(normalized):
        return False
    return bool(REQUEST_OUR_CONTACT_RE.search(normalized) or FORWARDED_TO_INTERNAL_TEAM_RE.search(normalized))


def offers_specialist_contact(text: str | None) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    return bool(SPECIALIST_CONTACT_OFFER_RE.search(normalized) or CANNOT_ACCEPT_PROPOSAL_RE.search(normalized))


def reply_language_choice_text() -> str:
    return "На русском, пожалуйста. Я коротко по коммерческому вопросу."


def referred_lpr_intro_text(lpr: dict[str, Any]) -> str:
    settings = get_settings()
    name = clean_person_name(lpr.get("name")) or ""
    greeting = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
    sales_angle = clip_text(str(contact_sales_context(lpr).get("sales_angle") or ""), limit=120)
    reason = sales_angle or "Ваш контакт передали по коммерческому вопросу."
    return compact_message(
        f"{greeting} {agent_intro_text(settings)} {reason} Подскажите, это вы смотрите, или лучше обсудить с коллегой, кто отвечает за это направление?",
        limit=300,
    )


def has_explicit_proposal_request(text: str | None) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if CANNOT_ACCEPT_PROPOSAL_RE.search(normalized):
        return False
    if EMAIL_REQUEST_RE.search(normalized):
        return True
    return bool(PROPOSAL_REQUEST_RE.search(normalized))


def is_waiting_proposal_consent(contact: dict[str, Any]) -> bool:
    if contact.get("kind") != "lpr" or contact.get("status") != "lpr_self" or int(contact.get("proposal_sent") or 0):
        return False
    history = load_message_history(contact["id"], limit=3)
    last_outbound = next((item.get("text") or "" for item in reversed(history) if item.get("direction") == "out"), "")
    last_outbound_normalized = re.sub(r"\s+", " ", last_outbound).strip().lower()
    return "пришлю короткое кп сюда" in last_outbound_normalized or "коммерческ" in last_outbound_normalized


def has_proposal_consent_reply(contact: dict[str, Any], text: str | None) -> bool:
    if has_explicit_proposal_request(text):
        return True
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized or "?" in normalized or not PROPOSAL_CONSENT_RE.search(normalized):
        return False
    return is_waiting_proposal_consent(contact)


def proposal_already_sent(contact: dict[str, Any]) -> bool:
    return int(contact.get("proposal_sent") or 0) == 1 or (contact.get("status") or "") == "proposal_sent"


def local_reply_intent_fallback(contact: dict[str, Any], inbound_text: str) -> dict[str, bool]:
    text = str(inbound_text or "")
    normalized = re.sub(r"\s+", " ", text).strip()
    forward_summary_consent = has_forward_summary_consent(contact, text)
    answers_qualification_focus = is_qualification_focus_answer(contact, text)
    specialist_contact_offer = offers_specialist_contact(text)
    callback_contact_request = asks_to_leave_our_contact(text)
    meeting_interest = bool(MEETING_INTEREST_RE.search(text)) and not specialist_contact_offer and not callback_contact_request
    interested_next_step = is_interested_text(text) and not specialist_contact_offer and not callback_contact_request
    return {
        "proposal_consent": has_proposal_consent_reply(contact, text),
        "explicit_proposal_request": has_explicit_proposal_request(text),
        "self_responsible": bool(SELF_LPR_RE.search(text) or is_direct_self_reply(contact["id"], text)),
        "asks_if_proposal_already_sent": bool(
            re.search(r"\b(уже|же).{0,30}(отправил|отправили|скинул|прислал|прикрепил)\b", text, re.IGNORECASE)
        ),
        "asks_details": bool(DETAIL_REQUEST_RE.search(text) or BENEFIT_QUESTION_RE.search(text) or AUTOCREDIT_MATCHING_QUESTION_RE.search(text)),
        "meeting_interest": meeting_interest,
        "interested_next_step": interested_next_step,
        "will_forward_to_responsible": bool(FORWARD_TO_RESPONSIBLE_RE.search(text)),
        "forward_summary_consent": forward_summary_consent,
        "answers_qualification_focus": answers_qualification_focus,
        "terminal_ack": (
            is_low_value_step_done_reply(text)
            or bool(proposal_already_sent(contact) and AFTER_PROPOSAL_ACK_RE.match(normalized))
        )
        and not forward_summary_consent
        and not answers_qualification_focus,
    }


async def classify_reply_intent(settings: dict[str, str], contact: dict[str, Any], inbound_text: str) -> dict[str, bool]:
    fallback = local_reply_intent_fallback(contact, inbound_text)
    if not ai_credentials_available(settings):
        return fallback

    history = load_message_history(contact["id"], limit=8)
    history_text = "\n".join(
        f"{'assistant' if item['direction'] == 'out' else 'client'}: {history_item_content(item)}"
        for item in history
        if history_item_content(item)
    )
    messages = [
        {
            "role": "system",
            "content": (
                "Ты классификатор намерения клиента в WhatsApp B2B-продаже. "
                "Оцени смысл последнего сообщения с учетом истории и CRM-статуса, не по отдельным словам. "
                "Верни только JSON с boolean-полями: "
                "proposal_consent, explicit_proposal_request, self_responsible, asks_if_proposal_already_sent, "
                "asks_details, meeting_interest, interested_next_step, will_forward_to_responsible, "
                "forward_summary_consent, answers_qualification_focus, terminal_ack. "
                "proposal_consent=true только если клиент соглашается на уже предложенную отправку КП или просит прислать КП. "
                "self_responsible=true, если клиент по смыслу говорит, что сам занимается этим вопросом, сам отвечает "
                "за направление или с ним можно обсуждать коммерческий вопрос. "
                "Если предыдущее сообщение ассистента спрашивало, прислать ли КП, короткие ответы со смыслом согласия "
                "вроде 'ок', 'да', 'хорошо', 'отправляй' считаются proposal_consent=true. "
                "Если предыдущее сообщение ассистента предлагало скинуть короткий текст для руководителя, короткое согласие "
                "считай forward_summary_consent=true, а не terminal_ack. "
                "Если предыдущее сообщение ассистента спрашивало, что важнее: заявки или скорость обработки, "
                "а клиент выбирает один вариант или оба, ставь answers_qualification_focus=true. "
                "Если клиент пишет, что сам передаст или перешлет руководителю/ответственному, ставь will_forward_to_responsible=true. "
                "Если КП уже отправлено и клиент спрашивает 'вы же уже отправили?', ставь asks_if_proposal_already_sent=true. "
                "terminal_ack=true только для закрывающего подтверждения без нового вопроса или нового шага."
            ),
        },
        {
            "role": "user",
            "content": (
                "CRM:\n"
                f"kind={contact.get('kind')}; status={contact.get('status')}; stage={contact.get('stage')}; "
                f"proposal_sent={proposal_already_sent(contact)}; proposal_offer_pending={is_waiting_proposal_consent(contact)}\n\n"
                f"История:\n{history_text or 'нет'}\n\n"
                f"Последнее сообщение клиента:\n{inbound_text}"
            ),
        },
    ]
    try:
        parsed = await call_ai_json(settings, messages, max_tokens=220)
    except Exception as exc:
        log_event("ai", f"AI-классификация намерения не выполнена: {exc}", level="warning", contact_id=contact["id"])
        return fallback
    if not isinstance(parsed, dict):
        return fallback
    merged = {key: bool(parsed.get(key)) or fallback.get(key, False) for key in fallback}
    if merged.get("forward_summary_consent") or merged.get("answers_qualification_focus"):
        merged["terminal_ack"] = False
    if offers_specialist_contact(inbound_text) or asks_to_leave_our_contact(inbound_text):
        merged["meeting_interest"] = False
        merged["interested_next_step"] = False
    if CANNOT_ACCEPT_PROPOSAL_RE.search(inbound_text or ""):
        merged["proposal_consent"] = False
        merged["explicit_proposal_request"] = False
    return merged


def local_reply_offers_proposal_again(reply_text: str | None) -> bool:
    text = str(reply_text or "").strip()
    if not text:
        return False
    if re.search(r"\b(кп|коммерческ\w*\s+предложени\w*)\b", text, re.IGNORECASE) and re.search(
        r"\b(могу|можем|отправлю|отправить|пришлю|прислать|скину|прикреплю|направлю|дать|дать\s+доступ)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    return False


async def reply_offers_proposal_again(settings: dict[str, str], reply_text: str | None) -> bool:
    fallback = local_reply_offers_proposal_again(reply_text)
    text = str(reply_text or "").strip()
    if not text or not ai_credentials_available(settings):
        return fallback
    messages = [
        {
            "role": "system",
            "content": (
                "Ты проверяешь исходящий WhatsApp-ответ менеджера. Верни только JSON "
                '{"offers_proposal_again":true/false}. '
                "true, если ответ предлагает, обещает или спрашивает про отправку КП/коммерческого предложения. "
                "false, если ответ просто говорит, что КП уже отправлено, или обсуждает содержание КП."
            ),
        },
        {"role": "user", "content": text},
    ]
    try:
        parsed = await call_ai_json(settings, messages, max_tokens=80)
    except Exception as exc:
        log_event("ai", f"AI-проверка повтора КП не выполнена: {exc}", level="warning")
        return fallback
    if not isinstance(parsed, dict):
        return fallback
    return bool(parsed.get("offers_proposal_again", fallback))


def after_proposal_followup_reply(contact: dict[str, Any], inbound_text: str) -> str:
    if uses_keramo_investor_flow():
        if DETAIL_REQUEST_RE.search(inbound_text or "") or BENEFIT_QUESTION_RE.search(inbound_text or ""):
            return after_proposal_detail_reply(contact, inbound_text)
        return "КП уже отправил. Главное там: 35 млн тенге инвестиций, базовый возврат 25 месяцев и 30% прибыли после возврата капитала."
    if DETAIL_REQUEST_RE.search(inbound_text or "") or BENEFIT_QUESTION_RE.search(inbound_text or "") or AUTOCREDIT_MATCHING_QUESTION_RE.search(inbound_text or ""):
        return after_proposal_detail_reply(contact, inbound_text)
    return (
        "КП уже отправил. Главное там: быстрее собрать заявку, сделать предварительную оценку и не терять клиента между менеджером и кредитным решением."
    )


def after_proposal_detail_reply(contact: dict[str, Any], inbound_text: str) -> str:
    if is_active_interest_contact(contact):
        update_contact_fields(contact["id"], status="interested", stage="interest_dialog")
    else:
        update_contact_fields(contact["id"], status="replied_after_proposal", stage="after_proposal")

    text = str(inbound_text or "")
    if uses_keramo_investor_flow():
        if BENEFIT_QUESTION_RE.search(text):
            return (
                "Инвестор сначала возвращает вложенные 35 млн тенге из чистой прибыли, затем получает 30% прибыли бизнеса. "
                "Это базовый прогноз, не гарантия: результат зависит от продаж и рынка."
            )
        return (
            "Если коротко: запускается шоурум и производство керамогранита в Астане. Деньги идут на CAPEX, первые месяцы OPEX и буфер; возврат по базовому плану - 25 месяцев."
        )
    if AUTOCREDIT_MATCHING_QUESTION_RE.search(text):
        return (
            "Да, логика такая: клиент заполняет анкету, система делает предварительную оценку и показывает подходящие кредитные варианты. "
            "Финальное решение остается за вашей командой и финорганизацией."
        )
    if BENEFIT_QUESTION_RE.search(text):
        return (
            "Для вас это меньше ручного сбора анкеты, быстрее первичная оценка и меньше потерь между заявкой, менеджером и кредитным решением."
        )
    return (
        "Если совсем коротко: клиент сам заполняет анкету, менеджер видит заявку и предварительный скоринг в кабинете. "
        "Дальше проще понять, кому и какие условия можно предложить."
    )


def qualification_focus_followup_reply(contact: dict[str, Any], inbound_text: str) -> str:
    update_contact_fields(contact["id"], status="replied", stage="qualification_focus_answered")
    if uses_keramo_investor_flow():
        return (
            "Понял. Тогда корректнее обсудить саму модель: возврат капитала, долю 30% и операционные риски. "
            "Вы сами смотрите такие инвестиции или лучше обсудить с партнером/финансовым человеком?"
        )
    text = str(inbound_text or "")
    if re.search(r"\b(оба|обе|и\s+то\s+и\s+то|все)\b", text, re.IGNORECASE):
        return (
            "Понял. Тогда логично смотреть весь путь: от заявки до первичной оценки. "
            "Кто у вас отвечает за кредитные продажи или цифровую воронку?"
        )
    if re.search(r"\b(быстр(?:ее|о)|скорост\w*|обработк\w*|анкет\w*)\b", text, re.IGNORECASE):
        return (
            "Понял. Тогда фокус — быстрее собрать анкету, сделать первичную оценку и передать заявку менеджеру без ручной переписки. "
            "Кто у вас отвечает за кредитные продажи?"
        )
    return (
        "Понял. Тогда фокус — удобная заявка в вашем бренде и меньше потерь до менеджера. "
        "Кто у вас отвечает за это направление?"
    )


def forward_summary_offer_reply(contact: dict[str, Any]) -> str:
    update_contact_fields(contact["id"], status="interested", stage="forward_summary_offer")
    return "Понял. Могу скинуть сюда короткий текст для руководителя, чтобы было удобно переслать?"


def forward_summary_text(contact: dict[str, Any]) -> str:
    update_contact_fields(contact["id"], status="interested", stage="forward_summary_sent")
    if uses_keramo_investor_flow():
        return (
            "Для руководителя: KERAMO BUILD ищет инвестора на 35 млн тенге в производство керамогранита в Астане. "
            "Базовый план - возврат капитала за 25 месяцев, затем 30% прибыли; показатели прогнозные и зависят от рынка."
        )
    return (
        "Для руководителя: предлагаем white-label приложение автодилера, где клиент сам заполняет анкету на автокредит, "
        "а менеджер видит заявку и предварительный скоринг. Цель — быстрее обработка и меньше ручной рутины."
    )


def strip_redundant_self_intro(text: str, contact: dict[str, Any], inbound_text: str, settings: dict[str, str] | None = None) -> str:
    cleaned = str(text or "").strip()
    if not cleaned or IDENTITY_QUESTION_RE.search(inbound_text or ""):
        return cleaned
    if not has_previous_outbound(contact["id"]):
        return cleaned

    resolved_settings = settings or get_settings()
    name = bot_name(resolved_settings)
    patterns: list[re.Pattern[str]] = []
    if name:
        patterns.append(re.compile(rf"^\s*{re.escape(name)}\s*[,.:;!?-]+\s*", re.IGNORECASE))
        patterns.append(re.compile(rf"^\s*меня\s+зовут\s+{re.escape(name)}\.?\s*", re.IGNORECASE))
    patterns.append(re.compile(r"^\s*я\s+из\s+команды\s+проекта\s+по\s+автоскорингу\s+для\s+автодилеров\.?\s*", re.IGNORECASE))
    patterns.append(re.compile(r"^\s*я\s+из\s+команды\s+keramo\s+build\.?\s*", re.IGNORECASE))

    for pattern in patterns:
        cleaned = pattern.sub("", cleaned, count=1).strip()

    if cleaned:
        return cleaned[0].upper() + cleaned[1:] if len(cleaned) > 1 else cleaned.upper()
    return text


def clean_person_name(text: str | None) -> str | None:
    raw = safe_cell(text)
    if not raw or UNKNOWN_NAME_RE.search(raw):
        return None
    raw_lower = raw.lower()
    if re.search(
        r"\b(можете|можешь|можно|его|ее|ему|ей|имя|зовут|написать|напишите|позвоните|скину|скиньте|щас|сейчас|контакт|номер|телефон)\b",
        raw_lower,
    ):
        return None
    cleaned = re.sub(r"[^\w\sА-Яа-яЁё-]", " ", raw, flags=re.UNICODE).strip()
    words = [word for word in cleaned.split() if len(word) > 1]
    if not 1 <= len(words) <= 4:
        return None
    blacklist = {
        "ок",
        "хорошо",
        "спасибо",
        "супер",
        "отлично",
        "директор",
        "руководитель",
        "лпр",
        "контакт",
        "номер",
        "телефон",
        "можете",
        "можно",
        "его",
        "ее",
        "ему",
        "ей",
        "имя",
        "зовут",
        "написать",
        "напишите",
        "позвоните",
        "скину",
        "скиньте",
        "щас",
        "сейчас",
        "false",
        "true",
        "isdeleted",
        "deleted",
    }
    if any(word.lower() in blacklist for word in words):
        return None
    return " ".join(words)


def local_person_name_from_text(text: str | None) -> str | None:
    raw = safe_cell(text)
    if not raw or UNKNOWN_NAME_RE.search(raw):
        return None

    declared = re.search(
        r"\b(?:его|ее)?\s*(?:имя|зовут|это)\s*[:\-]?\s+([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё-]{1,24}(?:\s+[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё-]{1,24}){0,2})",
        raw,
        re.IGNORECASE,
    )
    if declared:
        name = clean_person_name(declared.group(1))
        if name:
            return name

    return clean_person_name(raw)


def has_previous_outbound(contact_id: int) -> bool:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE project_id = ? AND contact_id = ? AND direction = 'out' LIMIT 1",
            (current_project_id(), contact_id),
        ).fetchone()
    return row is not None


def strip_repeated_greeting(text: str, contact_id: int) -> str:
    if not has_previous_outbound(contact_id):
        return text
    stripped = GREETING_PREFIX_RE.sub("", text, count=1).strip()
    if not stripped:
        return text
    return stripped[0].upper() + stripped[1:] if len(stripped) > 1 else stripped.upper()


def has_linked_lpr(owner_contact_id: int) -> bool:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM contacts WHERE project_id = ? AND owner_contact_id = ? AND kind = 'lpr' LIMIT 1",
            (current_project_id(), owner_contact_id),
        ).fetchone()
    return row is not None


def latest_outbound_text(contact_id: int) -> str:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT text FROM messages WHERE project_id = ? AND contact_id = ? AND direction = 'out' ORDER BY id DESC LIMIT 1",
            (current_project_id(), contact_id),
        ).fetchone()
    return str(row["text"] or "") if row else ""


def last_outbound_asked_qualification_focus(contact_id: int) -> bool:
    return bool(QUALIFICATION_FOCUS_RE.search(latest_outbound_text(contact_id)))


def has_asked_qualification_focus(contact_id: int, limit: int = 14) -> bool:
    return any(
        QUALIFICATION_FOCUS_RE.search(item.get("text") or "")
        for item in load_message_history(contact_id, limit=limit)
        if item.get("direction") == "out"
    )


def is_qualification_focus_answer(contact: dict[str, Any], inbound_text: str | None) -> bool:
    text = str(inbound_text or "").strip()
    if not text or "?" in text:
        return False
    return last_outbound_asked_qualification_focus(contact["id"]) and bool(QUALIFICATION_FOCUS_ANSWER_RE.search(text))


def is_waiting_forward_summary_consent(contact: dict[str, Any]) -> bool:
    last_text = latest_outbound_text(contact["id"]).lower()
    return "текст для руководител" in last_text or "удобно переслать" in last_text or "для пересылки руководител" in last_text


def has_forward_summary_consent(contact: dict[str, Any], text: str | None) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized or "?" in normalized:
        return False
    return is_waiting_forward_summary_consent(contact) and bool(PROPOSAL_CONSENT_RE.search(normalized))


def last_outbound_asked_for_decision_maker(contact_id: int) -> bool:
    last_text = latest_outbound_text(contact_id).lower()
    if not last_text:
        return False
    asks_person = any(token in last_text for token in ("кто", "с кем", "кому", "к кому"))
    asks_role = any(
        token in last_text
        for token in ("отвеча", "решени", "коммерчес", "цифров", "кредит", "лпр", "обсуд")
    )
    return asks_person and asks_role


def is_direct_self_reply(contact_id: int, inbound_text: str) -> bool:
    normalized = re.sub(r"\s+", " ", inbound_text or "").strip().lower()
    normalized = re.sub(r"^(?:ну|вообще|собственно|скорее)\s+", "", normalized)
    if normalized not in {"я", "это я", "со мной", "мне", "я отвечаю", "я занимаюсь", "я решаю"}:
        return False
    return last_outbound_asked_for_decision_maker(contact_id)


def polite_owner_follow_up(contact: dict[str, Any], inbound_text: str) -> str | None:
    if contact.get("kind") != "lead" or not has_linked_lpr(contact["id"]):
        return None
    if ACK_RE.match(inbound_text or ""):
        return None
    return None


def extract_email(text: str | None) -> str | None:
    match = EMAIL_RE.search(text or "")
    return match.group(0) if match else None


def email_request_reply(contact: dict[str, Any], inbound_text: str) -> str:
    email = extract_email(inbound_text)
    if uses_keramo_investor_flow():
        if email:
            update_contact_meta(contact["id"], requested_email=email, email_requested_at=now_iso())
            return "Принял почту. Что важнее подсветить в КП: модель возврата 35 млн тенге или операционную часть бизнеса?"
        return "Конечно. Подскажите почту; отправлю КП с цифрами по инвестициям, возврату и доле 30%."
    if email:
        update_contact_meta(contact["id"], requested_email=email, email_requested_at=now_iso())
        return "Принял почту. Что важнее подсветить в КП: рост заявок на автокредит или скорость обработки анкет?"
    return "Конечно. Подскажите почту; в КП сделаю акцент на заявках и скорости обработки анкет."


def budget_objection_reply(contact: dict[str, Any]) -> str:
    update_contact_fields(contact["id"], status="replied", stage="budget_objection")
    if uses_keramo_investor_flow():
        return "Понимаю. Там чек 35 млн тенге, поэтому лучше заранее понять рамки. Могу отправить короткое КП, чтобы вы спокойно оценили модель?"
    return "Понимаю. Такие решения часто смотрят за 3-6 месяцев до бюджета. Есть смысл коротко оценить цифры заранее?"


def existing_solution_reply(contact: dict[str, Any]) -> str:
    update_contact_fields(contact["id"], status="replied", stage="existing_solution_objection")
    if uses_keramo_investor_flow():
        return "Понял. Тогда вопрос скорее в диверсификации: вы в принципе рассматриваете долю в операционном бизнесе или только недвижимость?"
    return "Понял. Тогда вопрос не в замене, а в узком месте: больше теряется на заявках или на скоринге?"


def soft_negative_reply(contact: dict[str, Any]) -> str:
    update_contact_fields(contact["id"], status="not_interested", stage="closed_no_interest")
    return "Понял, тогда закрываю этот вопрос."


def save_message(
    contact_id: int | None,
    chat_id: str,
    direction: str,
    text: str | None,
    channel_msg_id: str | None = None,
    payload: dict[str, Any] | None = None,
    project_id: int | None = None,
) -> None:
    resolved_project_id = int(project_id or current_project_id())
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO messages(project_id, contact_id, chat_id, direction, text, channel_msg_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_project_id,
                contact_id,
                chat_id,
                direction,
                text,
                channel_msg_id,
                json.dumps(payload or {}, ensure_ascii=False),
                now_iso(),
            ),
        )


def message_exists(direction: str, channel_msg_id: str | None) -> bool:
    if not channel_msg_id:
        return False
    with db_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE project_id = ? AND direction = ? AND channel_msg_id = ? LIMIT 1",
            (current_project_id(), direction, channel_msg_id),
        ).fetchone()
    return row is not None


def get_contact_by_chat(chat_id: str) -> dict[str, Any] | None:
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM contacts
            WHERE project_id = ? AND chat_id = ?
            ORDER BY CASE WHEN kind = 'lpr' THEN 0 ELSE 1 END, id DESC
            LIMIT 1
            """,
            (current_project_id(), chat_id),
        ).fetchone()
    return row_dict(row)


def get_contact(contact_id: int) -> dict[str, Any] | None:
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE id = ? AND project_id = ?", (contact_id, current_project_id())).fetchone()
    return row_dict(row)


def contact_filters(
    *,
    kind: str | None = None,
    status: str | None = None,
    q: str | None = None,
    table_alias: str = "c",
) -> tuple[list[str], list[Any]]:
    where = [f"{table_alias}.project_id = ?"]
    values: list[Any] = [current_project_id()]
    if kind:
        where.append(f"{table_alias}.kind = ?")
        values.append(kind)
    if status:
        where.append(f"{table_alias}.status = ?")
        values.append(status)
    if q:
        where.append(f"({table_alias}.phone LIKE ? OR {table_alias}.company LIKE ? OR {table_alias}.name LIKE ?)")
        values.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    return where, values


def ensure_delete_allowed() -> None:
    active_tasks = []
    for key, label in (
        ("check", "проверка WhatsApp"),
        ("campaign", "кампания"),
        ("poller", "AI polling"),
        ("ai_sync", "синхронизация AI"),
    ):
        task = runtime_tasks.get(key)
        if task and not task.done():
            active_tasks.append(label)
    if active_tasks:
        joined = ", ".join(active_tasks)
        raise HTTPException(status_code=409, detail=f"Сначала остановите фоновые процессы: {joined}")


def matching_contact_ids(
    *,
    kind: str | None = None,
    status: str | None = None,
    q: str | None = None,
) -> list[int]:
    where, values = contact_filters(kind=kind, status=status, q=q)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with db_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT c.id
            FROM contacts c
            {where_sql}
            ORDER BY c.id DESC
            """,
            values,
        ).fetchall()
    return [int(row["id"]) for row in rows]


def related_contact_ids(conn: sqlite3.Connection, root_contact_ids: list[int]) -> list[int]:
    target_ids = sorted({int(contact_id) for contact_id in root_contact_ids if int(contact_id) > 0})
    if not target_ids:
        return []
    placeholders = ", ".join(["?"] * len(target_ids))
    rows = conn.execute(
        f"""
        WITH RECURSIVE related_contacts(id) AS (
            SELECT id
            FROM contacts
            WHERE project_id = ? AND id IN ({placeholders})

            UNION

            SELECT c.id
            FROM contacts c
            INNER JOIN related_contacts rc ON c.owner_contact_id = rc.id
            WHERE c.project_id = ?
        )
        SELECT DISTINCT id
        FROM related_contacts
        ORDER BY id
        """,
        [current_project_id(), *target_ids, current_project_id()],
    ).fetchall()
    return [int(row["id"]) for row in rows]


def delete_contacts_and_related(root_contact_ids: list[int]) -> dict[str, int]:
    target_ids = sorted({int(contact_id) for contact_id in root_contact_ids if int(contact_id) > 0})
    if not target_ids:
        return {"requested_contacts": 0, "deleted_contacts": 0, "deleted_messages": 0, "deleted_logs": 0}

    with db_conn() as conn:
        contact_ids = related_contact_ids(conn, target_ids)
        if not contact_ids:
            return {"requested_contacts": len(target_ids), "deleted_contacts": 0, "deleted_messages": 0, "deleted_logs": 0}

        placeholders = ", ".join(["?"] * len(contact_ids))
        protected_refs = conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM contacts
            WHERE project_id = ?
              AND owner_contact_id IN ({placeholders})
              AND id NOT IN ({placeholders})
            """,
            [current_project_id(), *contact_ids, *contact_ids],
        ).fetchone()["c"]
        if protected_refs:
            conn.execute(
                f"""
                UPDATE contacts
                SET owner_contact_id = NULL,
                    updated_at = ?
                WHERE project_id = ?
                  AND owner_contact_id IN ({placeholders})
                  AND id NOT IN ({placeholders})
                """,
                [now_iso(), current_project_id(), *contact_ids, *contact_ids],
            )

        deleted_messages = conn.execute(
            f"DELETE FROM messages WHERE project_id = ? AND contact_id IN ({placeholders})",
            [current_project_id(), *contact_ids],
        ).rowcount
        deleted_logs = conn.execute(
            f"DELETE FROM event_logs WHERE project_id = ? AND contact_id IN ({placeholders})",
            [current_project_id(), *contact_ids],
        ).rowcount
        deleted_contacts = conn.execute(
            f"DELETE FROM contacts WHERE project_id = ? AND id IN ({placeholders})",
            [current_project_id(), *contact_ids],
        ).rowcount

    return {
        "requested_contacts": len(target_ids),
        "deleted_contacts": deleted_contacts,
        "deleted_messages": deleted_messages,
        "deleted_logs": deleted_logs,
    }


def reset_sales_data() -> dict[str, int]:
    project_id = current_project_id()
    with db_conn() as conn:
        deleted_logs = conn.execute(
            """
            DELETE FROM event_logs
            WHERE project_id = ?
              AND (
                contact_id IS NOT NULL
                OR campaign_id IS NOT NULL
                OR category IN ('contact', 'message', 'campaign', 'whatsapp', 'proposal', 'handoff', 'lpr', 'import')
              )
            """,
            (project_id,),
        ).rowcount
        deleted_messages = conn.execute("DELETE FROM messages WHERE project_id = ?", (project_id,)).rowcount
        conn.execute("UPDATE contacts SET owner_contact_id = NULL WHERE project_id = ?", (project_id,))
        deleted_contacts = conn.execute("DELETE FROM contacts WHERE project_id = ?", (project_id,)).rowcount
        deleted_campaigns = conn.execute("DELETE FROM campaigns WHERE project_id = ?", (project_id,)).rowcount

    runtime_state["check"] = {"status": "idle", "processed": 0, "total": 0, "last_error": None}
    runtime_state["campaign"] = {"status": "idle", "campaign_id": None, "last_error": None}
    return {
        "deleted_contacts": deleted_contacts,
        "deleted_messages": deleted_messages,
        "deleted_campaigns": deleted_campaigns,
        "deleted_logs": deleted_logs,
    }


def create_or_update_contact(
    *,
    phone: str,
    kind: str,
    source: str,
    project_id: int | None = None,
    phone_raw: str | None = None,
    company: str | None = None,
    name: str | None = None,
    owner_contact_id: int | None = None,
    status: str = "new",
    stage: str = "new",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_time = now_iso()
    resolved_project_id = int(project_id or current_project_id())
    chat_id = chat_id_for_phone(phone)
    with db_conn() as conn:
        if kind == "lead":
            existing_lpr = conn.execute(
                "SELECT * FROM contacts WHERE project_id = ? AND phone = ? AND kind = 'lpr'",
                (resolved_project_id, phone),
            ).fetchone()
            if existing_lpr:
                conn.execute(
                    """
                    UPDATE contacts
                    SET phone_raw = COALESCE(?, phone_raw),
                        company = COALESCE(?, company),
                        name = COALESCE(?, name),
                        meta_json = CASE
                            WHEN ? != '{}' THEN ?
                            ELSE meta_json
                        END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        phone_raw,
                        company,
                        name,
                        json.dumps(meta or {}, ensure_ascii=False),
                        json.dumps(meta or {}, ensure_ascii=False),
                        current_time,
                        existing_lpr["id"],
                    ),
                )
                row = conn.execute("SELECT * FROM contacts WHERE id = ?", (existing_lpr["id"],)).fetchone()
                return row_dict(row)  # type: ignore[return-value]

        if kind == "lpr":
            existing_lpr = conn.execute(
                "SELECT * FROM contacts WHERE project_id = ? AND phone = ? AND kind = 'lpr'",
                (resolved_project_id, phone),
            ).fetchone()
            existing_lead = conn.execute(
                "SELECT * FROM contacts WHERE project_id = ? AND phone = ? AND kind = 'lead'",
                (resolved_project_id, phone),
            ).fetchone()
            if existing_lead and not existing_lpr:
                conn.execute(
                    """
                    UPDATE contacts
                    SET kind = 'lpr',
                        phone_raw = COALESCE(?, phone_raw),
                        source = ?,
                        company = COALESCE(?, company),
                        name = COALESCE(?, name),
                        owner_contact_id = COALESCE(?, owner_contact_id),
                        status = CASE
                            WHEN status IN ('proposal_sent', 'opt_out', 'interested') THEN status
                            ELSE ?
                        END,
                        stage = CASE
                            WHEN stage IN ('proposal_sent', 'done', 'opt_out', 'handoff') THEN stage
                            ELSE ?
                        END,
                        meta_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        phone_raw,
                        source,
                        company,
                        name,
                        owner_contact_id,
                        status,
                        stage,
                        json.dumps(meta or {}, ensure_ascii=False),
                        current_time,
                        existing_lead["id"],
                    ),
                )
                row = conn.execute("SELECT * FROM contacts WHERE id = ?", (existing_lead["id"],)).fetchone()
                return row_dict(row)  # type: ignore[return-value]

        conn.execute(
            """
            INSERT INTO contacts(
                project_id, phone_raw, phone, chat_id, kind, source, company, name, owner_contact_id,
                status, stage, meta_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, phone, kind) DO UPDATE SET
                phone_raw = COALESCE(excluded.phone_raw, contacts.phone_raw),
                company = COALESCE(excluded.company, contacts.company),
                name = COALESCE(excluded.name, contacts.name),
                owner_contact_id = COALESCE(excluded.owner_contact_id, contacts.owner_contact_id),
                source = COALESCE(excluded.source, contacts.source),
                status = CASE
                    WHEN contacts.status IN ('proposal_sent', 'opt_out', 'interested') THEN contacts.status
                    ELSE excluded.status
                END,
                stage = CASE
                    WHEN contacts.stage IN ('proposal_sent', 'done', 'opt_out', 'handoff') THEN contacts.stage
                    ELSE excluded.stage
                END,
                meta_json = CASE
                    WHEN excluded.meta_json != '{}' THEN excluded.meta_json
                    ELSE contacts.meta_json
                END,
                updated_at = excluded.updated_at
            """,
            (
                resolved_project_id,
                phone_raw,
                phone,
                chat_id,
                kind,
                source,
                company,
                name,
                owner_contact_id,
                status,
                stage,
                json.dumps(meta or {}, ensure_ascii=False),
                current_time,
                current_time,
            ),
        )
        row = conn.execute(
            "SELECT * FROM contacts WHERE project_id = ? AND phone = ? AND kind = ?",
            (resolved_project_id, phone, kind),
        ).fetchone()
    return row_dict(row)  # type: ignore[return-value]


def update_contact_fields(contact_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "company",
        "name",
        "whatsapp_exists",
        "status",
        "stage",
        "attempts",
        "proposal_sent",
        "last_inbound_at",
        "last_outbound_at",
        "last_error",
        "meta_json",
    }
    pairs = []
    values = []
    for key, value in fields.items():
        if key in allowed:
            pairs.append(f"{key} = ?")
            values.append(value)
    if not pairs:
        return
    pairs.append("updated_at = ?")
    values.append(now_iso())
    values.append(contact_id)
    with db_conn() as conn:
        conn.execute(f"UPDATE contacts SET {', '.join(pairs)} WHERE id = ?", values)


def contact_meta(contact: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = contact.get("meta_json") or "{}"
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def update_contact_meta(contact_id: int, **updates: Any) -> dict[str, Any]:
    contact = get_contact(contact_id) or {}
    meta = contact_meta(contact)
    meta.update({key: value for key, value in updates.items() if value is not None})
    update_contact_fields(contact_id, meta_json=json.dumps(meta, ensure_ascii=False))
    return meta


CRM_COLUMNS = [
    {"key": "base", "title": "База"},
    {"key": "ready", "title": "Готовы"},
    {"key": "sent", "title": "Ждем ответ"},
    {"key": "dialog", "title": "В диалоге"},
    {"key": "lpr", "title": "Ответственные"},
    {"key": "proposal", "title": "КП"},
    {"key": "interested", "title": "Зацепки"},
    {"key": "closed", "title": "Закрыто"},
]


def crm_stage_for_contact(contact: dict[str, Any]) -> str:
    status = contact.get("status") or ""
    stage = contact.get("stage") or ""
    kind = contact.get("kind") or ""
    if status == "interested" or stage == "handoff":
        return "interested"
    if status in {"opt_out", "not_interested", "no_whatsapp", "error"} or stage in {"proposal_failed", "proposal_recovery_needed", "closed_no_interest"}:
        return "closed"
    if int(contact.get("proposal_sent") or 0) == 1 or status in {"proposal_sent", "proposal_sending", "replied_after_proposal"}:
        return "proposal"
    if kind == "lpr" or status in {"lpr_self", "lpr_ready", "lpr_needs_name", "lpr_duplicate"}:
        return "lpr"
    if contact.get("last_inbound_at") or status == "replied":
        return "dialog"
    if status == "sent" or stage == "waiting_reply":
        return "sent"
    if contact.get("whatsapp_exists") == 1 or status == "ready":
        return "ready"
    return "base"


def crm_stage_title(key: str) -> str:
    titles = {column["key"]: column["title"] for column in CRM_COLUMNS}
    return titles.get(key, key)


def format_phone(phone: str | None) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D+", "", str(phone))
    return f"+{digits}" if digits else str(phone)


def compact_message(text: str, limit: int = 320) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= limit:
        return cleaned
    boundary = max(cleaned.rfind(". ", 0, limit), cleaned.rfind("? ", 0, limit), cleaned.rfind("! ", 0, limit))
    if boundary >= 80:
        return cleaned[: boundary + 1].strip()
    return f"{cleaned[: limit - 1].rstrip()}…"


def sanitize_user_message_text(text: str) -> str:
    cleaned = str(text or "")
    replacements = [
        (r"\bимя\s+лпр\b", "имя руководителя"),
        (r"\bконтакт\s+лпр\b", "контакт ответственного"),
        (r"\bномер\s+лпр\b", "номер ответственного"),
        (r"\bкто\s+лпр\b", "кто отвечает за этот вопрос"),
        (r"\bлпр(?:у|а|ом|е)?\b", "ответственный"),
    ]
    for pattern, replacement in replacements:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def is_low_value_step_done_reply(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized or len(normalized) > 140:
        return False
    if "?" in normalized:
        return False
    if extract_phones(normalized):
        return False
    if any(
        pattern.search(normalized)
        for pattern in (OPT_OUT_RE, SOFT_NEGATIVE_RE, INTEREST_RE, IDENTITY_QUESTION_RE, EMAIL_REQUEST_RE, SELF_LPR_RE)
    ):
        return False
    return bool(ACK_RE.match(normalized) or STEP_DONE_RE.search(normalized))


def should_pause_without_reply(contact: dict[str, Any], inbound_text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(inbound_text or "")).strip()
    if (int(contact.get("proposal_sent") or 0) or (contact.get("stage") or "") in {"proposal_sent", "after_proposal_reply"}) and AFTER_PROPOSAL_ACK_RE.match(normalized):
        return True
    if not is_low_value_step_done_reply(inbound_text):
        return False
    status = contact.get("status") or ""
    stage = contact.get("stage") or ""
    if status in {"interested", "not_interested", "opt_out"}:
        return True
    if stage in {"handoff", "closed_no_interest", "lpr_contact_shared", "proposal_sent", "after_proposal_reply"}:
        return True
    if int(contact.get("proposal_sent") or 0):
        return True
    if contact.get("kind") == "lead" and has_linked_lpr(contact["id"]):
        return True
    return False


def build_campaign_greeting(contact: dict[str, Any]) -> str:
    settings = get_settings()
    project = get_project()
    if uses_autoscore_warmup_flow(project):
        return random.choice(AUTOSCORE_WARMUP_OPENERS)
    if uses_keramo_investor_flow(project):
        opener = random.choice(["Добрый день.", "Здравствуйте."])
        company = safe_cell(contact.get("company"))
        sales_context = contact_sales_context(contact)
        sales_angle = clip_text(str(sales_context.get("sales_angle") or ""), limit=120)
        signal = sales_angle or (
            f"Увидел у вас коммерческую недвижимость{f' ({company})' if company else ''}, поэтому пишу аккуратно по инвестиционной теме."
        )
        question = random.choice(
            [
                "Уместно коротко написать по инвестиционному предложению в производственный бизнес?",
                "Вы в принципе рассматриваете инвестиции в операционный бизнес с долей в прибыли?",
                "Кто у вас смотрит инвестиционные или партнерские предложения?",
            ]
        )
        return compact_message(f"{opener} {agent_intro_text(settings)} {signal} {question}", limit=280)
    opener = random.choice(GREETING_OPENERS)
    context = random.choice(GREETING_CONTEXTS)
    question = random.choice(GREETING_QUESTIONS)
    company = safe_cell(contact.get("company"))
    sales_context = contact_sales_context(contact)
    sales_angle = clip_text(str(sales_context.get("sales_angle") or ""), limit=130)
    if project.get("workflow_type") == "generic_b2b":
        product = clip_text(str(project.get("product_name") or "нашему продукту"), limit=90) or "нашему продукту"
        context = f"Пишу по короткому коммерческому вопросу: {product}."
        question = random.choice(["С кем корректнее это обсудить?", "Кто у вас отвечает за такие вопросы?", "К кому лучше обратиться по этой теме?"])
    elif sales_angle:
        context = sales_angle
    elif company and len(company) <= 45 and random.random() < 0.25:
        context = f"Пишу по коммерческому вопросу для {company}."
    text = f"{opener} {agent_intro_text(settings)} {context} {question}"
    return compact_message(text, limit=260)


class GreenApiClient:
    def __init__(self, settings: dict[str, str]):
        self.api_url = settings.get("green_api_url", "").rstrip("/")
        self.media_url = settings.get("green_media_url", "").rstrip("/")
        self.id_instance = settings.get("green_id_instance", "").strip()
        self.api_token = settings.get("green_api_token", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_url and self.media_url and self.id_instance and self.api_token)

    def _url(self, method: str) -> str:
        return f"{self.api_url}/waInstance{self.id_instance}/{method}/{self.api_token}"

    def _media_url(self, method: str) -> str:
        return f"{self.media_url}/waInstance{self.id_instance}/{method}/{self.api_token}"

    async def check_whatsapp(self, phone: str) -> bool:
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(self._url("checkWhatsapp"), json={"phoneNumber": int(phone)})
            response.raise_for_status()
            data = response.json()
        return bool(data.get("existsWhatsapp"))

    async def send_message(self, chat_id: str, message: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(
                self._url("sendMessage"),
                json={"chatId": chat_id, "message": message},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            return response.json()

    async def send_typing(self, chat_id: str, typing_time_ms: int, typing_type: str = "text") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chatId": chat_id,
            "typingTime": max(1000, min(20000, int(typing_time_ms))),
        }
        if typing_type == "recording":
            payload["typingType"] = typing_type
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self._url("sendTyping"),
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()

    async def send_file_by_upload(self, chat_id: str, path: Path, caption: str) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Файл КП не найден: {path}")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        async with httpx.AsyncClient(timeout=120) as client:
            with path.open("rb") as file_obj:
                response = await client.post(
                    self._media_url("sendFileByUpload"),
                    data={"chatId": chat_id, "fileName": path.name, "caption": caption},
                    files={"file": (path.name, file_obj, content_type)},
                )
            response.raise_for_status()
            return response.json()

    async def receive_notification(self, receive_timeout: int = 5) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=receive_timeout + 15) as client:
            response = await client.get(
                f"{self._url('receiveNotification')}?receiveTimeout={receive_timeout}"
            )
            response.raise_for_status()
            if not response.content:
                return None
            data = response.json()
        return data or None

    async def delete_notification(self, receipt_id: int) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.delete(f"{self._url('deleteNotification')}/{receipt_id}")
            response.raise_for_status()
            return response.json()

    async def last_incoming_messages(self, minutes: int = 1440) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=80) as client:
            response = await client.get(
                f"{self._url('lastIncomingMessages')}?minutes={max(1, int(minutes))}"
            )
            response.raise_for_status()
            data = response.json()
        return data if isinstance(data, list) else []


def clean_ai_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


async def call_ai(settings: dict[str, str], contact: dict[str, Any], inbound_text: str) -> dict[str, Any]:
    provider = (settings.get("ai_provider") or "deepseek").lower()
    system_prompt = settings.get("ai_system_prompt") or DEFAULT_AI_PROMPT
    project = get_project()
    linked_lpr = has_linked_lpr(contact["id"]) if contact.get("kind") == "lead" else False
    sales_context = contact_sales_context(contact)
    system_prompt = (
        f"{system_prompt}\n\n"
        f"Активный проект: {project.get('name')}. Продукт проекта: {project.get('product_name')}. "
        f"Тип воркфлоу: {project.get('workflow_type')}. "
        f"Имя менеджера для самоидентификации: {bot_name(settings) or 'не задано'}. "
        "Техническое правило CRM: если текущий собеседник сам является ответственным руководителем, директором, собственником "
        "или ответственным за коммерческий вопрос, верни is_lpr=true и stage='lpr_self'. "
        "send_proposal=true ставь только если клиент явно попросил прислать КП или прямо согласился получить его сейчас. "
        "Если твое предыдущее исходящее сообщение уже спрашивало, прислать ли короткое КП в WhatsApp, то короткие подтверждения "
        "и согласия надо трактовать как send_proposal=true, а не повторять тот же вопрос заново. "
        "Если он дал свой же номер как контакт ответственного, укажи этот номер в lpr_phone. "
        "Если КП уже отправлено, не проси контакт ответственного повторно и не предлагай отправить КП еще раз; отвечай по сути, "
        "веди к короткому созвону или обсуждению условий. "
        "Если у контакта уже есть найденный ответственный, не возвращайся к вопросам про ответственного и его имя. "
        "В reply не используй термин 'ЛПР' вообще; клиенту понятнее слова 'ответственный' или 'руководитель'. "
        "Не повторяй свое имя и не представляйся заново, если уже представился в начале диалога и тебя повторно не спрашивали, кто ты. "
        "Не предлагай варианты 'в WhatsApp или на почту', если клиент сам не попросил почту. По умолчанию предлагай отправить КП сюда, в текущий чат. "
        "Если клиент пишет, что не понял, отвечай вежливо и конкретно, без фразы 'поясню проще'. "
        "Если клиент просит уточнить, рассказать подробнее или продолжает разговор после предыдущего шага, не обрывай диалог пустым reply: коротко объясни суть и дай один следующий вопрос. "
        "Не используй шаблонные завершающие фразы вроде 'если захотите вернуться к вопросу' или 'я на связи'; либо веди разговор дальше по сути, либо закрывай его коротко и без такого хвоста. "
        "Если клиент предлагает связать с менеджером или отделом продаж, не повторяй общий вопрос заново; поблагодари и дай один конкретный следующий шаг: "
        "попроси знакомство, номер, WhatsApp или пересылку твоего контакта. "
        "Если клиент или автоответчик просит оставить наши контакты, дать номер или пишет, что менеджер сам свяжется, дай контакт менеджера и не продолжай квалификацию. "
        "Если клиент пишет, что не может принимать коммерческие предложения, но может дать контакты специалиста, попроси номер или WhatsApp специалиста; не трактуй это как интерес к встрече или просьбу прислать КП. "
        "Если сообщение пришло как ответ на цитату, учитывай и сам текст клиента, и процитированное сообщение; не игнорируй короткие ответы вроде 'да есть' или 'на этот номер'. "
        "Если клиент спрашивает, что именно от него требуется, ответь одним конкретным micro-CTA, а не общим повтором про ответственного. "
        "Если клиент проявил интерес, не спеши завершать диалог благодарностью и мгновенным handoff: сначала задай один полезный квалифицирующий или next-step вопрос. "
        "Если контакт ответственного передали, сначала начни короткий диалог с ним и проверь релевантность; не отправляй КП автоматически в первом же сообщении. "
        "Если клиент просто поздоровался в середине диалога, не здоровайся второй раз, а мягко продолжи разговор. "
        "Если клиент спрашивает, кто ты, сначала представься по имени и коротко объясни причину сообщения. "
        "Если CRM дала sales_signal или sales_angle, используй их как причину релевантности, но не придумывай новые факты. "
        "В любом возражении применяй LAER: признать позицию, задать один короткий уточняющий вопрос, затем дать следующий шаг. "
        "Если клиент явно заинтересован в созвоне, встрече, обсуждении условий, демо или следующих шагах, "
        "верни interested=true и stage='interested'. "
        "reply всегда делай коротким: максимум 1-2 предложения, без длинных объяснений, без списков."
    )
    crm_context = (
        "CRM context: "
        f"kind={contact.get('kind')}; status={contact.get('status')}; stage={contact.get('stage')}; "
        f"proposal_sent={bool(contact.get('proposal_sent'))}; attempts={contact.get('attempts') or 0}; linked_lpr_exists={linked_lpr}; "
        f"proposal_offer_pending={is_waiting_proposal_consent(contact)}; "
        f"sales_signal={sales_context.get('sales_signal') or 'нет'}; sales_angle={sales_context.get('sales_angle') or 'нет'}; "
        "qualification=authority_need_capacity."
    )
    history = load_message_history(contact["id"], limit=14)
    messages = [{"role": "system", "content": system_prompt}, {"role": "system", "content": crm_context}]
    for item in history:
        role = "assistant" if item["direction"] == "out" else "user"
        content = history_item_content(item)
        if content:
            messages.append({"role": role, "content": content})
    if not history or history[-1]["direction"] != "in" or history[-1]["text"] != inbound_text:
        messages.append({"role": "user", "content": inbound_text})

    temperature = float(settings.get("ai_temperature") or 0.35)

    if provider == "openai":
        api_key = settings.get("openai_api_key", "").strip()
        if not api_key:
            return fallback_reply(contact, inbound_text)
        base_url = settings.get("openai_base_url", "https://api.openai.com/v1").rstrip("/")
        model = settings.get("openai_model", "gpt-5.2")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": 500,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    else:
        api_key = settings.get("deepseek_api_key", "").strip()
        if not api_key:
            return fallback_reply(contact, inbound_text)
        base_url = settings.get("deepseek_base_url", "https://api.deepseek.com").rstrip("/")
        model = settings.get("deepseek_model", "deepseek-v4-flash")
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 500,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=80) as client:
        response: httpx.Response | None = None
        for _ in range(3):
            response = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
            if response.status_code < 400:
                break
            error_text = response.text.lower()
            changed = False
            if "max_completion_tokens" in error_text and "max_completion_tokens" in payload:
                payload.pop("max_completion_tokens", None)
                payload["max_tokens"] = 500
                changed = True
            if "temperature" in error_text and "temperature" in payload:
                payload.pop("temperature", None)
                changed = True
            if "thinking" in error_text and "thinking" in payload:
                payload.pop("thinking", None)
                changed = True
            if not changed:
                break
        assert response is not None
        response.raise_for_status()
        data = response.json()

    content = data["choices"][0]["message"].get("content") or ""
    parsed = clean_ai_json(content)
    if parsed is None:
        parsed = fallback_reply(contact, inbound_text)
        parsed["reply"] = content[:1000] or parsed["reply"]
    return normalize_ai_action(parsed)


def normalize_ai_action(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "reply": sanitize_user_message_text(compact_message(str(action.get("reply") or "").strip(), limit=320)),
        "stage": str(action.get("stage") or "continue").strip(),
        "send_proposal": bool(action.get("send_proposal")),
        "is_lpr": bool(action.get("is_lpr")),
        "interested": bool(action.get("interested")) or str(action.get("stage") or "").strip() in {"interested", "handoff", "meeting"},
        "lpr_phone": str(action.get("lpr_phone") or "").strip(),
        "lpr_name": str(action.get("lpr_name") or "").strip(),
        "stop": bool(action.get("stop")),
    }


def ai_credentials_available(settings: dict[str, str]) -> bool:
    provider = (settings.get("ai_provider") or "deepseek").lower()
    if provider == "openai":
        return bool(settings.get("openai_api_key", "").strip())
    return bool(settings.get("deepseek_api_key", "").strip())


async def call_ai_json(settings: dict[str, str], messages: list[dict[str, str]], max_tokens: int = 180) -> dict[str, Any] | None:
    provider = (settings.get("ai_provider") or "deepseek").lower()
    if provider == "openai":
        api_key = settings.get("openai_api_key", "").strip()
        if not api_key:
            return None
        base_url = settings.get("openai_base_url", "https://api.openai.com/v1").rstrip("/")
        model = settings.get("openai_model", "gpt-5.2")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_completion_tokens": max_tokens,
        }
    else:
        api_key = settings.get("deepseek_api_key", "").strip()
        if not api_key:
            return None
        base_url = settings.get("deepseek_base_url", "https://api.deepseek.com").rstrip("/")
        model = settings.get("deepseek_model", "deepseek-v4-flash")
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
            "thinking": {"type": "disabled"},
        }

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=40) as client:
        response: httpx.Response | None = None
        for _ in range(3):
            response = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
            if response.status_code < 400:
                break
            error_text = response.text.lower()
            changed = False
            if "max_completion_tokens" in error_text and "max_completion_tokens" in payload:
                payload.pop("max_completion_tokens", None)
                payload["max_tokens"] = max_tokens
                changed = True
            if "temperature" in error_text and "temperature" in payload:
                payload.pop("temperature", None)
                changed = True
            if "thinking" in error_text and "thinking" in payload:
                payload.pop("thinking", None)
                changed = True
            if not changed:
                break
        if response is None:
            return None
        response.raise_for_status()
        data = response.json()

    content = data["choices"][0]["message"].get("content") or ""
    return clean_ai_json(content)


async def resolve_person_name(text: str | None, *, context: str = "", settings: dict[str, str] | None = None) -> str | None:
    raw = safe_cell(text)
    if not raw:
        return None
    local_name = local_person_name_from_text(raw)
    resolved_settings = settings or get_settings()
    if not ai_credentials_available(resolved_settings):
        return local_name

    messages = [
        {
            "role": "system",
            "content": (
                "Ты классификатор имени человека для CRM WhatsApp. Верни только JSON "
                '{"is_name":true/false,"name":"нормализованное имя или пусто"}. '
                "true только если текст действительно содержит имя человека. "
                "Должности, согласия, инструкции вроде 'можете ему написать', 'директор', 'номер', "
                "служебные поля и мусор не являются именем. Не выдумывай имя."
            ),
        },
        {
            "role": "user",
            "content": f"Контекст: {context or 'нет'}\nТекст: {raw}\nЛокальный кандидат: {local_name or ''}",
        },
    ]
    try:
        parsed = await call_ai_json(resolved_settings, messages)
    except Exception as exc:
        log_event("ai", f"AI-проверка имени не выполнена: {exc}", level="warning", payload={"text": raw[:120]})
        return local_name

    if not isinstance(parsed, dict):
        return local_name
    if not parsed.get("is_name"):
        return None
    return local_person_name_from_text(str(parsed.get("name") or "")) or local_name


def fallback_reply(contact: dict[str, Any], inbound_text: str) -> dict[str, Any]:
    settings = get_settings()
    if IDENTITY_QUESTION_RE.search(inbound_text):
        return normalize_ai_action(
            {
                "reply": f"{identity_reply_text(settings)} Если удобно, могу дальше коротко написать уже по сути.",
                "stage": "continue",
            }
        )
    if CONFUSION_RE.search(inbound_text):
        return normalize_ai_action(
            {
                "reply": clarify_offer_reply(contact),
                "stage": "continue",
            }
        )
    if DETAIL_REQUEST_RE.search(inbound_text):
        return normalize_ai_action(
            {
                "reply": detail_request_reply(contact),
                "stage": "continue",
            }
        )
    if TRANSFER_OFFER_RE.search(inbound_text):
        return normalize_ai_action({"reply": transfer_offer_reply(contact), "stage": "continue"})
    if ACTION_REQUEST_RE.search(inbound_text):
        return normalize_ai_action({"reply": action_request_reply(contact), "stage": "continue"})
    if BUYER_CONFUSION_RE.search(inbound_text):
        return normalize_ai_action({"reply": buyer_confusion_reply(contact), "stage": "continue"})
    if OPT_OUT_RE.search(inbound_text):
        return normalize_ai_action(
            {"reply": "Понял, больше не будем беспокоить. Спасибо.", "stage": "stop", "stop": True}
        )
    if SOFT_NEGATIVE_RE.search(inbound_text):
        return normalize_ai_action({"reply": soft_negative_reply(contact), "stage": "not_interested"})
    if BUDGET_OBJECTION_RE.search(inbound_text):
        return normalize_ai_action({"reply": budget_objection_reply(contact), "stage": "budget_objection"})
    if ALREADY_HAVE_RE.search(inbound_text):
        return normalize_ai_action({"reply": existing_solution_reply(contact), "stage": "existing_solution_objection"})
    if asks_to_leave_our_contact(inbound_text):
        return normalize_ai_action({"reply": our_callback_contact_reply(contact), "stage": "handoff", "interested": True})
    if offers_specialist_contact(inbound_text):
        return normalize_ai_action({"reply": specialist_contact_request_reply(contact), "stage": "continue"})
    if SELF_LPR_RE.search(inbound_text) or is_direct_self_reply(contact["id"], inbound_text):
        return normalize_ai_action(
            {
                "reply": proposal_offer_reply(),
                "stage": "lpr_self",
                "send_proposal": False,
                "is_lpr": True,
            }
        )
    if is_active_interest_contact(contact) and MEETING_INTEREST_RE.search(inbound_text):
        return normalize_ai_action(
            {
                "reply": meeting_schedule_reply(contact),
                "stage": "interested",
                "interested": True,
            }
        )
    if is_interested_text(inbound_text):
        return normalize_ai_action(
            {
                "reply": "",
                "stage": "interested",
                "interested": True,
            }
        )
    if EMAIL_REQUEST_RE.search(inbound_text):
        return normalize_ai_action(
            {
                "reply": email_request_reply(contact, inbound_text),
                "stage": "continue",
            }
        )
    owner_follow_up = polite_owner_follow_up(contact, inbound_text)
    if owner_follow_up:
        return normalize_ai_action({"reply": owner_follow_up, "stage": "continue"})
    if should_pause_without_reply(contact, inbound_text):
        return normalize_ai_action({"reply": "", "stage": "continue"})
    if (
        contact.get("kind") == "lpr"
        and contact.get("status") == "lpr_self"
        and not contact.get("proposal_sent")
    ):
        if has_proposal_consent_reply(contact, inbound_text):
            return normalize_ai_action(
                {
                    "reply": "Отлично, прикреплю короткое КП. После ознакомления можем обсудить, насколько такой формат вам подходит.",
                    "stage": "send_proposal",
                    "send_proposal": True,
                    "is_lpr": True,
                }
            )
        return normalize_ai_action(
            {
                "reply": proposal_offer_reply(),
                "stage": "lpr_self",
                "send_proposal": False,
                "is_lpr": True,
            }
        )
    if contact.get("proposal_sent"):
        return normalize_ai_action(
            {
                "reply": after_proposal_followup_reply(contact, inbound_text),
                "stage": "after_proposal",
            }
        )
    if GREETING_ONLY_RE.match(inbound_text):
        if uses_keramo_investor_flow():
            return normalize_ai_action(
                {
                    "reply": "Подскажите, пожалуйста, уместно коротко написать по инвестиционному предложению KERAMO BUILD?",
                    "stage": "ask_lpr",
                }
            )
        return normalize_ai_action(
            {
                "reply": "Понял. Подскажите, пожалуйста, кто у вас смотрит коммерческие вопросы по цифровым продуктам или автокредитам?",
                "stage": "ask_lpr",
            }
        )
    attempts = int(contact.get("attempts") or 0)
    max_attempts = int(settings.get("max_lpr_attempts") or 4)
    if is_truthy(settings.get("send_proposal_after_failed_attempts")) and attempts >= max_attempts:
        if uses_keramo_investor_flow():
            return normalize_ai_action(
                {
                    "reply": "Понял, тогда отправлю короткое КП с цифрами. Если инвестиции смотрит другой человек, подскажите контакт, напишу напрямую.",
                    "stage": "send_proposal",
                    "send_proposal": True,
                }
            )
        return normalize_ai_action(
            {
                "reply": "Понял, тогда отправлю короткое КП. Если подскажете контакт ответственного, напишу уже напрямую.",
                "stage": "send_proposal",
                "send_proposal": True,
            }
        )
    return normalize_ai_action(
        {
            "reply": (
                "Подскажите, пожалуйста, кто у вас смотрит инвестиционные вопросы?"
                if uses_keramo_investor_flow()
                else "Подскажите, пожалуйста, кто у вас принимает решения по цифровым продуктам для кредитных продаж авто?"
            ),
            "stage": "ask_lpr",
        }
    )


def typing_delay_seconds(settings: dict[str, str], text: str | None = None) -> float:
    min_seconds = max(1.0, float(settings.get("typing_min_seconds") or 2))
    max_seconds = min(20.0, max(min_seconds, float(settings.get("typing_max_seconds") or 6)))
    if text:
        estimated = len(text) / random.uniform(32, 55)
        max_seconds = min(max_seconds, max(min_seconds, estimated))
    return random.uniform(min_seconds, max_seconds)


async def simulate_typing(
    green: GreenApiClient,
    settings: dict[str, str],
    chat_id: str,
    *,
    text: str | None = None,
    typing_type: str = "text",
) -> None:
    if not is_truthy(settings.get("typing_enabled")):
        return
    delay = typing_delay_seconds(settings, text)
    try:
        await green.send_typing(chat_id, int(delay * 1000), typing_type=typing_type)
        await asyncio.sleep(delay)
    except Exception as exc:
        log_event(
            "message",
            f"Не удалось показать статус набора для {chat_id}: {exc}",
            level="warning",
            payload={"chat_id": chat_id, "typing_type": typing_type},
        )


def load_message_history(contact_id: int, limit: int = 12) -> list[dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT direction, text, created_at, payload_json
            FROM messages
            WHERE project_id = ? AND contact_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (current_project_id(), contact_id, limit),
        ).fetchall()
    history: list[dict[str, Any]] = []
    for row in reversed(rows):
        item = dict(row)
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        message_data = payload.get("messageData") or {}
        item["quoted_context"] = quoted_message_context(message_data)
        item.pop("payload_json", None)
        history.append(item)
    return history


def history_item_content(item: dict[str, Any]) -> str:
    text = str(item.get("text") or "").strip()
    quoted = str(item.get("quoted_context") or "").strip()
    if text and quoted:
        return f"{text}\n[ответ на цитату: {quoted}]"
    return text


async def send_and_log(contact: dict[str, Any], text: str) -> dict[str, Any]:
    settings = get_settings()
    green = GreenApiClient(settings)
    if not green.configured:
        raise RuntimeError("GreenAPI не настроен")
    text = strip_repeated_greeting(sanitize_user_message_text(text), contact["id"])
    await simulate_typing(green, settings, contact["chat_id"], text=text)
    result = await green.send_message(contact["chat_id"], text)
    channel_msg_id = result.get("idMessage")
    save_message(contact["id"], contact["chat_id"], "out", text, channel_msg_id, result)
    update_contact_fields(contact["id"], last_outbound_at=now_iso(), last_error=None)
    log_event(
        "message",
        f"Отправлено сообщение на {contact['phone']}",
        contact_id=contact["id"],
        payload={"chat_id": contact["chat_id"], "idMessage": channel_msg_id},
    )
    return result


def claim_proposal_send(contact_id: int) -> tuple[bool, dict[str, Any] | None, str | None]:
    current_time = now_iso()
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not row:
            return False, None, "contact_missing"
        contact = dict(row)
        if contact["status"] in {"opt_out", "not_interested"}:
            return False, contact, contact["status"]
        if int(contact.get("proposal_sent") or 0) != 0:
            return False, contact, "already_sent_or_sending"
        cursor = conn.execute(
            """
            UPDATE contacts
            SET proposal_sent = 2,
                status = 'proposal_sending',
                stage = 'proposal_sending',
                updated_at = ?
            WHERE id = ? AND proposal_sent = 0 AND status NOT IN ('opt_out', 'not_interested')
            """,
            (current_time, contact_id),
        )
        if cursor.rowcount != 1:
            row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
            return False, row_dict(row), "already_claimed"
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    return True, row_dict(row), None


def release_proposal_claim(contact_id: int, error: str) -> None:
    update_contact_fields(
        contact_id,
        proposal_sent=0,
        status="error",
        stage="proposal_failed",
        last_error=error,
    )


async def send_proposal(contact: dict[str, Any]) -> None:
    fresh_contact = get_contact(contact["id"]) or contact
    if fresh_contact.get("status") in {"opt_out", "not_interested"}:
        log_event(
            "proposal",
            f"КП не отправлено на {fresh_contact['phone']}: отказ или неактуально",
            level="warning",
            contact_id=fresh_contact["id"],
        )
        return
    if fresh_contact.get("proposal_sent"):
        log_event("proposal", f"КП уже было отправлено или отправляется на {fresh_contact['phone']}", contact_id=fresh_contact["id"])
        return
    settings = get_settings()
    green = GreenApiClient(settings)
    if not green.configured:
        raise RuntimeError("GreenAPI не настроен")
    proposal_name = settings.get("proposal_filename") or PROPOSAL_PATH.name
    proposal_path = BASE_DIR / proposal_name
    if not proposal_path.exists():
        raise FileNotFoundError(f"Файл КП не найден: {proposal_path}")
    claimed, claimed_contact, reason = claim_proposal_send(fresh_contact["id"])
    if not claimed:
        if claimed_contact:
            log_event(
                "proposal",
                f"КП не отправлено на {claimed_contact['phone']}: {reason}",
                contact_id=claimed_contact["id"],
                payload={"reason": reason},
            )
        return
    active_contact = claimed_contact or fresh_contact
    if uses_keramo_investor_flow():
        caption = "Короткое КП KERAMO BUILD: инвестиции в производство керамогранита в Астане."
    elif get_project().get("workflow_type") == "generic_b2b":
        caption = f"Короткое КП: {project_topic_text()}."
    else:
        caption = "Короткое КП по мобильному приложению автоскоринга для автодилера."
    try:
        await simulate_typing(green, settings, active_contact["chat_id"], text=caption, typing_type="file")
        result = await green.send_file_by_upload(active_contact["chat_id"], proposal_path, caption)
        save_message(active_contact["id"], active_contact["chat_id"], "out", caption, result.get("idMessage"), result)
        update_contact_fields(
            active_contact["id"],
            proposal_sent=1,
            status="proposal_sent",
            stage="proposal_sent",
            last_outbound_at=now_iso(),
            last_error=None,
        )
        log_event(
            "proposal",
            f"КП отправлено на {active_contact['phone']}",
            contact_id=active_contact["id"],
            payload={"file": str(proposal_path.name), "idMessage": result.get("idMessage")},
        )
    except Exception as exc:
        release_proposal_claim(active_contact["id"], str(exc))
        log_event(
            "proposal",
            f"Ошибка отправки КП на {active_contact['phone']}: {exc}",
            level="error",
            contact_id=active_contact["id"],
        )
        raise


async def notify_handoff(contact: dict[str, Any], inbound_text: str, source: str) -> bool:
    settings = get_settings()
    if not is_truthy(settings.get("handoff_enabled")):
        update_contact_fields(contact["id"], status="interested", stage="handoff")
        log_event("handoff", f"Зацепка по {contact['phone']} помечена без уведомления: handoff выключен", contact_id=contact["id"])
        return False

    fresh_contact = get_contact(contact["id"]) or contact
    meta = contact_meta(fresh_contact)
    if meta.get("handoff_notified_at"):
        update_contact_fields(fresh_contact["id"], status="interested", stage="handoff")
        log_event(
            "handoff",
            f"Зацепка по {fresh_contact['phone']} уже была передана",
            contact_id=fresh_contact["id"],
            payload={"notified_at": meta.get("handoff_notified_at"), "source": source},
        )
        return False

    handoff_phones = parse_handoff_phones(settings.get("handoff_phone"), settings.get("default_country_code", "7"))
    if not handoff_phones:
        update_contact_fields(fresh_contact["id"], status="interested", stage="handoff", last_error="Не заданы номера для handoff")
        log_event("handoff", "Зацепка не передана: не заданы номера для уведомлений", level="error", contact_id=fresh_contact["id"])
        return False

    green = GreenApiClient(settings)
    if not green.configured:
        update_contact_fields(fresh_contact["id"], status="interested", stage="handoff", last_error="GreenAPI не настроен для handoff")
        log_event("handoff", f"Зацепка по {fresh_contact['phone']} помечена, но GreenAPI не настроен", level="warning", contact_id=fresh_contact["id"])
        return False

    text_preview = re.sub(r"\s+", " ", inbound_text or "").strip()
    if len(text_preview) > 700:
        text_preview = f"{text_preview[:700]}..."
    alert_title = "Новая зацепка по KERAMO BUILD." if uses_keramo_investor_flow() else "Новая зацепка по автоскорингу."
    alert = (
        f"{alert_title}\n"
        f"Контакт: {format_phone(fresh_contact.get('phone'))}\n"
        f"Организация: {fresh_contact.get('company') or 'не указана'}\n"
        f"Имя: {fresh_contact.get('name') or 'не указано'}\n"
        f"Тип: {fresh_contact.get('kind')}\n"
        f"КП отправлено: {'да' if int(fresh_contact.get('proposal_sent') or 0) == 1 else 'нет'}\n"
        f"Источник распознавания: {source}\n"
        f"Сообщение клиента: {text_preview or 'без текста'}\n"
        "Нужно связаться вручную."
    )

    sent_results: list[dict[str, Any]] = []
    failed_results: list[dict[str, str]] = []
    for handoff_phone in handoff_phones:
        try:
            result = await green.send_message(chat_id_for_phone(handoff_phone), alert)
            save_message(None, chat_id_for_phone(handoff_phone), "out", alert, result.get("idMessage"), result)
            sent_results.append({"phone": handoff_phone, "idMessage": result.get("idMessage")})
        except Exception as exc:
            failed_results.append({"phone": handoff_phone, "error": str(exc)})
            log_event(
                "handoff",
                f"Не удалось передать зацепку по {fresh_contact['phone']} на {format_phone(handoff_phone)}: {exc}",
                level="error",
                contact_id=fresh_contact["id"],
                payload={"handoff_phone": handoff_phone, "source": source},
            )

    if not sent_results:
        update_contact_fields(
            fresh_contact["id"],
            status="interested",
            stage="handoff",
            last_error="Не удалось отправить handoff ни на один номер",
        )
        return False

    notified_at = now_iso()
    update_contact_fields(fresh_contact["id"], status="interested", stage="handoff", last_error=None)
    update_contact_meta(
        fresh_contact["id"],
        handoff_notified_at=notified_at,
        handoff_phone=sent_results[0]["phone"],
        handoff_phones=[item["phone"] for item in sent_results],
        handoff_failed_phones=failed_results,
        handoff_source=source,
        handoff_inbound_text=text_preview,
    )
    formatted_phones = ", ".join(format_phone(item["phone"]) for item in sent_results)
    level = "warning" if failed_results else "info"
    log_event(
        "handoff",
        f"Зацепка по {fresh_contact['phone']} передана на {formatted_phones}",
        level=level,
        contact_id=fresh_contact["id"],
        payload={"handoff_phones": sent_results, "failed": failed_results, "source": source},
    )
    return True


async def handle_interested_contact(contact: dict[str, Any], inbound_text: str, source: str) -> None:
    active_contact = get_contact(contact["id"]) or contact
    update_contact_fields(active_contact["id"], status="interested_pending", stage="interest_dialog")
    active_contact = get_contact(contact["id"]) or active_contact
    await send_and_log(active_contact, interested_reply_text(active_contact, inbound_text))
    active_contact = get_contact(contact["id"]) or active_contact
    if has_explicit_proposal_request(inbound_text) and not int(active_contact.get("proposal_sent") or 0):
        await send_proposal(active_contact)
    active_contact = get_contact(contact["id"]) or active_contact
    await notify_handoff(active_contact, inbound_text, source)


async def handle_pending_proposal_consent(contact: dict[str, Any], inbound_text: str, settings: dict[str, str]) -> bool:
    intent = await classify_reply_intent(settings, contact, inbound_text)
    if not (intent.get("proposal_consent") or intent.get("explicit_proposal_request")):
        return False

    active_contact = mark_contact_as_lpr(contact, "proposal_consent_intent")
    await send_and_log(
        active_contact,
        "Отлично, прикреплю короткое КП. После ознакомления можем обсудить, насколько такой формат вам подходит.",
    )
    await send_proposal(get_contact(active_contact["id"]) or active_contact)
    return True


async def handle_after_proposal_contact(contact: dict[str, Any], inbound_text: str, settings: dict[str, str]) -> None:
    if asks_to_leave_our_contact(inbound_text):
        await send_and_log(contact, our_callback_contact_reply(contact))
        await notify_handoff(get_contact(contact["id"]) or contact, inbound_text, "after_proposal_callback_contact_requested")
        return

    if offers_specialist_contact(inbound_text):
        await send_and_log(contact, specialist_contact_request_reply(contact))
        return

    intent = await classify_reply_intent(settings, contact, inbound_text)
    if intent.get("terminal_ack") and not any(
        intent.get(key)
        for key in (
            "asks_if_proposal_already_sent",
            "asks_details",
            "meeting_interest",
            "interested_next_step",
            "will_forward_to_responsible",
            "forward_summary_consent",
            "answers_qualification_focus",
        )
    ):
        log_event(
            "message",
            f"Входящее от {contact['phone']} принято без ответа: КП уже отправлено и шаг закрыт",
            contact_id=contact["id"],
            payload={"stage": contact.get("stage"), "status": contact.get("status"), "text": inbound_text},
        )
        return

    if intent.get("asks_if_proposal_already_sent"):
        await send_and_log(
            contact,
            "Да, КП уже отправил сюда. Могу коротко ответить по любому пункту из него.",
        )
        return

    if intent.get("forward_summary_consent"):
        await send_and_log(contact, forward_summary_text(contact))
        await notify_handoff(get_contact(contact["id"]) or contact, inbound_text, "forward_summary_sent")
        return

    if intent.get("will_forward_to_responsible"):
        await send_and_log(contact, forward_summary_offer_reply(contact))
        return

    if intent.get("answers_qualification_focus"):
        await send_and_log(contact, qualification_focus_followup_reply(contact, inbound_text))
        return

    if intent.get("meeting_interest"):
        await send_and_log(contact, meeting_schedule_reply(contact))
        await notify_handoff(get_contact(contact["id"]) or contact, inbound_text, "after_proposal_intent")
        return

    if intent.get("asks_details"):
        await send_and_log(contact, after_proposal_detail_reply(contact, inbound_text))
        return

    action: dict[str, Any]
    if is_truthy(settings.get("ai_enabled")):
        try:
            action = await call_ai(settings, contact, inbound_text)
        except Exception as exc:
            update_contact_fields(contact["id"], last_error=f"AI error: {exc}")
            log_event("ai", f"Ошибка AI после КП по контакту {contact['phone']}: {exc}", level="error", contact_id=contact["id"])
            action = fallback_reply(contact, inbound_text)
    else:
        action = fallback_reply(contact, inbound_text)

    action["send_proposal"] = False
    action["stage"] = "after_proposal"
    if await reply_offers_proposal_again(settings, action.get("reply")):
        action["reply"] = after_proposal_followup_reply(contact, inbound_text)

    if action.get("interested") and not action.get("stop"):
        update_contact_fields(contact["id"], status="interested", stage="handoff")
        await notify_handoff(get_contact(contact["id"]) or contact, inbound_text, "after_proposal_ai")

    if action.get("stop"):
        update_contact_fields(contact["id"], status="opt_out", stage="opt_out")

    if action.get("reply"):
        await send_and_log(get_contact(contact["id"]) or contact, action["reply"])


def parse_vcard(vcard: str) -> tuple[str | None, list[str]]:
    name = None
    fn_match = re.search(r"^FN:(.+)$", vcard, flags=re.MULTILINE)
    if fn_match:
        name = fn_match.group(1).strip()
    phones = extract_phones(vcard, get_settings().get("default_country_code", "7"))
    return name, phones


def extract_contact_payload_candidates(payload: dict[str, Any] | None, source: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    settings = get_settings()
    default_country = settings.get("default_country_code", "7")
    name = safe_cell(
        payload.get("displayName")
        or payload.get("name")
        or payload.get("fullName")
        or payload.get("contactName")
    )
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(phone: str, candidate_name: str | None) -> None:
        if phone in seen:
            return
        seen.add(phone)
        candidates.append({"phone": phone, "name": candidate_name, "source": source})

    vcard = payload.get("vcard") or payload.get("vCard") or ""
    if vcard:
        vcard_name, phones = parse_vcard(str(vcard))
        for phone in phones:
            add(phone, name or vcard_name)

    for key in ("phone", "phoneNumber", "phoneContact", "number", "waid", "waId", "id"):
        for phone in extract_phones(payload.get(key), default_country):
            add(phone, name)

    return candidates


def contact_array_payloads(message_data: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    containers = [
        message_data,
        message_data.get("messageData") or {},
        message_data.get("contactsArrayMessageData") or {},
    ]
    for container in containers:
        if not isinstance(container, dict):
            continue
        contacts = container.get("contacts") or container.get("contact") or []
        if isinstance(contacts, dict):
            payloads.append(contacts)
        elif isinstance(contacts, list):
            payloads.extend([contact for contact in contacts if isinstance(contact, dict)])
    return payloads


def extract_name_near_phone(text: str, phone: str) -> str | None:
    digits = re.sub(r"\D+", "", text)
    if phone not in digits:
        return None
    without_numbers = re.sub(r"\+?\d[\d\s().-]{8,}\d", " ", text)
    words = re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", without_numbers)
    stop_words = {
        "номер",
        "телефон",
        "контакт",
        "вот",
        "его",
        "ее",
        "лпр",
        "директор",
        "руководитель",
        "можно",
        "можете",
        "можешь",
        "напишите",
        "написать",
        "скину",
        "скиньте",
        "щас",
        "сейчас",
        "ему",
        "ей",
    }
    useful = [word for word in words if word.lower() not in stop_words]
    capitalized = [word for word in useful if word[:1].isupper()]
    if capitalized:
        return clean_person_name(" ".join(capitalized[-2:]))
    if not re.search(r"\b(имя|зовут|это)\b", text, re.IGNORECASE):
        return None
    if 1 <= len(useful) <= 3:
        return clean_person_name(" ".join(useful))
    if len(useful) > 3:
        return clean_person_name(" ".join(useful[-3:]))
    return None


def extract_lpr_candidates(text: str, message_data: dict[str, Any]) -> list[dict[str, Any]]:
    settings = get_settings()
    default_country = settings.get("default_country_code", "7")
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(phone: str, name: str | None, source: str, source_text: str | None = None) -> None:
        if phone in seen:
            return
        seen.add(phone)
        candidate = {"phone": phone, "name": name, "source": source}
        if source_text:
            candidate["source_text"] = source_text
        candidates.append(candidate)

    type_message = message_data.get("typeMessage")
    if type_message == "contactMessage":
        data = message_data.get("contactMessageData") or message_data.get("contact") or {}
        for candidate in extract_contact_payload_candidates(data, "contact_message"):
            add(candidate["phone"], candidate.get("name"), candidate["source"])

    if type_message == "contactsArrayMessage":
        for contact in contact_array_payloads(message_data):
            for candidate in extract_contact_payload_candidates(contact, "contacts_array"):
                add(candidate["phone"], candidate.get("name"), candidate["source"])

    quoted = message_data.get("quotedMessage") or message_data.get("quotedMessageData")
    if isinstance(quoted, dict):
        quoted_type = quoted.get("typeMessage")
        if quoted_type == "contactMessage" or quoted.get("vcard") or quoted.get("contactMessageData"):
            quoted_data = quoted.get("contactMessageData") or quoted.get("contact") or quoted
            for candidate in extract_contact_payload_candidates(quoted_data, "quoted_contact"):
                add(candidate["phone"], candidate.get("name"), candidate["source"])
        if quoted_type == "contactsArrayMessage" or quoted.get("contacts") or quoted.get("contactsArrayMessageData"):
            for contact in contact_array_payloads(quoted):
                for candidate in extract_contact_payload_candidates(contact, "quoted_contacts_array"):
                    add(candidate["phone"], candidate.get("name"), candidate["source"])

    if type_message in {"textMessage", "extendedTextMessage", "quotedMessage"}:
        for phone in extract_phones(text, default_country):
            add(phone, extract_name_near_phone(text, phone), "text", text)

    return candidates


def latest_lpr_needing_name(owner_contact_id: int) -> dict[str, Any] | None:
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM contacts
            WHERE project_id = ? AND owner_contact_id = ? AND kind = 'lpr' AND status = 'lpr_needs_name'
            ORDER BY id DESC
            LIMIT 1
            """,
            (current_project_id(), owner_contact_id),
        ).fetchone()
    return row_dict(row)


def latest_lpr_ready_for_owner(owner_contact_id: int) -> dict[str, Any] | None:
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT c.*
            FROM contacts c
            WHERE c.project_id = ?
              AND c.owner_contact_id = ?
              AND c.kind = 'lpr'
              AND c.status = 'lpr_ready'
              AND c.proposal_sent = 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM messages m
                  WHERE m.project_id = c.project_id
                    AND m.contact_id = c.id
                    AND m.direction = 'out'
              )
            ORDER BY c.id DESC
            LIMIT 1
            """,
            (current_project_id(), owner_contact_id),
        ).fetchone()
    return row_dict(row)


def looks_like_name(text: str) -> str | None:
    if extract_phones(text):
        return None
    return local_person_name_from_text(text)


def mark_contact_as_lpr(contact: dict[str, Any], reason: str, name: str | None = None) -> dict[str, Any]:
    current_time = now_iso()
    resolved_name = clean_person_name(name) or clean_person_name(contact.get("name"))
    with db_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM contacts WHERE project_id = ? AND phone = ? AND kind = 'lpr' AND id != ?",
            (current_project_id(), contact["phone"], contact["id"]),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE contacts
                SET name = COALESCE(?, name),
                    company = COALESCE(?, company),
                    status = CASE
                        WHEN status IN ('proposal_sent', 'opt_out') THEN status
                        ELSE 'lpr_self'
                    END,
                    stage = CASE
                        WHEN stage IN ('proposal_sent', 'opt_out') THEN stage
                        ELSE 'self_lpr'
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (resolved_name, contact.get("company"), current_time, existing["id"]),
            )
            conn.execute(
                """
                UPDATE contacts
                SET status = CASE
                        WHEN status IN ('proposal_sent', 'opt_out') THEN status
                        ELSE 'lpr_duplicate'
                    END,
                    stage = 'self_lpr_duplicate',
                    updated_at = ?
                WHERE id = ?
                """,
                (current_time, contact["id"]),
            )
            row = conn.execute("SELECT * FROM contacts WHERE id = ?", (existing["id"],)).fetchone()
        else:
            conn.execute(
                """
                UPDATE contacts
                SET kind = 'lpr',
                    name = COALESCE(?, name),
                    status = CASE
                        WHEN status IN ('proposal_sent', 'opt_out') THEN status
                        ELSE 'lpr_self'
                    END,
                    stage = CASE
                        WHEN stage IN ('proposal_sent', 'opt_out') THEN stage
                        ELSE 'self_lpr'
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (resolved_name, current_time, contact["id"]),
            )
            row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact["id"],)).fetchone()
    marked = row_dict(row) or contact
    log_event(
        "lpr",
        f"Контакт {contact['phone']} помечен как ответственный",
        contact_id=marked["id"],
        payload={"reason": reason, "source_contact_id": contact["id"]},
    )
    return marked


async def check_and_mark_whatsapp(contact: dict[str, Any]) -> bool | None:
    settings = get_settings()
    green = GreenApiClient(settings)
    if not green.configured:
        update_contact_fields(contact["id"], last_error="GreenAPI не настроен")
        return None
    try:
        exists = await green.check_whatsapp(contact["phone"])
        if exists:
            next_status = "ready" if contact["kind"] == "lead" and contact["status"] in {"new", "error"} else contact["status"]
        else:
            next_status = "no_whatsapp"
        update_contact_fields(
            contact["id"],
            whatsapp_exists=1 if exists else 0,
            status=next_status,
            last_error=None,
        )
        return exists
    except Exception as exc:
        update_contact_fields(contact["id"], status="error", last_error=str(exc))
        return None


async def contact_lpr(lpr: dict[str, Any]) -> None:
    active_lpr = get_contact(lpr["id"]) or lpr
    if active_lpr.get("proposal_sent"):
        return
    if has_previous_outbound(active_lpr["id"]):
        log_event(
            "lpr",
            f"Первичное сообщение ответственному {active_lpr['phone']} пропущено: уже отправляли ранее",
            contact_id=active_lpr["id"],
            payload={"stage": active_lpr.get("stage"), "status": active_lpr.get("status")},
        )
        return
    update_contact_fields(active_lpr["id"], status="lpr_ready", stage="lpr_intro")
    active_lpr = get_contact(active_lpr["id"]) or active_lpr
    await send_and_log(active_lpr, referred_lpr_intro_text(active_lpr))


async def handle_lpr_candidate(owner: dict[str, Any], candidate: dict[str, Any]) -> None:
    if candidate["phone"] == owner["phone"]:
        owner_lpr = mark_contact_as_lpr(owner, "same_phone_candidate", candidate.get("name"))
        await send_and_log(owner_lpr, proposal_offer_reply())
        return
    name = await resolve_person_name(
        candidate.get("name") or candidate.get("source_text"),
        context="имя ответственного рядом с полученным номером",
    )
    owner_sales_context = contact_sales_context(owner)
    lpr = create_or_update_contact(
        phone=candidate["phone"],
        kind="lpr",
        source=f"extracted:{candidate.get('source') or 'unknown'}",
        name=name,
        company=owner.get("company"),
        owner_contact_id=owner["id"],
        status="lpr_ready" if name else "lpr_needs_name",
        stage="new_lpr",
        meta={
            "from_chat_id": owner["chat_id"],
            "sales_signal": owner_sales_context.get("sales_signal"),
            "sales_angle": owner_sales_context.get("sales_angle"),
            "signal_fields": owner_sales_context.get("signal_fields"),
            "qualification_focus": owner_sales_context.get("qualification_focus"),
        },
    )
    log_event(
        "lpr",
        f"Найден кандидат ответственного: {candidate['phone']}",
        contact_id=owner["id"],
        payload={"lpr_contact_id": lpr["id"], "name": name, "source": candidate.get("source")},
    )
    update_contact_fields(owner["id"], stage="lpr_contact_shared")
    exists = lpr.get("whatsapp_exists")
    if exists is None:
        exists = await check_and_mark_whatsapp(lpr)
        lpr = get_contact(lpr["id"]) or lpr
    if exists is False:
        await send_and_log(owner, "По этому номеру WhatsApp не нашелся. Если есть другой контакт ответственного, пришлите, пожалуйста.")
    elif name:
        await send_and_log(owner, "Понял. Напишу ответственному напрямую и коротко объясню контекст.")
        await contact_lpr(lpr)
    elif not name:
        await send_and_log(owner, "Если знаете имя руководителя, напишите, пожалуйста. Если нет, это не проблема — я могу обратиться и без имени.")


async def remember_lpr_candidate(owner: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if candidate["phone"] == owner["phone"]:
        return mark_contact_as_lpr(owner, "same_phone_candidate_history", candidate.get("name"))

    name = await resolve_person_name(
        candidate.get("name") or candidate.get("source_text"),
        context="имя ответственного рядом с полученным номером из истории",
    )
    owner_sales_context = contact_sales_context(owner)
    lpr = create_or_update_contact(
        phone=candidate["phone"],
        kind="lpr",
        source=f"extracted:{candidate.get('source') or 'unknown'}",
        name=name,
        company=owner.get("company"),
        owner_contact_id=owner["id"],
        status="lpr_ready" if name else "lpr_needs_name",
        stage="new_lpr",
        meta={
            "from_chat_id": owner["chat_id"],
            "deferred_outbound": True,
            "sales_signal": owner_sales_context.get("sales_signal"),
            "sales_angle": owner_sales_context.get("sales_angle"),
            "signal_fields": owner_sales_context.get("signal_fields"),
            "qualification_focus": owner_sales_context.get("qualification_focus"),
        },
    )
    log_event(
        "lpr",
        f"Найден кандидат ответственного в истории: {candidate['phone']}",
        contact_id=owner["id"],
        payload={"lpr_contact_id": lpr["id"], "name": name, "source": candidate.get("source")},
    )
    if lpr.get("whatsapp_exists") is None:
        await check_and_mark_whatsapp(lpr)
        lpr = get_contact(lpr["id"]) or lpr
    return lpr


def buttons_message_text(message_data: dict[str, Any]) -> str:
    data = message_data.get("buttonsMessage") or message_data.get("buttonsMessageData") or {}
    parts = [
        safe_cell(data.get("contentText") or data.get("text") or data.get("title") or data.get("body")),
        safe_cell(data.get("footerText") or data.get("footer")),
    ]
    buttons = data.get("buttons") or []
    labels: list[str] = []
    if isinstance(buttons, list):
        for button in buttons:
            if not isinstance(button, dict):
                continue
            label = button.get("buttonText") or button.get("text") or button.get("displayText")
            if isinstance(label, dict):
                label = label.get("displayText") or label.get("text")
            label_text = safe_cell(label)
            if label_text:
                labels.append(label_text)
    if labels:
        parts.append("Варианты: " + ", ".join(labels))
    return "\n".join(part for part in parts if part).strip()


def message_text_from_data(message_data: dict[str, Any] | None) -> str:
    if not isinstance(message_data, dict):
        return ""
    type_message = message_data.get("typeMessage")
    if type_message == "textMessage" or message_data.get("textMessage"):
        return safe_cell((message_data.get("textMessageData") or {}).get("textMessage") or message_data.get("textMessage"))
    if type_message in {"extendedTextMessage", "quotedMessage"} or message_data.get("extendedTextMessageData"):
        data = message_data.get("extendedTextMessageData") or {}
        return safe_cell(data.get("text") or data.get("description") or data.get("title") or message_data.get("text"))
    if type_message == "contactMessage" or message_data.get("contactMessageData") or message_data.get("contact"):
        data = message_data.get("contactMessageData") or message_data.get("contact") or message_data
        return f"Контакт: {data.get('displayName') or data.get('name') or ''}\n{data.get('vcard') or data.get('vCard') or ''}".strip()
    if type_message == "contactsArrayMessage" or message_data.get("contactsArrayMessageData"):
        return json.dumps(message_data, ensure_ascii=False)
    if type_message == "buttonsMessage" or message_data.get("buttonsMessage") or message_data.get("buttonsMessageData"):
        return buttons_message_text(message_data)
    media_data = (
        message_data.get("fileMessageData")
        or message_data.get("imageMessageData")
        or message_data.get("videoMessageData")
        or message_data.get("documentMessageData")
        or message_data.get("audioMessageData")
        or message_data.get("journalData")
        or {}
    )
    return safe_cell(media_data.get("caption") or media_data.get("description") or media_data.get("textMessage")) or ""


def quoted_message_context(message_data: dict[str, Any] | None) -> str:
    if not isinstance(message_data, dict):
        return ""
    quoted = message_data.get("quotedMessage") or message_data.get("quotedMessageData")
    text = message_text_from_data(quoted) if isinstance(quoted, dict) else ""
    return compact_message(text, limit=400) if text else ""


def extract_inbound_text(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    message_data = body.get("messageData") or {}
    text = message_text_from_data(message_data)
    return text.strip(), message_data


def backfill_message_text_from_payloads(conn: sqlite3.Connection) -> int:
    updated = 0
    rows = conn.execute(
        """
        SELECT id, payload_json
        FROM messages
        WHERE direction = 'in'
          AND payload_json IS NOT NULL
          AND (text IS NULL OR TRIM(text) = '')
        """
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        message_data = payload.get("messageData") or {}
        text = message_text_from_data(message_data).strip()
        if not text:
            continue
        conn.execute("UPDATE messages SET text = ? WHERE id = ?", (text, row["id"]))
        updated += 1
    return updated


def audio_file_message_data(message_data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message_data, dict) or message_data.get("typeMessage") != "audioMessage":
        return {}
    data = message_data.get("fileMessageData") or message_data.get("audioMessageData") or {}
    return data if isinstance(data, dict) else {}


def groq_transcription_configured(settings: dict[str, str]) -> bool:
    return bool(settings.get("groq_api_key", "").strip())


async def transcribe_audio_message(
    settings: dict[str, str],
    message_data: dict[str, Any],
    contact_id: int | None = None,
) -> str | None:
    if not is_truthy(settings.get("audio_transcription_enabled")):
        return None
    if not groq_transcription_configured(settings):
        log_event(
            "audio",
            "Аудио не расшифровано: Groq API key не задан",
            level="warning",
            contact_id=contact_id,
            payload={"typeMessage": message_data.get("typeMessage")},
        )
        return None

    file_data = audio_file_message_data(message_data)
    download_url = safe_cell(file_data.get("downloadUrl") or file_data.get("urlFile") or file_data.get("url"))
    if not download_url:
        return None

    raw_name = safe_cell(file_data.get("fileName")) or "voice.ogg"
    mime_type = safe_cell(file_data.get("mimeType")) or "application/octet-stream"
    upload_name = raw_name
    if Path(upload_name).suffix.lower() == ".oga" or "ogg" in mime_type.lower():
        upload_name = f"{Path(upload_name).stem or 'voice'}.ogg"

    base_url = settings.get("groq_base_url", "https://api.groq.com/openai/v1").rstrip("/")
    model = settings.get("groq_transcription_model", "whisper-large-v3-turbo").strip() or "whisper-large-v3-turbo"
    headers = {"Authorization": f"Bearer {settings.get('groq_api_key', '').strip()}"}
    data = {
        "model": model,
        "response_format": "json",
        "temperature": "0",
        "prompt": "WhatsApp диалог B2B-продаж на русском или казахском языке. Сохрани исходный смысл коротко и точно.",
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            media_response = await client.get(download_url)
            media_response.raise_for_status()
            files = {"file": (upload_name, media_response.content, mime_type)}
            response = await client.post(
                f"{base_url}/audio/transcriptions",
                data=data,
                files=files,
                headers=headers,
            )
            response.raise_for_status()
            result = response.json()
    except Exception as exc:
        log_event(
            "audio",
            f"Ошибка расшифровки аудио через Groq: {exc}",
            level="error",
            contact_id=contact_id,
            payload={"fileName": raw_name, "mimeType": mime_type},
        )
        return None

    text = safe_cell(result.get("text")) if isinstance(result, dict) else ""
    if not text:
        return None
    message_data["transcriptionData"] = {
        "provider": "groq",
        "model": model,
        "text": text,
        "transcribed_at": now_iso(),
    }
    log_event(
        "audio",
        "Аудио расшифровано через Groq",
        contact_id=contact_id,
        payload={"fileName": raw_name, "mimeType": mime_type, "model": model},
    )
    return text


def journal_item_to_notification_body(item: dict[str, Any]) -> dict[str, Any]:
    type_message = item.get("typeMessage") or "textMessage"
    message_data: dict[str, Any] = {"typeMessage": type_message}

    if type_message == "textMessage":
        message_data["textMessageData"] = {"textMessage": item.get("textMessage") or ""}
    elif type_message == "extendedTextMessage":
        extended = item.get("extendedTextMessage") or item.get("extendedTextMessageData") or {}
        message_data["extendedTextMessageData"] = {
            "text": item.get("textMessage") or extended.get("text") or extended.get("description") or ""
        }
    elif type_message == "contactMessage":
        contact_data = item.get("contact") or item.get("contactMessageData") or {}
        message_data["contactMessageData"] = contact_data
    elif type_message == "contactsArrayMessage":
        contacts_data = item.get("contactsArrayMessageData") or {}
        contacts = item.get("contacts") or contacts_data.get("contacts") or []
        message_data["contactsArrayMessageData"] = {"contacts": contacts}
    else:
        message_data["journalData"] = item

    chat_id = item.get("chatId") or item.get("senderId")
    return {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": item.get("idMessage"),
        "timestamp": item.get("timestamp"),
        "senderData": {
            "chatId": chat_id,
            "sender": item.get("senderId") or chat_id,
            "senderName": item.get("senderName"),
            "senderContactName": item.get("senderContactName"),
            "chatName": item.get("chatName"),
        },
        "messageData": message_data,
    }


async def process_notification_body(body: dict[str, Any], source: str = "poll", allow_outbound: bool = True) -> None:
    resolved_project_id = project_id_from_notification_body(body)
    if resolved_project_id and resolved_project_id != current_project_id():
        with use_project(resolved_project_id):
            await _process_notification_body(body, source=source, allow_outbound=allow_outbound)
        return
    await _process_notification_body(body, source=source, allow_outbound=allow_outbound)


async def _process_notification_body(body: dict[str, Any], source: str = "poll", allow_outbound: bool = True) -> None:
    if body.get("typeWebhook") != "incomingMessageReceived":
        return

    id_message = body.get("idMessage")
    if message_exists("in", id_message):
        log_event(
            "message",
            f"Входящее {id_message} уже обработано",
            payload={"source": source, "idMessage": id_message},
        )
        return

    sender = body.get("senderData") or {}
    chat_id = sender.get("chatId") or sender.get("sender")
    if not chat_id or "@g.us" in chat_id:
        return

    settings = get_settings()
    phone = normalize_phone(chat_id.split("@", 1)[0], settings.get("default_country_code", "7"))
    if not phone:
        return

    contact = get_contact_by_chat(chat_id)
    if contact is None:
        contact = create_or_update_contact(
            phone=phone,
            kind="lead",
            source=f"inbound:{source}",
            name=safe_cell(sender.get("senderName") or sender.get("chatName") or sender.get("senderContactName")),
            status="replied",
            stage="inbound_unknown",
        )

    inbound_text, message_data = extract_inbound_text(body)
    sender_name = safe_cell(sender.get("senderName") or sender.get("chatName") or sender.get("senderContactName"))
    if sender_name and not contact.get("name"):
        update_contact_fields(contact["id"], name=sender_name)
    if not inbound_text and message_data.get("typeMessage") == "audioMessage":
        transcribed_text = await transcribe_audio_message(settings, message_data, contact_id=contact["id"])
        if transcribed_text:
            inbound_text = transcribed_text
            body["messageData"] = message_data
    save_message(contact["id"], chat_id, "in", inbound_text, body.get("idMessage"), body)
    log_event(
        "message",
        f"Получено входящее от {phone}",
        contact_id=contact["id"],
        payload={"chat_id": chat_id, "idMessage": body.get("idMessage")},
    )
    if contact["status"] == "opt_out":
        next_status = "opt_out"
        next_stage = contact.get("stage") or "opt_out"
    elif contact["status"] == "not_interested":
        next_status = "not_interested"
        next_stage = contact.get("stage") or "closed_no_interest"
    elif contact["status"] in {"interested", "interested_pending"}:
        next_status = contact["status"]
        next_stage = contact.get("stage") or "handoff"
    elif contact.get("proposal_sent"):
        next_status = "replied_after_proposal"
        next_stage = "after_proposal_reply"
    elif contact.get("kind") == "lpr" and contact.get("status") in LPR_ACTIVE_STATUSES:
        next_status = contact["status"]
        next_stage = contact.get("stage") or "lpr_reply"
    else:
        next_status = "replied"
        next_stage = contact.get("stage") or "replied"
    update_contact_fields(
        contact["id"],
        status=next_status,
        stage=next_stage,
        last_inbound_at=now_iso(),
        attempts=int(contact.get("attempts") or 0) + 1,
    )
    contact = get_contact(contact["id"]) or contact

    if not inbound_text and message_data.get("typeMessage") not in {"contactMessage", "contactsArrayMessage"}:
        log_event(
            "message",
            f"Входящее от {phone} без текстового содержимого сохранено без ответа",
            contact_id=contact["id"],
            payload={"typeMessage": message_data.get("typeMessage")},
        )
        return

    if OPT_OUT_RE.search(inbound_text):
        update_contact_fields(contact["id"], status="opt_out", stage="opt_out")
        log_event("contact", f"Контакт {phone} отказался от переписки", level="warning", contact_id=contact["id"])
        if allow_outbound:
            await send_and_log(contact, "Понял, больше не будем беспокоить. Спасибо.")
        return
    if contact["status"] == "opt_out":
        return
    if contact["status"] == "not_interested":
        log_event(
            "message",
            f"Входящее от {phone} принято без ответа: контакт уже закрыт как неактуальный",
            contact_id=contact["id"],
            payload={"stage": contact.get("stage"), "text": inbound_text},
        )
        return

    if SOFT_NEGATIVE_RE.search(inbound_text):
        log_event("contact", f"Контакт {phone} ответил мягким отказом", contact_id=contact["id"])
        if allow_outbound:
            await send_and_log(contact, soft_negative_reply(contact))
        else:
            update_contact_fields(contact["id"], status="not_interested", stage="closed_no_interest")
        return

    if LANGUAGE_PROMPT_RE.search(inbound_text) and (
        message_data.get("typeMessage") == "buttonsMessage"
        or BOT_OR_AUTO_REPLY_RE.search(inbound_text)
        or contact.get("stage") == "warmup_permission"
    ):
        if allow_outbound:
            await send_and_log(contact, reply_language_choice_text())
        else:
            update_contact_fields(contact["id"], status="replied", stage="language_prompt")
        return

    if asks_to_leave_our_contact(inbound_text):
        if allow_outbound:
            await send_and_log(contact, our_callback_contact_reply(contact))
            await notify_handoff(get_contact(contact["id"]) or contact, inbound_text, "callback_contact_requested")
        else:
            update_contact_fields(contact["id"], status="interested_pending", stage="awaiting_callback")
            update_contact_meta(contact["id"], deferred_interest_text=inbound_text, deferred_interest_at=now_iso())
        return

    if offers_specialist_contact(inbound_text):
        if allow_outbound:
            await send_and_log(contact, specialist_contact_request_reply(contact))
        else:
            update_contact_fields(contact["id"], status="replied", stage="awaiting_specialist_contact")
        return

    if proposal_already_sent(contact):
        if not allow_outbound:
            return
        await handle_after_proposal_contact(contact, inbound_text, settings)
        return

    if EMAIL_REQUEST_RE.search(inbound_text):
        if allow_outbound:
            await send_and_log(contact, email_request_reply(contact, inbound_text))
        return

    if has_explicit_proposal_request(inbound_text):
        contact = mark_contact_as_lpr(contact, "explicit_proposal_request", sender_name)
        if allow_outbound:
            await send_and_log(
                contact,
                "Отлично, прикреплю короткое КП. После ознакомления можем обсудить, насколько такой формат вам подходит.",
            )
            await send_proposal(get_contact(contact["id"]) or contact)
        return

    if BUYER_CONFUSION_RE.search(inbound_text):
        if allow_outbound:
            await send_and_log(contact, buyer_confusion_reply(contact))
        else:
            update_contact_fields(contact["id"], status="replied", stage="clarified_offer")
        return

    if uses_autoscore_warmup_flow() and contact.get("stage") == "warmup_permission" and not IDENTITY_QUESTION_RE.search(inbound_text):
        if WARMUP_DECLINE_RE.search(inbound_text or ""):
            if allow_outbound:
                await send_and_log(contact, warmup_decline_reply(contact))
            else:
                update_contact_fields(contact["id"], status="not_interested", stage="closed_no_interest")
            return
        if allow_outbound:
            await send_and_log(contact, warmup_credit_check_reply(contact))
        else:
            warmup_credit_check_reply(contact)
        return

    if uses_autoscore_warmup_flow() and contact.get("stage") == "warmup_credit_check" and not IDENTITY_QUESTION_RE.search(inbound_text):
        if WARMUP_AUTOCREDIT_NO_RE.search(inbound_text or ""):
            if allow_outbound:
                await send_and_log(contact, warmup_decline_reply(contact))
            else:
                update_contact_fields(contact["id"], status="not_interested", stage="closed_no_interest")
            return
        if WARMUP_AUTOCREDIT_YES_RE.search(inbound_text or ""):
            intent = await classify_reply_intent(settings, contact, inbound_text)
            updated_contact, reply = warmup_autocredit_positive_reply(
                contact,
                inbound_text,
                sender_name,
                self_responsible=intent.get("self_responsible", False),
            )
            if allow_outbound:
                await send_and_log(updated_contact, reply)
            return

    pending_lpr = latest_lpr_needing_name(contact["id"])
    pending_name = (
        await resolve_person_name(
            inbound_text,
            context="ответ на мягкую просьбу назвать имя ответственного после получения его номера",
            settings=get_settings(),
        )
        if pending_lpr and not extract_phones(inbound_text)
        else None
    )
    if pending_lpr and pending_name:
        update_contact_fields(pending_lpr["id"], name=pending_name, status="lpr_ready")
        if allow_outbound:
            await send_and_log(contact, "Понял, написал ответственному напрямую.")
            await contact_lpr(get_contact(pending_lpr["id"]) or pending_lpr)
        return

    if pending_lpr and UNKNOWN_NAME_RE.search(inbound_text):
        update_contact_fields(pending_lpr["id"], name=None, status="lpr_ready")
        if allow_outbound:
            await send_and_log(contact, "Понял. Этого достаточно, напишу аккуратно и без имени.")
            await contact_lpr(get_contact(pending_lpr["id"]) or pending_lpr)
        return

    if pending_lpr and is_low_value_step_done_reply(inbound_text):
        update_contact_fields(pending_lpr["id"], name=None, status="lpr_ready")
        log_event(
            "lpr",
            f"Имя ответственного не получено, продолжаем без имени: {pending_lpr['phone']}",
            contact_id=contact["id"],
            payload={"lpr_contact_id": pending_lpr["id"], "inbound_text": inbound_text},
        )
        if allow_outbound:
            await contact_lpr(get_contact(pending_lpr["id"]) or pending_lpr)
        return

    if SELF_LPR_RE.search(inbound_text) or is_direct_self_reply(contact["id"], inbound_text):
        contact = mark_contact_as_lpr(contact, "self_reply_regex", sender_name)
        if allow_outbound:
            if has_explicit_proposal_request(inbound_text):
                await send_and_log(
                    contact,
                    "Отлично, прикреплю короткое КП. После ознакомления можем обсудить, насколько такой формат вам подходит.",
                )
                await send_proposal(get_contact(contact["id"]) or contact)
            else:
                await send_and_log(contact, proposal_offer_reply())
        return

    if has_explicit_proposal_request(inbound_text) and not EMAIL_REQUEST_RE.search(inbound_text):
        contact = mark_contact_as_lpr(contact, "explicit_proposal_request", sender_name)
        if allow_outbound:
            await send_and_log(
                contact,
                "Отлично, прикреплю короткое КП. После ознакомления можем обсудить, насколько такой формат вам подходит.",
            )
            await send_proposal(get_contact(contact["id"]) or contact)
        return

    if DETAIL_REQUEST_RE.search(inbound_text):
        if allow_outbound:
            await send_and_log(contact, detail_request_reply(contact))
        else:
            detail_request_reply(contact)
        return

    if is_active_interest_contact(contact) and MEETING_INTEREST_RE.search(inbound_text):
        if allow_outbound:
            await send_and_log(contact, meeting_schedule_reply(contact))
        else:
            update_contact_fields(contact["id"], status="interested", stage="interest_dialog")
        return

    if is_qualification_focus_answer(contact, inbound_text):
        if allow_outbound:
            await send_and_log(contact, qualification_focus_followup_reply(contact, inbound_text))
        else:
            update_contact_fields(contact["id"], status="replied", stage="qualification_focus_answered")
        return

    if is_interested_text(inbound_text):
        if SELF_LPR_RE.search(inbound_text) or is_direct_self_reply(contact["id"], inbound_text):
            contact = mark_contact_as_lpr(contact, "self_lpr_interested_regex", sender_name)
        if allow_outbound:
            await handle_interested_contact(contact, inbound_text, "regex")
        else:
            update_contact_fields(contact["id"], status="interested_pending", stage="handoff_pending")
            update_contact_meta(contact["id"], deferred_interest_text=inbound_text, deferred_interest_at=now_iso())
            log_event("handoff", f"Зацепка по {contact['phone']} найдена в истории", contact_id=contact["id"])
        return

    if BUDGET_OBJECTION_RE.search(inbound_text):
        if allow_outbound:
            await send_and_log(contact, budget_objection_reply(contact))
        else:
            update_contact_fields(contact["id"], status="replied", stage="budget_objection")
        return

    if ALREADY_HAVE_RE.search(inbound_text):
        if allow_outbound:
            await send_and_log(contact, existing_solution_reply(contact))
        else:
            update_contact_fields(contact["id"], status="replied", stage="existing_solution_objection")
        return

    if TRANSFER_OFFER_RE.search(inbound_text):
        if allow_outbound:
            await send_and_log(contact, transfer_offer_reply(contact))
        else:
            update_contact_fields(contact["id"], status="replied", stage="awaiting_intro")
        return

    if ACTION_REQUEST_RE.search(inbound_text):
        if allow_outbound:
            await send_and_log(contact, action_request_reply(contact))
        else:
            update_contact_fields(contact["id"], status="replied", stage="awaiting_intro")
        return

    if BUYER_CONFUSION_RE.search(inbound_text):
        if allow_outbound:
            await send_and_log(contact, buyer_confusion_reply(contact))
        else:
            update_contact_fields(contact["id"], status="replied", stage="clarified_offer")
        return

    candidates = extract_lpr_candidates(inbound_text, message_data)
    if candidates:
        for candidate in candidates[:3]:
            if allow_outbound:
                await handle_lpr_candidate(contact, candidate)
            else:
                await remember_lpr_candidate(contact, candidate)
        return

    if should_pause_without_reply(contact, inbound_text):
        log_event(
            "message",
            f"Входящее от {phone} принято без ответа: шаг уже закрыт",
            contact_id=contact["id"],
            payload={"stage": contact.get("stage"), "status": contact.get("status"), "text": inbound_text},
        )
        return

    if CONFUSION_RE.search(inbound_text):
        await send_and_log(contact, clarify_offer_reply(contact))
        return

    if not allow_outbound:
        return

    if IDENTITY_QUESTION_RE.search(inbound_text):
        await send_and_log(contact, f"{identity_reply_text(settings)} Если удобно, могу дальше коротко написать уже по сути.")
        return

    if EMAIL_REQUEST_RE.search(inbound_text):
        await send_and_log(contact, email_request_reply(contact, inbound_text))
        return

    owner_follow_up = polite_owner_follow_up(contact, inbound_text)
    if owner_follow_up:
        await send_and_log(contact, owner_follow_up)
        return

    meta = contact_meta(get_contact(contact["id"]) or contact)
    if contact.get("status") == "interested_pending" and not meta.get("handoff_notified_at"):
        await handle_interested_contact(contact, meta.get("deferred_interest_text") or inbound_text, "history_deferred_interest")
        return

    ready_lpr = latest_lpr_ready_for_owner(contact["id"])
    if ready_lpr:
        await send_and_log(contact, "Понял, написал ответственному напрямую и коротко объясню контекст.")
        await contact_lpr(ready_lpr)
        return

    if contact.get("kind") == "lead" and has_linked_lpr(contact["id"]) and looks_like_name(inbound_text):
        log_event(
            "message",
            f"Входящее от {phone} принято без ответа: ответственный уже найден и проконтактирован",
            contact_id=contact["id"],
            payload={"stage": contact.get("stage"), "status": contact.get("status"), "text": inbound_text},
        )
        return

    if is_waiting_proposal_consent(contact):
        if await handle_pending_proposal_consent(contact, inbound_text, settings):
            return
        contact = get_contact(contact["id"]) or contact

    if is_waiting_proposal_consent(contact) and not is_truthy(settings.get("ai_enabled")):
        action = fallback_reply(contact, inbound_text)
        if action["reply"]:
            await send_and_log(contact, action["reply"])
        if action["send_proposal"]:
            await send_proposal(get_contact(contact["id"]) or contact)
        return

    if not is_truthy(settings.get("ai_enabled")):
        return

    try:
        action = await call_ai(settings, contact, inbound_text)
    except Exception as exc:
        update_contact_fields(contact["id"], last_error=f"AI error: {exc}")
        log_event("ai", f"Ошибка AI по контакту {contact['phone']}: {exc}", level="error", contact_id=contact["id"])
        action = fallback_reply(contact, inbound_text)

    action["reply"] = strip_redundant_self_intro(action["reply"], contact, inbound_text, settings)

    lpr_phone = normalize_phone(action.get("lpr_phone"), settings.get("default_country_code", "7"))
    if lpr_phone:
        await handle_lpr_candidate(
            contact,
            {"phone": lpr_phone, "name": action.get("lpr_name") or None, "source": "ai"},
        )
        return

    if int(contact.get("proposal_sent") or 0) and action["send_proposal"]:
        action["send_proposal"] = False
        if not action["reply"] or re.search(r"\b(отправлю|прикреплю|скину).{0,40}кп\b", action["reply"], re.IGNORECASE):
            action["reply"] = "КП уже отправил. Если удобно, можем коротко обсудить, подходит ли такой формат под вашу воронку продаж."
        action["stage"] = "after_proposal"

    if int(contact.get("proposal_sent") or 0) and re.search(
        r"\b(могу\s+(?:отправить|прислать)|отправлю|пришлю|прикреплю|скину).{0,50}кп\b",
        action["reply"] or "",
        re.IGNORECASE,
    ):
        action["reply"] = after_proposal_followup_reply(contact, inbound_text)
        action["stage"] = "after_proposal"
        action["send_proposal"] = False

    if action["interested"] and not action["stop"]:
        if action["is_lpr"] or action["stage"] in {"lpr_self", "self_lpr"}:
            contact = mark_contact_as_lpr(contact, "ai_detected_interested_self_lpr", action.get("lpr_name") or sender_name)
            if action["send_proposal"] or has_explicit_proposal_request(inbound_text):
                await send_and_log(
                    contact,
                    action["reply"] or "Отлично, прикреплю короткое КП. После ознакомления можем обсудить, насколько такой формат вам подходит.",
                )
                await send_proposal(get_contact(contact["id"]) or contact)
            else:
                await send_and_log(contact, action["reply"] or proposal_offer_reply())
            return
        await handle_interested_contact(contact, inbound_text, "ai")
        return

    is_self_lpr_action = action["is_lpr"] or action["stage"] in {"lpr_self", "self_lpr"}
    if is_self_lpr_action and not action["send_proposal"] and not has_explicit_proposal_request(inbound_text):
        action["send_proposal"] = False
        action["stage"] = "lpr_self"
        if (
            not action["reply"]
            or re.search(r"\b(отправлю|прикреплю|пришлю|скину).{0,40}кп\b", action["reply"], re.IGNORECASE)
            or re.search(r"\bпочт\w*\b|whatsapp", action["reply"], re.IGNORECASE)
        ):
            action["reply"] = proposal_offer_reply()

    max_attempts = int(settings.get("max_lpr_attempts") or 4)
    if (
        not action["stop"]
        and not action["send_proposal"]
        and not int(contact.get("proposal_sent") or 0)
        and is_truthy(settings.get("send_proposal_after_failed_attempts"))
        and int(contact.get("attempts") or 0) >= max_attempts
    ):
        action["reply"] = (
            "Понял, тогда отправлю короткое КП. Если подскажете контакт ответственного, напишу уже напрямую."
        )
        action["send_proposal"] = True
        action["stage"] = "send_proposal"

    if action["stop"]:
        update_contact_fields(contact["id"], status="opt_out", stage="opt_out")

    if is_self_lpr_action and not action["stop"]:
        contact = mark_contact_as_lpr(contact, "ai_detected_self_lpr", action.get("lpr_name") or sender_name)

    if action["reply"]:
        await send_and_log(contact, action["reply"])

    if action["send_proposal"]:
        await send_proposal(get_contact(contact["id"]) or contact)


async def check_whatsapp_worker() -> None:
    project_id = current_project_id()
    with use_project(project_id):
        runtime_state["check"] = {"status": "running", "processed": 0, "total": 0, "last_error": None, "project_id": project_id}
        log_event("whatsapp", "Проверка WhatsApp запущена")
        with db_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM contacts
                WHERE project_id = ? AND whatsapp_exists IS NULL AND status NOT IN ('opt_out')
                ORDER BY id
                """,
                (project_id,),
            ).fetchall()
        contacts = [dict(row) for row in rows]
        runtime_state["check"]["total"] = len(contacts)
        if not contacts:
            runtime_state["check"]["status"] = "done"
            log_event("whatsapp", "Проверка WhatsApp завершена: нет контактов для проверки")
            return

        settings = get_settings()
        green = GreenApiClient(settings)
        if not green.configured:
            runtime_state["check"]["status"] = "error"
            runtime_state["check"]["last_error"] = "GreenAPI не настроен"
            log_event("whatsapp", "Проверка WhatsApp остановлена: GreenAPI не настроен", level="error")
            return

        for contact in contacts:
            try:
                exists = await green.check_whatsapp(contact["phone"])
                if exists:
                    next_status = "ready" if contact["kind"] == "lead" and contact["status"] in {"new", "error"} else contact["status"]
                else:
                    next_status = "no_whatsapp"
                update_contact_fields(
                    contact["id"],
                    whatsapp_exists=1 if exists else 0,
                    status=next_status,
                    last_error=None,
                )
            except asyncio.CancelledError:
                runtime_state["check"]["status"] = "cancelled"
                raise
            except Exception as exc:
                update_contact_fields(contact["id"], status="error", last_error=str(exc))
                runtime_state["check"]["last_error"] = str(exc)
                log_event(
                    "whatsapp",
                    f"Ошибка проверки WhatsApp для {contact['phone']}: {exc}",
                    level="error",
                    contact_id=contact["id"],
                )
            runtime_state["check"]["processed"] += 1
            await asyncio.sleep(0.7)

        runtime_state["check"]["status"] = "done"
        log_event("whatsapp", f"Проверка WhatsApp завершена: {len(contacts)} контактов")


def pick_next_campaign_contact(target_kind: str) -> dict[str, Any] | None:
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM contacts
            WHERE project_id = ?
              AND kind = ?
              AND whatsapp_exists = 1
              AND status IN ('ready', 'new')
            ORDER BY id
            LIMIT 1
            """,
            (current_project_id(), target_kind),
        ).fetchone()
    return row_dict(row)


def campaign_task_is_running() -> bool:
    task = runtime_tasks.get("campaign")
    return bool(task and not task.done())


def validate_campaign_config(
    max_messages: int,
    delay_min_seconds: int,
    delay_max_seconds: int,
    target_kind: str,
) -> None:
    if max_messages < 1 or max_messages > 10000:
        raise ValueError("Количество сообщений должно быть от 1 до 10000")
    if delay_min_seconds < 1 or delay_max_seconds < 1:
        raise ValueError("Задержка должна быть больше 0")
    if delay_max_seconds < delay_min_seconds:
        raise ValueError("Максимальная задержка должна быть больше минимальной")
    if target_kind not in {"lead", "lpr"}:
        raise ValueError("Некорректный тип контактов для рассылки")


def create_campaign_record(
    *,
    project_id: int,
    target_kind: str,
    max_messages: int,
    delay_min_seconds: int,
    delay_max_seconds: int,
    source: str,
) -> int:
    validate_campaign_config(max_messages, delay_min_seconds, delay_max_seconds, target_kind)
    current_time = now_iso()
    with db_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO campaigns(project_id, status, target_kind, max_messages, delay_min_seconds, delay_max_seconds, started_at, created_at, updated_at)
            VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                target_kind,
                max_messages,
                delay_min_seconds,
                delay_max_seconds,
                current_time,
                current_time,
                current_time,
            ),
        )
        campaign_id = int(cursor.lastrowid)
    log_event(
        "campaign",
        f"Создана кампания #{campaign_id}",
        campaign_id=campaign_id,
        project_id=project_id,
        payload={
            "source": source,
            "target_kind": target_kind,
            "max_messages": max_messages,
            "delay_min_seconds": delay_min_seconds,
            "delay_max_seconds": delay_max_seconds,
        },
    )
    return campaign_id


def start_campaign_task(campaign_id: int, project_id: int) -> None:
    runtime_tasks["campaign"] = asyncio.create_task(campaign_worker(campaign_id))
    runtime_state["campaign"] = {
        "status": "running",
        "campaign_id": campaign_id,
        "last_error": None,
        "project_id": project_id,
    }


def ready_campaign_contacts_count(project_id: int, target_kind: str) -> int:
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM contacts
            WHERE project_id = ?
              AND kind = ?
              AND whatsapp_exists = 1
              AND status IN ('ready', 'new')
            """,
            (project_id, target_kind),
        ).fetchone()
    return int(row["c"] if row else 0)


def pending_whatsapp_contacts_count(project_id: int, target_kind: str) -> int:
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM contacts
            WHERE project_id = ?
              AND kind = ?
              AND whatsapp_exists IS NULL
              AND status NOT IN ('opt_out', 'no_whatsapp')
            """,
            (project_id, target_kind),
        ).fetchone()
    return int(row["c"] if row else 0)


def parse_int_setting(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def parse_auto_campaign_config(settings: dict[str, str]) -> dict[str, Any]:
    max_messages = parse_int_setting(settings.get("auto_campaign_max_messages"), 25, minimum=1, maximum=10000)
    delay_min_seconds = parse_int_setting(settings.get("auto_campaign_delay_min_seconds"), 40, minimum=1, maximum=86400)
    delay_max_seconds = parse_int_setting(settings.get("auto_campaign_delay_max_seconds"), 120, minimum=1, maximum=86400)
    target_kind = (settings.get("auto_campaign_target_kind") or "lead").strip() or "lead"
    validate_campaign_config(max_messages, delay_min_seconds, delay_max_seconds, target_kind)
    return {
        "max_messages": max_messages,
        "delay_min_seconds": delay_min_seconds,
        "delay_max_seconds": delay_max_seconds,
        "target_kind": target_kind,
    }


def parse_auto_campaign_time(value: str | None) -> dt_time:
    raw = (value or "10:00").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        raise ValueError("Время автоработы должно быть в формате HH:MM")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("Некорректное время автоработы")
    return dt_time(hour=hour, minute=minute)


def auto_campaign_due(
    settings: dict[str, str],
    now_utc: datetime | None = None,
) -> tuple[bool, str, datetime, str]:
    tz_name = (settings.get("auto_campaign_timezone") or "Asia/Qyzylorda").strip() or "Asia/Qyzylorda"
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        raise ValueError(f"Некорректный часовой пояс автоработы: {tz_name}") from exc
    now_value = now_utc or datetime.now(timezone.utc)
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=timezone.utc)
    local_now = now_value.astimezone(tz)
    date_key = local_now.date().isoformat()
    run_time = parse_auto_campaign_time(settings.get("auto_campaign_time"))
    scheduled_at = datetime.combine(local_now.date(), run_time, tzinfo=tz)
    if settings.get("auto_campaign_last_run_date") == date_key:
        return False, date_key, scheduled_at, "already_ran"
    if local_now < scheduled_at:
        return False, date_key, scheduled_at, "too_early"
    return True, date_key, scheduled_at, "due"


def log_auto_wait_once(project_id: int, date_key: str, message: str, *, level: str = "warning") -> None:
    settings = get_settings(project_id)
    if settings.get("auto_campaign_last_wait_date") == date_key:
        return
    log_event("auto_work", message, level=level, project_id=project_id)
    update_settings({"auto_campaign_last_wait_date": date_key}, project_id=project_id)


async def run_auto_campaign_for_project(
    project_id: int,
    *,
    now_utc: datetime | None = None,
    start_worker: bool = True,
) -> dict[str, Any]:
    with use_project(project_id):
        settings = get_settings()
        if not is_truthy(settings.get("auto_campaign_enabled")):
            return {"started": False, "reason": "disabled", "project_id": project_id}

        due, date_key, scheduled_at, reason = auto_campaign_due(settings, now_utc)
        runtime_state["auto_work"].update(
            {
                "status": "running",
                "last_check_at": now_iso(),
                "last_project_id": project_id,
                "next_scheduled_at": scheduled_at.isoformat(),
                "last_error": None,
            }
        )
        if not due:
            return {"started": False, "reason": reason, "project_id": project_id, "date": date_key}

        try:
            config = parse_auto_campaign_config(settings)
        except ValueError as exc:
            runtime_state["auto_work"]["last_error"] = str(exc)
            log_auto_wait_once(project_id, date_key, f"Авторабота не запущена: {exc}", level="error")
            return {"started": False, "reason": "invalid_config", "project_id": project_id, "error": str(exc)}

        if not GreenApiClient(settings).configured:
            log_auto_wait_once(project_id, date_key, "Авторабота ждет GreenAPI: заполните ID инстанса и токен")
            return {"started": False, "reason": "greenapi_not_configured", "project_id": project_id}

        if campaign_task_is_running():
            log_auto_wait_once(project_id, date_key, "Авторабота ждет: сейчас уже идет рассылка")
            return {"started": False, "reason": "campaign_running", "project_id": project_id}

        ready_count = ready_campaign_contacts_count(project_id, str(config["target_kind"]))
        if ready_count <= 0:
            pending_count = pending_whatsapp_contacts_count(project_id, str(config["target_kind"]))
            if pending_count > 0:
                log_auto_wait_once(
                    project_id,
                    date_key,
                    f"Авторабота ждет проверку WhatsApp: еще не проверено {pending_count} контактов",
                )
                runtime_state["auto_work"]["last_action"] = "waiting_whatsapp_check"
                return {"started": False, "reason": "waiting_whatsapp_check", "project_id": project_id}
            update_settings(
                {
                    "auto_campaign_last_run_date": date_key,
                    "auto_campaign_last_wait_date": "",
                },
                project_id=project_id,
            )
            log_event(
                "auto_work",
                "Авторабота на сегодня завершена: нет готовых контактов для рассылки",
                project_id=project_id,
            )
            runtime_state["auto_work"]["last_action"] = "no_ready_contacts"
            return {"started": False, "reason": "no_ready_contacts", "project_id": project_id}

        campaign_id = create_campaign_record(project_id=project_id, source="auto_work", **config)
        update_settings(
            {
                "auto_campaign_last_run_date": date_key,
                "auto_campaign_last_wait_date": "",
            },
            project_id=project_id,
        )
        if start_worker:
            start_campaign_task(campaign_id, project_id)
        log_event(
            "auto_work",
            f"Авторабота запустила рассылку #{campaign_id}: лимит {config['max_messages']}, готово {ready_count}",
            project_id=project_id,
            campaign_id=campaign_id,
            payload={"date": date_key, **config},
        )
        runtime_state["auto_work"]["last_action"] = f"started_campaign_{campaign_id}"
        return {"started": True, "reason": "started", "project_id": project_id, "campaign_id": campaign_id}


async def send_campaign_message(contact: dict[str, Any]) -> None:
    project = get_project()
    text = build_campaign_greeting(contact)
    await send_and_log(contact, text)
    next_stage = "warmup_permission" if uses_autoscore_warmup_flow(project) else "waiting_reply"
    update_contact_fields(contact["id"], status="sent", stage=next_stage)


async def campaign_worker(campaign_id: int) -> None:
    with db_conn() as conn:
        row = conn.execute("SELECT project_id FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    project_id = int(row["project_id"] if row else current_project_id())
    with use_project(project_id):
      runtime_state["campaign"] = {"status": "running", "campaign_id": campaign_id, "last_error": None, "project_id": project_id}
      log_event("campaign", f"Кампания #{campaign_id} запущена", campaign_id=campaign_id)
      while True:
        with db_conn() as conn:
            campaign = conn.execute("SELECT * FROM campaigns WHERE id = ? AND project_id = ?", (campaign_id, project_id)).fetchone()
        if not campaign:
            runtime_state["campaign"]["status"] = "missing"
            return
        campaign_dict = dict(campaign)
        if campaign_dict["status"] != "running":
            runtime_state["campaign"]["status"] = campaign_dict["status"]
            return
        if campaign_dict["sent_count"] >= campaign_dict["max_messages"]:
            with db_conn() as conn:
                conn.execute(
                    "UPDATE campaigns SET status = 'done', stopped_at = ?, updated_at = ? WHERE id = ? AND project_id = ?",
                    (now_iso(), now_iso(), campaign_id, project_id),
                )
            runtime_state["campaign"]["status"] = "done"
            log_event("campaign", f"Кампания #{campaign_id} завершена по лимиту", campaign_id=campaign_id)
            return

        contact = pick_next_campaign_contact(campaign_dict["target_kind"])
        if contact is None:
            with db_conn() as conn:
                conn.execute(
                    "UPDATE campaigns SET status = 'done', stopped_at = ?, updated_at = ? WHERE id = ? AND project_id = ?",
                    (now_iso(), now_iso(), campaign_id, project_id),
                )
            runtime_state["campaign"]["status"] = "done"
            log_event("campaign", f"Кампания #{campaign_id} завершена: нет готовых контактов", campaign_id=campaign_id)
            return

        try:
            await send_campaign_message(contact)
            with db_conn() as conn:
                conn.execute(
                    "UPDATE campaigns SET sent_count = sent_count + 1, updated_at = ? WHERE id = ? AND project_id = ?",
                    (now_iso(), campaign_id, project_id),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            update_contact_fields(contact["id"], status="error", last_error=str(exc))
            with db_conn() as conn:
                conn.execute(
                    "UPDATE campaigns SET error_count = error_count + 1, updated_at = ? WHERE id = ? AND project_id = ?",
                    (now_iso(), campaign_id, project_id),
                )
            runtime_state["campaign"]["last_error"] = str(exc)
            log_event(
                "campaign",
                f"Ошибка отправки в кампании #{campaign_id} для {contact['phone']}: {exc}",
                level="error",
                contact_id=contact["id"],
                campaign_id=campaign_id,
            )

        delay = random.randint(campaign_dict["delay_min_seconds"], campaign_dict["delay_max_seconds"])
        for _ in range(delay):
            await asyncio.sleep(1)
            with db_conn() as conn:
                status = conn.execute(
                    "SELECT status FROM campaigns WHERE id = ? AND project_id = ?",
                    (campaign_id, project_id),
                ).fetchone()
            if not status or status["status"] != "running":
                runtime_state["campaign"]["status"] = status["status"] if status else "missing"
                return


async def notification_poller(project_id: int | None = None) -> None:
    resolved_project_id = int(project_id or current_project_id())
    with use_project(resolved_project_id):
        await _notification_poller()


async def _notification_poller() -> None:
    runtime_state["ai"] = {"status": "running", "processed": 0, "last_error": None, "project_id": current_project_id()}
    while is_truthy(get_settings().get("ai_enabled")):
        settings = get_settings()
        runtime_state["ai"]["project_id"] = current_project_id()
        green = GreenApiClient(settings)
        if not green.configured:
            runtime_state["ai"]["status"] = "waiting_greenapi"
            runtime_state["ai"]["last_error"] = "GreenAPI не настроен"
            await asyncio.sleep(5)
            continue
        runtime_state["ai"]["status"] = "running"
        try:
            notification = await green.receive_notification(receive_timeout=5)
            if notification:
                receipt_id = notification.get("receiptId")
                body = notification.get("body") or {}
                try:
                    await process_notification_body(body, source="poll")
                    runtime_state["ai"]["processed"] += 1
                finally:
                    if receipt_id is not None:
                        await green.delete_notification(int(receipt_id))
            else:
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            runtime_state["ai"]["status"] = "stopped"
            raise
        except Exception as exc:
            runtime_state["ai"]["last_error"] = str(exc)
            await asyncio.sleep(3)
    runtime_state["ai"]["status"] = "stopped"


async def sync_recent_incoming_history(minutes: int | None = None, only_known: bool = True) -> None:
    settings = get_settings()
    green = GreenApiClient(settings)
    if not green.configured:
        log_event("ai", "Синхронизация истории GreenAPI пропущена: GreenAPI не настроен", level="warning")
        return

    sync_minutes = minutes or int(settings.get("green_history_sync_minutes") or 1440)
    runtime_state["ai"]["status"] = "history_sync"
    log_event("ai", f"Синхронизация входящей истории GreenAPI за {sync_minutes} мин. запущена")

    try:
        items = await green.last_incoming_messages(sync_minutes)
    except Exception as exc:
        runtime_state["ai"]["last_error"] = str(exc)
        log_event("ai", f"Ошибка синхронизации истории GreenAPI: {exc}", level="error")
        return

    grouped_items: dict[str, list[dict[str, Any]]] = {}
    skipped = 0
    for item in sorted(items, key=lambda row: (int(row.get("timestamp") or 0), str(row.get("idMessage") or ""))):
        chat_id = item.get("chatId") or item.get("senderId")
        id_message = item.get("idMessage")
        if not chat_id:
            skipped += 1
            continue
        if only_known and get_contact_by_chat(chat_id) is None:
            skipped += 1
            continue
        if message_exists("in", id_message):
            skipped += 1
            continue
        grouped_items.setdefault(chat_id, []).append(item)

    processed = 0
    deferred = 0
    for chat_items in grouped_items.values():
        last_index = len(chat_items) - 1
        for index, item in enumerate(chat_items):
            allow_outbound = index == last_index
            if not allow_outbound:
                deferred += 1
            await process_notification_body(
                journal_item_to_notification_body(item),
                source="history_sync",
                allow_outbound=allow_outbound,
            )
            processed += 1

    runtime_state["ai"]["processed"] = int(runtime_state["ai"].get("processed") or 0) + processed
    log_event(
        "ai",
        f"Синхронизация истории GreenAPI завершена: новых {processed}, чатов {len(grouped_items)}, пропущено {skipped}",
        payload={
            "minutes": sync_minutes,
            "received": len(items),
            "processed": processed,
            "skipped": skipped,
            "chats": len(grouped_items),
            "deferred_messages": deferred,
        },
    )


async def ai_resume_worker(project_id: int | None = None) -> None:
    resolved_project_id = int(project_id or current_project_id())
    with use_project(resolved_project_id):
        try:
            await sync_recent_incoming_history(only_known=True)
        finally:
            if is_truthy(get_settings().get("ai_enabled")):
                start_poller_task(resolved_project_id)


def start_check_task() -> None:
    task = runtime_tasks.get("check")
    if task and not task.done():
        return
    runtime_tasks["check"] = asyncio.create_task(check_whatsapp_worker())


def start_poller_task(project_id: int | None = None) -> None:
    resolved_project_id = int(project_id or current_project_id())
    task = runtime_tasks.get("poller")
    if task and not task.done():
        if int(runtime_state.get("ai", {}).get("project_id") or 0) == resolved_project_id:
            return
        task.cancel()
    runtime_tasks["poller"] = asyncio.create_task(notification_poller(resolved_project_id))


def start_ai_resume_task(project_id: int | None = None) -> None:
    resolved_project_id = int(project_id or current_project_id())
    task = runtime_tasks.get("ai_sync")
    if task and not task.done():
        if int(runtime_state.get("ai", {}).get("project_id") or 0) == resolved_project_id:
            return
        task.cancel()
    runtime_state["ai"] = {"status": "history_sync", "processed": 0, "last_error": None, "project_id": resolved_project_id}
    runtime_tasks["ai_sync"] = asyncio.create_task(ai_resume_worker(resolved_project_id))


async def auto_work_scheduler() -> None:
    runtime_state["auto_work"].update(
        {
            "status": "running",
            "checked_projects": 0,
            "last_check_at": now_iso(),
            "last_error": None,
        }
    )
    while True:
        try:
            projects = list_projects()
            runtime_state["auto_work"]["checked_projects"] = len(projects)
            runtime_state["auto_work"]["last_check_at"] = now_iso()
            for project in projects:
                project_id = int(project["id"])
                try:
                    await run_auto_campaign_for_project(project_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    runtime_state["auto_work"]["last_error"] = str(exc)
                    log_event(
                        "auto_work",
                        f"Ошибка автоработы для проекта {project.get('name') or project_id}: {exc}",
                        level="error",
                        project_id=project_id,
                    )
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            runtime_state["auto_work"]["status"] = "stopped"
            raise
        except Exception as exc:
            runtime_state["auto_work"]["last_error"] = str(exc)
            log_event("auto_work", f"Ошибка фонового планировщика автоработы: {exc}", level="error")
            await asyncio.sleep(30)


def start_auto_work_task() -> None:
    task = runtime_tasks.get("auto_work")
    if task and not task.done():
        return
    runtime_tasks["auto_work"] = asyncio.create_task(auto_work_scheduler())


def recover_stale_proposal_sends() -> None:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, phone, project_id FROM contacts WHERE proposal_sent = 2 OR status = 'proposal_sending'"
        ).fetchall()
        conn.execute(
            """
            UPDATE contacts
            SET proposal_sent = 0,
                status = 'error',
                stage = 'proposal_recovery_needed',
                last_error = 'Сервис остановился во время отправки КП. Проверьте чат и запустите отправку повторно при необходимости.',
                updated_at = ?
            WHERE proposal_sent = 2 OR status = 'proposal_sending'
            """,
            (now_iso(),),
        )
    for row in rows:
        log_event(
            "proposal",
            f"Восстановлено зависшее состояние отправки КП для {row['phone']}",
            level="warning",
            contact_id=row["id"],
            project_id=row["project_id"],
        )


def resume_running_campaign() -> None:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM campaigns WHERE status = 'running' ORDER BY id DESC"
        ).fetchall()
    if not rows:
        return
    campaign = dict(rows[0])
    if len(rows) > 1:
        old_ids = [row["id"] for row in rows[1:]]
        with db_conn() as conn:
            conn.executemany(
                "UPDATE campaigns SET status = 'paused', updated_at = ? WHERE id = ?",
                [(now_iso(), old_id) for old_id in old_ids],
            )
        log_event("campaign", f"Найдено несколько running-кампаний, старые поставлены на паузу: {old_ids}", level="warning")

    task = runtime_tasks.get("campaign")
    if not task or task.done():
        start_campaign_task(int(campaign["id"]), int(campaign["project_id"]))
        log_event(
            "campaign",
            f"Кампания #{campaign['id']} восстановлена после старта сервиса",
            campaign_id=int(campaign["id"]),
            project_id=int(campaign["project_id"]),
        )


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    recover_stale_proposal_sends()
    resume_running_campaign()
    start_auto_work_task()
    if is_truthy(get_settings().get("ai_enabled")):
        start_ai_resume_task()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for task in runtime_tasks.values():
        if task and not task.done():
            task.cancel()
    await asyncio.sleep(0)


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "auth.html")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "auth.html")


@app.get("/api/auth/status")
async def api_auth_status(request: Request) -> dict[str, Any]:
    admin = current_admin(request) if auth_configured() else None
    return {
        "configured": auth_configured(),
        "authenticated": bool(admin),
        "username": admin or get_setting("admin_username") or "",
    }


@app.post("/api/auth/setup")
async def api_auth_setup(payload: AuthSetupPayload) -> JSONResponse:
    if auth_configured():
        raise HTTPException(status_code=409, detail="Администратор уже настроен")
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Укажите логин")
    set_setting("admin_username", username)
    set_setting("admin_password_hash", hash_password(payload.password))
    response = JSONResponse({"ok": True, "username": username})
    set_auth_cookie(response, username)
    log_event("settings", "Администратор создан")
    return response


@app.post("/api/auth/login")
async def api_auth_login(payload: LoginPayload) -> JSONResponse:
    username = get_setting("admin_username")
    stored_hash = get_setting("admin_password_hash")
    if not username or not stored_hash:
        raise HTTPException(status_code=409, detail="Сначала создайте администратора")
    if payload.username.strip() != username or not verify_password(payload.password, stored_hash):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    response = JSONResponse({"ok": True, "username": username})
    set_auth_cookie(response, username)
    return response


@app.post("/api/auth/logout")
async def api_auth_logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    clear_auth_cookie(response)
    return response


@app.post("/api/auth/password")
async def api_auth_change_password(payload: PasswordChangePayload) -> dict[str, Any]:
    stored_hash = get_setting("admin_password_hash")
    if not stored_hash or not verify_password(payload.current_password, stored_hash):
        raise HTTPException(status_code=401, detail="Текущий пароль указан неверно")
    set_setting("admin_password_hash", hash_password(payload.new_password))
    log_event("settings", "Пароль администратора изменен")
    return {"ok": True}


@app.get("/api/projects")
async def api_projects() -> dict[str, Any]:
    return {"projects": list_projects(), "current_project_id": current_project_id(), "current": get_project()}


@app.post("/api/projects/current")
async def api_switch_project(payload: ProjectSwitchPayload) -> dict[str, Any]:
    project = get_project(payload.project_id)
    if int(project["id"]) != payload.project_id:
        raise HTTPException(status_code=404, detail="Проект не найден")
    update_settings({"current_project_id": str(payload.project_id)})
    log_event("settings", f"Активный проект переключен: {project['name']}", project_id=payload.project_id)
    return {"ok": True, "project": project}


@app.post("/api/projects")
async def api_create_project(payload: ProjectPayload) -> dict[str, Any]:
    slug_base = re.sub(r"[^a-z0-9]+", "-", payload.name.lower(), flags=re.IGNORECASE).strip("-") or f"project-{uuid.uuid4().hex[:8]}"
    slug = slug_base
    current_time = now_iso()
    with db_conn() as conn:
        suffix = 2
        while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
            slug = f"{slug_base}-{suffix}"
            suffix += 1
        cursor = conn.execute(
            """
            INSERT INTO projects(slug, name, product_name, workflow_type, proposal_filename, ai_system_prompt, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                slug,
                payload.name.strip(),
                safe_cell(payload.product_name) or payload.name.strip(),
                payload.workflow_type.strip() or "generic_b2b",
                safe_cell(payload.proposal_filename) or DEFAULT_SETTINGS["proposal_filename"],
                safe_cell(payload.ai_system_prompt) or SECOND_PROJECT_AI_PROMPT,
                current_time,
                current_time,
            ),
        )
        project_id = int(cursor.lastrowid)
    return {"ok": True, "project": get_project(project_id)}


@app.patch("/api/projects/{project_id}")
async def api_update_project(project_id: int, payload: ProjectPayload) -> dict[str, Any]:
    project = get_project(project_id)
    if int(project["id"]) != project_id:
        raise HTTPException(status_code=404, detail="Проект не найден")
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE projects
            SET name = ?,
                product_name = ?,
                workflow_type = ?,
                proposal_filename = ?,
                ai_system_prompt = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                payload.name.strip(),
                safe_cell(payload.product_name) or payload.name.strip(),
                payload.workflow_type.strip() or "generic_b2b",
                safe_cell(payload.proposal_filename) or project.get("proposal_filename") or DEFAULT_SETTINGS["proposal_filename"],
                safe_cell(payload.ai_system_prompt) or project.get("ai_system_prompt") or SECOND_PROJECT_AI_PROMPT,
                now_iso(),
                project_id,
            ),
        )
    log_event("settings", f"Проект обновлен: {payload.name}", project_id=project_id)
    return {"ok": True, "project": get_project(project_id)}


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/proposal")
async def proposal() -> FileResponse:
    settings = get_settings()
    proposal_path = BASE_DIR / (settings.get("proposal_filename") or PROPOSAL_PATH.name)
    if not proposal_path.exists():
        raise HTTPException(status_code=404, detail="КП не найдено")
    return FileResponse(proposal_path, media_type="text/html")


@app.get("/api/settings")
async def api_get_settings() -> dict[str, Any]:
    settings = get_settings()
    return {key: value for key, value in settings.items() if key not in AUTH_SETTING_KEYS}


@app.post("/api/settings")
async def api_save_settings(payload: SettingsPayload) -> dict[str, Any]:
    raw_payload = payload.model_dump(exclude_none=True)
    settings = update_settings(raw_payload)
    changed_keys = [key for key in raw_payload if "key" not in key and "token" not in key]
    log_event("settings", "Настройки сохранены", payload={"changed_keys": changed_keys})
    return {"ok": True, "settings": {key: value for key, value in settings.items() if key not in AUTH_SETTING_KEYS}}


@app.post("/api/upload/excel")
async def api_upload_excel(file: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(file.filename or "upload.xlsx").suffix.lower()
    if suffix not in {".xlsx", ".xls", ".xlsm"}:
        raise HTTPException(status_code=400, detail="Нужен Excel файл .xlsx/.xls/.xlsm")
    upload_id = uuid.uuid4().hex
    upload_path = UPLOAD_DIR / f"{upload_id}{suffix}"
    upload_path.write_bytes(await file.read())

    try:
        df = pd.read_excel(upload_path, nrows=25)
    except Exception as exc:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Не удалось прочитать Excel: {exc}") from exc

    columns = [str(col) for col in df.columns]
    preview = df.head(10).fillna("").astype(str).to_dict(orient="records")
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO uploads(id, project_id, path, filename, columns_json, preview_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                upload_id,
                current_project_id(),
                str(upload_path),
                file.filename or upload_path.name,
                json.dumps(columns, ensure_ascii=False),
                json.dumps(preview, ensure_ascii=False),
                now_iso(),
            ),
        )
    log_event(
        "import",
        f"Excel загружен: {file.filename or upload_path.name}",
        payload={"upload_id": upload_id, "columns": columns, "preview_rows": len(preview)},
    )
    return {"upload_id": upload_id, "columns": columns, "preview": preview}


@app.post("/api/import")
async def api_import_excel(payload: ImportRequest) -> dict[str, Any]:
    settings = get_settings()
    with db_conn() as conn:
        upload = conn.execute(
            "SELECT * FROM uploads WHERE id = ? AND project_id = ?",
            (payload.upload_id, current_project_id()),
        ).fetchone()
    if not upload:
        raise HTTPException(status_code=404, detail="Загрузка не найдена")

    path = Path(upload["path"])
    try:
        df = pd.read_excel(path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не удалось прочитать Excel: {exc}") from exc

    phone_columns = payload.phone_columns or ([payload.phone_column] if payload.phone_column else [])
    phone_columns = [column for column in phone_columns if column]
    signal_columns = [column for column in (payload.signal_columns or []) if column]
    if not phone_columns:
        raise HTTPException(status_code=400, detail="Выберите хотя бы один столбец с телефонами")
    missing_columns = [column for column in [*phone_columns, *signal_columns] if column not in df.columns]
    if missing_columns:
        raise HTTPException(status_code=400, detail=f"Выбранные столбцы не найдены: {', '.join(missing_columns)}")

    imported = 0
    skipped = 0
    for _, row in df.iterrows():
        row_candidates: list[tuple[str, str | None]] = []
        seen_row_phones: set[str] = set()
        for phone_column in phone_columns:
            raw_value = safe_cell(row.get(phone_column))
            for phone in extract_phones(row.get(phone_column), settings.get("default_country_code", "7")):
                if phone in seen_row_phones:
                    continue
                seen_row_phones.add(phone)
                row_candidates.append((phone, raw_value))
        if not row_candidates:
            skipped += 1
            continue
        company = safe_cell(row.get(payload.company_column)) if payload.company_column else None
        name = safe_cell(row.get(payload.name_column)) if payload.name_column else None
        signal_fields = row_signal_fields(row, signal_columns)
        meta = build_sales_context_meta(company, signal_fields)
        for phone, phone_raw in row_candidates:
            create_or_update_contact(
                phone=phone,
                kind="lead",
                source="excel",
                phone_raw=phone_raw,
                company=company,
                name=name,
                status="new",
                stage="imported",
                meta=meta,
            )
            imported += 1

    check_started = False
    if GreenApiClient(get_settings()).configured:
        start_check_task()
        check_started = True
    else:
        runtime_state["check"] = {
            "status": "waiting_greenapi",
            "processed": 0,
            "total": imported,
            "last_error": None,
        }
        log_event(
            "whatsapp",
            "Проверка WhatsApp ожидает настройки GreenAPI",
            level="warning",
        )
    log_event(
        "import",
        f"Импорт Excel завершен: {imported} номеров, пропущено строк: {skipped}",
        payload={
            "upload_id": payload.upload_id,
            "phone_columns": phone_columns,
            "signal_columns": signal_columns,
            "company_column": payload.company_column,
            "name_column": payload.name_column,
            "imported": imported,
            "skipped_rows": skipped,
            "check_started": check_started,
        },
    )
    return {"ok": True, "imported": imported, "skipped_rows": skipped, "check_started": check_started}


@app.post("/api/krisha/import")
async def api_import_krisha(payload: KrishaImportRequest) -> dict[str, Any]:
    if not uses_keramo_investor_flow():
        raise HTTPException(status_code=400, detail="Переключите активный проект на KERAMO BUILD")
    return await run_krisha_import_cycle(payload)


@app.get("/api/krisha/parser/status")
async def api_krisha_parser_status() -> dict[str, Any]:
    task = runtime_tasks.get("krisha_parser")
    return {"ok": True, "running": bool(task and not task.done()), "state": runtime_state["krisha_parser"]}


@app.post("/api/krisha/parser/start")
async def api_start_krisha_parser() -> dict[str, Any]:
    if not uses_keramo_investor_flow():
        raise HTTPException(status_code=400, detail="Переключите активный проект на KERAMO BUILD")
    payload = krisha_payload_from_settings(get_settings())
    task = runtime_tasks.get("krisha_parser")
    if task and not task.done():
        return {"ok": True, "running": True, "state": runtime_state["krisha_parser"]}
    project_id = current_project_id()
    runtime_state["krisha_parser"] = {
        "status": "starting",
        "processed": 0,
        "found": 0,
        "imported": 0,
        "updated": 0,
        "max_contacts": payload.max_contacts,
        "last_error": None,
        "last_run_at": None,
        "project_id": project_id,
    }
    runtime_tasks["krisha_parser"] = asyncio.create_task(krisha_parser_worker(project_id))
    return {"ok": True, "running": True, "state": runtime_state["krisha_parser"]}


@app.post("/api/krisha/parser/stop")
async def api_stop_krisha_parser() -> dict[str, Any]:
    task = runtime_tasks.get("krisha_parser")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    runtime_tasks["krisha_parser"] = None
    if runtime_state["krisha_parser"].get("status") not in {"captcha_required", "error"}:
        runtime_state["krisha_parser"]["status"] = "stopped"
    return {"ok": True, "running": False, "state": runtime_state["krisha_parser"]}


@app.post("/api/check-whatsapp")
async def api_check_whatsapp() -> dict[str, Any]:
    start_check_task()
    return {"ok": True, "state": runtime_state["check"]}


@app.post("/api/campaign/start")
async def api_campaign_start(payload: CampaignStart) -> dict[str, Any]:
    try:
        validate_campaign_config(
            payload.max_messages,
            payload.delay_min_seconds,
            payload.delay_max_seconds,
            payload.target_kind,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    current_task = runtime_tasks.get("campaign")
    if current_task and not current_task.done():
        raise HTTPException(status_code=409, detail="Кампания уже запущена")
    settings = get_settings()
    if not GreenApiClient(settings).configured:
        raise HTTPException(status_code=400, detail="Сначала заполните GreenAPI ID и токен")
    project_id = current_project_id()
    campaign_id = create_campaign_record(
        project_id=project_id,
        target_kind=payload.target_kind,
        max_messages=payload.max_messages,
        delay_min_seconds=payload.delay_min_seconds,
        delay_max_seconds=payload.delay_max_seconds,
        source="manual",
    )
    start_campaign_task(campaign_id, project_id)
    return {"ok": True, "campaign_id": campaign_id}


@app.post("/api/campaign/pause")
async def api_campaign_pause() -> dict[str, Any]:
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE campaigns
            SET status = 'paused', stopped_at = ?, updated_at = ?
            WHERE id = (SELECT id FROM campaigns WHERE project_id = ? ORDER BY id DESC LIMIT 1)
              AND project_id = ?
              AND status = 'running'
            """,
            (now_iso(), now_iso(), current_project_id(), current_project_id()),
        )
    log_event("campaign", "Кампания поставлена на паузу")
    return {"ok": True}


@app.post("/api/campaign/resume")
async def api_campaign_resume() -> dict[str, Any]:
    with db_conn() as conn:
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE project_id = ? AND status = 'paused' ORDER BY id DESC LIMIT 1",
            (current_project_id(),),
        ).fetchone()
        if not campaign:
            raise HTTPException(status_code=404, detail="Нет кампании на паузе")
        campaign_id = int(campaign["id"])
        conn.execute(
            "UPDATE campaigns SET status = 'running', stopped_at = NULL, updated_at = ? WHERE id = ? AND project_id = ?",
            (now_iso(), campaign_id, current_project_id()),
        )
    task = runtime_tasks.get("campaign")
    if not task or task.done():
        runtime_tasks["campaign"] = asyncio.create_task(campaign_worker(campaign_id))
    runtime_state["campaign"]["status"] = "running"
    runtime_state["campaign"]["campaign_id"] = campaign_id
    log_event("campaign", f"Кампания #{campaign_id} продолжена", campaign_id=campaign_id)
    return {"ok": True, "campaign_id": campaign_id}


@app.post("/api/campaign/stop")
async def api_campaign_stop() -> dict[str, Any]:
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE campaigns
            SET status = 'stopped', stopped_at = ?, updated_at = ?
            WHERE id = (SELECT id FROM campaigns WHERE project_id = ? ORDER BY id DESC LIMIT 1)
              AND project_id = ?
              AND status IN ('running', 'paused')
            """,
            (now_iso(), now_iso(), current_project_id(), current_project_id()),
        )
    task = runtime_tasks.get("campaign")
    if task and not task.done():
        task.cancel()
    runtime_state["campaign"]["status"] = "stopped"
    log_event("campaign", "Кампания остановлена")
    return {"ok": True}


@app.post("/api/ai/toggle")
async def api_ai_toggle(payload: AiToggle) -> dict[str, Any]:
    project_id = current_project_id()
    update_settings({"ai_enabled": "true" if payload.enabled else "false"}, project_id=project_id)
    if payload.enabled:
        start_ai_resume_task(project_id)
    else:
        task = runtime_tasks.get("poller")
        if task and not task.done() and int(runtime_state.get("ai", {}).get("project_id") or 0) == project_id:
            task.cancel()
        sync_task = runtime_tasks.get("ai_sync")
        if sync_task and not sync_task.done() and int(runtime_state.get("ai", {}).get("project_id") or 0) == project_id:
            sync_task.cancel()
        runtime_state["ai"]["status"] = "stopped"
    log_event("ai", "AI-режим включен" if payload.enabled else "AI-режим выключен")
    return {"ok": True, "enabled": payload.enabled}


@app.post("/api/webhook/greenapi")
async def api_greenapi_webhook(request: Request) -> dict[str, Any]:
    body = await request.json()
    await process_notification_body(body, source="webhook")
    return {"ok": True}


@app.get("/api/stats")
async def api_stats() -> dict[str, Any]:
    project_id = current_project_id()
    settings = get_settings(project_id)
    with db_conn() as conn:
        stats = {
            "total": conn.execute("SELECT COUNT(*) AS c FROM contacts WHERE project_id = ?", (project_id,)).fetchone()["c"],
            "leads": conn.execute("SELECT COUNT(*) AS c FROM contacts WHERE project_id = ? AND kind = 'lead'", (project_id,)).fetchone()["c"],
            "lpr": conn.execute("SELECT COUNT(*) AS c FROM contacts WHERE project_id = ? AND kind = 'lpr'", (project_id,)).fetchone()["c"],
            "whatsapp_ok": conn.execute("SELECT COUNT(*) AS c FROM contacts WHERE project_id = ? AND whatsapp_exists = 1", (project_id,)).fetchone()["c"],
            "sent": conn.execute("SELECT COUNT(*) AS c FROM contacts WHERE project_id = ? AND status IN ('sent', 'replied', 'proposal_sending', 'proposal_sent', 'replied_after_proposal', 'interested')", (project_id,)).fetchone()["c"],
            "replied": conn.execute("SELECT COUNT(*) AS c FROM contacts WHERE project_id = ? AND last_inbound_at IS NOT NULL", (project_id,)).fetchone()["c"],
            "proposal_sent": conn.execute("SELECT COUNT(*) AS c FROM contacts WHERE project_id = ? AND proposal_sent = 1", (project_id,)).fetchone()["c"],
            "interested": conn.execute("SELECT COUNT(*) AS c FROM contacts WHERE project_id = ? AND (status = 'interested' OR stage = 'handoff')", (project_id,)).fetchone()["c"],
            "errors": conn.execute("SELECT COUNT(*) AS c FROM contacts WHERE project_id = ? AND status = 'error'", (project_id,)).fetchone()["c"],
            "opt_out": conn.execute("SELECT COUNT(*) AS c FROM contacts WHERE project_id = ? AND status = 'opt_out'", (project_id,)).fetchone()["c"],
            "not_interested": conn.execute("SELECT COUNT(*) AS c FROM contacts WHERE project_id = ? AND status = 'not_interested'", (project_id,)).fetchone()["c"],
        }
        campaign = conn.execute("SELECT * FROM campaigns WHERE project_id = ? ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
        recent_messages = conn.execute(
            """
            SELECT m.*, c.phone, c.name, c.company, c.kind
            FROM messages m
            LEFT JOIN contacts c ON c.id = m.contact_id
            WHERE m.project_id = ?
            ORDER BY m.id DESC
            LIMIT 12
            """,
            (project_id,),
        ).fetchall()
    return {
        "stats": stats,
        "campaign": row_dict(campaign),
        "runtime": runtime_state,
        "recent_messages": [dict(row) for row in recent_messages],
        "project": get_project(project_id),
        "auto_work_settings": {
            key: settings.get(key, "")
            for key in DEFAULT_SETTINGS
            if key.startswith("auto_campaign_")
        },
    }


@app.get("/api/logs")
async def api_logs(
    level: str | None = None,
    category: str | None = None,
    q: str | None = None,
    limit: int = 150,
) -> dict[str, Any]:
    limit = min(max(limit, 1), 500)
    where = ["e.project_id = ?"]
    values: list[Any] = [current_project_id()]
    if level:
        where.append("e.level = ?")
        values.append(level)
    if category:
        where.append("e.category = ?")
        values.append(category)
    if q:
        where.append("(e.message LIKE ? OR c.phone LIKE ? OR c.company LIKE ? OR c.name LIKE ?)")
        values.extend([f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with db_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT e.*, c.phone, c.name, c.company, c.kind
            FROM event_logs e
            LEFT JOIN contacts c ON c.id = e.contact_id
            {where_sql}
            ORDER BY e.id DESC
            LIMIT ?
            """,
            values + [limit],
        ).fetchall()
    return {"logs": [dict(row) for row in rows]}


@app.get("/api/contacts")
async def api_contacts(
    kind: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    limit = min(max(limit, 1), 500)
    where, values = contact_filters(kind=kind, status=status, q=q)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with db_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT c.*, owner.phone AS owner_phone, owner.company AS owner_company
            FROM contacts c
            LEFT JOIN contacts owner ON owner.id = c.owner_contact_id
            {where_sql}
            ORDER BY c.id DESC
            LIMIT ?
            """,
            values + [limit],
        ).fetchall()
    return {"contacts": [dict(row) for row in rows]}


@app.delete("/api/contacts/{contact_id}")
async def api_delete_contact(contact_id: int) -> dict[str, Any]:
    ensure_delete_allowed()
    contact = get_contact(contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Контакт не найден")

    result = delete_contacts_and_related([contact_id])
    log_event(
        "contact",
        f"Удален контакт {contact['phone']}",
        payload={"contact_id": contact_id, **result},
    )
    return {"ok": True, **result}


@app.post("/api/contacts/delete")
async def api_delete_contacts(payload: ContactDeletePayload) -> dict[str, Any]:
    ensure_delete_allowed()
    contact_ids = sorted({int(contact_id) for contact_id in payload.contact_ids if int(contact_id) > 0})
    if payload.delete_all_matching:
        contact_ids = matching_contact_ids(kind=payload.kind, status=payload.status, q=payload.q)

    if not contact_ids:
        raise HTTPException(status_code=400, detail="Не выбраны контакты для удаления")

    result = delete_contacts_and_related(contact_ids)
    log_event(
        "contact",
        "Удалены контакты пакетом",
        payload={
            "kind": payload.kind,
            "status": payload.status,
            "q": payload.q,
            "delete_all_matching": payload.delete_all_matching,
            **result,
        },
    )
    return {"ok": True, **result}


@app.post("/api/contacts/reset")
async def api_reset_contacts() -> dict[str, Any]:
    ensure_delete_allowed()
    result = reset_sales_data()
    log_event(
        "contact",
        "Тестовая база очищена: контакты, диалоги и кампании удалены",
        payload=result,
    )
    return {"ok": True, **result}


@app.get("/api/crm/kanban")
async def api_crm_kanban(q: str | None = None, limit: int = 2000) -> dict[str, Any]:
    limit = min(max(limit, 1), 5000)
    where = ["c.project_id = ?"]
    values: list[Any] = [current_project_id()]
    if q:
        where.append("(c.phone LIKE ? OR c.company LIKE ? OR c.name LIKE ?)")
        values.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with db_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT c.*,
                   owner.phone AS owner_phone,
                   owner.company AS owner_company,
                   (
                       SELECT m.text
                       FROM messages m
                       WHERE m.project_id = c.project_id AND m.contact_id = c.id
                       ORDER BY m.id DESC
                       LIMIT 1
                   ) AS last_message_text,
                   (
                       SELECT m.direction
                       FROM messages m
                       WHERE m.project_id = c.project_id AND m.contact_id = c.id
                       ORDER BY m.id DESC
                       LIMIT 1
                   ) AS last_message_direction,
                   (
                       SELECT m.created_at
                       FROM messages m
                       WHERE m.project_id = c.project_id AND m.contact_id = c.id
                       ORDER BY m.id DESC
                       LIMIT 1
                   ) AS last_message_at,
                   (
                       SELECT COUNT(*)
                       FROM messages m
                       WHERE m.project_id = c.project_id AND m.contact_id = c.id
                   ) AS message_count
            FROM contacts c
            LEFT JOIN contacts owner ON owner.id = c.owner_contact_id
            {where_sql}
            ORDER BY
                CASE
                    WHEN c.status = 'interested' OR c.stage = 'handoff' THEN 0
                    WHEN c.last_inbound_at IS NOT NULL THEN 1
                    WHEN c.last_outbound_at IS NOT NULL THEN 2
                    ELSE 3
                END,
                c.updated_at DESC,
                c.id DESC
            LIMIT ?
            """,
            values + [limit],
        ).fetchall()

    columns = [
        {"key": column["key"], "title": column["title"], "deals": []}
        for column in CRM_COLUMNS
    ]
    by_key = {column["key"]: column for column in columns}
    for row in rows:
        deal = dict(row)
        stage_key = crm_stage_for_contact(deal)
        deal["crm_stage"] = stage_key
        deal["crm_stage_title"] = crm_stage_title(stage_key)
        by_key[stage_key]["deals"].append(deal)

    return {"columns": columns, "total": len(rows)}


@app.get("/api/contacts/{contact_id}/messages")
async def api_contact_messages(contact_id: int) -> dict[str, Any]:
    with db_conn() as conn:
        contact = conn.execute(
            """
            SELECT c.*, owner.phone AS owner_phone, owner.company AS owner_company
            FROM contacts c
            LEFT JOIN contacts owner ON owner.id = c.owner_contact_id
            WHERE c.id = ? AND c.project_id = ?
            """,
            (contact_id, current_project_id()),
        ).fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Контакт не найден")
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE project_id = ? AND contact_id = ?
            ORDER BY id ASC
            """,
            (current_project_id(), contact_id),
        ).fetchall()
    return {"contact": dict(contact), "messages": [dict(row) for row in rows]}
