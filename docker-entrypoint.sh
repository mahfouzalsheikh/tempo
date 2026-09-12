#!/bin/sh
set -eu

codex_source=/run/tempo-host-codex
codex_home=/home/tempo/.codex

install -d -o tempo -g tempo -m 0700 "$codex_home"

# Keep Codex's mutable SQLite/session state in its named volume. Only seed the
# portable authentication and configuration files from the host bind mount.
for name in auth.json config.toml; do
    source_path="$codex_source/$name"
    target_path="$codex_home/$name"
    if [ -f "$source_path" ] && { [ ! -f "$target_path" ] || [ "$source_path" -nt "$target_path" ]; }; then
        install -o tempo -g tempo -m 0600 "$source_path" "$target_path"
    fi
done

mkdir -p /data/agent-state /data/previews /data/releases
install -d -o tempo -g tempo -m 0700 /data/account-credentials
chown tempo:tempo /data/previews /data/releases
chmod 0700 /data/previews /data/releases
chown tempo:tempo /data/workspaces /data/agent-state /data/database /data/log "$codex_home"
chmod 0700 /data/agent-state

exec setpriv --reuid=tempo --regid=tempo --clear-groups tempo "$@"
