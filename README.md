# YouGile MCP Cloud

[![CI](https://github.com/indalo-tech/yougile-mcp-cloud/actions/workflows/ci.yml/badge.svg)](https://github.com/indalo-tech/yougile-mcp-cloud/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

Серверный вариант [YouGile MCP](https://github.com/indalo-tech/yougile-mcp): подключение
по адресу, без установки. Человек входит своим логином YouGile, сервер выпускает ему отдельный
ключ и работает с его правами. Инструменты, права и лимиты — из ядра
[`yougile-mcp`](https://pypi.org/project/yougile-mcp/) с PyPI.

Работающий сервис: `https://yougile.indalo.ru/mcp` — адрес для AI-клиента (Claude, Cursor и
другие с поддержкой удалённых MCP-серверов и OAuth). Администраторы компаний настраивают права
на `https://yougile.indalo.ru/admin`.

## Как устроено

| часть | что делает |
|---|---|
| `oauth.py` | OAuth 2.1 для MCP-клиентов: регистрация клиентов (DCR), PKCE, JWT доступа с `iss`/`aud`/типом, одноразовые коды в Valkey, токены обновления — случайные, в базе только хэш, меняются при каждом обмене |
| `web.py`, `pages.py` | страница входа через YouGile: CSRF, проверка Origin, CSP без скриптов, экранирование всех подстановок, выбор компании, лимит попыток |
| `signin.py`, `yougile_auth.py` | выпуск отдельного ключа YouGile на человека; при повторном входе старый ключ удаляется (у YouGile лимит 30 ключей на аккаунт); пароль не хранится |
| `admin.py`, `admin_pages.py` | страница администратора компании `/admin`: вход логином YouGile (только для админов компании), сессия в Valkey, настройки компании, права сотрудников, отключение |
| `permissions.py` | группы запретов для страницы администратора (покрывают все записывающие операции ядра) и разбор цепочек Workflow с проверкой по реальным доскам |
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
| `ADMIN_SESSION_TTL` | нет | сколько секунд живёт вход на страницу администратора, по умолчанию 43200 (12 часов) |
| `HOST`, `PORT` | нет | по умолчанию `127.0.0.1:8000`; снаружи — только через reverse proxy |
| `FORWARDED_ALLOW_IPS` | нет | адреса прокси, которым верим в `X-Forwarded-For`, по умолчанию `127.0.0.1` |

Секреты: `uv run yougile-cloud genkeys`.

## Страница администратора

`<PUBLIC_URL>/admin` — для администраторов компании в YouGile. Вход тем же логином YouGile;
в списке компаний только те, где человек админ. Вход выпускает ему собственный ключ, как
подключение AI-клиента.

- **Сотрудники** — все люди компании из YouGile: кто подключён, последний запрос, роль,
  проекты, запреты. Права можно задать заранее, до подключения. Отключение удаляет ключ
  сервера и токены человека.
- **Настройки** — роль по умолчанию, правила компании для ассистента, проекты с
  подтверждением записи, запреты для всех, цепочки Workflow (проверяются по реальным доскам),
  часовой пояс.

Права MCP только сужают права человека в самом YouGile. Сессия — случайный токен в
HttpOnly-cookie (`__Host-` на https), в Valkey хранится только его хэш. Каждый запрос
заново спрашивает YouGile, остался ли человек админом: снятого админа выбрасывает сразу.
Формы защищены CSRF-токеном, привязанным к сессии, и проверкой Origin; CSP запрещает
скрипты и отправку форм на чужие адреса. Изменения попадают в журнал `audit_log`.

## Команды

На сервере — внутри контейнера: `docker compose exec app yougile-cloud companies`.

```bash
uv run yougile-cloud serve                     # HTTP-сервер
uv run yougile-cloud migrate                   # миграции (serve тоже применяет их при старте)
uv run yougile-cloud companies                 # компании и их доступ
uv run yougile-cloud company <id> --exempt     # бесплатно навсегда
uv run yougile-cloud company <id> --trial-days 14
uv run yougile-cloud company <id> --paid-until 2026-12-31
uv run yougile-cloud company <id> --block
```

## Развёртывание

Образ `ghcr.io/indalo-tech/yougile-mcp-cloud` (публичный) собирает CI на каждый коммит в
`main`: тег `sha-<коммит>` и `latest`. На сервере — Docker Compose из [`deploy/`](deploy):
приложение, PostgreSQL 18 и Valkey 9.2 во внутренней сети; наружу только приложение и только
на `127.0.0.1:8100`, TLS и лимиты по IP — у Caddy хоста. Подробности и команды —
[deploy/README.md](deploy/README.md).

CI деплоит сам: после тестов и сборки образа заходит на сервер ключом, который там привязан к
[`deploy/deploy.sh`](deploy/deploy.sh) (`command=` в `authorized_keys`). Скрипт принимает
только тег образа, разворачивает его и откатывается на прежний, если новые контейнеры не
поднялись. `compose.yaml` и блок Caddy ставятся на сервер вручную: утёкший ключ CI может
выбрать сборку образа, но не то, что и с какими правами запускается на хосте. После деплоя CI
проверяет сервис снаружи — через DNS, TLS и Caddy, как его видит клиент. Секреты деплоя живут
в окружении `production`, доступном только ветке `main`.

## Разработка

Тестам нужны Postgres и Valkey:

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
обновления, пробный период, лимиты попыток, CSRF и экранирование. Страница администратора
проверяется так же: вход, настройки доходят до MCP-сессии сотрудника, права, отключение,
снятие прав админа в YouGile, подделка форм.

Чтобы править ядро вместе с облаком, временно добавьте в `pyproject.toml`
`[tool.uv.sources] yougile-mcp = { path = "../yougile-mcp", editable = true }`. В коммит это
не попадает: облако всегда собирается с выпущенным ядром с PyPI.

## Лицензия

[GNU AGPL-3.0](LICENSE), © Indalo. Код можно запускать и менять, но если вы даёте доступ к
изменённой версии по сети, её исходный код нужно открыть пользователям. Ядро
[`yougile-mcp`](https://github.com/indalo-tech/yougile-mcp) — под MIT.
