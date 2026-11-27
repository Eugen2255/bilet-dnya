#!/bin/sh
set -eu
umask 077
cd "$(dirname "$0")/.."

root=${BILET_BACKUP_DIR:-data/backups}
mkdir -p "$root"
find "$root" -mindepth 2 -maxdepth 2 -type f \( -path "$root/bilet-*.backup/*" -o -path "$root/.bilet-*/*" \) \
    \( -name db.dump -o -name settings.json \) -mmin +10079 -delete
find "$root" -mindepth 1 -maxdepth 1 -type d -name 'bilet-*.backup' -empty -delete
find "$root" -mindepth 1 -maxdepth 1 -type d -name '.bilet-*' -mmin +10079 -empty -delete
if [ "${1:-}" = --prune ]; then
    exit 0
fi

compose() {
    docker compose --env-file "${BILET_ENV_FILE:-.env.deploy}" -f compose.yaml -f compose.deploy.yaml "$@"
}

stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$(mktemp -d "$root/.bilet-XXXXXX")
trap 'rm -f "$work/db.dump" "$work/settings.json"; rmdir "$work" 2>/dev/null || true' EXIT
compose exec -T db sh -ec 'exec pg_dump -Fc --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$work/db.dump"
compose exec -T db pg_restore --list < "$work/db.dump" > /dev/null
compose run --rm --no-deps -T worker python -c 'import json, os; keys = ("MODEL_PRIMARY_ID", "MODEL_REVIEW_ID", "MODEL_FALLBACK_ID"); print(json.dumps({key: os.getenv(key, "") for key in keys}))' > "$work/settings.json"
mv -T "$work" "$root/bilet-$stamp.backup"
printf '%s\n' "$root/bilet-$stamp.backup"
