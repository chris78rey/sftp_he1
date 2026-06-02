from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime, timezone
from datetime import timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

from sftp_download import cleanup_job_artifacts, run_download

JOBS: dict[str, dict] = {}
JOB_LOCK = threading.Lock()
DOWNLOAD_LOCK = threading.Lock()


def _ecuador_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo("America/Guayaquil")
        except Exception:
            pass
    return timezone(timedelta(hours=-5))


def _now_ecuador_iso() -> str:
    return datetime.now(_ecuador_tz()).isoformat()


def create_job(
    search_mode: str,
    search_value: str,
    preview_data: dict,
    *,
    source_mode: str = "sftp",
) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOB_LOCK:
        JOBS[job_id] = {
            "job_id_value": job_id,
            "search_mode": search_mode,
            "search_value": search_value,
            "source_mode": source_mode,
            "dig_id_tramite": int(search_value) if search_mode == "dig_id_tramite" else None,
            "preview": preview_data,
            "status": "queued",
            "created_at": _now_ecuador_iso(),
            "started_at": None,
            "finished_at": None,
            "progress": {"files_done": 0, "files_total": 0, "bytes_done": 0, "bytes_total": 0, "folder": "", "file": ""},
            "log_lines": [],
            "last_message": "Trabajo en cola",
        }
    return job_id


def get_job(job_id: str) -> dict | None:
    with JOB_LOCK:
        return JOBS.get(job_id)


def update_job(job_id: str, **changes):
    with JOB_LOCK:
        job = JOBS.setdefault(job_id, {"job_id_value": job_id})
        job.update(changes)
        return job


def append_log(job_id: str, message: str):
    with JOB_LOCK:
        job = JOBS.setdefault(job_id, {})
        lines = list(job.get("log_lines", []))
        lines.append(message)
        job["log_lines"] = lines[-300:]
        job["last_message"] = message


def run_job_async(job_id: str):
    def _worker():
        try:
            job = get_job(job_id) or {}
            search_mode = str(job.get("search_mode") or "dig_id_tramite")
            source_mode = str(job.get("source_mode") or "sftp")
            if search_mode == "dig_tramite":
                update_job(job_id, status="running", started_at=_now_ecuador_iso(), last_message="Iniciando descarga")
                _run_download_job(job_id, source_mode=source_mode)
            else:
                update_job(job_id, status="waiting", started_at=_now_ecuador_iso(), last_message="Esperando turno")
                with DOWNLOAD_LOCK:
                    update_job(job_id, status="running", started_at=_now_ecuador_iso(), last_message="Iniciando descarga")
                    _run_download_job(job_id, source_mode=source_mode)
        except Exception as exc:
            job = get_job(job_id) or {}
            cleanup_job_artifacts((job.get("report") or {}).get("job_root") or (job.get("report") or {}).get("data_root"))
            update_job(job_id, status="failed", finished_at=_now_ecuador_iso(), error=str(exc), traceback=traceback.format_exc())
    threading.Thread(target=_worker, daemon=True).start()


def _run_download_job(job_id: str, *, source_mode: str = "sftp"):
    def _progress(payload: dict):
        update_job(job_id, progress=payload)

    def _log(message: str):
        append_log(job_id, message)

    job = get_job(job_id) or {}
    report = run_download(
        str(job.get("search_mode") or "dig_id_tramite"),
        str(job.get("search_value") or ""),
        progress_cb=_progress,
        log_cb=_log,
        source_mode=source_mode,
    )
    update_job(
        job_id,
        status="finished",
        finished_at=_now_ecuador_iso(),
        report=report,
        progress={
            "files_done": int(report.get("total_files", 0)),
            "files_total": int(report.get("total_files", 0)),
            "bytes_done": int(report.get("total_bytes", 0)),
            "bytes_total": int(report.get("total_bytes", 0)),
            "folder": "",
            "file": "",
        },
    )
