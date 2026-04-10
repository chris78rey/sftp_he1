#!/usr/bin/env sh
set -eu

DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
UNIT_NAME="sftp_he1.service"
TARGET_UNIT="/etc/systemd/system/${UNIT_NAME}"

if [ ! -d "$DIR/.venv" ]; then
  echo "Falta el entorno virtual en $DIR/.venv" >&2
  exit 1
fi

if [ ! -f "$DIR/.env" ]; then
  echo "Falta $DIR/.env" >&2
  exit 1
fi

sudo install -m 0644 "$DIR/$UNIT_NAME" "$TARGET_UNIT"
sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT_NAME"
sudo systemctl status --no-pager "$UNIT_NAME"
