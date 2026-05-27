from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class Settings:
    FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret")
    ADMIN_USER = os.environ.get("ADMIN_USER", "Leticia").strip()
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "7eticia1234").strip()

    ORACLE_USER = os.environ.get("ORACLE_USER", "").strip()
    ORACLE_PASSWORD = os.environ.get("ORACLE_PASSWORD", "").strip()
    ORACLE_JDBC_JAR = os.environ.get("ORACLE_JDBC_JAR", str(BASE_DIR / "jdbc" / "ojdbc8.jar")).strip()
    ORACLE_TARGETS = os.environ.get("ORACLE_TARGETS", "").strip()
    ORACLE_OWNER = os.environ.get("ORACLE_OWNER", "DIGITALIZACION").strip().upper()
    ORACLE_TABLE = os.environ.get("ORACLE_TABLE", "DIGITALIZACION").strip().upper()

    SFTP_HOST = os.environ.get("SFTP_HOST", "").strip()
    SFTP_PORT = int(os.environ.get("SFTP_PORT", "2223"))
    SFTP_USER = os.environ.get("SFTP_USER", "").strip()
    SFTP_PASSWORD = os.environ.get("SFTP_PASSWORD", "").strip()
    SFTP_REMOTE_BASE = os.environ.get("SFTP_REMOTE_BASE", "/repositorio").strip()
    LOCAL_REPO_ROOT = Path(
        os.environ.get("LOCAL_REPO_ROOT", "/data_nuevo/repo_grande/data/datos")
    ).expanduser().resolve()

    DOWNLOAD_OUTPUT_ROOT = Path(
        os.environ.get("DOWNLOAD_OUTPUT_ROOT", str(BASE_DIR / "output"))
    ).expanduser().resolve()

    APP_HOST = os.environ.get("APP_HOST", "127.0.0.1").strip()
    APP_PORT = int(os.environ.get("APP_PORT", "5085"))
