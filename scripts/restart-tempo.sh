#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"

cd "${project_root}"

python3 scripts/provision-runner-token.py
echo "Building Tempo application services..."
tempo_revision="$(git rev-parse HEAD)"
docker compose build --build-arg "TEMPO_GIT_SHA=${tempo_revision}" tempo validation-runner

echo "Starting execution dependencies without recreating the database..."
docker compose up --detach --no-recreate --wait --wait-timeout 180 postgres project-runner

echo "Updating Tempo and the validation runner..."
docker compose stop --timeout 60 tempo
umask 077
mkdir -p var/backups
chmod 0700 var/backups
backup_path="var/backups/tempo-$(date -u +%Y%m%dT%H%M%SZ)-${tempo_revision:0:12}.dump"
docker compose exec -T postgres pg_dump --username=tempo --dbname=tempo --format=custom > "$backup_path"
echo "Pre-deployment database backup saved to ${backup_path}"
docker compose up \
  --detach \
  --wait \
  --wait-timeout 180 \
  tempo validation-runner

echo
echo "Tempo services are running:"
docker compose ps

docker compose exec -T tempo python - "$tempo_revision" <<'PY'
import json
import sys
import urllib.error
import urllib.request

with urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5) as response:
    health = json.load(response)
if health.get('revision') != sys.argv[1]:
    raise SystemExit('Deployment revision does not match the requested commit')
try:
    urllib.request.urlopen('http://127.0.0.1:8000/api/v1/state', timeout=5)
except urllib.error.HTTPError as error:
    if error.code != 401:
        raise
else:
    raise SystemExit('Unauthenticated operational reads were not rejected')
print('Deployment revision and protected-read smoke check passed.')
PY
