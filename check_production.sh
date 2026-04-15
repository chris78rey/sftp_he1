#!/usr/bin/env sh
set -eu

DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ENV_FILE="$DIR/.env"
JAR_PATH="$DIR/jdbc/ojdbc8.jar"

fail() {
  echo "ERROR: $1" >&2
  exit 1
}

[ -f "$ENV_FILE" ] || fail "No existe .env. Copia .env.production.example a .env y ajusta valores."
[ -f "$JAR_PATH" ] || fail "No existe el driver JDBC en $JAR_PATH"
[ -d "$DIR/.venv" ] || fail "No existe el entorno virtual en $DIR/.venv"

eval "$(
  "$DIR/.venv/bin/python" - "$ENV_FILE" <<'PY'
import shlex
import sys
from dotenv import dotenv_values

for key, value in dotenv_values(sys.argv[1]).items():
    if value is None:
        continue
    print(f"export {key}={shlex.quote(value)}")
PY
)"

[ -n "${ORACLE_USER:-}" ] || fail "Falta ORACLE_USER"
[ -n "${ORACLE_PASSWORD:-}" ] || fail "Falta ORACLE_PASSWORD"
[ -n "${ORACLE_TARGETS:-}" ] || fail "Falta ORACLE_TARGETS"
[ -n "${SFTP_HOST:-}" ] || fail "Falta SFTP_HOST"
[ -n "${SFTP_USER:-}" ] || fail "Falta SFTP_USER"
[ -n "${SFTP_PASSWORD:-}" ] || fail "Falta SFTP_PASSWORD"

echo "OK: prerequisitos locales presentes"
echo "OK: driver JDBC: $JAR_PATH"
echo "OK: env cargado desde: $ENV_FILE"
