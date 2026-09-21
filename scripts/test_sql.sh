#!/usr/bin/env bash
# Apply every migration to a fresh scratch database, then run the SQL tests.
# Usage: PGHOST=... PGPORT=... PGUSER=... scripts/test_sql.sh
# Needs a Postgres >= 14 you can create databases on. NEVER point this at Supabase.
set -euo pipefail
cd "$(dirname "$0")/.."
DB="billing_test_$$"
createdb "$DB"
trap 'dropdb --if-exists "$DB"' EXIT
psql -q -v ON_ERROR_STOP=1 -d "$DB" -f tests/sql/stub_public.sql
for f in migrations/*.sql; do
  psql -q -v ON_ERROR_STOP=1 -d "$DB" -f "$f"
done
status=0
for t in tests/sql/test_*.sql; do
  echo "== $t"
  psql -q -v ON_ERROR_STOP=1 -d "$DB" -f "$t" -o /dev/null 2>&1 | sed -n "s/.*NOTICE:  //p; /FAIL\|ERROR/p"
  [ "${PIPESTATUS[0]}" -eq 0 ] || status=1
done
[ "$status" -eq 0 ] && echo "ALL SQL TESTS PASSED" || { echo "SQL TESTS FAILED"; exit 1; }
