from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for

from config import Settings
from jobs import create_job, get_job, run_job_async
from oracle_client import build_preview, oracle_diagnostics
from sftp_download import sftp_diagnostics

app = Flask(__name__)
app.config["SECRET_KEY"] = Settings.FLASK_SECRET_KEY


def _progress_pct(job: dict | None) -> int:
    if not job:
        return 0
    pr = job.get("progress") or {}
    total = int(pr.get("files_total") or 0)
    done = int(pr.get("files_done") or 0)
    return int((done / total) * 100) if total > 0 else 0


@app.get("/healthz")
def healthz():
    return {"ok": True}, 200


@app.get("/")
def home():
    return render_template("index.html", defaults=Settings, diagnostics={})


@app.get("/diagnostics")
def diagnostics():
    return {
        "oracle": oracle_diagnostics(),
        "sftp": sftp_diagnostics(),
    }


@app.post("/preview")
def preview():
    raw = (request.form.get("dig_id_tramite") or "").strip()
    if not raw.isdigit():
        flash("DIG_ID_TRAMITE debe ser numérico.", "error")
        return render_template("index.html", defaults=Settings)
    try:
        preview_data = build_preview(int(raw))
        if int(preview_data.get("count") or 0) == 0:
            flash("No se encontraron carpetas para ese DIG_ID_TRAMITE.", "error")
        return render_template("index.html", defaults=Settings, preview=preview_data)
    except Exception as exc:
        flash(f"Error consultando Oracle: {exc}", "error")
        return render_template("index.html", defaults=Settings)


@app.post("/start")
def start():
    raw = (request.form.get("dig_id_tramite") or "").strip()
    if not raw.isdigit():
        flash("DIG_ID_TRAMITE debe ser numérico.", "error")
        return render_template("index.html", defaults=Settings)
    dig_id_tramite = int(raw)
    try:
        preview_data = build_preview(dig_id_tramite)
        if int(preview_data.get("count") or 0) == 0:
            flash("No se encontraron carpetas para ese DIG_ID_TRAMITE.", "error")
            return render_template("index.html", defaults=Settings, preview=preview_data)
    except Exception as exc:
        flash(f"Error consultando Oracle: {exc}", "error")
        return render_template("index.html", defaults=Settings)
    job_id = create_job(dig_id_tramite, preview_data)
    run_job_async(job_id)
    return redirect(url_for("job_view", job_id=job_id))


@app.get("/jobs/<job_id>")
def job_view(job_id: str):
    job = get_job(job_id)
    if job is None:
        abort(404)
    return render_template("job.html", job=job, progress_pct=_progress_pct(job))


@app.get("/jobs/<job_id>/download")
def job_download(job_id: str):
    job = get_job(job_id)
    if job is None:
        abort(404)
    report = job.get("report") or {}
    zip_path = Path(str(report.get("zip_path") or "")).expanduser().resolve()
    if not zip_path.exists() or not zip_path.is_file():
        abort(404)
    return send_file(str(zip_path), as_attachment=True, download_name=zip_path.name, mimetype="application/zip")
