"""Parsing enhancements: XLSX/CSV row blocks, PDF tables, OCR fallback."""

import sys
import types

import pytest

from src.config import settings
from src.loaders import _row_blocks, load_file


def test_row_blocks_repeat_header():
    rows = [["item", "qty"]] + [[f"row{i}", i] for i in range(5)]
    blocks = list(_row_blocks(rows, rows_per_block=2, max_rows=100))
    assert len(blocks) == 3
    for header, block, _first, _last in blocks:
        assert header == ["item", "qty"]
        assert block and all("|" in line for line in block)


def test_row_blocks_cap_and_warn(caplog):
    rows = [["h"]] + [[f"r{i}"] for i in range(10)]
    blocks = list(_row_blocks(rows, rows_per_block=3, max_rows=5))
    assert len(blocks) == 2
    assert any("capped" in record.message for record in caplog.records)


def test_row_blocks_header_only():
    blocks = list(_row_blocks([["only", "header"]], rows_per_block=10, max_rows=10))
    assert len(blocks) == 1
    assert blocks[0][0] == ["only", "header"]
    assert blocks[0][1] == []


def test_xlsx_blocks_carry_sheet_and_header(tmp_path, monkeypatch):
    from openpyxl import Workbook

    monkeypatch.setattr(settings, "XLSX_ROWS_PER_CHUNK", 3)
    path = tmp_path / "big.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["name", "score"])
    for i in range(7):
        ws.append([f"u{i}", i])
    wb.save(path)

    docs = load_file(path)
    assert len(docs) >= 2
    for doc in docs:
        assert doc.metadata["sheet"] == "Data"
        assert doc.page_content.splitlines()[0] == "name | score"

    # Every data row is present somewhere: nothing is silently dropped.
    joined = "\n".join(doc.page_content for doc in docs)
    for i in range(7):
        assert f"u{i}" in joined


def test_xlsx_respects_row_block_setting(tmp_path, monkeypatch):
    from openpyxl import Workbook

    monkeypatch.setattr(settings, "XLSX_ROWS_PER_CHUNK", 2)
    path = tmp_path / "small.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append(["h"])
    for i in range(4):
        ws.append([i])
    wb.save(path)
    docs = load_file(path)
    assert len(docs) == 2
    assert all(doc.metadata["sheet"] == "S" for doc in docs)


def test_csv_blocks_repeat_header(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CSV_ROWS_PER_CHUNK", 2)
    path = tmp_path / "rows.csv"
    path.write_text("a,b\n1,2\n3,4\n5,6\n", encoding="utf-8")
    docs = load_file(path)
    assert len(docs) == 2
    for doc in docs:
        assert doc.page_content.splitlines()[0] == "a | b"


def _fake_pdfplumber(tables_by_page):
    module = types.ModuleType("pdfplumber")

    class Page:
        def __init__(self, tables):
            self._tables = tables

        def extract_tables(self):
            return self._tables

    class Pdf:
        def __init__(self, pages):
            self.pages = [Page(t) for t in pages]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class PDF:
        @staticmethod
        def open(_path):
            return Pdf(tables_by_page)

    module.open = PDF.open
    return module


def test_pdf_tables_are_appended(tmp_path, monkeypatch):
    from reportlab.pdfgen import canvas

    path = tmp_path / "table.pdf"
    c = canvas.Canvas(str(path))
    c.drawString(100, 750, "Revenue summary")
    c.showPage()
    c.save()

    # extract_tables() returns a list of tables; each table is a list of rows.
    monkeypatch.setitem(
        sys.modules, "pdfplumber", _fake_pdfplumber([[[["a", "b"], ["1", "2"]]]])
    )
    docs = load_file(path)
    text = "\n".join(doc.page_content for doc in docs)
    assert "Revenue summary" in text
    assert "a | b" in text


def test_pdf_without_pdfplumber_still_works(tmp_path, monkeypatch):
    from reportlab.pdfgen import canvas

    path = tmp_path / "plain.pdf"
    c = canvas.Canvas(str(path))
    c.drawString(100, 750, "Plain content")
    c.showPage()
    c.save()

    monkeypatch.setitem(sys.modules, "pdfplumber", None)
    docs = load_file(path)
    assert "Plain content" in "\n".join(doc.page_content for doc in docs)


def test_pdf_table_extraction_can_be_disabled(tmp_path, monkeypatch):
    from reportlab.pdfgen import canvas

    path = tmp_path / "table2.pdf"
    c = canvas.Canvas(str(path))
    c.drawString(100, 750, "Body")
    c.showPage()
    c.save()

    monkeypatch.setattr(settings, "PDF_TABLES_ENABLED", False)
    monkeypatch.setitem(sys.modules, "pdfplumber", _fake_pdfplumber([[[["x", "y"]]]]))
    docs = load_file(path)
    assert "x | y" not in "\n".join(doc.page_content for doc in docs)


@pytest.fixture()
def fake_ocr(monkeypatch):
    module = types.ModuleType("pytesseract")
    module.image_to_string = lambda image, lang=None: "OCR TEXT FROM PAGE"
    monkeypatch.setitem(sys.modules, "pytesseract", module)

    pdf2image = types.ModuleType("pdf2image")
    pdf2image.convert_from_path = lambda *args, **kwargs: [object()]
    monkeypatch.setitem(sys.modules, "pdf2image", pdf2image)
    return module


def test_ocr_used_when_page_has_little_text(tmp_path, monkeypatch, fake_ocr):
    from reportlab.pdfgen import canvas

    path = tmp_path / "scan.pdf"
    c = canvas.Canvas(str(path))
    # A page with no text at all stands in for a scanned image.
    c.showPage()
    c.save()

    monkeypatch.setattr(settings, "OCR_ENABLED", True)
    monkeypatch.setattr(settings, "OCR_MIN_CHARS", 20)
    docs = load_file(path)
    assert any("OCR TEXT FROM PAGE" in doc.page_content for doc in docs)
    assert any(doc.metadata.get("ocr") == "1" for doc in docs)


def test_ocr_skipped_when_disabled(tmp_path, monkeypatch, fake_ocr):
    from reportlab.pdfgen import canvas

    path = tmp_path / "scan2.pdf"
    c = canvas.Canvas(str(path))
    c.showPage()
    c.save()

    monkeypatch.setattr(settings, "OCR_ENABLED", False)
    docs = load_file(path)
    assert not any("OCR TEXT FROM PAGE" in doc.page_content for doc in docs)


def test_ocr_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    from reportlab.pdfgen import canvas

    path = tmp_path / "scan3.pdf"
    c = canvas.Canvas(str(path))
    c.showPage()
    c.save()

    bad = types.ModuleType("pytesseract")
    bad.image_to_string = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ocr boom"))
    monkeypatch.setitem(sys.modules, "pytesseract", bad)
    pdf2image = types.ModuleType("pdf2image")
    pdf2image.convert_from_path = lambda *a, **k: [object()]
    monkeypatch.setitem(sys.modules, "pdf2image", pdf2image)

    monkeypatch.setattr(settings, "OCR_ENABLED", True)
    docs = load_file(path)  # must not raise
    assert isinstance(docs, list)


def test_loader_metadata_keys_are_stable(tmp_path):
    """Ingestion derives the Milvus schema from metadata, so keys must not drift."""

    (tmp_path / "a.json").write_text('{"k": "v"}', encoding="utf-8")
    (tmp_path / "a.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    allowed = {"kind", "page", "slide", "sheet", "title", "ocr"}
    for path in (tmp_path / "a.json", tmp_path / "a.csv"):
        for doc in load_file(path):
            assert set(doc.metadata) <= allowed, doc.metadata
