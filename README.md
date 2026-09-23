# YouGile MCP Cloud

Приватный серверный вариант [YouGile MCP](https://github.com/indalo-tech/yougile-mcp): подключение
по адресу, без установки. Человек входит своим логином YouGile, сервер выпускает ему отдельный
ключ и работает с его правами. Инструменты, права и лимиты — из публичного ядра `yougile-mcp`.

## Как устроено

| часть | что делает |
|---|---|
| `oauth.py` | OAuth 2.1 для MCP-клиентов: регистрация клиентов (DCR), PKCE, JWT доступа с `iss`/`aud`/типом, одноразовые коды в Valkey, токены обновления — случайные, в базе только хэш, меняются при каждом обмене |
| `web.py`, `pages.py` | страница входа через YouGile: CSRF, проверка Origin, CSP без скриптов, экранирование всех подстановок, выбор компании, лимит попыток |
| `signin.py`, `yougile_auth.py` | выпуск отдельного ключа YouGile на человека; при повторном входе старый ключ удаляется (у YouGile лимит 30 ключей на аккаунт); пароль не хранится |
| `tenancy.py` | окружение ядра на каждого пользователя, привязка к запросу через ContextVar, проверка доступа компании на каждом вызове, запрет загрузки файлов по локальному пути |
| `access.py` | доступ компании: `exempt` (свои, бесплатно), `active` (оплачено), `trial`, `expired`, `blocked` |
| `kv.py` | Valkey: состояние OAuth, счётчики попыток входа, общий лимит YouGile на компанию (скользящее окно на Lua, часы сервера Valkey) |
| `db.py`, `migrations/` | Postgres: компании, пользователи (ключи зашифрованы MultiFernet), права, OAuth-клиенты, токены обновления, журнал |

## Переменные окружения

| переменная | обязательна | смысл |
|---|---|---|
| `PUBLIC_URL` | да | внешний адрес, `https://…`; из него — issuer и аудитория токенов (`…/mcp`) |
| `DATABASE_URL` | да | `postgresql://user:pass@host:5432/db` |
| `VALKEY_URL` | да | `valkey://[:pass@]host:6379/0` (`valkeys://` — TLS) |
| `ENCRYPTION_KEYS` | да | ключи Fernet через запятую; первый шифрует. Ротация: новый ключ в начало |
| `JWT_SECRET` | да | секрет от 32 символов для подписи токенов и CSRF |
| `FREE_COMPANY_IDS` | нет | ID компаний YouGile, которые не платят (свои) |
| `TRIAL_DAYS` | нет | длина пробного периода, по умолчанию 14 |
| `RATE_LIMIT_PER_COMPANY` | нет | запросов к YouGile в минуту на компанию, по умолчанию 45 |
| `YOUGILE_BASE_URL` | нет | по умолчанию `https://ru.yougile.com` |
| `HOST`, `PORT` | нет | по умолчанию `127.0.0.1:8000`; снаружи — только через reverse proxy |
| `FORWARDED_ALLOW_IPS` | нет | адреса прокси, которым верим в `X-Forwarded-For`, по умолчанию `127.0.0.1` |

Секреты: `uv run yougile-cloud genkeys`.

## Команды

```bash
uv run yougile-cloud serve                     # HTTP-сервер
uv run yougile-cloud migrate                   # миграции (serve тоже применяет их при старте)
uv run yougile-cloud companies                 # компании и их доступ
uv run yougile-cloud company <id> --exempt     # бесплатно навсегда
uv run yougile-cloud company <id> --trial-days 14
uv run yougile-cloud company <id> --paid-until 2026-12-31
uv run yougile-cloud company <id> --block
```

## Разработка

Ядро подключено соседней папкой `../yougile-mcp` (пока 0.3.0 нет на PyPI). Тестам нужны
Postgres и Valkey:

```bash
docker run -d --name ygc-test-pg --restart unless-stopped -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=ygc -p 127.0.0.1:55432:5432 postgres:18-alpine
docker run -d --name ygc-test-valkey --restart unless-stopped \
  -p 127.0.0.1:56379:6379 valkey/valkey:9.2-alpine
uv sync
uv run pytest
```

Тесты поднимают настоящий сервер на uvicorn и проходят весь путь, как Claude: регистрация
клиента, вход через YouGile (фейковый), токены, вызов инструментов, повтор кода и токена
обновления, пробный период, лимиты попыток, CSRF и экранирование.
