#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"

cd "${project_root}"

echo "Rebuilding and restarting Tempo services..."
docker compose up \
  --detach \
  --build \
  --force-recreate \
  --remove-orphans \
  --wait \
  --wait-timeout 180

echo
echo "Tempo services are running:"
docker compose ps
