#!/usr/bin/env bash
# Called by GitHub Actions after tests/build, or manually with "rollback".
set -Eeuo pipefail
umask 077
app="mira-api"
root="/opt/$app"
env_file="$root/.env.digitalocean"
tag_key="MIRA_IMAGE_TAG"
state="/var/lib/mira-deploy/$app"
bundle="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:-}"
[[ "$mode" = apply || "$mode" = rollback ]] || { echo 'Usage: release.sh apply IMAGE@DIGEST COMMIT ACTOR | rollback' >&2; exit 2; }
[[ "$(id -u)" = 0 ]] || { echo 'Run as root.' >&2; exit 2; }
test -f "$env_file"
mkdir -p "$state"
# API and ETL share a host: serialize deployments, without stopping either service.
exec 8>/run/lock/mira-deploy.lock
flock -w 600 8
if [[ "$app" = mira-etl ]]; then
    exec 9>/run/lock/mira-etl.lock
    flock -n 9 || { echo 'ETL ocupado: conservar version actual y reintentar cuando termine.' >&2; exit 75; }
    if systemctl list-units --type=service --state=active,activating --no-legend 'mira-etl*' | grep -q .; then
        echo 'Hay cargas ETL activas o esperando; reintentar cuando terminen.' >&2
        exit 75
    fi
fi
cd "$root"
compose=(docker compose --env-file "$env_file")
# Only deployment version fields are backed up. Credentials never leave .env.
version_fields() {
    python3 - "$env_file" "$state/previous/versions.json" "$tag_key" "$1" "${2:-}" <<'PY'
import json, os, re, sys
from pathlib import Path
env, backup, tag_key, mode, revision = sys.argv[1:]
path = Path(env)
text = path.read_text()
keys = [tag_key] + (["APP_VERSION"] if tag_key == "MIRA_IMAGE_TAG" and path.parent.name == "mira-api" else [])
pattern = lambda key: re.compile(r"^" + re.escape(key) + r"=.*$", re.M)
if mode == "save":
    Path(backup).write_text(json.dumps({key: (m.group() if (m := pattern(key).search(text)) else None) for key in keys}))
    sys.exit(0)
values = json.loads(Path(backup).read_text()) if mode == "restore" else {
    key: key + "=" + ("ci-" + revision if key == tag_key else revision) for key in keys
}
for key, line in values.items():
    if pattern(key).search(text):
        text = pattern(key).sub(lambda _: line or "", text)
    elif line is not None:
        text = text.rstrip() + "\n" + line + "\n"
temporary = path.with_name(path.name + ".release-tmp")
temporary.write_text(text)
temporary.chmod(0o600)
os.replace(temporary, path)
PY
}
activate() {
    "${compose[@]}" config --quiet || return
    if [[ "$app" = mira-etl ]]; then
        chmod 755 deploy/mira-etl.sh
        install -m 0644 deploy/mira-etl@.service deploy/mira-etl-daily.timer deploy/mira-etl-monthly.timer /etc/systemd/system/ || return
        systemd-analyze verify /etc/systemd/system/mira-etl@.service /etc/systemd/system/mira-etl-daily.timer /etc/systemd/system/mira-etl-monthly.timer || return
        systemctl daemon-reload || return
        ln -sfn "$root/deploy/mira-etl.sh" /usr/local/bin/mira-etl
        "${compose[@]}" run --rm -T etl check-db </dev/null || return
    else
        "${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120 || return
        curl --fail --silent --show-error --max-time 15 http://127.0.0.1:8080/healthz >/dev/null || return
        if [[ "$app" = mira-api ]]; then
            "${compose[@]}" exec -T api python scripts/check_db.py </dev/null || return
            curl --fail --silent --show-error --max-time 15 http://127.0.0.1:8080/procedures/statuses >/dev/null || return
        fi
    fi
}
restore() {
    test -f "$state/previous/config.tar" || return
    tar -xf "$state/previous/config.tar" -C "$root" || return
    version_fields restore || return
    activate || return
    if [[ -f "$state/previous/current.txt" ]]; then cp "$state/previous/current.txt" "$state/current.txt"; fi
}
if [[ "$mode" = rollback ]]; then
    restore
    echo "Rollback completado: $app"
    exit
fi
[[ $# = 4 ]] || exit 2
image="$2"; revision="$3"; actor="$4"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ "$image" =~ ^ghcr\.io/mira-observatory/$app@sha256:[0-9a-f]{64}$ ]] || exit 2
export DOCKER_CONFIG
DOCKER_CONFIG="$(mktemp -d)"
mutated=0
cleanup() {
    rc=$?
    if [[ "$mutated" = 1 ]]; then
        echo 'Deploy fallo; restaurando la version anterior.' >&2
        if ! restore; then echo 'ERROR: rollback incompleto; revisar el servidor.' >&2; fi
    fi
    rm -rf -- "$DOCKER_CONFIG"
    return "$rc"
}
trap cleanup EXIT
trap 'exit 143' TERM HUP
trap 'exit 130' INT
# The short-lived GITHUB_TOKEN arrives on stdin, never as an argument or log.
docker login ghcr.io --username "$actor" --password-stdin
docker pull "$image"
docker tag "$image" "$app:ci-$revision"
mkdir -p "$state/previous"
tar -cf "$state/previous/config.tar" compose.yaml deploy
version_fields save
if [[ -f "$state/current.txt" ]]; then
    cp "$state/current.txt" "$state/previous/current.txt"
else
    "${compose[@]}" config --images > "$state/previous/current.txt"
fi
mutated=1
# Ship only release configuration; application source is inside the image.
install -m 0644 "$bundle/compose.yaml" "$root/compose.yaml"
cp -a "$bundle/deploy/." "$root/deploy/"
version_fields set "$revision"
activate
printf '%s\n%s\n' "$revision" "$image" > "$state/current.txt"
mutated=0
echo "Deploy completado: $app $revision"
