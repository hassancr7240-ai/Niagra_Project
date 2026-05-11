from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pdfplumber


@dataclass
class TextChunk:
    chunk_id: str
    text: str
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    source_file: str


def extract_text_from_pdf(pdf_path: Path) -> tuple[str, dict[int, int]]:
    """
    Extract full text from PDF with page boundary tracking.
    Returns (full_text, page_char_offsets).
    """
    full_text = ""
    page_offsets: dict[int, int] = {}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            page_offsets[i] = len(full_text)
            page_text = page.extract_text() or ""
            full_text += page_text + "\n"
    return full_text, page_offsets


def chunk_text(
    text: str,
    source_file: str,
    chunk_size: int = 500,
    overlap: int = 103,
    page_offsets: Optional[dict[int, int]] = None,
) -> list[TextChunk]:
    """
    Split text into overlapping word-based chunks.
    chunk_size=500 words, overlap=103 words (per architecture diagram).
    """
    words = text.split()
    chunks: list[TextChunk] = []

    start = 0
    chunk_idx = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunk_words = words[start:end]
        chunk_text_str = " ".join(chunk_words)

        # Approximate character positions
        char_start = text.find(chunk_words[0]) if chunk_words else 0
        char_end = char_start + len(chunk_text_str)

        # Find page numbers from offsets
        p_start = _char_to_page(char_start, page_offsets)
        p_end = _char_to_page(char_end, page_offsets)

        chunks.append(
            TextChunk(
                chunk_id=f"{source_file}__chunk_{chunk_idx:04d}",
                text=chunk_text_str,
                page_start=p_start,
                page_end=p_end,
                char_start=char_start,
                char_end=char_end,
                source_file=source_file,
            )
        )
        chunk_idx += 1
        start += chunk_size - overlap
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
    """
    Detect the chapter number(s) containing maintenance content.
    Returns (chapter_number, detection_method).
    """
    text_lower = text.lower()

    # Krones auto-detection
    if manufacturer == "KRONES":
        for keyword in ["chapter 12", "kapitel 12", "12. maintenance", "12 maintenance"]:
            if keyword in text_lower:
                return 12, "krones_auto"
        for keyword in ["chapter 11", "kapitel 11", "11. maintenance", "11 maintenance"]:
            if keyword in text_lower:
                return 11, "krones_auto"

    # Generic chapter detection via TOC patterns
    chapter_patterns = [
        r"(\d+)[.\s]+(?:maintenance|service|inspection|lubrication|wartung|instandhaltung)",
        r"chapter\s+(\d+)[\s\S]{0,50}(?:maintenance|service|inspection)",
    ]
    for pat in chapter_patterns:
        m = re.search(pat, text_lower)
        if m:
            return int(m.group(1)), "toc_pattern"

    return None, "not_found"


def extract_chapter_text(full_text: str, chapter_num: int) -> str:
    """Extract text for a specific chapter number."""
    patterns = [
        rf"(?:chapter|kapitel)\s+{chapter_num}[\s\S]*?(?=(?:chapter|kapitel)\s+{chapter_num + 1}|\Z)",
        rf"{chapter_num}\.[0-9][\s\S]*?(?={chapter_num + 1}\.[0-9]|\Z)",
    ]
    for pat in patterns:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            return m.group(0)

    # Fallback: return last 30% of the document (maintenance is usually near the end)
    return full_text[int(len(full_text) * 0.7):]
