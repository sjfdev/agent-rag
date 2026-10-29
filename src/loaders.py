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

from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document

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


def _load_pdf(path: Path) -> List[Document]:
    docs = PyPDFLoader(str(path)).load()
    out = []
    for doc in docs:
        page = doc.metadata.get("page")
        meta = {"kind": "pdf"}
        if isinstance(page, int):
            meta["page"] = page + 1
        out.append(Document(page_content=doc.page_content, metadata=meta))
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


def _load_xlsx(path: Path) -> List[Document]:
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    out = []
    try:
        for sheet in workbook.worksheets:
            lines = []
            for row in sheet.iter_rows(values_only=True, max_row=MAX_ROWS_PER_SHEET):
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    lines.append(" | ".join(cells).rstrip(" |"))
            text = _clean_lines(lines)
            if text:
                out.append(
                    Document(
                        page_content=text,
                        metadata={"kind": "xlsx", "sheet": sheet.title},
                    )
                )
            if sheet.max_row and sheet.max_row > MAX_ROWS_PER_SHEET:
                logger.warning(
                    "%s sheet %s truncated at %s rows",
                    path.name,
                    sheet.title,
                    MAX_ROWS_PER_SHEET,
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
    lines = _table_lines(rows)
    body = _clean_lines(lines)
    return [Document(page_content=body, metadata={"kind": "csv"})] if body else []


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
