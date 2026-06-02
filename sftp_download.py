from __future__ import annotations

import csv
import json
import posixpath
import shutil
import stat
import time
import subprocess
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import paramiko

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

from config import Settings
from oracle_client import (
    FolderItem,
    fetch_items_by_dig_id_tramite,
    fetch_items_by_dig_tramite,
)


def _safe_name(value: object) -> str:
    s = str(value or "").strip()
    s = s.replace("/", "_").replace("\\", "_").replace("\x00", "")
    return s or "_"


def _ecuador_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo("America/Guayaquil")
        except Exception:
            pass
    return timezone(timedelta(hours=-5))


def _should_download_file(filename: str) -> bool:
    lower = str(filename or "").lower()
    return lower.endswith(".pdf")


def _remote_join(base: str, rel_path: str) -> str:
    base = (base or "").replace("\\", "/").strip()
    rel_path = (rel_path or "").replace("\\", "/").strip().strip("/")
    if not base or base == ".":
        return rel_path or "."
    return posixpath.normpath(posixpath.join(base, rel_path))


def _candidate_bases(requested: str) -> list[str]:
    out: list[str] = []
    for item in [
        requested.strip(),
        requested.strip().lstrip("/"),
        "/repositorio",
        "repositorio",
        ".",
        "",
    ]:
        if item not in out:
            out.append(item)
    return out


def _is_dir(sftp: paramiko.SFTPClient, remote_path: str) -> bool:
    try:
        return stat.S_ISDIR(sftp.stat(remote_path).st_mode)
    except Exception:
        return False


def _pick_remote_base(
    sftp: paramiko.SFTPClient, requested_base: str, items: list[FolderItem]
):
    probes: list[dict] = []
    best_base = ""
    best_hits = -1
    for base in _candidate_bases(requested_base):
        hits = 0
        misses = 0
        for item in items:
            if _is_dir(sftp, _remote_join(base, item.remote_rel_path)):
                hits += 1
            else:
                misses += 1
        probes.append({"base": base, "hits": hits, "misses": misses})
        if hits > best_hits:
            best_hits = hits
            best_base = base
    return best_base, probes


def _count_recursive(sftp: paramiko.SFTPClient, remote_dir: str) -> tuple[int, int]:
    files = 0
    total_bytes = 0
    for entry in sftp.listdir_attr(remote_dir):
        if entry.filename in {".", ".."}:
            continue
        child = posixpath.join(remote_dir, entry.filename)
        if stat.S_ISDIR(entry.st_mode):
            f_count, b_count = _count_recursive(sftp, child)
            files += f_count
            total_bytes += b_count
        else:
            files += 1
            total_bytes += int(entry.st_size or 0)
    return files, total_bytes


def _count_local_recursive(local_dir: Path) -> tuple[int, int]:
    files = 0
    total_bytes = 0
    for entry in sorted(local_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir():
            f_count, b_count = _count_local_recursive(entry)
            files += f_count
            total_bytes += b_count
        elif entry.is_file() and _should_download_file(entry.name):
            files += 1
            try:
                total_bytes += int(entry.stat().st_size or 0)
            except FileNotFoundError:
                pass
    return files, total_bytes


def _download_recursive(
    sftp,
    remote_dir: str,
    local_dir: Path,
    *,
    progress_cb=None,
    counters=None,
    current_folder: str = "",
):
    local_dir.mkdir(parents=True, exist_ok=True)
    total_files = 0
    total_bytes = 0

    for entry in sorted(
        sftp.listdir_attr(remote_dir), key=lambda x: x.filename.lower()
    ):
        if entry.filename in {".", ".."}:
            continue

        remote_child = posixpath.join(remote_dir, entry.filename)
        local_child = local_dir / entry.filename

        if stat.S_ISDIR(entry.st_mode):
            f_count, b_count = _download_recursive(
                sftp,
                remote_child,
                local_child,
                progress_cb=progress_cb,
                counters=counters,
                current_folder=current_folder,
            )
            total_files += f_count
            total_bytes += b_count
        else:
            if not _should_download_file(entry.filename):
                continue
            sftp.get(remote_child, str(local_child))
            size = int(entry.st_size or 0)
            total_files += 1
            total_bytes += size

            if counters is not None:
                counters["files_done"] += 1
                counters["bytes_done"] += size

            if progress_cb:
                progress_cb(
                    {
                        "folder": current_folder,
                        "file": entry.filename,
                        "files_done": counters["files_done"]
                        if counters
                        else total_files,
                        "files_total": counters["files_total"]
                        if counters
                        else total_files,
                        "bytes_done": counters["bytes_done"]
                        if counters
                        else total_bytes,
                        "bytes_total": counters["bytes_total"]
                        if counters
                        else total_bytes,
                    }
                )

    return total_files, total_bytes


def _copy_local_recursive(
    source_dir: Path,
    local_dir: Path,
    *,
    progress_cb=None,
    counters=None,
    current_folder: str = "",
):
    local_dir.mkdir(parents=True, exist_ok=True)
    total_files = 0
    total_bytes = 0

    for entry in sorted(source_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir():
            f_count, b_count = _copy_local_recursive(
                entry,
                local_dir / entry.name,
                progress_cb=progress_cb,
                counters=counters,
                current_folder=current_folder,
            )
            total_files += f_count
            total_bytes += b_count
        elif entry.is_file():
            if not _should_download_file(entry.name):
                continue
            target = local_dir / entry.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)
            size = int(entry.stat().st_size or 0)
            total_files += 1
            total_bytes += size

            if counters is not None:
                counters["files_done"] += 1
                counters["bytes_done"] += size

            if progress_cb:
                progress_cb(
                    {
                        "folder": current_folder,
                        "file": entry.name,
                        "files_done": counters["files_done"]
                        if counters
                        else total_files,
                        "files_total": counters["files_total"]
                        if counters
                        else total_files,
                        "bytes_done": counters["bytes_done"]
                        if counters
                        else total_bytes,
                        "bytes_total": counters["bytes_total"]
                        if counters
                        else total_bytes,
                    }
                )

    return total_files, total_bytes


# === NUEVO: incluir directorios vacíos dentro del ZIP ===
def _zip_dir(src_dir: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    all_paths = sorted(
        src_dir.rglob("*"),
        key=lambda p: (0 if p.is_dir() else 1, str(p.relative_to(src_dir)).lower()),
    )

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        seen_dirs: set[str] = set()

        for path in all_paths:
            rel = path.relative_to(src_dir).as_posix()

            if path.is_dir():
                rel_dir = rel.rstrip("/") + "/"
                if rel_dir not in seen_dirs:
                    info = zipfile.ZipInfo(rel_dir)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    zf.writestr(info, "")
                    seen_dirs.add(rel_dir)
                continue

            if path.is_file():
                if not _should_download_file(path.name):
                    continue
                parent_rel = path.parent.relative_to(src_dir).as_posix()
                if parent_rel and parent_rel != ".":
                    rel_dir = parent_rel.rstrip("/") + "/"
                    if rel_dir not in seen_dirs:
                        info = zipfile.ZipInfo(rel_dir)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        zf.writestr(info, "")
                        seen_dirs.add(rel_dir)

                zf.write(path, arcname=rel)


def _compress_pdf_with_ghostscript(pdf_path: Path, *, log_cb=None) -> bool:
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        return False

    tmp_path = pdf_path.with_suffix(".gs.tmp.pdf")
    cmd = [
        "gs",
        "-q",
        "-dNOPAUSE",
        "-dBATCH",
        "-dSAFER",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dPDFSETTINGS=/ebook",
        f"-sOutputFile={str(tmp_path)}",
        str(pdf_path),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not tmp_path.exists():
            if log_cb:
                log_cb(f"Ghostscript falló para {pdf_path.name}: {proc.stderr.strip() or proc.stdout.strip() or 'sin detalle'}")
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return False

        try:
            original_size = pdf_path.stat().st_size
            new_size = tmp_path.stat().st_size
        except Exception:
            original_size = 0
            new_size = 0

        if new_size > 0 and (original_size == 0 or new_size < original_size):
            tmp_path.replace(pdf_path)
            if log_cb:
                log_cb(f"PDF optimizado con Ghostscript: {pdf_path.name} ({original_size} -> {new_size} bytes)")
            return True

        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False
    except Exception as exc:
        if log_cb:
            log_cb(f"Ghostscript no pudo procesar {pdf_path.name}: {exc}")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _compress_pdfs_recursive(root: Path, *, log_cb=None) -> dict:
    summary = {"checked": 0, "optimized": 0, "skipped": 0}
    if not root.exists():
        return summary

    for pdf_path in sorted(root.rglob("*.pdf")):
        if not pdf_path.is_file():
            continue
        summary["checked"] += 1
        ok = _compress_pdf_with_ghostscript(pdf_path, log_cb=log_cb)
        if ok:
            summary["optimized"] += 1
        else:
            summary["skipped"] += 1
    return summary


def cleanup_job_artifacts(job_root: str | Path | None) -> None:
    if not job_root:
        return
    path = Path(job_root)
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _connect_sftp_with_retry(
    *, attempts: int = 3, delay_seconds: float = 2.0, timeout: int = 30, log_cb=None
):
    last_exc = None
    for attempt in range(1, attempts + 1):
        if log_cb:
            log_cb(f"Intento de conexión SFTP {attempt}/{attempts} ...")

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            ssh.connect(
                hostname=Settings.SFTP_HOST,
                port=Settings.SFTP_PORT,
                username=Settings.SFTP_USER,
                password=Settings.SFTP_PASSWORD,
                timeout=timeout,
                banner_timeout=timeout,
                auth_timeout=timeout,
                look_for_keys=False,
                allow_agent=False,
            )
            return ssh, ssh.open_sftp()
        except Exception as exc:
            last_exc = exc
            if log_cb:
                log_cb(f"Fallo en conexión SFTP {attempt}/{attempts}: {exc}")
            try:
                ssh.close()
            except Exception:
                pass

            if attempt < attempts:
                if log_cb:
                    log_cb(f"Reintentando en {delay_seconds} s ...")
                time.sleep(delay_seconds)

    raise RuntimeError(
        f"No se pudo conectar a SFTP tras {attempts} intento(s): {last_exc}"
    )


def sftp_diagnostics() -> dict:
    started = time.perf_counter()
    result = {
        "host": Settings.SFTP_HOST,
        "port": Settings.SFTP_PORT,
        "user": Settings.SFTP_USER,
        "base": Settings.SFTP_REMOTE_BASE,
    }

    ssh = None
    try:
        ssh, sftp = _connect_sftp_with_retry(attempts=2, delay_seconds=1.0, timeout=10)
        try:
            result.update(
                {
                    "ok": True,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                    "cwd": sftp.getcwd(),
                }
            )
        finally:
            sftp.close()
    except Exception as exc:
        result.update(
            {
                "ok": False,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": str(exc),
            }
        )
    finally:
        try:
            if ssh is not None:
                ssh.close()
        except Exception:
            pass

    return result


# === NUEVO: helper para no romper la estructura actual ===
def _local_dir_for_item(mode: str, data_root: Path, item: FolderItem) -> Path:
    if mode == "dig_tramite":
        return data_root
    return data_root / _safe_name(item.dig_tramite)


# === NUEVO: CSV amigable para Excel ===
def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def run_download(
    search_mode: str,
    raw_value: str | int,
    *,
    progress_cb=None,
    log_cb=None,
    source_mode: str = "sftp",
) -> dict:
    mode = (search_mode or "dig_id_tramite").strip()
    value = str(raw_value).strip()
    mode_source = (source_mode or "sftp").strip().lower()

    if mode == "dig_tramite":
        items = fetch_items_by_dig_tramite(value)
        search_label = "DIG_TRAMITE"
    else:
        mode = "dig_id_tramite"
        items = fetch_items_by_dig_id_tramite(int(value))
        search_label = "DIG_ID_TRAMITE"

    if not items:
        raise RuntimeError(f"No existen filas Oracle para {search_label}={value}")

    out_root = Settings.DOWNLOAD_OUTPUT_ROOT
    out_root.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(_ecuador_tz()).strftime("%Y%m%d_%H%M%S")
    safe_value = _safe_name(value)
    job_root = (out_root / f"{mode}_{safe_value}_{stamp}").resolve()
    zip_path = (out_root / f"{mode}_{safe_value}_{stamp}.zip").resolve()
    manifest_path = (job_root / "manifest.json").resolve()

    data_root = (
        (job_root / safe_value).resolve()
        if mode == "dig_id_tramite"
        else job_root.resolve()
    )
    data_root.mkdir(parents=True, exist_ok=True)

    audit_csv_path = (data_root / "_reporte_descarga.csv").resolve()
    missing_csv_path = (data_root / "_reporte_faltantes.csv").resolve()

    csv_fieldnames = [
        "search_mode",
        "search_value",
        "dig_id_tramite",
        "dig_tramite",
        "dig_anio",
        "dig_expediente",
        "fe_pla_aniomes",
        "dig_area_dep",
        "remote_rel_path",
        "remote_dir",
        "local_dir",
        "status_sftp",
        "reason",
        "folder_files",
        "folder_bytes",
    ]

    audit_rows: list[dict] = []
    missing_rows: list[dict] = []

    ssh = None
    sftp = None
    try:
        planned: list[tuple[FolderItem, str, Path, int, int]] = []
        missing: list[dict] = []
        files_total = 0
        bytes_total = 0

        if mode_source == "local":
            detected_base = str(Settings.LOCAL_REPO_ROOT)
            probes = [{"base": detected_base, "hits": len(items), "misses": 0}]
            if log_cb:
                log_cb(f"Usando repositorio local: {detected_base}")

            for item in items:
                source_dir = (Settings.LOCAL_REPO_ROOT / item.remote_rel_path).resolve()
                local_dir = _local_dir_for_item(mode, data_root, item)

                if source_dir.is_dir():
                    f_count, b_count = _count_local_recursive(source_dir)
                    planned.append((item, str(source_dir), local_dir, f_count, b_count))
                    files_total += f_count
                    bytes_total += b_count

                    audit_rows.append(
                        {
                            "search_mode": mode,
                            "search_value": value,
                            "dig_id_tramite": item.dig_id_tramite,
                            "dig_tramite": item.dig_tramite,
                            "dig_anio": item.dig_anio,
                            "dig_expediente": item.dig_expediente,
                            "fe_pla_aniomes": item.fe_pla_aniomes,
                            "dig_area_dep": item.dig_area_dep,
                            "remote_rel_path": item.remote_rel_path,
                            "remote_dir": str(source_dir),
                            "local_dir": str(local_dir),
                            "status_sftp": "FOUND",
                            "reason": "",
                            "folder_files": f_count,
                            "folder_bytes": b_count,
                        }
                    )
                else:
                    if mode == "dig_id_tramite":
                        local_dir.mkdir(parents=True, exist_ok=True)

                    missing_item = {
                        "dig_id_tramite": item.dig_id_tramite,
                        "dig_tramite": item.dig_tramite,
                        "dig_anio": item.dig_anio,
                        "dig_expediente": item.dig_expediente,
                        "fe_pla_aniomes": item.fe_pla_aniomes,
                        "dig_area_dep": item.dig_area_dep,
                        "remote_rel_path": item.remote_rel_path,
                        "remote_dir": str(source_dir),
                        "local_dir": str(local_dir),
                        "reason": "local_folder_not_found",
                        "files": 0,
                        "bytes": 0,
                    }
                    missing.append(missing_item)

                    row = {
                        "search_mode": mode,
                        "search_value": value,
                        "dig_id_tramite": item.dig_id_tramite,
                        "dig_tramite": item.dig_tramite,
                        "dig_anio": item.dig_anio,
                        "dig_expediente": item.dig_expediente,
                        "fe_pla_aniomes": item.fe_pla_aniomes,
                        "dig_area_dep": item.dig_area_dep,
                        "remote_rel_path": item.remote_rel_path,
                        "remote_dir": str(source_dir),
                        "local_dir": str(local_dir),
                        "status_sftp": "MISSING",
                        "reason": "local_folder_not_found",
                        "folder_files": 0,
                        "folder_bytes": 0,
                    }
                    audit_rows.append(row)
                    missing_rows.append(row)

                    if log_cb:
                        log_cb(f"FALTANTE LOCAL: {item.dig_tramite} -> {source_dir}")

            counters = {
                "files_done": 0,
                "files_total": files_total,
                "bytes_done": 0,
                "bytes_total": bytes_total,
            }

            downloaded: list[dict] = []

            for item, source_dir, local_dir, _fc, _bc in planned:
                if log_cb:
                    log_cb(f"Copiando {item.dig_tramite} desde {source_dir}")

                f_count, b_count = _copy_local_recursive(
                    Path(source_dir),
                    local_dir,
                    progress_cb=progress_cb,
                    counters=counters,
                    current_folder=item.dig_tramite,
                )

                downloaded.append(
                    {
                        "dig_id_tramite": item.dig_id_tramite,
                        "dig_tramite": item.dig_tramite,
                        "dig_anio": item.dig_anio,
                        "dig_expediente": item.dig_expediente,
                        "fe_pla_aniomes": item.fe_pla_aniomes,
                        "dig_area_dep": item.dig_area_dep,
                        "remote_rel_path": item.remote_rel_path,
                        "remote_dir": str(source_dir),
                        "local_dir": str(local_dir),
                        "files": f_count,
                        "bytes": b_count,
                    }
                )
        else:
            if log_cb:
                log_cb(f"Conectando a SFTP {Settings.SFTP_HOST}:{Settings.SFTP_PORT} ...")

            ssh, sftp = _connect_sftp_with_retry(
                attempts=3,
                delay_seconds=2.0,
                timeout=30,
                log_cb=log_cb,
            )

            detected_base, probes = _pick_remote_base(
                sftp, Settings.SFTP_REMOTE_BASE, items
            )

            if log_cb:
                log_cb(f"Base remota detectada: {detected_base!r}")

            for item in items:
                remote_dir = _remote_join(detected_base, item.remote_rel_path)
                local_dir = _local_dir_for_item(mode, data_root, item)

                if _is_dir(sftp, remote_dir):
                    f_count, b_count = _count_recursive(sftp, remote_dir)
                    planned.append((item, remote_dir, local_dir, f_count, b_count))
                    files_total += f_count
                    bytes_total += b_count

                    audit_rows.append(
                        {
                            "search_mode": mode,
                            "search_value": value,
                            "dig_id_tramite": item.dig_id_tramite,
                            "dig_tramite": item.dig_tramite,
                            "dig_anio": item.dig_anio,
                            "dig_expediente": item.dig_expediente,
                            "fe_pla_aniomes": item.fe_pla_aniomes,
                            "dig_area_dep": item.dig_area_dep,
                            "remote_rel_path": item.remote_rel_path,
                            "remote_dir": remote_dir,
                            "local_dir": str(local_dir),
                            "status_sftp": "FOUND",
                            "reason": "",
                            "folder_files": f_count,
                            "folder_bytes": b_count,
                        }
                    )
                else:
                    if mode == "dig_id_tramite":
                        local_dir.mkdir(parents=True, exist_ok=True)

                    missing_item = {
                        "dig_id_tramite": item.dig_id_tramite,
                        "dig_tramite": item.dig_tramite,
                        "dig_anio": item.dig_anio,
                        "dig_expediente": item.dig_expediente,
                        "fe_pla_aniomes": item.fe_pla_aniomes,
                        "dig_area_dep": item.dig_area_dep,
                        "remote_rel_path": item.remote_rel_path,
                        "remote_dir": remote_dir,
                        "local_dir": str(local_dir),
                        "reason": "remote_folder_not_found",
                        "files": 0,
                        "bytes": 0,
                    }
                    missing.append(missing_item)

                    row = {
                        "search_mode": mode,
                        "search_value": value,
                        "dig_id_tramite": item.dig_id_tramite,
                        "dig_tramite": item.dig_tramite,
                        "dig_anio": item.dig_anio,
                        "dig_expediente": item.dig_expediente,
                        "fe_pla_aniomes": item.fe_pla_aniomes,
                        "dig_area_dep": item.dig_area_dep,
                        "remote_rel_path": item.remote_rel_path,
                        "remote_dir": remote_dir,
                        "local_dir": str(local_dir),
                        "status_sftp": "MISSING",
                        "reason": "remote_folder_not_found",
                        "folder_files": 0,
                        "folder_bytes": 0,
                    }
                    audit_rows.append(row)
                    missing_rows.append(row)

                    if log_cb:
                        log_cb(f"FALTANTE: {item.dig_tramite} -> {remote_dir}")

            counters = {
                "files_done": 0,
                "files_total": files_total,
                "bytes_done": 0,
                "bytes_total": bytes_total,
            }

            downloaded: list[dict] = []

            for item, remote_dir, local_dir, _fc, _bc in planned:
                if log_cb:
                    log_cb(f"Descargando {item.dig_tramite} desde {remote_dir}")

                f_count, b_count = _download_recursive(
                    sftp,
                    remote_dir,
                    local_dir,
                    progress_cb=progress_cb,
                    counters=counters,
                    current_folder=item.dig_tramite,
                )

                downloaded.append(
                    {
                        "dig_id_tramite": item.dig_id_tramite,
                        "dig_tramite": item.dig_tramite,
                        "dig_anio": item.dig_anio,
                        "dig_expediente": item.dig_expediente,
                        "fe_pla_aniomes": item.fe_pla_aniomes,
                        "dig_area_dep": item.dig_area_dep,
                        "remote_rel_path": item.remote_rel_path,
                        "remote_dir": remote_dir,
                        "local_dir": str(local_dir),
                        "files": f_count,
                        "bytes": b_count,
                    }
                )

        # === NUEVO: reportes CSV ===
        _write_csv(audit_csv_path, audit_rows, csv_fieldnames)
        _write_csv(missing_csv_path, missing_rows, csv_fieldnames)

        pdf_optimization = _compress_pdfs_recursive(data_root, log_cb=log_cb)
        if log_cb and pdf_optimization["checked"]:
            log_cb(
                f"Ghostscript revisó {pdf_optimization['checked']} PDF(s), "
                f"optimizó {pdf_optimization['optimized']}."
            )

        expected_folders = len(items)
        found_folders = len(planned)
        missing_folders = len(missing)

        payload = {
            "source_mode": mode_source,
            "search_mode": mode,
            "search_value": value,
            "generated_at": datetime.now(_ecuador_tz()).strftime("%Y-%m-%d %H:%M:%S"),
            "remote_base_requested": Settings.SFTP_REMOTE_BASE
            if mode_source == "sftp"
            else str(Settings.LOCAL_REPO_ROOT),
            "remote_base_detected": detected_base,
            "remote_base_probes": probes,
            "expected_folders": expected_folders,
            "found_folders": found_folders,
            "missing_folders": missing_folders,
            "represented_folders_in_zip": expected_folders
            if mode == "dig_id_tramite"
            else found_folders,
            "folder_quadrature_enabled": mode == "dig_id_tramite",
            "placeholder_dirs_created": missing_folders
            if mode == "dig_id_tramite"
            else 0,
            "total_files": files_total,
            "total_bytes": bytes_total,
            "pdf_optimization": pdf_optimization,
            "downloaded": downloaded,
            "missing": missing,
            "job_root": str(job_root),
            "data_root": str(data_root),
            "audit_csv_path": str(audit_csv_path),
            "missing_csv_path": str(missing_csv_path),
        }

        job_root.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        _zip_dir(data_root, zip_path)

        payload["zip_path"] = str(zip_path)
        payload["manifest_path"] = str(manifest_path)
        return payload

    finally:
        try:
            if sftp is not None:
                sftp.close()
        except Exception:
            pass

        try:
            if ssh is not None:
                ssh.close()
        except Exception:
            pass
