#!/usr/bin/env sh
set -eu

DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_NAME="sftp_he1.service"
TARGET_UNIT="$UNIT_DIR/$UNIT_NAME"

mkdir -p "$UNIT_DIR"
cp "$DIR/$UNIT_NAME" "$TARGET_UNIT"
systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
systemctl --user restart "$UNIT_NAME"
systemctl --user status --no-pager "$UNIT_NAME"
