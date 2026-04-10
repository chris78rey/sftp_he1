from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime, timezone

from sftp_download import cleanup_job_artifacts, run_download

JOBS: dict[str, dict] = {}
JOB_LOCK = threading.Lock()
DOWNLOAD_LOCK = threading.Lock()


def create_job(dig_id_tramite: int, preview_data: dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOB_LOCK:
        JOBS[job_id] = {
            "job_id_value": job_id,
            "dig_id_tramite": int(dig_id_tramite),
            "preview": preview_data,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
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
            update_job(job_id, status="waiting", started_at=datetime.now(timezone.utc).isoformat(), last_message="Esperando turno")
            with DOWNLOAD_LOCK:
                update_job(job_id, status="running", started_at=datetime.now(timezone.utc).isoformat(), last_message="Iniciando descarga")
                def _progress(payload: dict):
                    update_job(job_id, progress=payload)

                def _log(message: str):
                    append_log(job_id, message)

                job = get_job(job_id) or {}
                report = run_download(int(job["dig_id_tramite"]), progress_cb=_progress, log_cb=_log)
                update_job(
                    job_id,
                    status="finished",
                    finished_at=datetime.now(timezone.utc).isoformat(),
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
        except Exception as exc:
            job = get_job(job_id) or {}
            cleanup_job_artifacts((job.get("report") or {}).get("job_root") or (job.get("report") or {}).get("data_root"))
            update_job(job_id, status="failed", finished_at=datetime.now(timezone.utc).isoformat(), error=str(exc), traceback=traceback.format_exc())
    threading.Thread(target=_worker, daemon=True).start()
