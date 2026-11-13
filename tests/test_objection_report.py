from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import objection_report as rep  # noqa: E402


def _p(t, grupo, resuelta, **kw):
    return {"tramite": t, "grupo": grupo, "resuelta": resuelta, "revisada_por": kw.get("rev", ""),
            "digitalizo": kw.get("dig", ""), "hc": kw.get("hc", "")}


class ReporteVaciasTests(unittest.TestCase):
    def _build(self):
        planillas = [
            _p("1", "GRUPO_01", True, rev="ROSA"),
            _p("2", "GRUPO_02", False, dig="MELANY", hc="55"),
            _p("3", "GRUPO_02", False),
            _p("4", "GRUPO_03", True),  # resuelta sin selección: anomalía
        ]
        return rep.build_reports(
            "161363", planillas, {"1": 3, "9": 1}, {"1": 2, "9": 1}, {"2": 7, "3": 4, "4": 2},
            now=datetime(2026, 11, 13, 1, 15),
        )

    def test_marca_las_vacias_y_el_motivo(self) -> None:
        r = self._build()
        self.assertEqual([x["tramite"] for x in r["empty"]], ["2", "3", "4"])
        txt = r["txt"]
        self.assertIn("Planillas con CARPETA VACIA: 3", txt)
        self.assertIn("2026-11-13 01:15", txt)
        self.assertIn("solo trae los PDFs seleccionados", txt)
        self.assertIn("GRUPO_02: 2 vacías de 2", txt)
        self.assertIn("PENDIENTE  HC 55 | PDFs en disco: 7 | digitalizó: MELANY", txt)
        self.assertIn("RESUELTA sin selección guardada", txt)

    def test_csv_tiene_todas_las_planillas(self) -> None:
        rows = list(csv.DictReader(io.StringIO(self._build()["csv"].lstrip("﻿"))))
        by = {r["planilla"]: r for r in rows}
        self.assertEqual(by["1"]["resultado"], "CON_PDFS")
        self.assertEqual(by["2"]["resultado"], "CARPETA_VACIA")
        self.assertEqual(by["2"]["pdfs_en_disco"], "7")
        self.assertEqual(by["9"]["grupo"], "(fuera del label)")  # selección sin planilla objetada
        self.assertEqual(len(rows), 5)

    def test_conteo_de_pdfs_en_disco(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for t, names in {"10": ["a.pdf", "B.PDF", "x.txt"], "11": []}.items():
                d = Path(tmp) / "2026" / "HSP02" / t
                d.mkdir(parents=True)
                for n in names:
                    (d / n).write_bytes(b"x")
            (Path(tmp) / "2026" / "HSP02" / "99").mkdir()
            self.assertEqual(rep.disk_pdf_counts(Path(tmp), {"10", "11"}), {"10": 2, "11": 0})


class PantallaPendientesTests(unittest.TestCase):
    def _overview(self, filtro="sin_seleccion"):
        planillas = [
            _p("1", "GRUPO_01", True, rev="ROSA"),
            _p("2", "GRUPO_02", False, dig="MELANY", hc="55"),
            _p("3", "GRUPO_02", False),
            _p("4", "GRUPO_03", True),
            _p("5", "GRUPO_03", False),
        ]
        selection = {"1": {"seleccionados": 3, "usuario": "ROSA", "fecha": "2026-10-07 13:46"},
                     "5": {"seleccionados": 2, "usuario": "JOHANNA", "fecha": "2026-10-27 14:28"}}
        return rep.compose_overview("161363", planillas, selection, {"2": 7}, filtro)

    def test_categorias_y_conteos(self) -> None:
        o = self._overview("todas")
        cat = {r["tramite"]: r["categoria"] for r in o["rows"]}
        self.assertEqual(cat, {"1": "RESUELTA", "2": "PENDIENTE_SIN_SELECCION",
                               "3": "PENDIENTE_SIN_SELECCION", "4": "RESUELTA_SIN_SELECCION",
                               "5": "PENDIENTE_CON_SELECCION"})
        self.assertEqual(o["counts"]["PENDIENTE_SIN_SELECCION"], 2)
        self.assertEqual(o["total"], 5)
        g = {x["grupo"]: x for x in o["by_group"]}
        self.assertEqual(g["GRUPO_02"]["pendientes_sin_seleccion"], 2)
        self.assertEqual(g["GRUPO_02"]["total"], 2)

    def test_filtros(self) -> None:
        self.assertEqual([r["tramite"] for r in self._overview("sin_seleccion")["rows"]], ["2", "3", "4"])
        self.assertEqual([r["tramite"] for r in self._overview("pendientes")["rows"]], ["2", "3", "5"])
        self.assertEqual(len(self._overview("todas")["rows"]), 5)
        self.assertEqual(self._overview("raro")["filtro"], "sin_seleccion")

    def test_csv_exportable(self) -> None:
        rows = list(csv.DictReader(io.StringIO(rep.overview_csv(self._overview()).lstrip("﻿"))))
        self.assertEqual([r["planilla"] for r in rows], ["2", "3", "4"])
        self.assertEqual(rows[0]["categoria"], "Pendiente sin selección")
        self.assertEqual(rows[0]["pdfs_en_disco"], "7")
        self.assertEqual(rows[1]["pdfs_en_disco"], "")


class ComprobacionDeDiscoTests(unittest.TestCase):
    def test_areas_y_comprobacion_directa(self) -> None:
        from unittest.mock import patch
        import oracle_client as oc

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026" / "HSP02" / "100").mkdir(parents=True)
            (root / "2026" / "HSP02" / "100" / "ORS.pdf").write_bytes(b"x")
            (root / "2025" / "EMR09" / "200").mkdir(parents=True)
            (root / "2026" / "archivo_suelto.txt").write_bytes(b"x")  # no es carpeta de área
            with patch.object(oc.Settings, "LOCAL_REPO_ROOT", root):
                areas = oc._repo_area_dirs()
            self.assertEqual(sorted(a.name for a in areas), ["EMR09", "HSP02"])
            hit = lambda t, n: any((a / t / n).is_file() for a in areas)  # noqa: E731
            self.assertTrue(hit("100", "ORS.pdf"))
            self.assertFalse(hit("100", "PI.pdf"))      # archivo que no existe
            self.assertFalse(hit("999", "ORS.pdf"))     # planilla que no existe

    def test_raiz_inexistente_no_falla(self) -> None:
        from unittest.mock import patch
        import oracle_client as oc

        with patch.object(oc.Settings, "LOCAL_REPO_ROOT", Path("/ruta/que/no/existe")):
            self.assertEqual(oc._repo_area_dirs(), [])


if __name__ == "__main__":
    unittest.main()
