from __future__ import annotations

from dataclasses import asdict, dataclass
import time
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


def oracle_diagnostics() -> dict:
    diagnostics: dict = {
        "targets": [],
        "jar": str(Path(Settings.ORACLE_JDBC_JAR).expanduser().resolve()),
        "user": Settings.ORACLE_USER,
        "owner": Settings.ORACLE_OWNER,
        "table": Settings.ORACLE_TABLE,
    }

    jar = Path(Settings.ORACLE_JDBC_JAR).expanduser().resolve()
    diagnostics["jar_exists"] = jar.exists()

    raw_targets = str(Settings.ORACLE_TARGETS or "").strip()
    for item in [x.strip() for x in raw_targets.split(",") if x.strip()]:
        try:
            host, port, sid = _parse_target(item)
        except Exception as exc:
            diagnostics["targets"].append({"target": item, "ok": False, "error": str(exc)})
            continue

        url = _jdbc_url(host, port, sid)
        started = time.perf_counter()
        try:
            conn = jaydebeapi.connect(
                "oracle.jdbc.OracleDriver",
                url,
                [Settings.ORACLE_USER, Settings.ORACLE_PASSWORD],
                jars=[str(jar)],
            )
            conn.close()
            diagnostics["targets"].append({"target": item, "url": url, "ok": True, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)})
        except Exception as exc:
            diagnostics["targets"].append({"target": item, "url": url, "ok": False, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "error": str(exc)})

    return diagnostics


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
            dig_anio = str(row[2]).strip()
            dig_expediente = str(row[3]).strip()
            dig_tramite = str(row[1]).strip()
            out.append(
                FolderItem(
                    dig_id_tramite=int(row[0]),
                    dig_tramite=dig_tramite,
                    dig_anio=dig_anio,
                    dig_expediente=dig_expediente,
                    fe_pla_aniomes=str(row[4]).strip(),
                    dig_area_dep=str(row[5]).strip(),
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
    return {"dig_id_tramite": int(dig_id_tramite), "count": len(items), "items": [x.to_dict() for x in items]}
