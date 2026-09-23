# Развёртывание

Сервер: Ubuntu с Docker Compose и системным Caddy (сейчас — общий хост с другими проектами).
Всё живёт в `/home/deploy/yougile-mcp-cloud`:

| файл | откуда | что это |
|---|---|---|
| `compose.yaml` | копия [`compose.yaml`](compose.yaml) | приложение, PostgreSQL, Valkey |
| `deploy.sh` | копия [`deploy.sh`](deploy.sh) | единственное, что может ключ CI |
| `.env` | создаётся на сервере по [`env.example`](env.example), `chmod 600` | настройки и секреты |
| `/etc/caddy/sites/yougile.caddy` | копия [`yougile.caddy`](yougile.caddy) | сайт в Caddy хоста |

Секреты создаются на сервере и никуда не копируются. `ENCRYPTION_KEYS` расшифровывает ключи
YouGile в базе: база без него бесполезна, поэтому в резервную копию идут вместе.

## Первая установка

```bash
D=/home/deploy/yougile-mcp-cloud
mkdir -p $D && cd $D
# compose.yaml, deploy.sh и env.example — из репозитория (scp)
chmod 755 deploy.sh
cp env.example .env && chmod 600 .env
# заполнить .env: PUBLIC_URL, секреты (yougile-cloud genkeys), пароли (openssl rand -hex 24)

# Caddy: блок сайта отдельным файлом, основной Caddyfile подключает его строкой
#   import /etc/caddy/sites/*.caddy
sudo install -D -m 644 yougile.caddy /etc/caddy/sites/yougile.caddy
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

Ключ CI — отдельная пара ed25519. В `~/.ssh/authorized_keys` он привязан к скрипту:

```
command="/home/deploy/yougile-mcp-cloud/deploy.sh",restrict ssh-ed25519 AAAA… yougile-mcp-cloud-ci
```

Приватная часть — в секрете `DEPLOY_SSH_KEY` окружения `production` репозитория, там же
`DEPLOY_HOST`, `DEPLOY_PORT` и `DEPLOY_KNOWN_HOSTS` (строка ключа хоста из `known_hosts`).
Отозвать доступ CI — удалить эту строку из `authorized_keys`.

## Обновление

Обычный деплой — коммит в `main`, дальше всё делает CI. Руками:

```bash
cd /home/deploy/yougile-mcp-cloud
./deploy.sh sha-<коммит> $(sha256sum < compose.yaml | cut -d' ' -f1) \
  $(sha256sum < /etc/caddy/sites/yougile.caddy | cut -d' ' -f1)
```

Изменился `compose.yaml` в репозитории — CI откажется деплоить, пока его не поставить:
скопировать файл на сервер и `docker compose up -d`. Изменился `yougile.caddy` — CI только
предупредит; поставить: бэкап, `sudo install`, `caddy validate`, `systemctl reload caddy`.

Откат: `./deploy.sh` с прежним тегом (теги — на странице пакета в GHCR). Сам `deploy.sh`
откатывается, если новые контейнеры не стали здоровыми за 2 минуты.

## Эксплуатация

```bash
cd /home/deploy/yougile-mcp-cloud
docker compose ps
docker compose logs --tail 100 app
docker compose exec app yougile-cloud companies
docker compose exec app yougile-cloud company <id> --exempt
docker compose exec postgres psql -U yougile yougile
```

Резервные копии пока не настроены: потеря базы — это переподключение всех людей и потеря
настроек компаний (ключи YouGile выпускаются заново при входе).
