"""Fusión al vuelo de fragmentos de objeciones, con las mismas reglas que Rust.

Las reglas de nombres (VALID_NAMES, normalize_stem, classify_objection_pdf y
objection_sort_key) están portadas de rust_cambia_nombre_planillas_he1/src/main.rs.

Solo lectura: nunca modifica ni borra archivos del repositorio ni toca Oracle.
Se usa al armar el ZIP de objeciones para que, si Oracle tiene marcados
ORS.pdf y ORS_0.pdf, el ZIP traiga un único ORS.pdf (canónico primero y luego
los fragmentos por número).
"""

from __future__ import annotations

import io
from pathlib import Path

VALID_NAMES = (
    "PI", "CC", "CV", "AES", "053", "006", "007", "017", "018", "018A", "113", "114",
    "115", "ORS", "002", "010A", "010B", "012A", "012B", "033", "013A", "013B", "PTR",
    "RTR", "08", "008", "FSCS", "FSICS", "FRDCS", "ANX2", "HR", "RHD", "IMT", "CEC",
    "RAD", "ITS", "RVD", "119",
)


def normalize_stem(stem: str) -> str:
    end = len(stem)
    for idx, ch in enumerate(stem):
        if ch in " ()[]{}.":
            end = idx
            break
    return stem[:end].rstrip("_-").strip()


def _variant_suffix_ok(suffix: str) -> bool:
    value = suffix.strip()
    if not value or value[0] not in "_-":
        return False
    return bool(value[1:].strip())


def classify_pdf(name: str) -> tuple[str, bool] | None:
    """(documento_base, es_fragmento) o None. Igual que classify_objection_pdf de Rust."""
    path = Path(str(name or ""))
    if path.suffix.lower() != ".pdf":
        return None
    stem = path.stem
    normalized = normalize_stem(stem).upper()
    if normalized == "RDH":
        return ("RHD", stem.lower() != "rdh")
    if normalized.startswith("RDH") and _variant_suffix_ok(normalized[3:]):
        return ("RHD", True)
    base = None
    for candidate in VALID_NAMES:
        cand_up = candidate.upper()
        if normalized == cand_up or (
            normalized.startswith(cand_up) and _variant_suffix_ok(normalized[len(cand_up):])
        ):
            base = candidate
            break
    if base is None:
        return None
    if normalized == base.upper():
        return (base, stem.lower() != base.lower())
    return (base, True)


def sort_key(name: str, base: str) -> tuple[int, int, int, str]:
    normalized = normalize_stem(Path(name).stem).upper()
    base_up = base.upper()
    if base_up == "RHD" and normalized.startswith("RDH"):
        normalized = "RHD" + normalized[3:]
    if normalized == base_up:
        return (0, 0, 0, normalized)
    suffix = normalized[len(base_up):] if normalized.startswith(base_up) else ""
    suffix = suffix.lstrip("_-")
    if suffix.isascii() and suffix.isdigit():
        return (1, 0, int(suffix), normalized)
    return (1, 1, (1 << 64) - 1, normalized)


def merge_groups(
    disk_names: list[str], requested_upper: set[str]
) -> list[tuple[str, list[str]]]:
    """Grupos a fundir en una carpeta.

    Un documento se funde cuando Oracle pide al menos un fragmento existente en
    disco y, junto con el canónico presente en la carpeta (si lo hay), quedan 2
    o más PDFs. Orden: canónico primero y luego fragmentos por número (clave de
    Rust). Los fragmentos no pedidos no se tocan. (Rust exige 2 fragmentos; aquí
    basta 1 fragmento + el canónico.)
    """
    by_upper = {name.upper(): name for name in disk_names}
    parts_by_base: dict[str, set[str]] = {}
    for wanted in requested_upper:
        disk_name = by_upper.get(wanted)
        if disk_name is None:
            continue
        cls = classify_pdf(disk_name)
        if cls and cls[1]:
            parts_by_base.setdefault(cls[0], set()).add(disk_name)
    groups: list[tuple[str, list[str]]] = []
    for base in sorted(parts_by_base):
        members = set(parts_by_base[base])
        for name in disk_names:
            if classify_pdf(name) == (base, False):
                members.add(name)
        if len(members) < 2:
            continue
        groups.append((base, sorted(members, key=lambda n: sort_key(n, base))))
    return groups


def plan_merges(
    resolved: list[tuple[str, str, Path]],
) -> tuple[list[dict], set[tuple[str, str]]]:
    """Planifica las fusiones para los PDFs ya localizados.

    `resolved` es una lista de (planilla, nombre_pdf, ruta_en_disco). Devuelve
    (grupos, consumidos): `consumidos` son los (planilla, nombre) que pasan a
    formar parte de un PDF fundido y no deben añadirse por separado.
    """
    by_folder: dict[tuple[str, Path], set[str]] = {}
    for tramite, name, source in resolved:
        by_folder.setdefault((tramite, Path(source).parent), set()).add(name.upper())
    groups: list[dict] = []
    consumed: set[tuple[str, str]] = set()
    for (tramite, folder), requested in sorted(by_folder.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        try:
            disk_names = [e.name for e in folder.iterdir() if e.is_file()]
        except OSError:
            continue
        for base, members in merge_groups(disk_names, requested):
            groups.append(
                {
                    "tramite": tramite,
                    "base": base,
                    "output_name": f"{base}.pdf",
                    "members": members,
                    "paths": [folder / m for m in members],
                }
            )
            consumed.update((tramite, m) for m in members)
    return groups, consumed


def merge_pdfs(paths: list[Path]) -> tuple[bytes, int]:
    """Une PDFs en memoria y verifica que las páginas de salida sean la suma de entrada."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    expected = 0
    for path in paths:
        reader = PdfReader(str(path))
        count = len(reader.pages)
        if count <= 0:
            raise ValueError(f"PDF sin páginas: {Path(path).name}")
        expected += count
        for page in reader.pages:
            writer.add_page(page)
    buffer = io.BytesIO()
    writer.write(buffer)
    data = buffer.getvalue()
    produced = len(PdfReader(io.BytesIO(data)).pages)
    if produced != expected:
        raise ValueError(f"páginas de salida {produced} != suma de entrada {expected}")
    return data, produced
