from __future__ import annotations

import functools
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from config import Settings
from jobs import create_job, get_job, run_job_async
from oracle_client import build_preview, oracle_diagnostics
from sftp_download import sftp_diagnostics

app = Flask(__name__)
app.config["SECRET_KEY"] = Settings.FLASK_SECRET_KEY


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_authenticated"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def _progress_pct(job: dict | None) -> int:
    if not job:
        return 0
    pr = job.get("progress") or {}
    total = int(pr.get("files_total") or 0)
    done = int(pr.get("files_done") or 0)
    return int((done / total) * 100) if total > 0 else 0


def _duration_text(job: dict | None) -> str:
    if not job:
        return "-"
    started_at = job.get("started_at") or job.get("created_at")
    if not started_at:
        return "-"
    try:
        started = datetime.fromisoformat(str(started_at))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except Exception:
        return "-"
    if job.get("finished_at"):
        try:
            finished = datetime.fromisoformat(str(job["finished_at"]))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
        except Exception:
            finished = datetime.now(timezone.utc)
    else:
        finished = datetime.now(timezone.utc)
    seconds = max(0, int((finished - started).total_seconds()))
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"


def _search_mode_label(mode: str | None) -> str:
    if (mode or "").strip() == "dig_tramite":
        return "DIG_TRAMITE"
    return "DIG_ID_TRAMITE"


def _extract_search(form) -> tuple[str, str]:
    mode = (form.get("search_mode") or "dig_id_tramite").strip()
    value = (form.get("search_value") or form.get("dig_id_tramite") or "").strip()
    return mode, value


def _job_report_file(job_id: str, report_key: str) -> Path:
    job = get_job(job_id)
    if job is None:
        abort(404)

    report = job.get("report") or {}
    file_path = Path(str(report.get(report_key) or "")).expanduser().resolve()

    if not file_path.exists() or not file_path.is_file():
        abort(404)

    return file_path


@app.get("/healthz")
def healthz():
    return {"ok": True}, 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        if username == Settings.ADMIN_USER and password == Settings.ADMIN_PASSWORD:
            session["is_authenticated"] = True
            session["username"] = username
            return redirect(url_for("home"))
        flash("Credenciales inválidas.", "error")
    return render_template("login.html", defaults=Settings)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    jobs = []
    from jobs import JOB_LOCK, JOBS

    with JOB_LOCK:
        for job in JOBS.values():
            jobs.append(dict(job))

    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    active = next((j for j in jobs if j.get("status") in {"waiting", "running"}), None)

    return render_template(
        "index.html",
        defaults=Settings,
        diagnostics={},
        last_search_mode="dig_id_tramite",
        last_search_value="",
        username=session.get("username"),
        jobs=jobs,
        active_job=active,
        duration_text=_duration_text,
    )


@app.get("/admin")
@login_required
def admin():
    root = Settings.DOWNLOAD_OUTPUT_ROOT
    pdfs = []
    zips = []
    total_size = 0

    for path in root.rglob("*"):
        if path.is_file():
            try:
                total_size += path.stat().st_size
            except FileNotFoundError:
                pass
            if path.suffix.lower() == ".pdf":
                pdfs.append(path)
            elif path.suffix.lower() == ".zip":
                zips.append(path)

    total_bytes = sum(p.stat().st_size for p in pdfs if p.exists())

    return render_template(
        "admin.html",
        defaults=Settings,
        username=session.get("username"),
        pdf_count=len(pdfs),
        zip_count=len(zips),
        total_bytes=total_bytes,
        total_output_bytes=total_size,
        output_root=str(root),
    )


@app.post("/admin/cleanup-pdfs")
@login_required
def cleanup_pdfs():
    root = Settings.DOWNLOAD_OUTPUT_ROOT
    removed = 0
    bytes_freed = 0

    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() == ".pdf":
            try:
                bytes_freed += path.stat().st_size
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        current = Path(dirpath)
        try:
            if current != root and not any(current.iterdir()):
                current.rmdir()
        except Exception:
            pass

    flash(
        f"Se eliminaron {removed} PDF(s) y se liberaron {human_bytes(bytes_freed)}.",
        "ok",
    )
    return redirect(url_for("admin"))


@app.post("/admin/cleanup-output")
@login_required
def cleanup_output():
    root = Settings.DOWNLOAD_OUTPUT_ROOT
    removed_files = 0
    freed = 0

    if root.exists():
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                try:
                    freed += path.stat().st_size
                    path.unlink()
                    removed_files += 1
                except FileNotFoundError:
                    pass
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    flash(
        f"Se eliminó todo el contenido de output: {removed_files} archivo(s), {human_bytes(freed)} liberados.",
        "ok",
    )
    return redirect(url_for("admin"))


@app.get("/diagnostics")
@login_required
def diagnostics():
    return {
        "oracle": oracle_diagnostics(),
        "sftp": sftp_diagnostics(),
    }


@app.post("/preview")
@login_required
def preview():
    search_mode, raw = _extract_search(request.form)

    if not raw.isdigit():
        flash(f"{_search_mode_label(search_mode)} debe ser numérico.", "error")
        return render_template(
            "index.html",
            defaults=Settings,
            last_search_mode=search_mode,
            last_search_value=raw,
        )

    try:
        preview_data = build_preview(search_mode, raw)
        if int(preview_data.get("count") or 0) == 0:
            flash(
                f"No se encontraron carpetas para ese {_search_mode_label(preview_data.get('search_mode'))}.",
                "error",
            )
        return render_template(
            "index.html",
            defaults=Settings,
            preview=preview_data,
            last_search_mode=preview_data.get("search_mode"),
            last_search_value=raw,
        )
    except Exception as exc:
        flash(f"Error consultando Oracle: {exc}", "error")
        return render_template(
            "index.html",
            defaults=Settings,
            last_search_mode=search_mode,
            last_search_value=raw,
        )


@app.post("/start")
@login_required
def start():
    search_mode, raw = _extract_search(request.form)

    if not raw.isdigit():
        flash(f"{_search_mode_label(search_mode)} debe ser numérico.", "error")
        return render_template(
            "index.html",
            defaults=Settings,
            last_search_mode=search_mode,
            last_search_value=raw,
        )

    try:
        preview_data = build_preview(search_mode, raw)
        if int(preview_data.get("count") or 0) == 0:
            flash(
                f"No se encontraron carpetas para ese {_search_mode_label(preview_data.get('search_mode'))}.",
                "error",
            )
            return render_template(
                "index.html",
                defaults=Settings,
                preview=preview_data,
                last_search_mode=preview_data.get("search_mode"),
                last_search_value=raw,
            )
    except Exception as exc:
        flash(f"Error consultando Oracle: {exc}", "error")
        return render_template(
            "index.html",
            defaults=Settings,
            last_search_mode=search_mode,
            last_search_value=raw,
        )

    job_id = create_job(
        str(preview_data.get("search_mode") or search_mode), raw, preview_data
    )
    run_job_async(job_id)
    return redirect(url_for("job_view", job_id=job_id))


@app.get("/jobs/<job_id>")
@login_required
def job_view(job_id: str):
    job = get_job(job_id)
    if job is None:
        abort(404)
    return render_template(
        "job.html",
        job=job,
        progress_pct=_progress_pct(job),
        duration_text=_duration_text,
    )


# === EXISTENTE: descarga principal ZIP ===
@app.get("/jobs/<job_id>/download")
@login_required
def job_download(job_id: str):
    zip_path = _job_report_file(job_id, "zip_path")
    return send_file(
        str(zip_path),
        as_attachment=True,
        download_name=zip_path.name,
        mimetype="application/zip",
    )


# === NUEVO: descarga del CSV de auditoría ===
@app.get("/jobs/<job_id>/download-audit")
@login_required
def job_download_audit(job_id: str):
    csv_path = _job_report_file(job_id, "audit_csv_path")
    return send_file(
        str(csv_path),
        as_attachment=True,
        download_name=csv_path.name,
        mimetype="text/csv",
    )


# === NUEVO: descarga del CSV de faltantes ===
@app.get("/jobs/<job_id>/download-missing")
@login_required
def job_download_missing(job_id: str):
    csv_path = _job_report_file(job_id, "missing_csv_path")
    return send_file(
        str(csv_path),
        as_attachment=True,
        download_name=csv_path.name,
        mimetype="text/csv",
    )


@app.template_filter("human_bytes")
def human_bytes(value: int | float | None) -> str:
    try:
        n = float(value or 0)
    except Exception:
        n = 0.0

    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1

    return ("%0.1f %s" % (n, units[i])).replace(".0 ", " ")
