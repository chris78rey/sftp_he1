"""Reporte de planillas de un label de objeciones que bajan con carpeta vacía.

El ZIP selectivo solo trae los PDFs seleccionados ("Guardar selección"). Las
planillas del label que no tienen ninguna selección guardada se incluyen como
carpeta vacía y este módulo genera el informe que explica el motivo
(un .txt legible y un .csv para filtrar en Excel).

Todo es solo lectura: consulta Oracle con SELECT y lista carpetas en disco.
"""

from __future__ import annotations

import csv
import io
import os
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import Settings
from oracle_client import connect_with_failover

_IDENT = re.compile(r"^[A-Z0-9_$#]{1,30}$")


def _table_ref() -> str:
    owner = Settings.ORACLE_OWNER if _IDENT.match(Settings.ORACLE_OWNER or "") else "DIGITALIZACION"
    table = Settings.ORACLE_TABLE if _IDENT.match(Settings.ORACLE_TABLE or "") else "DIGITALIZACION"
    return f"{owner}.{table}"


def fetch_label_planillas(label: str) -> list[dict]:
    """Planillas objetadas del label según la tabla maestra de Oracle (SELECT)."""
    value = str(label or "").strip()
    conn = connect_with_failover()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT TRIM(TO_CHAR(DIG_TRAMITE)), "
            "MAX(NVL(NULLIF(TRIM(OBJECION_ASIGNADA_A), ''), 'SIN_GRUPO')), "
            "CASE WHEN SUM(CASE WHEN UPPER(TRIM(NVL(OBJECION_RESUELTA, 'N'))) "
            "<> 'S' THEN 1 ELSE 0 END) > 0 THEN 'N' ELSE 'S' END, "
            "MAX(OBJECION_REVISADA_POR), MAX(DIG_USUARIO), MAX(DIG_HC) "
            f"FROM {_table_ref()} "
            "WHERE TRIM(LABEL_OBJECION) = TRIM(?) "
            "AND UPPER(TRIM(NVL(OBJETADO, 'N'))) = 'S' "
            "GROUP BY TRIM(TO_CHAR(DIG_TRAMITE)) ORDER BY 2, 1",
            (value,),
        )
        rows = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    out = []
    for row in rows:
        if not row or row[0] is None or not str(row[0]).strip():
            continue
        out.append(
            {
                "tramite": str(row[0]).strip(),
                "grupo": str(row[1] or "SIN_GRUPO").strip().upper(),
                "resuelta": str(row[2] or "N").strip().upper() == "S",
                "revisada_por": str(row[3] or "").strip(),
                "digitalizo": str(row[4] or "").strip(),
                "hc": str(row[5] or "").strip(),
            }
        )
    return out


def disk_pdf_counts(root: Path, wanted: set[str]) -> dict[str, int]:
    """Cuenta los PDFs que hay en disco para cada planilla (root/año/área/planilla)."""
    counts: dict[str, int] = {}
    if not wanted:
        return counts
    try:
        years = [p for p in Path(root).iterdir() if p.is_dir()]
    except OSError:
        return counts
    for year in years:
        try:
            areas = [p for p in year.iterdir() if p.is_dir()]
        except OSError:
            continue
        for area in areas:
            try:
                with os.scandir(area) as it:
                    for entry in it:
                        if entry.name in wanted and entry.is_dir():
                            try:
                                n = sum(
                                    1
                                    for f in os.scandir(entry.path)
                                    if f.is_file() and f.name.lower().endswith(".pdf")
                                )
                            except OSError:
                                continue
                            counts[entry.name] = counts.get(entry.name, 0) + n
            except OSError:
                continue
    return counts


def build_reports(
    label: str,
    planillas: list[dict],
    selected_by_tramite: dict[str, int],
    written_by_tramite: dict[str, int],
    disk_counts: dict[str, int],
    *,
    now: datetime | None = None,
) -> dict:
    """Arma el .txt y el .csv del informe. Devuelve también las planillas vacías."""
    stamp = (now or datetime.now(timezone(timedelta(hours=-5)))).strftime("%Y-%m-%d %H:%M")
    rows = []
    known = {p["tramite"] for p in planillas}
    for p in planillas:
        t = p["tramite"]
        selected = int(selected_by_tramite.get(t, 0))
        written = int(written_by_tramite.get(t, 0))
        en_disco = disk_counts.get(t)
        if written > 0:
            resultado, motivo = "CON_PDFS", ""
        elif p["resuelta"]:
            resultado = "CARPETA_VACIA"
            motivo = "RESUELTA sin selección guardada (revisar: no debería ocurrir)"
        else:
            resultado = "CARPETA_VACIA"
            motivo = "PENDIENTE sin selección guardada: nadie ha marcado PDFs con 'Guardar selección'"
        rows.append({**p, "seleccionados": selected, "en_zip": written, "en_disco": en_disco,
                     "resultado": resultado, "motivo": motivo})
    for t in sorted(set(selected_by_tramite) - known):
        rows.append({"tramite": t, "grupo": "(fuera del label)", "resuelta": False,
                     "revisada_por": "", "digitalizo": "", "hc": "",
                     "seleccionados": int(selected_by_tramite[t]),
                     "en_zip": int(written_by_tramite.get(t, 0)), "en_disco": disk_counts.get(t),
                     "resultado": "CON_PDFS" if written_by_tramite.get(t) else "CARPETA_VACIA",
                     "motivo": "Tiene selección pero Oracle ya no la marca como objetada en este label"})
    empty = [r for r in rows if r["resultado"] == "CARPETA_VACIA"]

    lines = [
        f"LABEL {label} - REPORTE DE CARPETAS VACIAS EN EL ZIP",
        f"Generado: {stamp} (hora Ecuador)",
        "",
        f"Planillas del label en Oracle (objetadas): {len(planillas)}",
        f"Planillas con PDFs en el ZIP: {sum(1 for r in rows if r['resultado'] == 'CON_PDFS')}",
        f"Planillas con CARPETA VACIA: {len(empty)}",
        "",
        "MOTIVO: el ZIP solo trae los PDFs seleccionados con 'Guardar selección'. Estas",
        "planillas pertenecen al label pero no tienen ninguna selección guardada, por eso",
        "su carpeta va vacía. Los PDFs SI existen en disco (ver 'PDFs en disco'); no se",
        "incluyen porque nadie los ha marcado todavía.",
        "",
        "RESUMEN POR GRUPO (vacías / total del grupo)",
    ]
    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in sorted(rows, key=lambda x: (x["grupo"], x["tramite"])):
        groups.setdefault(r["grupo"], []).append(r)
    for grupo, items in groups.items():
        vacias = [r for r in items if r["resultado"] == "CARPETA_VACIA"]
        pendientes = sum(1 for r in items if not r["resuelta"])
        lines.append(f"  {grupo}: {len(vacias)} vacías de {len(items)} (pendientes del grupo: {pendientes})")
    lines += ["", "DETALLE DE CARPETAS VACIAS", ""]
    if not empty:
        lines.append("  (ninguna: todas las planillas del label bajan con PDFs)")
    for grupo, items in groups.items():
        vacias = [r for r in items if r["resultado"] == "CARPETA_VACIA"]
        if not vacias:
            continue
        lines.append(grupo)
        for r in vacias:
            disco = "?" if r["en_disco"] is None else str(r["en_disco"])
            extra = []
            if r["hc"]:
                extra.append(f"HC {r['hc']}")
            extra.append(f"PDFs en disco: {disco}")
            if r["digitalizo"]:
                extra.append(f"digitalizó: {r['digitalizo']}")
            lines.append(
                f"  {r['tramite']}  {'RESUELTA ' if r['resuelta'] else 'PENDIENTE'}  "
                + " | ".join(extra)
            )
            if r["motivo"] and r["resuelta"]:
                lines.append(f"      * {r['motivo']}")
        lines.append("")
    text = "\r\n".join(lines) + "\r\n"

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["planilla", "grupo", "estado", "hc", "digitalizo", "revisada_por",
                     "pdfs_seleccionados", "pdfs_en_zip", "pdfs_en_disco", "resultado", "motivo"])
    for r in sorted(rows, key=lambda x: (x["grupo"], x["tramite"])):
        writer.writerow([r["tramite"], r["grupo"], "RESUELTA" if r["resuelta"] else "PENDIENTE",
                         r["hc"], r["digitalizo"], r["revisada_por"], r["seleccionados"],
                         r["en_zip"], "" if r["en_disco"] is None else r["en_disco"],
                         r["resultado"], r["motivo"]])
    return {"txt": text, "csv": "﻿" + buffer.getvalue(), "empty": empty, "rows": rows}


# ---------------------------------------------------------------------------
# Reporte web de pendientes (pantalla /objeciones/pendientes). Solo lectura.
# ---------------------------------------------------------------------------

FILTROS = ("sin_seleccion", "pendientes", "todas")
CATEGORIAS = {
    "PENDIENTE_SIN_SELECCION": "Pendiente sin selección",
    "PENDIENTE_CON_SELECCION": "Pendiente con selección",
    "RESUELTA_SIN_SELECCION": "Resuelta sin selección (revisar)",
    "RESUELTA": "Resuelta",
}


def fetch_labels_overview() -> list[dict]:
    """Un renglón por label: total, pendientes y pendientes sin selección guardada."""
    table = _table_ref()
    conn = connect_with_failover()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT TRIM(m.LABEL_OBJECION), COUNT(DISTINCT m.DIG_TRAMITE), "
            "COUNT(DISTINCT CASE WHEN UPPER(TRIM(NVL(m.OBJECION_RESUELTA, 'N'))) <> 'S' "
            "THEN m.DIG_TRAMITE END) "
            f"FROM {table} m "
            "WHERE UPPER(TRIM(NVL(m.OBJETADO, 'N'))) = 'S' AND m.LABEL_OBJECION IS NOT NULL "
            "GROUP BY TRIM(m.LABEL_OBJECION) ORDER BY 1 DESC"
        )
        base = {str(r[0]): {"label": str(r[0]), "total": int(r[1]), "pendientes": int(r[2]),
                            "pendientes_sin_seleccion": 0, "sin_seleccion": 0}
                for r in cur.fetchall() if r and r[0]}
        # Planillas sin ninguna selección guardada (pendientes y resueltas): son las que
        # el ZIP selectivo incluye como carpeta vacía.
        cur.execute(
            "SELECT TRIM(m.LABEL_OBJECION), COUNT(DISTINCT m.DIG_TRAMITE), "
            "COUNT(DISTINCT CASE WHEN UPPER(TRIM(NVL(m.OBJECION_RESUELTA, 'N'))) <> 'S' "
            "THEN m.DIG_TRAMITE END) "
            f"FROM {table} m "
            "WHERE UPPER(TRIM(NVL(m.OBJETADO, 'N'))) = 'S' "
            "AND m.LABEL_OBJECION IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM DIGITALIZACION.CD_DESCARGA_OBJECION_ITEM i "
            "JOIN DIGITALIZACION.CD_DESCARGA_OBJECION d ON d.ID_DESCARGA = i.ID_DESCARGA "
            "WHERE TRIM(d.LABEL_OBJECION) = TRIM(m.LABEL_OBJECION) "
            "AND i.DIG_TRAMITE = m.DIG_TRAMITE) "
            "GROUP BY TRIM(m.LABEL_OBJECION)"
        )
        for r in cur.fetchall():
            if r and str(r[0]) in base:
                base[str(r[0])]["sin_seleccion"] = int(r[1])
                base[str(r[0])]["pendientes_sin_seleccion"] = int(r[2])
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return list(base.values())


def fetch_selection_summary(label: str) -> dict[str, dict]:
    """Por planilla: PDFs seleccionados, quién y cuándo guardó la selección vigente."""
    conn = connect_with_failover()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT i.DIG_TRAMITE, d.ID_DESCARGA, d.FECHA_SOLICITUD, d.USUARIO_SOLICITA, "
            "i.NOMBRE_PDF FROM DIGITALIZACION.CD_DESCARGA_OBJECION d "
            "JOIN DIGITALIZACION.CD_DESCARGA_OBJECION_ITEM i ON i.ID_DESCARGA = d.ID_DESCARGA "
            "WHERE TRIM(d.LABEL_OBJECION) = TRIM(?)",
            (str(label or "").strip(),),
        )
        rows = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    latest: dict[str, tuple[str, str, str]] = {}
    for r in rows:
        if not r or r[0] is None:
            continue
        t = str(int(r[0]))
        stamp = str(r[2] or "")
        if t not in latest or stamp > latest[t][0]:
            latest[t] = (stamp, str(r[1]), str(r[3] or ""))
    out: dict[str, dict] = {}
    for r in rows:
        if not r or r[0] is None:
            continue
        t = str(int(r[0]))
        if latest[t][1] != str(r[1]):
            continue
        item = out.setdefault(t, {"seleccionados": 0, "usuario": latest[t][2],
                                  "fecha": latest[t][0][:16]})
        item["seleccionados"] += 1
    return out


def compose_overview(
    label: str,
    planillas: list[dict],
    selection: dict[str, dict],
    disk_counts: dict[str, int],
    filtro: str = "sin_seleccion",
) -> dict:
    """Combina Oracle (planillas y selección) con el disco y filtra para la pantalla."""
    filtro = filtro if filtro in FILTROS else "sin_seleccion"
    rows = []
    for p in sorted(planillas, key=lambda x: (x["grupo"], x["tramite"])):
        sel = selection.get(p["tramite"])
        tiene = bool(sel and sel["seleccionados"] > 0)
        if p["resuelta"]:
            categoria = "RESUELTA" if tiene else "RESUELTA_SIN_SELECCION"
        else:
            categoria = "PENDIENTE_CON_SELECCION" if tiene else "PENDIENTE_SIN_SELECCION"
        rows.append({
            **p,
            "categoria": categoria,
            "estado": "RESUELTA" if p["resuelta"] else "PENDIENTE",
            "seleccionados": sel["seleccionados"] if sel else 0,
            "usuario_seleccion": sel["usuario"] if sel else "",
            "fecha_seleccion": sel["fecha"] if sel else "",
            "en_disco": disk_counts.get(p["tramite"]),
        })
    by_group: "OrderedDict[str, dict]" = OrderedDict()
    for r in rows:
        g = by_group.setdefault(r["grupo"], {"grupo": r["grupo"], "total": 0, "pendientes": 0,
                                             "pendientes_sin_seleccion": 0, "resueltas": 0})
        g["total"] += 1
        g["pendientes"] += r["estado"] == "PENDIENTE"
        g["resueltas"] += r["estado"] == "RESUELTA"
        g["pendientes_sin_seleccion"] += r["categoria"] == "PENDIENTE_SIN_SELECCION"
    if filtro == "sin_seleccion":
        shown = [r for r in rows if r["categoria"] in ("PENDIENTE_SIN_SELECCION", "RESUELTA_SIN_SELECCION")]
    elif filtro == "pendientes":
        shown = [r for r in rows if r["estado"] == "PENDIENTE"]
    else:
        shown = rows
    counts = {k: sum(1 for r in rows if r["categoria"] == k) for k in CATEGORIAS}
    return {"label": label, "filtro": filtro, "rows": shown, "total": len(rows),
            "counts": counts, "by_group": list(by_group.values())}


def build_label_overview(label: str, filtro: str = "sin_seleccion") -> dict:
    planillas = fetch_label_planillas(label)
    selection = fetch_selection_summary(label)
    disk = disk_pdf_counts(Settings.LOCAL_REPO_ROOT, {p["tramite"] for p in planillas})
    return compose_overview(label, planillas, selection, disk, filtro)


def overview_csv(overview: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["label", "planilla", "grupo", "estado", "categoria", "hc", "digitalizo",
                     "revisada_por", "pdfs_seleccionados", "seleccion_guardada_por",
                     "seleccion_fecha", "pdfs_en_disco"])
    for r in overview["rows"]:
        writer.writerow([overview["label"], r["tramite"], r["grupo"], r["estado"],
                         CATEGORIAS[r["categoria"]], r["hc"], r["digitalizo"], r["revisada_por"],
                         r["seleccionados"], r["usuario_seleccion"], r["fecha_seleccion"],
                         "" if r["en_disco"] is None else r["en_disco"]])
    return "﻿" + buffer.getvalue()
