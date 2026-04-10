from __future__ import annotations

import json
import posixpath
import stat
import shutil
import time
import zipfile
from pathlib import Path

import paramiko

from config import Settings
from oracle_client import FolderItem, fetch_items_by_dig_id_tramite


def _safe_name(value: object) -> str:
    s = str(value or "").strip()
    s = s.replace("/", "_").replace("\\", "_").replace("\x00", "")
    return s or "_"


def _remote_join(base: str, rel_path: str) -> str:
    base = (base or "").replace("\\", "/").strip()
    rel_path = (rel_path or "").replace("\\", "/").strip().strip("/")
    if not base or base == ".":
        return rel_path or "."
    return posixpath.normpath(posixpath.join(base, rel_path))


def _candidate_bases(requested: str) -> list[str]:
    out: list[str] = []
    for item in [requested.strip(), requested.strip().lstrip("/"), "/repositorio", "repositorio", ".", ""]:
        if item not in out:
            out.append(item)
    return out


def _is_dir(sftp: paramiko.SFTPClient, remote_path: str) -> bool:
    try:
        return stat.S_ISDIR(sftp.stat(remote_path).st_mode)
    except Exception:
        return False


def _pick_remote_base(sftp: paramiko.SFTPClient, requested_base: str, items: list[FolderItem]):
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


def _download_recursive(sftp, remote_dir: str, local_dir: Path, *, progress_cb=None, counters=None, current_folder: str = ""):
    local_dir.mkdir(parents=True, exist_ok=True)
    total_files = 0
    total_bytes = 0
    for entry in sorted(sftp.listdir_attr(remote_dir), key=lambda x: x.filename.lower()):
        if entry.filename in {".", ".."}:
            continue
        remote_child = posixpath.join(remote_dir, entry.filename)
        local_child = local_dir / entry.filename
        if stat.S_ISDIR(entry.st_mode):
            f_count, b_count = _download_recursive(sftp, remote_child, local_child, progress_cb=progress_cb, counters=counters, current_folder=current_folder)
            total_files += f_count
            total_bytes += b_count
        else:
            sftp.get(remote_child, str(local_child))
            size = int(entry.st_size or 0)
            total_files += 1
            total_bytes += size
            if counters is not None:
                counters["files_done"] += 1
                counters["bytes_done"] += size
            if progress_cb:
                progress_cb({
                    "folder": current_folder,
                    "file": entry.filename,
                    "files_done": counters["files_done"] if counters else total_files,
                    "files_total": counters["files_total"] if counters else total_files,
                    "bytes_done": counters["bytes_done"] if counters else total_bytes,
                    "bytes_total": counters["bytes_total"] if counters else total_bytes,
                })
    return total_files, total_bytes


def _zip_dir(src_dir: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(src_dir))


def cleanup_job_artifacts(job_root: str | Path | None) -> None:
    if not job_root:
        return
    path = Path(job_root)
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _connect_sftp_with_retry(*, attempts: int = 3, delay_seconds: float = 2.0, timeout: int = 30, log_cb=None):
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
    raise RuntimeError(f"No se pudo conectar a SFTP tras {attempts} intento(s): {last_exc}")


def sftp_diagnostics() -> dict:
    started = time.perf_counter()
    result = {
        "host": Settings.SFTP_HOST,
        "port": Settings.SFTP_PORT,
        "user": Settings.SFTP_USER,
        "base": Settings.SFTP_REMOTE_BASE,
    }
    try:
        ssh, sftp = _connect_sftp_with_retry(attempts=2, delay_seconds=1.0, timeout=10)
        try:
            result.update({"ok": True, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "cwd": sftp.getcwd()})
        finally:
            sftp.close()
    except Exception as exc:
        result.update({"ok": False, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "error": str(exc)})
    finally:
        try:
            ssh.close()
        except Exception:
            pass
    return result


def run_download(dig_id_tramite: int, *, progress_cb=None, log_cb=None) -> dict:
    items = fetch_items_by_dig_id_tramite(dig_id_tramite)
    if not items:
        raise RuntimeError(f"No existen filas Oracle para DIG_ID_TRAMITE={dig_id_tramite}")

    out_root = Settings.DOWNLOAD_OUTPUT_ROOT
    out_root.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    job_root = (out_root / f"tramite_{dig_id_tramite}_{stamp}").resolve()
    data_root = (job_root / str(dig_id_tramite)).resolve()
    zip_path = (out_root / f"tramite_{dig_id_tramite}_{stamp}.zip").resolve()
    manifest_path = (job_root / "manifest.json").resolve()

    data_root.mkdir(parents=True, exist_ok=True)
    sftp = None
    try:
        if log_cb:
            log_cb(f"Conectando a SFTP {Settings.SFTP_HOST}:{Settings.SFTP_PORT} ...")
        ssh, sftp = _connect_sftp_with_retry(attempts=3, delay_seconds=2.0, timeout=30, log_cb=log_cb)
        detected_base, probes = _pick_remote_base(sftp, Settings.SFTP_REMOTE_BASE, items)
        if log_cb:
            log_cb(f"Base remota detectada: {detected_base!r}")
        planned = []
        missing = []
        files_total = 0
        bytes_total = 0
        for item in items:
            remote_dir = _remote_join(detected_base, item.remote_rel_path)
            if _is_dir(sftp, remote_dir):
                f_count, b_count = _count_recursive(sftp, remote_dir)
                planned.append((item, remote_dir, f_count, b_count))
                files_total += f_count
                bytes_total += b_count
            else:
                missing.append({"dig_tramite": item.dig_tramite, "remote_dir": remote_dir, "reason": "remote_folder_not_found"})
        counters = {"files_done": 0, "files_total": files_total, "bytes_done": 0, "bytes_total": bytes_total}
        downloaded = []
        for item, remote_dir, _fc, _bc in planned:
            local_dir = data_root / _safe_name(item.dig_tramite)
            if log_cb:
                log_cb(f"Descargando {item.dig_tramite} desde {remote_dir}")
            f_count, b_count = _download_recursive(sftp, remote_dir, local_dir, progress_cb=progress_cb, counters=counters, current_folder=item.dig_tramite)
            downloaded.append({"dig_tramite": item.dig_tramite, "dig_anio": item.dig_anio, "dig_expediente": item.dig_expediente, "fe_pla_aniomes": item.fe_pla_aniomes, "dig_area_dep": item.dig_area_dep, "remote_dir": remote_dir, "local_dir": str(local_dir), "files": f_count, "bytes": b_count})
        payload = {"dig_id_tramite": int(dig_id_tramite), "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "remote_base_requested": Settings.SFTP_REMOTE_BASE, "remote_base_detected": detected_base, "remote_base_probes": probes, "total_files": files_total, "total_bytes": bytes_total, "downloaded": downloaded, "missing": missing, "job_root": str(job_root), "data_root": str(data_root)}
        job_root.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
            ssh.close()
        except Exception:
            pass
