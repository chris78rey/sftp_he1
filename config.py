from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class Settings:
    FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret")

    ORACLE_USER = os.environ.get("ORACLE_USER", "").strip()
    ORACLE_PASSWORD = os.environ.get("ORACLE_PASSWORD", "").strip()
    ORACLE_JDBC_JAR = os.environ.get("ORACLE_JDBC_JAR", "/app/jdbc/ojdbc8.jar").strip()
    ORACLE_TARGETS = os.environ.get("ORACLE_TARGETS", "").strip()
    ORACLE_OWNER = os.environ.get("ORACLE_OWNER", "DIGITALIZACION").strip().upper()
    ORACLE_TABLE = os.environ.get("ORACLE_TABLE", "DIGITALIZACION").strip().upper()

    SFTP_HOST = os.environ.get("SFTP_HOST", "").strip()
    SFTP_PORT = int(os.environ.get("SFTP_PORT", "2223"))
    SFTP_USER = os.environ.get("SFTP_USER", "").strip()
    SFTP_PASSWORD = os.environ.get("SFTP_PASSWORD", "").strip()
    SFTP_REMOTE_BASE = os.environ.get("SFTP_REMOTE_BASE", "/repositorio").strip()

    DOWNLOAD_OUTPUT_ROOT = Path(
        os.environ.get("DOWNLOAD_OUTPUT_ROOT", str(BASE_DIR / "output"))
    ).expanduser().resolve()
