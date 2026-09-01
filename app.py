from __future__ import annotations

import functools
import hashlib
import io
import csv
import json
import os
import re
import shlex
import subprocess
import threading
import traceback
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

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
from jobs import append_log, create_job, get_job, run_job_async, update_job
from oracle_client import (
    build_preview,
    fetch_items_by_fe_pla_aniomes_and_area_dep,
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


def _ecuador_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo("America/Guayaquil")
        except Exception:
            pass
    return timezone(timedelta(hours=-5))


def _now_ecuador_iso() -> str:
    return datetime.now(_ecuador_tz()).isoformat()


def _format_datetime_local(value: str | None) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(str(value))
    except Exception:
        return str(value)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(_ecuador_tz()).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


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
            finished = datetime.now(_ecuador_tz())
    else:
        finished = datetime.now(_ecuador_tz())
    seconds = max(0, int((finished - started).total_seconds()))
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"


def _load_download_history(root: Path, limit: int = 20) -> list[dict]:
    history: list[dict] = []
    if not root.exists():
        return history

    for manifest_path in root.rglob("manifest.json"):
        try:
            with manifest_path.open("r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except Exception:
            continue

        zip_path = manifest_path.parent.with_name(f"{manifest_path.parent.name}.zip")
        generated_at = str(manifest.get("generated_at") or "").strip()
        try:
            sort_key = datetime.fromisoformat(generated_at)
        except Exception:
            try:
                sort_key = datetime.fromtimestamp(manifest_path.stat().st_mtime, tz=_ecuador_tz())
            except Exception:
                sort_key = datetime.now(_ecuador_tz())

        history.append(
            {
                "manifest_rel": str(manifest_path.relative_to(root)),
                "zip_rel": str(zip_path.relative_to(root)),
                "zip_exists": zip_path.exists(),
                "search_mode": manifest.get("search_mode") or "-",
                "search_value": manifest.get("search_value") or "-",
                "generated_at": generated_at or "-",
                "expected_folders": manifest.get("expected_folders", 0),
                "found_folders": manifest.get("found_folders", 0),
                "missing_folders": manifest.get("missing_folders", 0),
                "total_files": manifest.get("total_files", 0),
                "total_bytes": manifest.get("total_bytes", 0),
                "_sort_key": sort_key,
            }
        )

    history.sort(key=lambda item: item["_sort_key"], reverse=True)
    for item in history:
        item.pop("_sort_key", None)
    return history[:limit]


def _safe_output_path(relative_path: str) -> Path:
    root = Settings.DOWNLOAD_OUTPUT_ROOT.resolve()
    rel = (relative_path or "").strip().lstrip("/\\")
    target = (root / rel).resolve()
    if root not in target.parents and target != root:
        abort(404)
    if not target.exists() or not target.is_file():
        abort(404)
    return target


def _search_mode_label(mode: str | None) -> str:
    if (mode or "").strip() == "dig_tramite":
        return "DIG_TRAMITE"
    if (mode or "").strip() == "label_objecion":
        return "LABEL_OBJECION"
    return "DIG_ID_TRAMITE"


def _extract_search(form) -> tuple[str, str]:
    mode = (form.get("search_mode") or "dig_id_tramite").strip()
    value = (form.get("search_value") or form.get("dig_id_tramite") or "").strip()
    return mode, value


def _validate_search_value(mode: str, value: str) -> bool:
    if mode == "label_objecion":
        return bool(value) and len(value) <= 60
    return value.isdigit()


def _search_value_error(mode: str) -> str:
    if mode == "label_objecion":
        return "LABEL_OBJECION debe tener un valor de hasta 60 caracteres."
    return f"{_search_mode_label(mode)} debe ser numérico."


def _extract_aniomes(form) -> str:
    return (form.get("aniomes") or "").strip()


def _validate_aniomes(value: str) -> bool:
    if not (len(value) == 6 and value.isdigit()):
        return False
    month = int(value[4:])
    return 1 <= month <= 12


def _folder_code(prefix: str, month: int) -> str:
    return f"{prefix}{month:02d}"


def _previous_month(year: int, month: int) -> tuple[int, int]:
    if month <= 1:
        return year - 1, 12
    return year, month - 1


def _folder_name_for_item(item) -> str:
    return str(getattr(item, "dig_tramite", "") or getattr(item, "dig_id_tramite", "")).strip()


def _folder_name_variants(folder_name: str) -> list[str]:
    text = str(folder_name or "").strip()
    variants = [text]
    if text.isdigit():
        trimmed = text.rstrip("0")
        if trimmed and trimmed not in variants:
            variants.append(trimmed)
    return variants


def _expected_tramite_name(item) -> str:
    return _folder_name_for_item(item)


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


def _repo_match_paths(year: str, expediente: str, folder_name: str) -> list[Path]:
    year_root = Settings.LOCAL_REPO_ROOT / str(year)
    if not year_root.exists():
        return []

    expediente_root = year_root / str(expediente)
    candidate = expediente_root / str(folder_name)
    matches: list[Path] = []

    try:
        if candidate.is_dir():
            matches.append(candidate.resolve())

        for found in year_root.rglob(str(folder_name)):
            if not found.is_dir():
                continue
            resolved = found.resolve()
            if resolved not in matches:
                matches.append(resolved)
    except Exception:
        return matches

    return sorted(matches, key=lambda p: str(p))


def _build_path_validation(aniomes: str) -> dict:
    items = fetch_items_by_fe_pla_aniomes(aniomes)
    year = int(aniomes[:4])
    month = int(aniomes[4:])
    month_prefix = f"{month:02d}"

    if not items:
        return {
            "aniomes": aniomes,
            "year": f"{year:04d}",
            "month": month_prefix,
            "rows": [],
            "summary": {
                "total": 0,
                "matched": 0,
                "discrepancies": 0,
                "movable": 0,
                "missing": 0,
                "match_pct": 0.0,
                "discrepancy_pct": 0.0,
                "movable_pct": 0.0,
                "nonmovable_pct": 0.0,
            },
        }

    rows: list[dict] = []
    matched = 0
    discrepancies = 0
    movable = 0
    missing = 0

    for item in items:
        folder_name = _folder_name_for_item(item)
        if not folder_name:
            continue

        year_text = str(item.dig_anio or year).strip() or f"{year:04d}"
        expediente = str(item.dig_expediente or "").strip()
        expected_tramite = _expected_tramite_name(item)
        expected_path = (Settings.LOCAL_REPO_ROOT / year_text / expediente / expected_tramite).resolve()
        found_paths = _repo_match_paths(year_text, item.dig_expediente, folder_name)
        expected_exists = expected_path in found_paths
        wrong_paths = [p for p in found_paths if p != expected_path]
        source_path = wrong_paths[0] if wrong_paths else None
        source_exists = source_path is not None and source_path.is_dir()

        if expected_exists and len(found_paths) == 1:
            matched += 1
            status = "COINCIDE"
            continue

        discrepancies += 1
        if not found_paths:
            missing += 1
            status = "NO_EN_REPOSITORIO"
        elif expected_exists:
            status = "DUPLICADO_O_DESPLAZADO"
            if source_exists:
                movable += 1
        else:
            status = "EN_OTRA_RUTA"
            if source_exists:
                movable += 1

        rows.append(
            {
                "dig_id_tramite": item.dig_id_tramite,
                "dig_tramite": item.dig_tramite,
                "dig_anio": item.dig_anio,
                "dig_expediente": item.dig_expediente,
                "fe_pla_aniomes": item.fe_pla_aniomes,
                "dig_area_dep": item.dig_area_dep,
                "tramite_esperado": expected_tramite,
                "expected_path": str(expected_path),
                "found_paths": [str(p) for p in found_paths],
                "source_path": str(source_path) if source_path else "",
                "target_path": str(expected_path),
                "expected_exists": expected_exists,
                "source_exists": source_exists,
                "found_count": len(found_paths),
                "status": status,
                "movable": source_exists and str(source_path) != str(expected_path),
                "reason": "source_missing" if not source_exists else "",
                "candidate": source_exists and str(source_path) != str(expected_path),
            }
        )

    total = len(items)
    return {
        "aniomes": aniomes,
        "year": f"{year:04d}",
        "month": month_prefix,
        "rows": rows,
        "summary": {
            "total": total,
            "matched": matched,
            "discrepancies": discrepancies,
            "movable": movable,
            "missing": missing,
            "match_pct": round((matched / total) * 100, 1) if total else 0.0,
            "discrepancy_pct": round((discrepancies / total) * 100, 1) if total else 0.0,
            "movable_pct": round((movable / total) * 100, 1) if total else 0.0,
            "nonmovable_pct": round(((discrepancies - movable) / total) * 100, 1) if total else 0.0,
        },
    }


def _validation_rows(preview: dict) -> list[dict]:
    rows = preview.get("rows") or []
    return [row for row in rows if row.get("movable")]


def _run_validation_batch(preview: dict, *, dry_run: bool) -> dict:
    rows = _validation_rows(preview)
    results = []
    for row in rows:
        payload = {
            "dig_tramite": row.get("dig_tramite"),
            "dig_id_tramite": row.get("dig_id_tramite"),
            "source_path": row.get("source_path"),
            "target_path": row.get("target_path"),
        }
        if not payload["source_path"]:
            continue
        results.append(_run_rsync_job(payload, dry_run=dry_run))

    succeeded = sum(1 for row in results if row.get("ok"))
    failed = len(results) - succeeded
    return {
        "mode": "dry_run" if dry_run else "execute",
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


def _path_validation_job_key() -> str:
    return "path_validation_job_id"


def _path_validation_job_report(
    *,
    aniomes: str,
    year: str,
    month: str,
    total: int,
    matched: int,
    discrepancies: int,
    movable: int,
    missing: int,
    rows: list[dict],
) -> dict:
    return {
        "aniomes": aniomes,
        "year": year,
        "month": month,
        "rows": rows,
        "summary": {
            "total": total,
            "matched": matched,
            "discrepancies": discrepancies,
            "movable": movable,
            "missing": missing,
            "match_pct": round((matched / total) * 100, 1) if total else 0.0,
            "discrepancy_pct": round((discrepancies / total) * 100, 1) if total else 0.0,
            "movable_pct": round((movable / total) * 100, 1) if total else 0.0,
            "nonmovable_pct": round(((discrepancies - movable) / total) * 100, 1) if total else 0.0,
        },
    }


def _path_validation_signature(rows: list[dict]) -> str:
    parts = []
    for row in sorted(
        rows,
        key=lambda item: (str(item.get("source_path") or ""), str(item.get("target_path") or "")),
    ):
        parts.append(f"{row.get('source_path')}->{row.get('target_path')}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _path_validation_worker(job_id: str, aniomes: str) -> None:
    try:
        items = fetch_items_by_fe_pla_aniomes(aniomes)
        year = int(aniomes[:4])
        month = int(aniomes[4:])
        total = len(items)
        matched = 0
        discrepancies = 0
        movable = 0
        missing = 0
        rows: list[dict] = []

        update_job(
            job_id,
            status="running",
            started_at=_now_ecuador_iso(),
            last_message="Iniciando revisión",
            progress={
                "files_done": 0,
                "files_total": total,
                "bytes_done": 0,
                "bytes_total": total,
                "folder": "",
                "file": "",
            },
            report=_path_validation_job_report(
                aniomes=aniomes,
                year=f"{year:04d}",
                month=f"{month:02d}",
                total=total,
                matched=0,
                discrepancies=0,
                movable=0,
                missing=0,
                rows=[],
            ),
        )

        for idx, item in enumerate(items, start=1):
            folder_name = _folder_name_for_item(item)
            year_text = str(item.dig_anio or year).strip() or f"{year:04d}"
            expediente = str(item.dig_expediente or "").strip()
            expected_tramite = _expected_tramite_name(item)
            expected_path = (Settings.LOCAL_REPO_ROOT / year_text / expediente / expected_tramite).resolve()
            found_paths = _repo_match_paths(year_text, item.dig_expediente, folder_name)
            expected_exists = expected_path in found_paths
            wrong_paths = [p for p in found_paths if p != expected_path]
            source_path = wrong_paths[0] if wrong_paths else None
            source_exists = source_path is not None and source_path.is_dir()

            if expected_exists and len(found_paths) == 1:
                matched += 1
            else:
                discrepancies += 1
                if not found_paths:
                    missing += 1
                    status = "NO_EN_REPOSITORIO"
                elif expected_exists:
                    status = "DUPLICADO_O_DESPLAZADO"
                    if source_exists:
                        movable += 1
                else:
                    status = "EN_OTRA_RUTA"
                    if source_exists:
                        movable += 1

                rows.append(
                    {
                        "dig_id_tramite": item.dig_id_tramite,
                        "dig_tramite": item.dig_tramite,
                        "dig_anio": item.dig_anio,
                        "dig_expediente": item.dig_expediente,
                        "fe_pla_aniomes": item.fe_pla_aniomes,
                        "dig_area_dep": item.dig_area_dep,
                        "tramite_esperado": expected_tramite,
                        "expected_path": str(expected_path),
                        "found_paths": [str(p) for p in found_paths],
                        "source_path": str(source_path) if source_path else "",
                        "target_path": str(expected_path),
                        "expected_exists": expected_exists,
                        "source_exists": source_exists,
                        "found_count": len(found_paths),
                        "status": status,
                        "movable": source_exists and str(source_path) != str(expected_path),
                        "reason": "source_missing" if not source_exists else "",
                        "candidate": source_exists and str(source_path) != str(expected_path),
                    }
                )

            report = _path_validation_job_report(
                aniomes=aniomes,
                year=f"{year:04d}",
                month=f"{month:02d}",
                total=total,
                matched=matched,
                discrepancies=discrepancies,
                movable=movable,
                missing=missing,
                rows=list(rows),
            )
            update_job(
                job_id,
                progress={
                    "files_done": idx,
                    "files_total": total,
                    "bytes_done": idx,
                    "bytes_total": total,
                    "folder": folder_name,
                    "file": "",
                },
                report=report,
                last_message=f"Revisando {folder_name} ({idx}/{total})",
            )

        final_report = _path_validation_job_report(
            aniomes=aniomes,
            year=f"{year:04d}",
            month=f"{month:02d}",
            total=total,
            matched=matched,
            discrepancies=discrepancies,
            movable=movable,
            missing=missing,
            rows=rows,
        )
        final_report["validation_signature"] = _path_validation_signature(rows)

        update_job(
            job_id,
            status="finished",
            finished_at=_now_ecuador_iso(),
            report=final_report,
            progress={
                "files_done": total,
                "files_total": total,
                "bytes_done": total,
                "bytes_total": total,
                "folder": "",
                "file": "",
            },
            last_message="Revisión terminada",
        )
    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            finished_at=_now_ecuador_iso(),
            error=str(exc),
            traceback=traceback.format_exc(),
            last_message="Revisión fallida",
        )


def _start_path_validation_job(aniomes: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    update_job(
        job_id,
        job_id_value=job_id,
        job_type="path_validation",
        phase="dry_run",
        aniomes=aniomes,
        status="queued",
        created_at=_now_ecuador_iso(),
        started_at=None,
        finished_at=None,
        progress={"files_done": 0, "files_total": 0, "bytes_done": 0, "bytes_total": 0, "folder": "", "file": ""},
        log_lines=[],
        last_message="Trabajo en cola",
        report={},
    )

    def _worker():
        _path_validation_worker(job_id, aniomes)

    threading.Thread(target=_worker, daemon=True).start()
    return job_id


def _build_aniomes_candidates(
    aniomes: str,
    area_dep_filter: str | None = None,
    folder_prefix: str = "HSP",
) -> dict:
    area_dep = str(area_dep_filter or "").strip()
    prefix = str(folder_prefix or "HSP").strip().upper() or "HSP"
    if area_dep:
        items = fetch_items_by_fe_pla_aniomes_and_area_dep(aniomes, area_dep)
    else:
        items = fetch_items_by_fe_pla_aniomes(aniomes)
    year = int(aniomes[:4])
    month = int(aniomes[4:])
    target_hsp = _folder_code(prefix, month)
    source_year, source_month = _previous_month(year, month)
    source_hsp = _folder_code(prefix, source_month)

    if not items:
        return {
            "aniomes": aniomes,
            "year": aniomes[:4],
            "month": aniomes[4:],
            "target_hsp": target_hsp,
            "source_hsp": source_hsp,
            "source_year": str(source_year),
            "folder_prefix": prefix,
            "items": [],
            "area_dep_filter": area_dep or None,
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
            status = f"NO_EN_{source_hsp}"

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
        "folder_prefix": prefix,
        "area_dep_filter": area_dep or None,
        "items": items,
        "rows": rows,
        "summary": {
            "total": len(rows),
            "candidates": candidates,
            "already_in_target": already_in_target,
            "missing_both": missing_both,
        },
    }


def _sync_cases_session_key(screen_key: str) -> str:
    return f"sync_cases_dry_run_{screen_key}"


def _handle_sync_cases(
    screen_key: str,
    screen_title: str,
    area_dep_filter: str | None = None,
    folder_prefix: str = "HSP",
):
    preview = None
    last_aniomes = ""
    action = "preview"
    session_key = _sync_cases_session_key(screen_key)

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
                screen_title=screen_title,
            )

        try:
            preview = _build_aniomes_candidates(
                last_aniomes,
                area_dep_filter=area_dep_filter,
                folder_prefix=folder_prefix,
            )
            preview["sync_signature"] = _sync_candidate_signature(preview)

            if int(preview.get("summary", {}).get("total") or 0) == 0:
                flash(
                    "No se encontraron registros Oracle para ese ANIOMES o no tenían carpeta válida.",
                    "error",
                )
            else:
                if action == "dry_run":
                    preview["sync_run"] = _run_sync_batch(preview, dry_run=True)
                    session[session_key] = {
                        "aniomes": last_aniomes,
                        "signature": preview["sync_signature"],
                    }
                    flash(
                        "Dry run completado. Revisa el detalle antes de ejecutar.",
                        "ok",
                    )
                elif action == "execute":
                    token = session.get(session_key) or {}
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
                        session.pop(session_key, None)
                        flash(
                            "Ejecución finalizada. Revisa el resumen y los resultados por carpeta.",
                            "ok" if preview["sync_run"]["failed"] == 0 else "error",
                        )
        except Exception as exc:
            flash(f"Error consultando Oracle o el repositorio local: {exc}", "error")

    can_execute = False
    if preview:
        token = session.get(session_key) or {}
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
        screen_title=screen_title,
        area_dep_filter=area_dep_filter,
        folder_prefix=folder_prefix,
    )


def _handle_path_validation(screen_key: str, screen_title: str):
    preview = None
    last_aniomes = ""
    active_job = None
    auto_refresh = False
    session_key = _path_validation_job_key()
    job_id = session.get(session_key)
    job = get_job(str(job_id)) if job_id else None

    if job:
        active_job = dict(job)
        preview = dict(job.get("report") or {}) or None
        last_aniomes = str(job.get("aniomes") or "")
        auto_refresh = active_job.get("status") in {"queued", "waiting", "running"}

    if request.method == "POST":
        action = (request.form.get("action") or "preview").strip()
        last_aniomes = _extract_aniomes(request.form) or last_aniomes

        if action == "dry_run":
            if not _validate_aniomes(last_aniomes):
                flash("ANIOMES debe tener 6 dígitos, por ejemplo 202604.", "error")
            else:
                job_id = _start_path_validation_job(last_aniomes)
                session[session_key] = job_id
                flash("Revisión iniciada. La pantalla se actualizará sola mientras avanza.", "ok")
                return redirect(url_for("path_validation"))

        if action == "execute":
            if not job or job.get("status") != "finished" or not job.get("report"):
                flash("Primero debes completar la revisión en la misma sesión.", "error")
            else:
                preview = dict(job.get("report") or {})
                preview["sync_run"] = _run_validation_batch(preview, dry_run=False)
                flash(
                    "Ejecución finalizada. Revisa el resumen y las rutas procesadas.",
                    "ok" if preview["sync_run"]["failed"] == 0 else "error",
                )

    can_execute = bool(
        job
        and job.get("status") == "finished"
        and (job.get("report") or {}).get("validation_signature")
    )

    return render_template(
        "path_validation.html",
        defaults=Settings,
        preview=preview,
        last_aniomes=last_aniomes,
        username=session.get("username"),
        can_execute=can_execute,
        screen_title=screen_title,
        active_job=active_job,
        auto_refresh=auto_refresh,
        progress_pct=_progress_pct(active_job),
        duration_text=_duration_text,
    )


def _job_report_file(job_id: str, report_key: str) -> Path:
    job = get_job(job_id)
    if job is None:
        abort(404)

    report = job.get("report") or {}
    file_path = Path(str(report.get(report_key) or "")).expanduser().resolve()

    if not file_path.exists() or not file_path.is_file():
        abort(404)

    return file_path


def _validation_report_csv(job_id: str) -> tuple[io.BytesIO, str]:
    job = get_job(job_id)
    if job is None:
        abort(404)

    report = job.get("report") or {}
    rows = report.get("rows") or []
    buffer = io.StringIO()
    fieldnames = [
        "dig_id_tramite",
        "dig_tramite",
        "dig_anio",
        "dig_expediente",
        "fe_pla_aniomes",
        "dig_area_dep",
        "tramite_esperado",
        "status",
        "expected_path",
        "source_path",
        "target_path",
        "found_paths",
        "movable",
        "reason",
    ]

    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "dig_id_tramite": row.get("dig_id_tramite"),
                "dig_tramite": row.get("dig_tramite"),
                "dig_anio": row.get("dig_anio"),
                "dig_expediente": row.get("dig_expediente"),
                "fe_pla_aniomes": row.get("fe_pla_aniomes"),
                "dig_area_dep": row.get("dig_area_dep"),
                "tramite_esperado": row.get("tramite_esperado"),
                "status": row.get("status"),
                "expected_path": row.get("expected_path"),
                "source_path": row.get("source_path"),
                "target_path": row.get("target_path"),
                "found_paths": " | ".join(row.get("found_paths") or []),
                "movable": row.get("movable"),
                "reason": row.get("reason"),
            }
        )

    payload = buffer.getvalue().encode("utf-8")
    data = io.BytesIO(payload)
    data.seek(0)
    return data, f"validation_{job_id}.csv"


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
        format_datetime_local=_format_datetime_local,
    )


@app.get("/admin")
@login_required
def admin():
    root = Settings.DOWNLOAD_OUTPUT_ROOT
    pdfs = []
    zips = []
    total_size = 0
    download_history = _load_download_history(root)

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
        download_history=download_history,
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


@app.get("/admin/download-output/<path:relative_path>")
@login_required
def admin_download_output(relative_path: str):
    target = _safe_output_path(relative_path)
    return send_file(
        str(target),
        as_attachment=True,
        download_name=target.name,
    )


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
    return _handle_sync_cases(
        "hospitalizacion", "HOSPITALIZACION CASOS", area_dep_filter="HSP"
    )


@app.route("/urgencias-casos", methods=["GET", "POST"])
@login_required
def urgencias_cases():
    return _handle_sync_cases(
        "urgencias",
        "URGENCIAS CASOS",
        area_dep_filter="URG",
        folder_prefix="URG",
    )


@app.route("/path-validation", methods=["GET", "POST"])
@login_required
def path_validation():
    return _handle_path_validation("path_validation", "VALIDACION DE RUTAS")


@app.get("/jobs/<job_id>/download-validation-csv")
@login_required
def job_download_validation_csv(job_id: str):
    data, filename = _validation_report_csv(job_id)
    return send_file(
        data,
        as_attachment=True,
        download_name=filename,
        mimetype="text/csv",
    )


@app.post("/preview")
@login_required
def preview():
    search_mode, raw = _extract_search(request.form)

    if not _validate_search_value(search_mode, raw):
        flash(_search_value_error(search_mode), "error")
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
    source_mode = (request.form.get("source_mode") or "local").strip().lower()
    if source_mode not in {"local", "sftp"}:
        source_mode = "local"

    if not _validate_search_value(search_mode, raw):
        flash(_search_value_error(search_mode), "error")
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
        str(preview_data.get("search_mode") or search_mode),
        raw,
        preview_data,
        source_mode=source_mode,
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
        format_datetime_local=_format_datetime_local,
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
