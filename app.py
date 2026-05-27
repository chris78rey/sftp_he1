from __future__ import annotations

import functools
import hashlib
import os
import shlex
import subprocess
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
from oracle_client import (
    build_preview,
    fetch_items_by_fe_pla_aniomes,
    oracle_diagnostics,
)
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


def _extract_aniomes(form) -> str:
    return (form.get("aniomes") or "").strip()


def _validate_aniomes(value: str) -> bool:
    if not (len(value) == 6 and value.isdigit()):
        return False
    month = int(value[4:])
    return 1 <= month <= 12


def _hsp_from_month(month: int) -> str:
    return f"HSP{month:02d}"


def _previous_month(year: int, month: int) -> tuple[int, int]:
    if month <= 1:
        return year - 1, 12
    return year, month - 1


def _folder_name_for_item(item) -> str:
    return str(getattr(item, "dig_tramite", "") or getattr(item, "dig_id_tramite", "")).strip()


def _sync_candidate_rows(preview: dict) -> list[dict]:
    rows = preview.get("rows") or []
    return [row for row in rows if row.get("candidate")]


def _sync_candidate_signature(preview: dict) -> str:
    parts = []
    for row in sorted(
        _sync_candidate_rows(preview),
        key=lambda item: (str(item.get("source_path") or ""), str(item.get("target_path") or "")),
    ):
        parts.append(f"{row.get('source_path')}->{row.get('target_path')}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _truncate_text(value: str, limit: int = 8000) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return

    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        current = Path(dirpath)
        try:
            if current.exists() and not any(current.iterdir()):
                current.rmdir()
        except Exception:
            pass


def _run_rsync_job(row: dict, *, dry_run: bool) -> dict:
    source_dir = Path(str(row.get("source_path") or "")).expanduser().resolve()
    target_dir = Path(str(row.get("target_path") or "")).expanduser().resolve()

    result = {
        "dig_tramite": row.get("dig_tramite"),
        "dig_id_tramite": row.get("dig_id_tramite"),
        "source_path": str(source_dir),
        "target_path": str(target_dir),
        "dry_run": dry_run,
        "ok": False,
        "returncode": None,
        "command": "",
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "reason": "",
    }

    if not source_dir.is_dir():
        result["reason"] = "source_missing"
        result["stderr"] = f"No existe la carpeta origen: {source_dir}"
        return result

    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "rsync",
        "-avhn" if dry_run else "-avh",
        "--ignore-existing",
        "--itemize-changes",
        "--stats",
    ]
    if not dry_run:
        cmd.append("--remove-source-files")
    cmd.extend([f"{source_dir}/", f"{target_dir}/"])

    proc = subprocess.run(cmd, capture_output=True, text=True)
    stdout, stdout_truncated = _truncate_text(proc.stdout)
    stderr, stderr_truncated = _truncate_text(proc.stderr)

    result.update(
        {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "command": shlex.join(cmd),
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }
    )

    if proc.returncode == 0 and not dry_run:
        _prune_empty_dirs(source_dir)

    return result


def _run_sync_batch(preview: dict, *, dry_run: bool) -> dict:
    rows = _sync_candidate_rows(preview)
    results = [_run_rsync_job(row, dry_run=dry_run) for row in rows]
    succeeded = sum(1 for row in results if row.get("ok"))
    failed = len(results) - succeeded
    return {
        "mode": "dry_run" if dry_run else "execute",
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


def _build_aniomes_candidates(aniomes: str) -> dict:
    items = fetch_items_by_fe_pla_aniomes(aniomes)
    year = int(aniomes[:4])
    month = int(aniomes[4:])
    target_hsp = _hsp_from_month(month)
    source_year, source_month = _previous_month(year, month)
    source_hsp = _hsp_from_month(source_month)

    if not items:
        return {
            "aniomes": aniomes,
            "year": aniomes[:4],
            "month": aniomes[4:],
            "target_hsp": target_hsp,
            "source_hsp": source_hsp,
            "source_year": str(source_year),
            "items": [],
            "rows": [],
            "summary": {
                "total": 0,
                "candidates": 0,
                "already_in_target": 0,
                "missing_both": 0,
            },
        }

    rows: list[dict] = []
    candidates = 0
    already_in_target = 0
    missing_both = 0

    for item in items:
        folder_name = _folder_name_for_item(item)
        if not folder_name:
            continue

        target_path = (
            Settings.LOCAL_REPO_ROOT / str(year) / target_hsp / folder_name
        )
        source_path = (
            Settings.LOCAL_REPO_ROOT / str(source_year) / source_hsp / folder_name
        )

        target_exists = target_path.is_dir()
        source_exists = source_path.is_dir()

        if target_exists:
            already_in_target += 1
            status = "YA_EN_HSP_OBJETIVO"
        elif source_exists:
            candidates += 1
            status = "CANDIDATA"
        else:
            missing_both += 1
            status = "NO_EN_HSP03"

        rows.append(
            {
                "dig_id_tramite": item.dig_id_tramite,
                "dig_tramite": item.dig_tramite,
                "dig_anio": item.dig_anio,
                "dig_expediente": item.dig_expediente,
                "fe_pla_aniomes": item.fe_pla_aniomes,
                "dig_area_dep": item.dig_area_dep,
                "folder_name": folder_name,
                "target_path": str(target_path),
                "source_path": str(source_path),
                "target_exists": target_exists,
                "source_exists": source_exists,
                "status": status,
                "candidate": status == "CANDIDATA",
            }
        )

    return {
        "aniomes": aniomes,
        "year": f"{year:04d}",
        "month": f"{month:02d}",
        "target_hsp": target_hsp,
        "source_hsp": source_hsp,
        "source_year": f"{source_year:04d}",
        "items": items,
        "rows": rows,
        "summary": {
            "total": len(rows),
            "candidates": candidates,
            "already_in_target": already_in_target,
            "missing_both": missing_both,
        },
    }


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


@app.route("/sync-candidates", methods=["GET", "POST"])
@login_required
def sync_candidates():
    preview = None
    last_aniomes = ""
    action = "preview"

    if request.method == "POST":
        last_aniomes = _extract_aniomes(request.form)
        action = (request.form.get("action") or "preview").strip()
        if not _validate_aniomes(last_aniomes):
            flash("ANIOMES debe tener 6 dígitos, por ejemplo 202604.", "error")
            return render_template(
                "sync_candidates.html",
                defaults=Settings,
                last_aniomes=last_aniomes,
                username=session.get("username"),
                can_execute=False,
            )

        try:
            preview = _build_aniomes_candidates(last_aniomes)
            preview["sync_signature"] = _sync_candidate_signature(preview)

            if int(preview.get("summary", {}).get("total") or 0) == 0:
                flash(
                    "No se encontraron registros Oracle para ese ANIOMES o no tenían carpeta válida.",
                    "error",
                )
            else:
                if action == "dry_run":
                    preview["sync_run"] = _run_sync_batch(preview, dry_run=True)
                    session["sync_candidates_dry_run"] = {
                        "aniomes": last_aniomes,
                        "signature": preview["sync_signature"],
                    }
                    flash(
                        "Dry run completado. Revisa el detalle antes de ejecutar.",
                        "ok",
                    )
                elif action == "execute":
                    token = session.get("sync_candidates_dry_run") or {}
                    if (
                        token.get("aniomes") != last_aniomes
                        or token.get("signature") != preview["sync_signature"]
                    ):
                        flash(
                            "Primero debes hacer un dry run en la web con el mismo ANIOMES.",
                            "error",
                        )
                    else:
                        preview["sync_run"] = _run_sync_batch(preview, dry_run=False)
                        session.pop("sync_candidates_dry_run", None)
                        flash(
                            "Ejecución finalizada. Revisa el resumen y los resultados por carpeta.",
                            "ok" if preview["sync_run"]["failed"] == 0 else "error",
                        )
        except Exception as exc:
            flash(f"Error consultando Oracle o el repositorio local: {exc}", "error")

    can_execute = False
    if preview:
        token = session.get("sync_candidates_dry_run") or {}
        can_execute = (
            bool(preview.get("sync_signature"))
            and token.get("aniomes") == last_aniomes
            and token.get("signature") == preview.get("sync_signature")
        )

    return render_template(
        "sync_candidates.html",
        defaults=Settings,
        preview=preview,
        last_aniomes=last_aniomes,
        username=session.get("username"),
        can_execute=can_execute,
    )


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
