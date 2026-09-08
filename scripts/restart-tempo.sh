#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"

cd "${project_root}"

python3 scripts/provision-runner-token.py
echo "Building Tempo application services..."
tempo_revision="$(git rev-parse HEAD)"
docker compose build --build-arg "TEMPO_GIT_SHA=${tempo_revision}" tempo validation-runner project-runner preview-server
docker build --build-arg "TEMPO_GIT_SHA=${tempo_revision}" -f Dockerfile.acceptance -t tempo-acceptance:latest .

echo "Starting the database without recreating it..."
docker compose up --detach --no-recreate --wait --wait-timeout 180 postgres

echo "Draining Tempo before updating execution infrastructure..."
docker compose stop --timeout 180 acceptance-worker
docker compose stop --timeout 60 tempo
docker compose stop --timeout 60 validation-runner
umask 077
mkdir -p var/backups
chmod 0700 var/backups
backup_path="var/backups/tempo-$(date -u +%Y%m%dT%H%M%SZ)-${tempo_revision:0:12}.dump"
docker compose exec -T postgres pg_dump --username=tempo --dbname=tempo --format=custom > "$backup_path"
echo "Pre-deployment database backup saved to ${backup_path}"

docker compose up --detach --wait --wait-timeout 180 project-runner
docker compose exec -T project-runner sh /usr/local/bin/tempo-execution-daemon --network

echo "Loading the immutable validation image into the execution daemon..."
export TEMPO_VALIDATION_IMAGE
# Docker engines using different image stores may expose manifest vs config IDs.
# Resolve the immutable ID in the daemon that will actually execute the jobs.
docker image save tempo-validation-runner:latest | docker compose exec -T project-runner docker image load
TEMPO_VALIDATION_IMAGE="$(docker compose exec -T project-runner docker image inspect --format '{{.Id}}' tempo-validation-runner:latest)"
docker compose exec -T project-runner docker image inspect "$TEMPO_VALIDATION_IMAGE" --format '{{.Id}}'
export TEMPO_ACCEPTANCE_IMAGE
docker image save tempo-acceptance:latest | docker compose exec -T project-runner docker image load
TEMPO_ACCEPTANCE_IMAGE="$(docker compose exec -T project-runner docker image inspect --format '{{.Id}}' tempo-acceptance:latest)"

echo "Updating Tempo and the validation runner..."
docker compose up \
  --detach \
  --wait \
  --wait-timeout 180 \
  tempo validation-runner preview-server acceptance-worker

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

docker compose exec -T tempo python < scripts/check-validation-sandbox.py
docker compose exec -T tempo python < scripts/check-execution-network.py
docker compose exec -T -e TEMPO_SMOKE_REAL_CODEX=1 tempo python < scripts/check-runtime-sandbox.py
docker compose exec -T tempo python < scripts/check-contribution-integration.py
docker compose exec -T tempo python < scripts/check-run-snapshots.py

docker compose exec -T tempo python < scripts/check-product-intake.py

docker compose exec -T --user 10001:10001 tempo python < scripts/check-static-builds.py

python3 scripts/check-previews.py

docker compose exec -T acceptance-worker python < scripts/check-acceptance.py
