#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

if [ "$#" -ne 2 ]; then
    printf '%s\n' 'Usage: sh deploy/restore.sh BACKUP_DIRECTORY NEW_DATABASE' >&2
    exit 2
fi
archive=$1/db.dump
target=$2
if ! printf '%s\n' "$target" | grep -Eq '^[a-z][a-z0-9_]{0,62}$'; then
    printf '%s\n' 'Invalid database name' >&2
    exit 2
fi
if [ ! -f "$archive" ] || [ -z "$(find "$archive" -mmin -10080 -print)" ]; then
    printf '%s\n' 'A backup from the past seven days is required' >&2
    exit 2
fi

compose() {
    docker compose --env-file "${BILET_ENV_FILE:-.env.deploy}" -f compose.yaml -f compose.deploy.yaml "$@"
}

# createdb requires a new database name for restore checks.
compose exec -T db sh -ec 'createdb -U "$POSTGRES_USER" --template=template0 -- "$1"' sh "$target"
compose exec -T db sh -ec 'pg_restore --exit-on-error --single-transaction --no-owner --no-privileges -U "$POSTGRES_USER" -d "$1"' sh "$target" < "$archive"
compose exec -T db sh -ec 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -f /db/migrate.sql' sh "$target"
compose run --rm --no-deps -T worker python -m app.operations replay-restored --database-name "$target"
printf '%s\n' "Restored and sanitized: $target"
