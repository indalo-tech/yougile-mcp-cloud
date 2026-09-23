#!/usr/bin/env bash
# Forced command of the CI deploy key: that key runs this script and nothing else.
#
#   ssh deploy@host "<image tag> <sha256 of deploy/compose.yaml> <sha256 of deploy/yougile.caddy>"
#
# Deploys ghcr.io/indalo-tech/yougile-mcp-cloud:<tag> with the compose.yaml installed next to this
# script and rolls back to the previous tag if the new containers do not become healthy.
# compose.yaml and the Caddy site block are installed by hand (README.md): a leaked CI key can
# choose which build of the image runs, not what runs on this host or with which privileges.
# CI logs are public, so this prints only tags and statuses.
set -euo pipefail
umask 077
cd "$(dirname "$(readlink -f "$0")")"

read -r tag compose_sum caddy_sum extra <<<"${SSH_ORIGINAL_COMMAND:-$*}"
if [[ ! "$tag" =~ ^sha-[0-9a-f]{7,40}$ || ! "$compose_sum" =~ ^[0-9a-f]{64}$ ||
    ! "$caddy_sum" =~ ^[0-9a-f]{64}$ || -n "${extra:-}" ]]; then
    echo "usage: <sha-tag> <compose.yaml sha256> <yougile.caddy sha256>" >&2
    exit 2
fi

sum() { sha256sum <"$1" | cut -d' ' -f1; }
if [[ "$(sum compose.yaml)" != "$compose_sum" ]]; then
    echo "compose.yaml on the server differs from the repo: install it first (deploy/README.md)" >&2
    exit 3
fi
if [[ "$(sum /etc/caddy/sites/yougile.caddy)" != "$caddy_sum" ]]; then
    echo "warning: the Caddy site block differs from the repo (deploy/README.md)" >&2
fi

exec 9>.deploy.lock
flock -n 9 || { echo "another deploy is running" >&2; exit 4; }

grep -q '^IMAGE_TAG=' .env || echo 'IMAGE_TAG=' >>.env
previous=$(sed -n 's/^IMAGE_TAG=//p' .env)
set_tag() { sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$1/" .env; }

echo "deploying $tag (running: ${previous:-nothing})"
set_tag "$tag"
if ! docker compose pull --quiet app; then
    set_tag "$previous"
    echo "cannot pull the image $tag" >&2
    exit 5
fi
if docker compose up -d --remove-orphans --wait --wait-timeout 120; then
    docker image prune -f \
        --filter "label=org.opencontainers.image.source=https://github.com/indalo-tech/yougile-mcp-cloud" \
        >/dev/null
    docker compose ps --format '{{.Service}}: {{.Status}}'
    echo "deployed $tag"
    exit 0
fi

echo "the new containers are not healthy; see 'docker compose logs app' on the server" >&2
if [[ -n "$previous" ]]; then
    echo "rolling back to $previous" >&2
    set_tag "$previous"
    docker compose up -d --remove-orphans --wait --wait-timeout 120 || true
fi
exit 1
