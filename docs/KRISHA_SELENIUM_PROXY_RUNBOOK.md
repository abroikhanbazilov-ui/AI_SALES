# Krisha Selenium v2 + proxy runbook

Дата актуализации: 2026-05-26.

## Контекст

Krisha-парсер для проекта KERAMO BUILD работает через Selenium Undetected v2. На сервере включен headless-режим и browser proxy:

- `krisha_browser_engine = selenium_undetected`
- `krisha_use_browser = true`
- `krisha_headless = true`
- `krisha_proxy_server = http://81.200.159.177:8000`
- proxy авторизация идет через привязку внешнего IP сервера `38.107.234.161`, поэтому `krisha_proxy_username` и `krisha_proxy_password` оставлены пустыми.

## Что было исправлено

1. Browser fingerprint

   Selenium больше не использует жестко заданный Windows UA `Chrome/123`. User-Agent теперь строится под реальную major-версию найденного Chromium на сервере. На проде это `Chrome/147.0.0.0` под Linux.

2. Отдельный Chrome-профиль на proxy

   Для proxy используется отдельный каталог профиля вида:

   ```text
   data/krisha_selenium_profile_proxy_<hash>
   ```

   Это не смешивает cookies/сессию старого серверного IP и нового proxy IP.

3. CDP stealth layer

   Перед первой навигацией Selenium применяет:

   - `Network.setUserAgentOverride`;
   - `Emulation.setLocaleOverride`;
   - `Emulation.setTimezoneOverride`;
   - `Emulation.setGeolocationOverride`;
   - JS-overrides для `navigator.webdriver`, `navigator.languages`, `navigator.platform`, `plugins`, `hardwareConcurrency`, `deviceMemory`.

4. Proxy для AJAX-показа телефона

   Главный найденный баг: браузер ходил через proxy, но быстрый запрос `/a/ajaxPhones` выполнялся через `httpx.Client()` напрямую с IP сервера. Теперь Selenium AJAX использует тот же proxy, включая credentials при необходимости.

5. Более надежный клик по телефону

   Кнопка показа телефона теперь кликается через JS-first. Обычный Selenium `WebElement.click()` оставлен только fallback, потому что на тяжелых страницах Krisha он мог зависать.

6. Audio reCAPTCHA solver для Selenium

   Selenium v2 теперь пробует автоматический audio solver:

   - ищет `recaptcha/api2/anchor` и `recaptcha/api2/bframe`;
   - включает audio challenge;
   - скачивает аудио через тот же proxy;
   - распознает через Groq/OpenAI STT;
   - вводит ответ и проверяет результат.

   Если Google блокирует audio challenge для сети, headless-режим все равно не сможет пройти CAPTCHA вручную.

7. Поведение при AJAX CAPTCHA

   Если `/a/ajaxPhones` возвращает CAPTCHA, парсер не считает это техническим падением proxy. Он пробует раскрыть номер через браузерный клик и audio solver.

## Проверенный production smoke

Команда:

```bash
/mnt/c/Python314/python.exe scripts/run_production_import.py \
  --password <SSH_PASSWORD> \
  --max-contacts 3 \
  --max-pages 1 \
  --remote-timeout-seconds 700 \
  --import-contacts true \
  --start-whatsapp-check true
```

Результат после фиксов:

```json
{
  "ok": true,
  "found": 3,
  "imported": 3,
  "updated": 0,
  "check_started": true,
  "source_errors": []
}
```

## Локальные тесты

Проверенная команда:

```bash
/mnt/c/Python314/python.exe -m pytest tests/test_dialog_behavior.py -k "krisha_selenium or krisha_httpx_proxy or recaptcha or krisha_wait_for_manual_captcha"
```

Результат:

```text
12 passed
```

## Расписание авторассылки

На проде оба проекта включены:

- Автоскоринг: `auto_campaign_enabled=true`, `auto_campaign_time=12:00`, `auto_campaign_timezone=Asia/Almaty`;
- KERAMO BUILD: `auto_campaign_enabled=true`, `auto_campaign_time=12:00`, `auto_campaign_timezone=Asia/Almaty`.

На 2026-05-26 `auto_campaign_last_run_date=2026-05-26` для обоих проектов, поэтому следующий штатный запуск ожидается 2026-05-27 в 12:00 по Астане.

Важное ограничение: включенное расписание не означает, что кампания стартует без готовых контактов. Логика кампании берет только контакты:

```sql
kind = target_kind
AND whatsapp_exists = 1
AND status IN ('ready', 'new')
```

## Состояние контактов на 2026-05-26

Автоскоринг:

- готовых контактов для рассылки: `0`;
- непроверенных WhatsApp-контактов до ручной проверки было `6`;
- после проверки: `0` готовых, `0` pending.

Вывод: база автоскоринга для завтрашней рассылки исчерпана. Чтобы Автоскоринг реально отправлял завтра в 12:00, нужно импортировать новые номера или вернуть подходящие контакты в `ready/new` с `whatsapp_exists=1`.

KERAMO BUILD:

- готовых контактов для рассылки: `11`;
- pending WhatsApp после парсинга: `3`;
- Krisha proxy и Selenium v2 работают, последний smoke импортировал 3 новых номера и запустил WhatsApp-check.

Вывод: KERAMO BUILD должен стартовать завтра в 12:00, если GreenAPI доступен и нет параллельной кампании.

## Быстрая диагностика

Проверить proxy с сервера:

```bash
curl -4 -s -S --connect-timeout 10 --max-time 20 \
  -x http://81.200.159.177:8000 \
  https://api.ipify.org
```

Ожидаемый ответ:

```text
81.200.159.177
```

Проверить настройки проектов:

```bash
/mnt/c/Python314/python.exe scripts/check_production_runtime.py project-settings --password <SSH_PASSWORD>
```

Не использовать `--show-settings` без необходимости: он может вывести секреты.

Проверить свежие Krisha-логи:

```bash
/mnt/c/Python314/python.exe scripts/check_production_runtime.py logs \
  --password <SSH_PASSWORD> \
  --project-id 2 \
  --log-category krisha \
  --log-limit 30
```

## Ограничения

Текущий fix повышает вероятность успешного сбора номеров, но не гарантирует обход Krisha/Google anti-bot. Если proxy IP получит плохую репутацию или Google заблокирует audio challenge, headless-парсер снова остановится на CAPTCHA. В этом случае нужны:

- другой KZ mobile/residential proxy;
- ручное прохождение CAPTCHA в видимом браузере;
- внешний CAPTCHA-сервис.
