"""File loaders for the ingestion pipeline.

Every loader returns a list of ``Document`` objects whose ``metadata`` contains
``kind`` and an optional positional field (``page`` / ``slide`` / ``sheet``) so
citations stay meaningful across formats. Text is extracted only; chunking is
handled by ``EnterpriseETL``.
"""

import csv
import io
import json
import logging
from pathlib import Path
from typing import List

from langchain_core.documents import Document

from src.config import settings

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".csv",
    ".txt",
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".json",
}

# Formats that need an external converter; reported with a hint instead of
# failing silently.
LEGACY_EXTENSIONS = {".doc": ".docx", ".ppt": ".pptx", ".xls": ".xlsx"}

MAX_ROWS_PER_SHEET = 5000


def _clean_lines(lines: List[str]) -> str:
    return "\n".join(line for line in lines if line and line.strip())


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    try:
        import chardet

        guessed = chardet.detect(data).get("encoding") or "utf-8"
        return data.decode(guessed, errors="replace")
    except Exception:
        return data.decode("utf-8", errors="replace")


def _pdf_tables(path: Path) -> dict:
    """Structured table rows per page via pdfplumber, when it is installed.

    Returns ``{page_number: [line, ...]}``. Any failure is logged and ignored:
    table extraction is a quality improvement, not a prerequisite.
    """
    if not settings.PDF_TABLES_ENABLED:
        return {}
    try:
        import pdfplumber
    except Exception:
        logger.debug("pdfplumber not installed; PDF tables are skipped")
        return {}
    tables: dict = {}
    try:
        with pdfplumber.open(str(path)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                try:
                    extracted = page.extract_tables() or []
                except Exception:
                    logger.debug("table extraction failed on page %s", number)
                    continue
                lines: List[str] = []
                for table in extracted:
                    lines.extend(_table_lines(table))
                if lines:
                    tables[number] = lines
    except Exception:
        logger.warning("pdfplumber failed on %s; using text only", path, exc_info=True)
        return {}
    return tables


def _ocr_page(path: Path, page_number: int) -> str:
    """OCR one PDF page; returns "" when the toolchain is unavailable."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except Exception:
        logger.debug("OCR dependencies missing; cannot OCR page %s", page_number)
        return ""
    try:
        images = convert_from_path(
            str(path), first_page=page_number, last_page=page_number, dpi=200
        )
        if not images:
            return ""
        return pytesseract.image_to_string(images[0], lang=settings.OCR_LANG) or ""
    except Exception:
        logger.warning("OCR failed for %s page %s", path, page_number, exc_info=True)
        return ""


def _load_pdf(path: Path) -> List[Document]:
    # Imported lazily: the loader pulls in pypdf, which is unnecessary for
    # non-PDF ingestion and slows down importing `src.loaders` in tests.
    from langchain_community.document_loaders import PyPDFLoader

    docs = PyPDFLoader(str(path)).load()
    tables = _pdf_tables(path)
    out = []
    ocr_used = 0
    for doc in docs:
        page = doc.metadata.get("page")
        page_number = page + 1 if isinstance(page, int) else None
        text = doc.page_content or ""

        if page_number and page_number in tables:
            # Keep the parsed text first; append rows so table cells stay
            # searchable even when the PDF layout splits them.
            text = _clean_lines([text.strip(), *tables[page_number]])

        if (
            settings.OCR_ENABLED
            and page_number
            and len(text.strip()) < settings.OCR_MIN_CHARS
        ):
            ocr_text = _ocr_page(path, page_number)
            if ocr_text.strip():
                text = _clean_lines([text.strip(), ocr_text])
                ocr_used += 1

        if not text.strip():
            continue
        meta = {"kind": "pdf"}
        if page_number:
            meta["page"] = page_number
        # Keep the metadata keys stable: the Milvus schema is created from the
        # first batch, so adding a key here would require a full re-ingestion.
        if settings.OCR_ENABLED and page_number and ocr_used:
            meta["ocr"] = "1"
        out.append(Document(page_content=text, metadata=meta))

    if not out:
        logger.warning("%s produced no extractable text (scanned PDF?)", path.name)
    return out


def _table_lines(rows) -> List[str]:
    lines = []
    for row in rows:
        cells = ["" if cell is None else str(cell).strip() for cell in row]
        if any(cells):
            lines.append(" | ".join(cells).rstrip(" |"))
    return lines


def _load_docx(path: Path) -> List[Document]:
    from docx import Document as DocxDocument

    doc = DocxDocument(str(path))
    parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        parts.extend(_table_lines([[c.text for c in row.cells] for row in table.rows]))

    meta = {"kind": "docx"}
    title = (doc.core_properties.title or "").strip()
    if title:
        meta["title"] = title
    text = _clean_lines(parts)
    return [Document(page_content=text, metadata=meta)] if text else []


def _load_pptx(path: Path) -> List[Document]:
    from pptx import Presentation

    prs = Presentation(str(path))
    out = []
    for index, slide in enumerate(prs.slides, start=1):
        parts = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                parts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False):
                parts.extend(
                    _table_lines(
                        [
                            [cell.text for cell in row.cells]
                            for row in shape.table.rows
                        ]
                    )
                )
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text
            if notes and notes.strip():
                parts.append(notes)

        text = _clean_lines(parts)
        if text:
            out.append(
                Document(page_content=text, metadata={"kind": "pptx", "slide": index})
            )
    return out


def _row_blocks(rows, rows_per_block: int, max_rows: int):
    """Split rows into blocks that repeat the header row.

    Yields ``(header_line, [row lines], first_row, last_row)`` so every block is
    self-contained: a retrievable chunk still says which columns it belongs to.
    A sheet that exceeds ``max_rows`` is capped and reported, never silently cut.
    """
    header: List[str] = []
    block: List[str] = []
    first = 0
    seen = 0
    emitted = 0
    truncated = False
    for row in rows:
        cells = ["" if cell is None else str(cell).strip() for cell in row]
        if not any(cells):
            continue
        if not header:
            header = cells
            seen = 1
            continue
        if seen >= max_rows:
            truncated = True
            break
        if not block:
            first = seen
        block.append(" | ".join(cells).rstrip(" |"))
        seen += 1
        if len(block) >= rows_per_block:
            yield header, block, first, first + len(block) - 1
            emitted += 1
            block = []
    if block:
        yield header, block, first, first + len(block) - 1
        emitted += 1
    elif emitted == 0 and header:
        # Header-only table (single-row CSV, empty sheet with column names):
        # keep it searchable instead of dropping the file entirely.
        yield header, [], 1, 1
    if truncated:
        logger.warning(
            "table capped at %s rows; remaining rows were not indexed", max_rows
        )


def _block_text(header: List[str], block: List[str]) -> str:
    lines = [" | ".join(header).rstrip(" |")] if header else []
    lines.extend(block)
    return _clean_lines(lines)


def _load_xlsx(path: Path) -> List[Document]:
    from openpyxl import load_workbook

    rows_per_block = max(int(settings.XLSX_ROWS_PER_CHUNK), 1)
    max_rows = max(int(settings.XLSX_MAX_ROWS), rows_per_block)
    workbook = load_workbook(str(path), read_only=True, data_only=True)
    out = []
    try:
        for sheet in workbook.worksheets:
            for header, block, _first, _last in _row_blocks(
                sheet.iter_rows(values_only=True), rows_per_block, max_rows
            ):
                text = _block_text(header, block)
                if text:
                    out.append(
                        Document(
                            page_content=text,
                            metadata={"kind": "xlsx", "sheet": sheet.title},
                        )
                    )
    finally:
        workbook.close()
    return out


def _load_csv(path: Path) -> List[Document]:
    text = read_text(path)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows_per_block = max(int(settings.CSV_ROWS_PER_CHUNK), 1)
    out = []
    # csv rows are plain strings; _row_blocks expects iterables of cells, which
    # a row already is, so it can be reused unchanged.
    for header, block, _first, _last in _row_blocks(
        rows, rows_per_block, max_rows=max(len(rows), 1)
    ):
        body = _block_text(header, block)
        if body:
            out.append(Document(page_content=body, metadata={"kind": "csv"}))
    return out


def _load_html(path: Path) -> List[Document]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(read_text(path), "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    meta = {"kind": "html"}
    if soup.title and soup.title.string:
        meta["title"] = soup.title.string.strip()
    text = _clean_lines(soup.get_text("\n").splitlines())
    return [Document(page_content=text, metadata=meta)] if text else []


def _load_plain(path: Path, kind: str) -> List[Document]:
    text = read_text(path)
    if kind == "json":
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, ValueError):
            pass
    text = _clean_lines(text.splitlines())
    return [Document(page_content=text, metadata={"kind": kind})] if text else []


def load_file(path: Path) -> List[Document]:
    """Extract text blocks from a supported file, preserving positional metadata."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _load_pdf(path)
    if suffix == ".docx":
        return _load_docx(path)
    if suffix == ".pptx":
        return _load_pptx(path)
    if suffix == ".xlsx":
        return _load_xlsx(path)
    if suffix == ".csv":
        return _load_csv(path)
    if suffix in {".html", ".htm"}:
        return _load_html(path)
    if suffix in {".txt", ".md", ".markdown"}:
        return _load_plain(path, "markdown" if suffix in {".md", ".markdown"} else "text")
    if suffix == ".json":
        return _load_plain(path, "json")
    raise ValueError(f"unsupported file type: {suffix}")
