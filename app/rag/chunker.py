from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pdfplumber


# ── Data Model ─────────────────────────────────────────────────────────────────

@dataclass
class TextChunk:
    chunk_id: str
    text: str
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    source_file: str
    # Structure metadata — populated by smart_chunk_pdf
    chunk_type: str = "text"              # table_row | section | checkbox | paragraph | text
    section_heading: str = ""             # parent heading detected from document structure
    interval_hint: Optional[int] = None   # PM interval hours detected from section context
    table_data: Optional[dict] = None     # column→value map for table_row chunks
    # Citation metadata — required for manager's production format
    content_type: str = "procedure"       # procedure | warning | loto | ppe | parts_list | startup | toc | table
    manual_id: str = ""                   # FK to manual_uploads.manual_id
    manual_version: str = ""              # e.g. "Rev-C" detected from doc header


# ── Content Type Detection ─────────────────────────────────────────────────────

_CONTENT_TYPE_RULES: list[tuple[str, list[str]]] = [
    ("warning",    ["warning", "danger", "caution", "hazard", "warnung", "gefahr", "vorsicht", "achtung"]),
    ("loto",       ["lockout", "tagout", "loto", "energy isolation", "de-energi", "absperren",
                    "verriegeln", "lock out", "energie", "isolation procedure"]),
    ("ppe",        ["ppe", "protective equipment", "safety gear", "glove", "goggle",
                    "helmet", "harness", "respirator", "schutzausrüstung"]),
    ("parts_list", ["spare part", "part number", "consumable", "bill of material", "material list",
                    "ersatzteil", "part no", "article number", "bestell"]),
    ("startup",    ["startup", "start-up", "restart", "re-start", "commissioning",
                    "inbetriebnahme", "after maintenance", "nach wartung", "recommission"]),
    ("toc",        ["table of contents", "contents", "inhaltsverzeichnis", "index",
                    "maintenance schedule", "service schedule", "wartungsplan"]),
]


def _detect_content_type(text: str, heading: str, chunk_type: str) -> str:
    """Classify a chunk into a content type used by the 8-pass retrieval strategy."""
    if chunk_type == "table_row":
        return "table"
    combined = (heading + " " + text[:400]).lower()
    for ct, keywords in _CONTENT_TYPE_RULES:
        if any(kw in combined for kw in keywords):
            return ct
    return "procedure"


# ── Heading Detection ──────────────────────────────────────────────────────────

_HEADING_PATTERNS: list[re.Pattern] = [
    re.compile(r'^\d+\.\d+(?:\.\d+)*\s+\S'),                       # 12.5.1 Title
    re.compile(r'^(?:CHAPTER|KAPITEL)\s+\d+', re.I),                # CHAPTER 12
    re.compile(r'^(?:Section|Abschnitt)\s+\d+(?:\.\d+)*', re.I),   # Section 12.3
    re.compile(r'^\d{1,6}\s*[-–]\s*(?:HOURS?|HRS?)\b', re.I),      # 500 - HOURS
    re.compile(r'^\d{1,6}\s*(?:HOURS?|HRS?|H)\b', re.I),           # 500 HOURS
    re.compile(r'^(?:DAILY|WEEKLY|BIWEEKLY|MONTHLY|QUARTERLY|SEMI.?ANNUAL|ANNUAL|YEARLY)\b', re.I),
    re.compile(r'^(?:TÄGLICH|WÖCHENTLICH|MONATLICH|VIERTELJÄHRLICH|HALBJÄHRLICH|JÄHRLICH)\b', re.I),
    re.compile(r'^(?:PREVENTIVE|PERIODIC|SCHEDULED|PLANNED)\s+MAINTENANCE\b', re.I),
    re.compile(r'^(?:ROUTINE|SCHEDULED)\s+(?:SERVICE|CHECK|INSPECTION)\b', re.I),
]

# Ordered longest-match first so "semi-annual" beats "annual", "3 month" beats "monthly"
_INTERVAL_KEYWORD_MAP: list[tuple[str, int]] = [
    ('semi-annual', 3000), ('semi annual', 3000), ('halbjährlich', 3000),
    ('quarterly', 1500), ('vierteljährlich', 1500), ('3 month', 1500),
    ('biweekly', 240), ('bi-weekly', 240), ('fortnightly', 240), ('2 week', 240),
    ('annual', 6000), ('yearly', 6000), ('jährlich', 6000),
    ('monthly', 500), ('monatlich', 500),
    ('weekly', 120), ('wöchentlich', 120),
    ('daily', 8), ('täglich', 8),
]

_INTERVAL_NUMERIC_RE = re.compile(
    r'(\d{1,6}[,.]?\d{0,3})\s*(?:operating\s+)?(?:hours?|hrs?|h\b|betriebsstunden|stunden)',
    re.I,
)

# Matches "approx. every 7 years", "every 7-year", "7 years", etc.
_INTERVAL_YEAR_RE = re.compile(r'(\d+)\s*[-–]?\s*years?', re.I)

# Lines that start with a checkbox / bullet indicator
_CHECKBOX_LINE_RE = re.compile(
    r'^[ \t]*(?:[□☐☑✓✔○●]'
    r'|\[\s*\]|\(\s*\)|[•\-\*])\s+(.{5,200})$',
    re.M,
)


def _detect_interval(text: str) -> Optional[int]:
    """Return the most specific PM interval (hours) found in text, or None."""
    low = text.lower()
    for keyword, hours in _INTERVAL_KEYWORD_MAP:
        if keyword in low:
            return hours
    # "approx. every 7 years" / "7-year" / "7 years" → 7 * 6000 = 42000h
    m_yr = _INTERVAL_YEAR_RE.search(text)
    if m_yr:
        return int(m_yr.group(1)) * 6000
    m = _INTERVAL_NUMERIC_RE.search(text)
    if m:
        try:
            # Strip thousands separators: "45,000" → 45000, "4.000" (German) → 4000
            return int(m.group(1).replace(',', '').replace('.', ''))
        except (ValueError, AttributeError):
            return None
    return None


def _is_heading(line: str, min_len: int = 4, max_len: int = 120) -> bool:
    s = line.strip()
    if not (min_len <= len(s) <= max_len):
        return False
    return any(p.match(s) for p in _HEADING_PATTERNS)


def _wc(text: str) -> int:
    return len(text.split())


# ── Pass 1: Table Row Extraction ───────────────────────────────────────────────

_TABLE_SCAN_KW = frozenset({
    'interval', 'hours', 'maintenance', 'lubrication', 'inspection',
    'replace', 'check', 'clean', 'service', 'wartung', 'filter',
    'grease', 'oil', 'torque', 'tighten', 'inspect', 'calibrate',
    'hrs', 'betriebsstunden', 'überprüfen', 'wechseln',
})
_TABLE_PAGE_CAP = 150   # never scan more than this many pages for tables


def _extract_table_chunks(pdf, source_file: str, manual_id: str = "", manual_version: str = "") -> list[TextChunk]:
    """
    Extract every structured table row as its own chunk.
    Highest-precision path — no word-window approximation.

    Speed: extract_text() (fast) gates each page before the slow
    extract_tables() call.  Hard cap at _TABLE_PAGE_CAP pages so
    very large PDFs don't take 10+ minutes.
    """
    chunks: list[TextChunk] = []
    idx = 0

    for page in pdf.pages[:_TABLE_PAGE_CAP]:
        pn = page.page_number
        # Fast gate: only run the slow extract_tables() on pages that
        # actually contain maintenance-related text.
        page_text_lower = (page.extract_text() or '').lower()
        if not any(kw in page_text_lower for kw in _TABLE_SCAN_KW):
            continue
        # Capture interval from page text — Krones format puts interval in the
        # heading ABOVE the table ("Interval: Every 120 Operating Hours"), not inside cells
        page_interval = _detect_interval(page_text_lower)

        for table in (page.extract_tables() or []):
            if not table or len(table) < 2:
                continue
            max_cols = max((len(r) for r in table if r), default=0)
            if max_cols < 2:
                continue

            # Detect header row (first row with ≥ 2 non-empty cells)
            raw_headers: list[str] = []
            data_start = 0
            for ri, row in enumerate(table[:4]):
                populated = [str(c or '').strip() for c in row if c and str(c).strip()]
                if len(populated) >= 2:
                    raw_headers = [str(c or '').strip().lower() for c in row]
                    data_start = ri + 1
                    break
            if not raw_headers:
                raw_headers = [f'col_{i}' for i in range(max_cols)]
                data_start = 0

            for row in table[data_start:]:
                if not row or all(not c or str(c).strip() == '' for c in row):
                    continue
                cells = [str(c or '').strip() for c in row]
                row_data = {h: v for h, v in zip(raw_headers, cells) if v}
                if not row_data:
                    continue

                row_text = ' | '.join(f'{h}: {v}' for h, v in row_data.items())
                # Use interval from row text first, fall back to page-level interval
                interval = _detect_interval(row_text) or page_interval

                chunks.append(TextChunk(
                    chunk_id=f'{source_file}__tbl_p{pn}_{idx:04d}',
                    text=f'[TABLE ROW] {row_text}',
                    page_start=pn, page_end=pn,
                    char_start=0, char_end=len(row_text),
                    source_file=source_file,
                    chunk_type='table_row',
                    interval_hint=interval,
                    table_data=row_data,
                    content_type='table',
                    manual_id=manual_id,
                    manual_version=manual_version,
                ))
                idx += 1

    return chunks


# ── Pass 2: Section-Based Text Extraction ─────────────────────────────────────

def _extract_section_chunks(
    pdf,
    source_file: str,
    max_section_words: int,
    min_section_words: int,
    manual_id: str = "",
    manual_version: str = "",
) -> list[TextChunk]:
    """
    Walk every page line-by-line.  Whenever a heading pattern is detected,
    flush the accumulated lines as a section chunk and start a new one.
    Sections longer than max_section_words are split by paragraph boundary.
    """
    chunks: list[TextChunk] = []
    idx = 0
    current_heading = ''
    current_interval: Optional[int] = None
    current_lines: list[str] = []
    current_page_start = 1

    def _flush(page_end: int) -> None:
        nonlocal idx
        if not current_lines:
            return
        body = '\n'.join(current_lines)
        if _wc(body) < min_section_words:
            return
        prefix = f'[SECTION: {current_heading}]\n' if current_heading else ''
        ct = _detect_content_type(body, current_heading, 'section')
        for sub in _split_long_section(
            prefix + body, source_file, idx,
            current_heading, current_page_start, page_end,
            current_interval, max_section_words, ct, manual_id, manual_version,
        ):
            chunks.append(sub)
            idx += 1

    for page in pdf.pages[:_TABLE_PAGE_CAP]:
        pn = page.page_number
        for line in (page.extract_text() or '').split('\n'):
            stripped = line.strip()
            if not stripped:
                continue
            if _is_heading(stripped):
                _flush(pn)
                current_heading = stripped
                current_interval = _detect_interval(stripped)
                current_lines = []
                current_page_start = pn
            else:
                current_lines.append(stripped)
                if current_interval is None:
                    current_interval = _detect_interval(stripped)

    if pdf.pages:
        _flush(pdf.pages[-1].page_number)

    return chunks


def _split_long_section(
    text: str,
    source_file: str,
    start_idx: int,
    heading: str,
    page_start: int,
    page_end: int,
    interval: Optional[int],
    max_words: int,
    content_type: str = "procedure",
    manual_id: str = "",
    manual_version: str = "",
) -> list[TextChunk]:
    """Split an oversized section at paragraph boundaries, repeating heading context."""
    if _wc(text) <= max_words:
        return [TextChunk(
            chunk_id=f'{source_file}__sec_{start_idx:04d}',
            text=text,
            page_start=page_start, page_end=page_end,
            char_start=0, char_end=len(text),
            source_file=source_file,
            chunk_type='section',
            section_heading=heading,
            interval_hint=interval,
            content_type=content_type,
            manual_id=manual_id,
            manual_version=manual_version,
        )]

    prefix = f'[SECTION: {heading}]\n' if heading else ''
    paragraphs = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    result: list[TextChunk] = []
    buf: list[str] = []
    buf_words = 0
    sub_idx = start_idx

    for para in paragraphs:
        pw = _wc(para)
        if buf_words + pw > max_words and buf:
            result.append(TextChunk(
                chunk_id=f'{source_file}__sec_{sub_idx:04d}',
                text=prefix + '\n\n'.join(buf),
                page_start=page_start, page_end=page_end,
                char_start=0, char_end=0,
                source_file=source_file,
                chunk_type='paragraph',
                section_heading=heading,
                interval_hint=interval,
                content_type=content_type,
                manual_id=manual_id,
                manual_version=manual_version,
            ))
            sub_idx += 1
            buf, buf_words = [para], pw
        else:
            buf.append(para)
            buf_words += pw

    if buf:
        result.append(TextChunk(
            chunk_id=f'{source_file}__sec_{sub_idx:04d}',
            text=prefix + '\n\n'.join(buf),
            page_start=page_start, page_end=page_end,
            char_start=0, char_end=0,
            source_file=source_file,
            chunk_type='paragraph',
            section_heading=heading,
            interval_hint=interval,
            content_type=content_type,
            manual_id=manual_id,
            manual_version=manual_version,
        ))

    return result


# ── Pass 3: Checkbox / Bullet Refinement ──────────────────────────────────────

def _extract_checkbox_chunks(section_chunks: list[TextChunk]) -> list[TextChunk]:
    """
    For sections that are checkbox/bullet task lists, split each item into its
    own chunk so the AI gets one task per chunk — not a wall of mixed content.
    Sections without checkbox patterns pass through unchanged.
    """
    result: list[TextChunk] = []
    for chunk in section_chunks:
        matches = list(_CHECKBOX_LINE_RE.finditer(chunk.text))
        if len(matches) < 2:
            result.append(chunk)
            continue
        heading_prefix = f'{chunk.section_heading}: ' if chunk.section_heading else ''
        for i, m in enumerate(matches):
            item_text = m.group(1).strip()
            result.append(TextChunk(
                chunk_id=f'{chunk.chunk_id}__cb{i:03d}',
                text=f'[TASK ITEM] {heading_prefix}{item_text}',
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                char_start=m.start(),
                char_end=m.end(),
                source_file=chunk.source_file,
                chunk_type='checkbox',
                section_heading=chunk.section_heading,
                interval_hint=chunk.interval_hint,
                content_type=chunk.content_type,
                manual_id=chunk.manual_id,
                manual_version=chunk.manual_version,
            ))
    return result


# ── Public API ─────────────────────────────────────────────────────────────────

def _parse_with_doc_intelligence(
    pdf_path: Path,
    source_file: str,
    manual_id: str,
    manual_version: str,
) -> list[TextChunk]:
    """
    Azure Document Intelligence parser (prebuilt-layout model).
    Returns TextChunks with accurate page_start/page_end, section headings, and table structure.
    Falls back to pdfplumber if ADI is not configured or call fails.
    """
    from app.config import get_settings
    settings = get_settings()

    if not settings.azure_doc_intelligence_endpoint or not settings.azure_doc_intelligence_key:
        return []  # Not configured — caller falls back to pdfplumber

    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential

        client = DocumentIntelligenceClient(
            endpoint=settings.azure_doc_intelligence_endpoint,
            credential=AzureKeyCredential(settings.azure_doc_intelligence_key),
        )

        with open(pdf_path, "rb") as f:
            poller = client.begin_analyze_document(
                model_id=settings.azure_doc_intelligence_model,
                body=f,
                content_type="application/octet-stream",
            )
        result = poller.result()

        chunks: list[TextChunk] = []
        chunk_counter = 0

        # Extract paragraphs with page numbers
        current_heading = ""
        for para in (result.paragraphs or []):
            text = para.content or ""
            if not text.strip():
                continue

            page_start = page_end = 0
            if para.bounding_regions:
                page_start = para.bounding_regions[0].page_number
                page_end = para.bounding_regions[-1].page_number

            role = getattr(para, "role", None) or ""
            is_heading = role in ("title", "sectionHeading", "heading")
            if is_heading:
                current_heading = text.strip()

            ct = _detect_content_type(text, current_heading, "section")
            chunk_id = f"{source_file}::adi::{chunk_counter}"
            chunks.append(TextChunk(
                chunk_id=chunk_id,
                text=text,
                page_start=page_start,
                page_end=page_end,
                char_start=0,
                char_end=len(text),
                source_file=source_file,
                chunk_type="section" if not is_heading else "text",
                section_heading=current_heading,
                interval_hint=_detect_interval(text),
                content_type=ct,
                manual_id=manual_id,
                manual_version=manual_version,
            ))
            chunk_counter += 1

        # Extract tables with page numbers
        for table in (result.tables or []):
            page_start = page_end = 0
            if table.bounding_regions:
                page_start = table.bounding_regions[0].page_number
                page_end = table.bounding_regions[-1].page_number

            # Build row text from cells
            rows: dict[int, list[str]] = {}
            for cell in (table.cells or []):
                rows.setdefault(cell.row_index, []).append(cell.content or "")

            for row_idx, cells in sorted(rows.items()):
                row_text = " | ".join(c.strip() for c in cells if c.strip())
                if not row_text or len(row_text) < 5:
                    continue
                chunk_id = f"{source_file}::adi::tbl::{chunk_counter}"
                chunks.append(TextChunk(
                    chunk_id=chunk_id,
                    text=row_text,
                    page_start=page_start,
                    page_end=page_end,
                    char_start=0,
                    char_end=len(row_text),
                    source_file=source_file,
                    chunk_type="table_row",
                    section_heading=current_heading,
                    interval_hint=_detect_interval(row_text),
                    content_type="table",
                    manual_id=manual_id,
                    manual_version=manual_version,
                ))
                chunk_counter += 1

        return chunks

    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Azure Document Intelligence failed (%s) — falling back to pdfplumber", exc
        )
        return []


def smart_chunk_pdf(
    pdf_path: Path,
    source_file: str,
    max_section_words: int = 800,
    min_section_words: int = 20,
    manual_id: str = "",
    manual_version: str = "",
) -> list[TextChunk]:
    """
    Structure-aware PDF chunking — preferred entry point.

    Parser priority:
      1. Azure Document Intelligence — accurate page numbers, tables, headings (production)
      2. pdfplumber structure-aware  — table rows → section → checkbox (local dev)
      3. Sliding window              — last-resort fallback only
    """
    # Try Azure Document Intelligence first (production path)
    adi_chunks = _parse_with_doc_intelligence(pdf_path, source_file, manual_id, manual_version)
    if adi_chunks:
        return adi_chunks

    # pdfplumber path (local dev / ADI not configured)
    with pdfplumber.open(str(pdf_path)) as pdf:
        table_chunks = _extract_table_chunks(pdf, source_file, manual_id, manual_version)
        section_chunks = _extract_section_chunks(
            pdf, source_file, max_section_words, min_section_words, manual_id, manual_version
        )
        refined_chunks = _extract_checkbox_chunks(section_chunks)

    all_chunks = table_chunks + refined_chunks

    if not all_chunks:
        # Absolute last resort — generic sliding window
        full_text, page_offsets = extract_text_from_pdf(pdf_path)
        return chunk_text(full_text, source_file, page_offsets=page_offsets)

    return all_chunks


_TEXT_PAGE_CAP = 60   # extract text from at most this many pages (enough for 15 000-char sample)
_TEXT_CHAR_CAP = 25000  # stop reading pages once we have this many chars


def extract_text_from_pdf(pdf_path: Path) -> tuple[str, dict[int, int]]:
    """Extract text from the first _TEXT_PAGE_CAP pages (enough for classification)."""
    full_text = ''
    page_offsets: dict[int, int] = {}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages[:_TEXT_PAGE_CAP], start=1):
            page_offsets[i] = len(full_text)
            full_text += (page.extract_text() or '') + '\n'
            if len(full_text) >= _TEXT_CHAR_CAP:
                break
    return full_text, page_offsets


def chunk_text(
    text: str,
    source_file: str,
    chunk_size: int = 500,
    overlap: int = 103,
    page_offsets: Optional[dict[int, int]] = None,
) -> list[TextChunk]:
    """
    Sliding-window word-based chunking.
    Called only as the fallback inside smart_chunk_pdf when no structure is detected.
    """
    words = text.split()
    chunks: list[TextChunk] = []
    step = max(1, chunk_size - overlap)
    start = 0
    idx = 0

    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunk_words = words[start:end]
        chunk_str = ' '.join(chunk_words)
        char_start = text.find(chunk_words[0]) if chunk_words else 0
        char_end = char_start + len(chunk_str)

        chunks.append(TextChunk(
            chunk_id=f'{source_file}__chunk_{idx:04d}',
            text=chunk_str,
            page_start=_char_to_page(char_start, page_offsets),
            page_end=_char_to_page(char_end, page_offsets),
            char_start=char_start,
            char_end=char_end,
            source_file=source_file,
            chunk_type='text',
        ))
        idx += 1
        start += step
        if start >= len(words):
            break

    return chunks


def _char_to_page(char_pos: int, page_offsets: Optional[dict[int, int]]) -> int:
    if not page_offsets:
        return 0
    page = 1
    for p, offset in sorted(page_offsets.items()):
        if char_pos >= offset:
            page = p
        else:
            break
    return page


def find_maintenance_chapter(text: str, manufacturer: str) -> tuple[Optional[int], str]:
    """Detect the chapter number containing maintenance content."""
    text_lower = text.lower()

    if manufacturer == 'KRONES':
        for kw in ['chapter 12', 'kapitel 12', '12. maintenance', '12 maintenance']:
            if kw in text_lower:
                return 12, 'krones_auto'
        for kw in ['chapter 11', 'kapitel 11', '11. maintenance', '11 maintenance']:
            if kw in text_lower:
                return 11, 'krones_auto'

    for pat in [
        r'(\d+)[.\s]+(?:maintenance|service|inspection|lubrication|wartung|instandhaltung)',
        r'chapter\s+(\d+)[\s\S]{0,50}(?:maintenance|service|inspection)',
    ]:
        m = re.search(pat, text_lower)
        if m:
            return int(m.group(1)), 'toc_pattern'

    return None, 'not_found'


def extract_chapter_text(full_text: str, chapter_num: int) -> str:
    """Extract text for a specific chapter number."""
    for pat in [
        rf'(?:chapter|kapitel)\s+{chapter_num}[\s\S]*?(?=(?:chapter|kapitel)\s+{chapter_num + 1}|\Z)',
        rf'{chapter_num}\.[0-9][\s\S]*?(?={chapter_num + 1}\.[0-9]|\Z)',
    ]:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            return m.group(0)
    return full_text[int(len(full_text) * 0.7):]
