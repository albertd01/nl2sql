#!/usr/bin/env bash
# Start a throwaway local Postgres (TCP only, port 55432) with the Chinook sample database
# and a SELECT-only role `nl2sql_ro`. Does not touch any Homebrew-managed Postgres service.
#
#   scripts/chinook_postgres.sh start   # URL: postgresql+psycopg://nl2sql_ro@127.0.0.1:55432/chinook
#   scripts/chinook_postgres.sh stop
set -euo pipefail

DATA_DIR="${NL2SQL_PG_DIR:-$HOME/.local/share/nl2sql/pgdata}"
PORT=55432
SQL_URL="https://raw.githubusercontent.com/lerocha/chinook-database/master/ChinookDatabase/DataSources/Chinook_PostgreSql.sql"
export PGHOST=127.0.0.1 PGPORT=$PORT PGUSER=postgres

case "${1:-start}" in
  start)
    if [ ! -d "$DATA_DIR" ]; then
      mkdir -p "$(dirname "$DATA_DIR")"
      initdb -D "$DATA_DIR" -U postgres --auth=trust -E UTF8 >/dev/null
      fresh=1
    fi
    # -k '' disables the Unix socket (long paths exceed macOS's 103-byte socket limit)
    pg_ctl -D "$DATA_DIR" -o "-p $PORT -k '' -h 127.0.0.1" -l "$DATA_DIR/server.log" start >/dev/null
    until pg_isready -q; do sleep 0.3; done
    if [ "${fresh:-0}" = 1 ]; then
      curl -sL "$SQL_URL" | psql -q -d postgres >/dev/null 2>&1
      psql -q -d chinook <<'SQL'
CREATE ROLE nl2sql_ro LOGIN;
GRANT CONNECT ON DATABASE chinook TO nl2sql_ro;
GRANT USAGE ON SCHEMA public TO nl2sql_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nl2sql_ro;
SQL
    fi
    echo "postgresql+psycopg://nl2sql_ro@127.0.0.1:$PORT/chinook"
    ;;
  stop)
    pg_ctl -D "$DATA_DIR" stop
    ;;
  *)
    echo "usage: $0 start|stop" >&2; exit 2
    ;;
esac
