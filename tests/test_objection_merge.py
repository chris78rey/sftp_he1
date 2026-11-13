from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import objection_merge as om  # noqa: E402


def _pdf(path: Path, pages: int) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    with open(path, "wb") as fh:
        writer.write(fh)


class ReglasRustTests(unittest.TestCase):
    def test_clasificacion_igual_que_rust(self) -> None:
        c = om.classify_pdf
        self.assertEqual(c("PI.pdf"), ("PI", False))
        self.assertEqual(c("pi.PDF"), ("PI", False))
        self.assertEqual(c("PI_01.pdf"), ("PI", True))
        self.assertEqual(c("PI-01.pdf"), ("PI", True))
        self.assertEqual(c("AES (copia).pdf"), ("AES", True))
        self.assertEqual(c("PI.copia.pdf"), ("PI", True))
        self.assertEqual(c("RHd-copia_extra.PDF"), ("RHD", True))
        self.assertEqual(c("ORS_0.pdf"), ("ORS", True))
        self.assertEqual(c("018A.pdf"), ("018A", False))
        self.assertIsNone(c("PI13.pdf"))
        self.assertIsNone(c("PIx.pdf"))
        self.assertIsNone(c("PI+1.pdf"))
        self.assertIsNone(c("notas.txt"))

    def test_orden_canonico_primero_y_fragmentos_numericos(self) -> None:
        disk = ["ORS_10.pdf", "ORS.pdf", "ORS_2.pdf", "ORS_0.pdf", "PI.pdf"]
        groups = om.merge_groups(disk, {"ORS_0.PDF", "ORS_10.PDF", "ORS_2.PDF"})
        self.assertEqual(groups, [("ORS", ["ORS.pdf", "ORS_0.pdf", "ORS_2.pdf", "ORS_10.pdf"])])

    def test_un_fragmento_sin_canonico_no_se_funde(self) -> None:
        self.assertEqual(om.merge_groups(["ORS_0.pdf"], {"ORS_0.PDF"}), [])

    def test_fragmento_no_pedido_no_entra(self) -> None:
        groups = om.merge_groups(["ORS.pdf", "ORS_0.pdf", "ORS_1.pdf"], {"ORS.PDF", "ORS_0.PDF"})
        self.assertEqual(groups, [("ORS", ["ORS.pdf", "ORS_0.pdf"])])


class FusionTests(unittest.TestCase):
    def _folder(self, tmp: str, *, corrupt: bool = False) -> Path:
        folder = Path(tmp) / "2026" / "HSP02" / "5844761"
        folder.mkdir(parents=True)
        _pdf(folder / "ORS.pdf", 3)
        if corrupt:
            (folder / "ORS_0.pdf").write_bytes(b"no es un pdf")
        else:
            _pdf(folder / "ORS_0.pdf", 1)
        _pdf(folder / "PI.pdf", 2)
        return folder

    def test_plan_y_fusion_de_la_planilla_5844761(self) -> None:
        from pypdf import PdfReader
        import io

        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp)
            before = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in folder.iterdir())
            resolved = [("5844761", n, folder / n) for n in ("ORS.pdf", "ORS_0.pdf", "PI.pdf")]
            groups, consumed = om.plan_merges(resolved)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["output_name"], "ORS.pdf")
            self.assertEqual(groups[0]["members"], ["ORS.pdf", "ORS_0.pdf"])
            self.assertEqual(consumed, {("5844761", "ORS.pdf"), ("5844761", "ORS_0.pdf")})
            data, pages = om.merge_pdfs(groups[0]["paths"])
            self.assertEqual(pages, 4)
            self.assertEqual(len(PdfReader(io.BytesIO(data)).pages), 4)
            after = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in folder.iterdir())
            self.assertEqual(before, after, "no debe modificar el disco")

    def test_pdf_corrupto_lanza_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp, corrupt=True)
            with self.assertRaises(Exception):
                om.merge_pdfs([folder / "ORS.pdf", folder / "ORS_0.pdf"])

    def test_sin_fragmento_pedido_no_hay_grupos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp)
            resolved = [("5844761", n, folder / n) for n in ("ORS.pdf", "PI.pdf")]
            groups, consumed = om.plan_merges(resolved)
            self.assertEqual(groups, [])
            self.assertEqual(consumed, set())


if __name__ == "__main__":
    unittest.main()
