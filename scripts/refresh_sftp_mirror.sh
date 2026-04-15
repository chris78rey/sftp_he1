#!/usr/bin/env bash
set -euo pipefail

SRC="/data_nuevo/repo_grande/data/datos/"
DST="/data_nuevo/sftp"
BUILD="/data_nuevo/sftp_build"
PREV="/data_nuevo/sftp_prev"
MOUNTPOINT="/sftp-jail/lmoreno/repositorio"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="/tmp/refresh_sftp_mirror_${STAMP}.log"

exec > >(tee -a "$LOG") 2>&1

echo "=== INICIO $(date) ==="

echo "[1] Preparando staging limpio"
sudo mkdir -p "$BUILD"
sudo rsync -aH --delete "$SRC" "$BUILD"/

echo "[2] Validacion rapida"
du -sh "$SRC" || true
du -sh "$BUILD" || true
find "$SRC" -type f | wc -l || true
find "$BUILD" -type f | wc -l || true

echo "[3] Desmontando publicacion SFTP"
sudo umount "$MOUNTPOINT" || true

echo "[4] Rotando destinos"
sudo rm -rf "$PREV"
if [ -d "$DST" ]; then
  sudo mv "$DST" "$PREV"
fi
sudo mv "$BUILD" "$DST"
sudo mkdir -p "$BUILD"

echo "[5] Restaurando mount y readonly"
sudo mount "$MOUNTPOINT"
sudo systemctl start sftp-lmoreno-ro.service
findmnt "$MOUNTPOINT" >/dev/null || { echo "ERROR: no quedo montado $MOUNTPOINT"; exit 1; }

echo "[6] Verificacion final"
findmnt -no TARGET,SOURCE,OPTIONS "$MOUNTPOINT"

if sudo touch "$MOUNTPOINT"/__test_write__ 2>/dev/null; then
  sudo rm -f "$MOUNTPOINT"/__test_write__
  echo "ERROR: sigue escribible"
  exit 1
fi

echo "=== FIN OK $(date) ==="
echo "LOG: $LOG"
