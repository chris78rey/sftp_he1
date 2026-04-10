Se entrega la versión **standalone** para `/home/crrb/codex_projects/sftp_he1`, usando **Oracle por JDBC + `ojdbc8.jar`**, que es coherente con el patrón que ya usa el proyecto actual: allí la conexión Oracle se hace con `jaydebeapi`, `ORACLE_JDBC_JAR` y `ORACLE_TARGETS`, armando URLs `jdbc:oracle:thin:@host:port:sid` .
También se toma como referencia que la exportación SFTP mensual existente ya evita bloquear la UI al manejar `_JOBS` y `threading.Thread`, y que el espejo SFTP publicado está montado en `/sftp-jail/lmoreno/repositorio`, por lo que `/repositorio` es una base remota lógica para arrancar .
Además, el caso funcional entregado para `DIG_ID_TRAMITE = 16356` sí justifica esta estructura, porque bajo ese id aparecen varios `DIG_TRAMITE` distintos como `5839624`, `5836208`, `5841776`, `5862211`, `5908737`, `5908738`, `5908729` y `5908726` .

---

# Parte 1 — Impacto, riesgos y preparación

## Impacto y riesgos

Esta variante **no toca** `/opt/digitalizacion_he1`.
Se desarrolla y se prueba aislada en:

```text
/home/crrb/codex_projects/sftp_he1
```

### Qué se protege

Se protege el sistema actual porque la app queda separada y no modifica la UI Flask existente ni la exportación mensual.

### Qué puede salir mal

La única precaución seria está en Docker: como los jobs se guardan **en memoria**, no conviene usar varios workers, porque cada worker tendría su propio diccionario de jobs.
Por eso en `docker-compose.yml` se deja **Gunicorn con 1 worker y varios threads**.

---

## Preparación

## Crear estructura base

```bash
mkdir -p /home/crrb/codex_projects/sftp_he1/templates
mkdir -p /home/crrb/codex_projects/sftp_he1/output
mkdir -p /home/crrb/codex_projects/sftp_he1/jdbc
cd /home/crrb/codex_projects/sftp_he1
```

El jar ya está donde corresponde:

```text
/home/crrb/codex_projects/sftp_he1/jdbc/ojdbc8.jar
```

---

# Parte 2 — Archivos completos

## 1) `requirements.txt`

```txt
Flask>=3.0,<4
python-dotenv>=1.0,<2
paramiko>=3.4,<4
jaydebeapi>=1.2,<2
JPype1>=1.5,<2
gunicorn>=22,<24
```

---

## 2) `.env.example`

```env
FLASK_SECRET_KEY=CAMBIAR_ESTA_CLAVE

# Oracle JDBC
ORACLE_USER=DIGITALIZACION
ORACLE_PASSWORD=CAMBIAR_PASSWORD
ORACLE_JDBC_JAR=/app/jdbc/ojdbc8.jar
ORACLE_TARGETS=172.16.60.20:1521:prdsgh1,172.16.60.21:1521:prdsgh2
ORACLE_OWNER=DIGITALIZACION
ORACLE_TABLE=DIGITALIZACION

# SFTP
SFTP_HOST=172.16.60.127
SFTP_PORT=2223
SFTP_USER=lmoreno
SFTP_PASSWORD=CAMBIAR_PASSWORD
SFTP_REMOTE_BASE=/repositorio

# Salida local dentro del contenedor
DOWNLOAD_OUTPUT_ROOT=/app/output

# Workaround Oracle JDBC timezone
JAVA_TOOL_OPTIONS=-Doracle.jdbc.timezoneAsRegion=false -Duser.timezone=UTC
```

Luego se copia a `.env`:

```bash
cp .env.example .env
```

---

## 3) `.dockerignore`

```dockerignore
.venv
__pycache__
*.pyc
*.pyo
*.pyd
.git
output/*
!output/.gitkeep
```

Crear el marcador:

```bash
touch /home/crrb/codex_projects/sftp_he1/output/.gitkeep
```

---

## 4) `Dockerfile`

```dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jre-headless \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN mkdir -p /app/output /app/jdbc

EXPOSE 5085

CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:5085", "app:app"]
```

---

## 5) `docker-compose.yml`

```yaml
services:
  sftp_he1:
    build: .
    container_name: sftp_he1
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "5085:5085"
    volumes:
      - ./output:/app/output
      - ./jdbc:/app/jdbc:ro
```

---

## 6) `config.py`

```python
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
```

---

## 7) `oracle_client.py`

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import jaydebeapi

from config import Settings


@dataclass(frozen=True)
class FolderItem:
    dig_id_tramite: int
    dig_tramite: str
    dig_anio: str
    dig_expediente: str
    fe_pla_aniomes: str
    dig_area_dep: str
    remote_rel_path: str

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_target(value: str) -> tuple[str, int, str]:
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"Target inválido: {value}. Se esperaba host:port:sid")
    host, port_s, sid = parts
    return host.strip(), int(port_s), sid.strip()


def _jdbc_url(host: str, port: int, sid: str) -> str:
    return f"jdbc:oracle:thin:@{host}:{port}:{sid}"


def connect_with_failover():
    if not Settings.ORACLE_USER or not Settings.ORACLE_PASSWORD:
        raise RuntimeError("Faltan ORACLE_USER y ORACLE_PASSWORD")

    jar = Path(Settings.ORACLE_JDBC_JAR).expanduser().resolve()
    if not jar.exists():
        raise RuntimeError(f"No existe el jar Oracle: {jar}")

    raw_targets = str(Settings.ORACLE_TARGETS or "").strip()
    if not raw_targets:
        raise RuntimeError("Falta ORACLE_TARGETS")

    targets: list[tuple[str, int, str]] = []
    for item in raw_targets.split(","):
        item = item.strip()
        if item:
            targets.append(_parse_target(item))

    if not targets:
        raise RuntimeError("No hay targets Oracle válidos")

    driver = "oracle.jdbc.OracleDriver"
    last_exc = None

    for host, port, sid in targets:
        url = _jdbc_url(host, port, sid)
        try:
            conn = jaydebeapi.connect(
                driver,
                url,
                [Settings.ORACLE_USER, Settings.ORACLE_PASSWORD],
                jars=[str(jar)],
            )
            return conn
        except Exception as exc:
            last_exc = exc
            continue

    raise RuntimeError(f"No se pudo conectar a Oracle. Último error: {last_exc}")


def fetch_items_by_dig_id_tramite(dig_id_tramite: int) -> list[FolderItem]:
    conn = connect_with_failover()
    try:
        cur = conn.cursor()
        sql = f"""
            SELECT DISTINCT
                   DIG_ID_TRAMITE,
                   DIG_TRAMITE,
                   TRIM(NVL(DIG_ANIO, '')) AS DIG_ANIO,
                   TRIM(NVL(DIG_EXPEDIENTE, '')) AS DIG_EXPEDIENTE,
                   TRIM(NVL(FE_PLA_ANIOMES, '')) AS FE_PLA_ANIOMES,
                   TRIM(NVL(DIG_AREA_DEP, '')) AS DIG_AREA_DEP
              FROM {Settings.ORACLE_OWNER}.{Settings.ORACLE_TABLE}
             WHERE DIG_ID_TRAMITE = ?
               AND DIG_TRAMITE IS NOT NULL
               AND NVL(TRIM(DIG_ANIO), '') <> ''
               AND NVL(TRIM(DIG_EXPEDIENTE), '') <> ''
             ORDER BY DIG_ANIO, DIG_EXPEDIENTE, DIG_TRAMITE
        """
        cur.execute(sql, [int(dig_id_tramite)])
        rows = cur.fetchall()

        out: list[FolderItem] = []
        for row in rows:
            dig_id_tramite_row = int(row[0])
            dig_tramite = str(row[1]).strip()
            dig_anio = str(row[2]).strip()
            dig_expediente = str(row[3]).strip()
            fe_pla_aniomes = str(row[4]).strip()
            dig_area_dep = str(row[5]).strip()

            out.append(
                FolderItem(
                    dig_id_tramite=dig_id_tramite_row,
                    dig_tramite=dig_tramite,
                    dig_anio=dig_anio,
                    dig_expediente=dig_expediente,
                    fe_pla_aniomes=fe_pla_aniomes,
                    dig_area_dep=dig_area_dep,
                    remote_rel_path=f"{dig_anio}/{dig_expediente}/{dig_tramite}",
                )
            )
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def build_preview(dig_id_tramite: int) -> dict:
    items = fetch_items_by_dig_id_tramite(dig_id_tramite)
    return {
        "dig_id_tramite": int(dig_id_tramite),
        "count": len(items),
        "items": [x.to_dict() for x in items],
    }
```

---

## 8) `sftp_download.py`

```python
from __future__ import annotations

import json
import posixpath
import stat
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
        attrs = sftp.stat(remote_path)
        return stat.S_ISDIR(attrs.st_mode)
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
            full = _remote_join(base, item.remote_rel_path)
            if _is_dir(sftp, full):
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
        name = entry.filename
        if name in {".", ".."}:
            continue
        child = posixpath.join(remote_dir, name)
        if stat.S_ISDIR(entry.st_mode):
            f_count, b_count = _count_recursive(sftp, child)
            files += f_count
            total_bytes += b_count
        else:
            files += 1
            total_bytes += int(entry.st_size or 0)
    return files, total_bytes


def _download_recursive(
    sftp: paramiko.SFTPClient,
    remote_dir: str,
    local_dir: Path,
    *,
    progress_cb=None,
    counters: dict | None = None,
    current_folder: str = "",
):
    local_dir.mkdir(parents=True, exist_ok=True)

    total_files = 0
    total_bytes = 0

    for entry in sorted(sftp.listdir_attr(remote_dir), key=lambda x: x.filename.lower()):
        name = entry.filename
        if name in {".", ".."}:
            continue

        remote_child = posixpath.join(remote_dir, name)
        local_child = local_dir / name

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
                        "file": name,
                        "files_done": counters["files_done"] if counters else total_files,
                        "files_total": counters["files_total"] if counters else total_files,
                        "bytes_done": counters["bytes_done"] if counters else total_bytes,
                        "bytes_total": counters["bytes_total"] if counters else total_bytes,
                    }
                )

    return total_files, total_bytes


def _zip_dir(src_dir: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(src_dir))


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

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sftp = None

    try:
        if log_cb:
            log_cb(f"Conectando a SFTP {Settings.SFTP_HOST}:{Settings.SFTP_PORT} ...")

        ssh.connect(
            hostname=Settings.SFTP_HOST,
            port=Settings.SFTP_PORT,
            username=Settings.SFTP_USER,
            password=Settings.SFTP_PASSWORD,
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
            look_for_keys=False,
            allow_agent=False,
        )
        sftp = ssh.open_sftp()

        detected_base, probes = _pick_remote_base(sftp, Settings.SFTP_REMOTE_BASE, items)

        if log_cb:
            log_cb(f"Base remota detectada: {detected_base!r}")

        planned: list[tuple[FolderItem, str, int, int]] = []
        missing: list[dict] = []

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
                missing.append(
                    {
                        "dig_tramite": item.dig_tramite,
                        "remote_dir": remote_dir,
                        "reason": "remote_folder_not_found",
                    }
                )

        counters = {
            "files_done": 0,
            "files_total": files_total,
            "bytes_done": 0,
            "bytes_total": bytes_total,
        }

        downloaded: list[dict] = []

        for item, remote_dir, _fc, _bc in planned:
            local_dir = data_root / _safe_name(item.dig_tramite)

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
                    "dig_tramite": item.dig_tramite,
                    "dig_anio": item.dig_anio,
                    "dig_expediente": item.dig_expediente,
                    "fe_pla_aniomes": item.fe_pla_aniomes,
                    "dig_area_dep": item.dig_area_dep,
                    "remote_dir": remote_dir,
                    "local_dir": str(local_dir),
                    "files": f_count,
                    "bytes": b_count,
                }
            )

        payload = {
            "dig_id_tramite": int(dig_id_tramite),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "remote_base_requested": Settings.SFTP_REMOTE_BASE,
            "remote_base_detected": detected_base,
            "remote_base_probes": probes,
            "total_files": files_total,
            "total_bytes": bytes_total,
            "downloaded": downloaded,
            "missing": missing,
            "job_root": str(job_root),
            "data_root": str(data_root),
        }

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
```

---

## 9) `jobs.py`

```python
from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime, timezone

from sftp_download import run_download


JOBS: dict[str, dict] = {}
JOB_LOCK = threading.Lock()


def get_job(job_id: str) -> dict | None:
    with JOB_LOCK:
        return JOBS.get(job_id)


def update_job(job_id: str, **fields):
    with JOB_LOCK:
        job = JOBS.setdefault(job_id, {})
        job.update(fields)


def create_job(dig_id_tramite: int, preview: dict) -> str:
    job_id = uuid.uuid4().hex
    update_job(
        job_id,
        job_id_value=job_id,
        dig_id_tramite=int(dig_id_tramite),
        preview=preview,
        status="queued",
        created_at=datetime.now(timezone.utc).isoformat(),
        progress={
            "files_done": 0,
            "files_total": 0,
            "bytes_done": 0,
            "bytes_total": 0,
            "folder": "",
            "file": "",
        },
        log_lines=[],
        last_message="Trabajo en cola",
    )
    return job_id


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
            update_job(
                job_id,
                status="running",
                started_at=datetime.now(timezone.utc).isoformat(),
            )

            def _progress(payload: dict):
                update_job(job_id, progress=payload)

            def _log(message: str):
                append_log(job_id, message)

            job = get_job(job_id) or {}
            dig_id_tramite = int(job["dig_id_tramite"])

            report = run_download(
                dig_id_tramite,
                progress_cb=_progress,
                log_cb=_log,
            )

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
            update_job(
                job_id,
                status="failed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                error=str(exc),
                traceback=traceback.format_exc(),
            )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
```

---

## 10) `app.py`

```python
from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for

from config import Settings
from jobs import create_job, get_job, run_job_async
from oracle_client import build_preview


app = Flask(__name__)
app.config["SECRET_KEY"] = Settings.FLASK_SECRET_KEY


def _progress_pct(job: dict | None) -> int:
    if not job:
        return 0
    pr = job.get("progress") or {}
    total = int(pr.get("files_total") or 0)
    done = int(pr.get("files_done") or 0)
    if total <= 0:
        return 0
    return int((done / total) * 100)


@app.get("/healthz")
def healthz():
    return {"ok": True}, 200


@app.get("/")
def home():
    return render_template("index.html", defaults=Settings)


@app.post("/preview")
def preview():
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

    return send_file(
        str(zip_path),
        as_attachment=True,
        download_name=zip_path.name,
        mimetype="application/zip",
    )
```

---

## 11) `templates/base.html`

```html
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>{{ title or "SFTP HE1" }}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {% if auto_refresh %}
  <meta http-equiv="refresh" content="4">
  {% endif %}
  <style>
    body { font-family: Arial, sans-serif; background:#f6f8fb; margin:0; color:#0f172a; }
    .wrap { max-width:1280px; margin:20px auto; padding:0 16px; }
    .card { background:#fff; border:1px solid #dbe3ef; border-radius:12px; padding:18px; margin-bottom:16px; }
    h1,h2,h3 { margin-top:0; }
    .grid { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:14px; }
    label { display:block; font-weight:bold; margin-bottom:6px; }
    input { width:100%; box-sizing:border-box; padding:10px; border:1px solid #cbd5e1; border-radius:8px; }
    .actions { display:flex; gap:12px; margin-top:18px; }
    button, a.btn { background:#0f766e; color:#fff; border:0; padding:10px 14px; border-radius:8px; text-decoration:none; cursor:pointer; display:inline-block; }
    button.secondary, a.secondary { background:#334155; }
    table { width:100%; border-collapse:collapse; font-size:14px; }
    th,td { padding:10px; border-bottom:1px solid #e5e7eb; text-align:left; vertical-align:top; }
    code { background:#f1f5f9; padding:2px 6px; border-radius:6px; }
    pre { background:#0f172a; color:#e2e8f0; padding:14px; border-radius:10px; overflow:auto; }
    .ok { color:#166534; font-weight:bold; }
    .err { color:#b91c1c; font-weight:bold; }
    .muted { color:#64748b; }
    .progress { width:100%; height:22px; background:#e2e8f0; border-radius:999px; overflow:hidden; margin-top:10px; }
    .progress > div { height:100%; background:#0f766e; color:#fff; line-height:22px; text-align:center; font-size:12px; }
  </style>
</head>
<body>
<div class="wrap">
  {% block content %}{% endblock %}
</div>
</body>
</html>
```

---

## 12) `templates/index.html`

```html
{% extends "base.html" %}
{% block content %}
<h1>Descarga SFTP por DIG_ID_TRAMITE</h1>

<div class="card">
  <form method="post" action="/preview">
    <div class="grid">
      <div>
        <label>DIG_ID_TRAMITE</label>
        <input type="text" name="dig_id_tramite" placeholder="Ejemplo: 16356">
      </div>
      <div>
        <label>Oracle destino</label>
        <input type="text" value="{{ defaults.ORACLE_TARGETS }}" readonly>
      </div>
      <div>
        <label>SFTP destino</label>
        <input type="text" value="{{ defaults.SFTP_HOST }}:{{ defaults.SFTP_PORT }} | {{ defaults.SFTP_REMOTE_BASE }}" readonly>
      </div>
    </div>
    <div class="actions">
      <button type="submit">Previsualizar</button>
    </div>
  </form>
</div>

{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
  <div class="card">
    {% for category, message in messages %}
      <div class="{{ 'err' if category == 'error' else 'ok' }}">{{ message }}</div>
    {% endfor %}
  </div>
  {% endif %}
{% endwith %}

{% if preview %}
<div class="card">
  <h2>Previsualización Oracle</h2>
  <p><b>DIG_ID_TRAMITE:</b> {{ preview.dig_id_tramite }} | <b>Total carpetas hijas:</b> {{ preview.count }}</p>

  <form method="post" action="/start">
    <input type="hidden" name="dig_id_tramite" value="{{ preview.dig_id_tramite }}">
    <div class="actions">
      <button type="submit" class="secondary">Iniciar descarga</button>
    </div>
  </form>

  <table>
    <thead>
      <tr>
        <th>DIG_TRAMITE</th>
        <th>AÑO</th>
        <th>EXPEDIENTE</th>
        <th>FE_PLA_ANIOMES</th>
        <th>ÁREA</th>
        <th>RUTA REMOTA</th>
      </tr>
    </thead>
    <tbody>
      {% for row in preview.items %}
      <tr>
        <td>{{ row.dig_tramite }}</td>
        <td>{{ row.dig_anio }}</td>
        <td>{{ row.dig_expediente }}</td>
        <td>{{ row.fe_pla_aniomes }}</td>
        <td>{{ row.dig_area_dep }}</td>
        <td><code>{{ row.remote_rel_path }}</code></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endif %}
{% endblock %}
```

---

## 13) `templates/job.html`

```html
{% extends "base.html" %}
{% set auto_refresh = job.status in ['queued', 'running'] %}
{% block content %}
<h1>Estado de descarga</h1>

<div class="card">
  <p><b>Job:</b> {{ job.job_id_value }}</p>
  <p><b>DIG_ID_TRAMITE:</b> {{ job.dig_id_tramite }}</p>
  <p><b>Estado:</b> {{ job.status }}</p>
  <p><b>Último mensaje:</b> {{ job.last_message or '-' }}</p>

  {% if job.status in ['queued', 'running'] %}
    <p class="muted">La página se refresca automáticamente cada 4 segundos.</p>
    <div class="progress"><div style="width: {{ progress_pct }}%;">{{ progress_pct }}%</div></div>
    {% if job.progress %}
    <p>
      <b>Carpeta:</b> {{ job.progress.folder or '-' }} |
      <b>Archivo:</b> {{ job.progress.file or '-' }} |
      <b>Archivos:</b> {{ job.progress.files_done or 0 }} / {{ job.progress.files_total or 0 }}
    </p>
    {% endif %}
  {% endif %}

  {% if job.status == 'finished' and job.report %}
    <p><b>Base remota detectada:</b> <code>{{ job.report.remote_base_detected }}</code></p>
    <p><b>Total archivos:</b> {{ job.report.total_files }}</p>
    <p><b>Total bytes:</b> {{ job.report.total_bytes }}</p>
    <p><b>ZIP:</b> <code>{{ job.report.zip_path }}</code></p>

    <p>
      <a class="btn" href="/jobs/{{ job.job_id_value }}/download">Descargar ZIP</a>
      <a class="btn secondary" href="/">Volver</a>
    </p>

    <h3>Carpetas descargadas</h3>
    <table>
      <thead>
        <tr>
          <th>DIG_TRAMITE</th>
          <th>REMOTA</th>
          <th>LOCAL</th>
          <th>FILES</th>
          <th>BYTES</th>
        </tr>
      </thead>
      <tbody>
        {% for row in job.report.downloaded %}
        <tr>
          <td>{{ row.dig_tramite }}</td>
          <td><code>{{ row.remote_dir }}</code></td>
          <td><code>{{ row.local_dir }}</code></td>
          <td>{{ row.files }}</td>
          <td>{{ row.bytes }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>

    {% if job.report.missing %}
    <h3>Carpetas no encontradas</h3>
    <table>
      <thead>
        <tr>
          <th>DIG_TRAMITE</th>
          <th>RUTA REMOTA</th>
          <th>MOTIVO</th>
        </tr>
      </thead>
      <tbody>
        {% for row in job.report.missing %}
        <tr>
          <td>{{ row.dig_tramite }}</td>
          <td><code>{{ row.remote_dir }}</code></td>
          <td>{{ row.reason }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% endif %}
  {% endif %}

  {% if job.status == 'failed' %}
    <pre>{{ job.traceback or job.error }}</pre>
  {% endif %}

  {% if job.log_lines %}
    <h3>Bitácora</h3>
    <pre>{% for line in job.log_lines %}{{ line }}
{% endfor %}</pre>
  {% endif %}
</div>
{% endblock %}
```

---

# Parte 3 — Ejecución

## Construcción

```bash
cd /home/crrb/codex_projects/sftp_he1
cp .env.example .env
docker compose build
docker compose up -d
```

## Verificación

```bash
docker compose ps
docker compose logs -f sftp_he1
```

Abrir:

```text
http://IP_DEL_EQUIPO:5085
```

---

# Parte 4 — Pruebas de verificación y regresión

## Prueba de red antes de correr

```bash
nc -vz 172.16.60.127 2223
nc -vz 172.16.60.20 1521
nc -vz 172.16.60.21 1521
```

## Prueba funcional

Usar `DIG_ID_TRAMITE = 16356`.

La pantalla debe previsualizar varias carpetas hijas y luego descargar una carpeta base `16356` con subcarpetas por `DIG_TRAMITE`, tal como fue solicitado en el caso entregado .

## Validación de no-freeze

Mientras corre una descarga:

1. la URL `/jobs/<job_id>` debe seguir respondiendo,
2. otra pestaña del navegador debe abrir `/`,
3. la barra debe avanzar por refresh automático.

Eso funciona porque la descarga va en thread aparte y la vista solo consulta estado, igual que el patrón de la exportación SFTP mensual existente .

---

# Parte 5 — Plan de reversión

Como el proyecto está aislado, el rollback es rápido.

## Detener y eliminar contenedor

```bash
cd /home/crrb/codex_projects/sftp_he1
docker compose down
```

## Script de emergencia

```bash
cd /home/crrb/codex_projects/sftp_he1 && docker compose down && mv /home/crrb/codex_projects/sftp_he1 /home/crrb/codex_projects/sftp_he1_DISABLED_$(date +%Y%m%d_%H%M%S)
```

---

# Parte 6 — Punto clave sobre Oracle

La respuesta exacta sería esta:

**Sí**, con el jar ubicado en:

```text
/home/crrb/codex_projects/sftp_he1/jdbc/ojdbc8.jar
```

**sí se puede conectar a Oracle**, pero no basta “solo tener el jar”.
También hacen falta:

* `jaydebeapi`
* `JPype1`
* una JVM dentro del contenedor
* y configurar `ORACLE_JDBC_JAR=/app/jdbc/ojdbc8.jar`

Eso queda cubierto en el `Dockerfile`, `requirements.txt` y `.env.example` entregados arriba.
Si se desea, en el siguiente paso se puede entregar la misma solución con un **botón de cancelar job** y otro de **borrar ZIPs viejos**.
