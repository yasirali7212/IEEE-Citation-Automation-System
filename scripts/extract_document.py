"""
Stage A: Extract and Inspect Word Document
==========================================
Reads a .docx file and extracts:
- Paragraphs (with style names)
- Existing citation markers [1], [2], etc.
- Reference list entries
- Tables, figures, equations
- Document styles

CRITICAL RULE: This script reads only. It NEVER modifies the document.
"""

import re
import json
import shutil
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
from docx import Document
from docx.oxml.ns import qn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class CitationMarker:
    """Represents a single [N] citation found in text."""
    number: int
    paragraph_index: int
    paragraph_text: str
    context_snippet: str  # ~80 chars around the citation
    position_in_para: int  # character offset


@dataclass
class ReferenceEntry:
    """Represents a single bibliography/reference list entry."""
    number: int
    raw_text: str
    authors: list = field(default_factory=list)
    title: str = ""
    venue: str = ""
    year: str = ""
    doi: str = ""
    url: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    verified: bool = False
    verification_notes: str = ""


@dataclass
class ExtractedDocument:
    """Full extraction result from a Word document."""
    file_path: str
    paragraphs: list = field(default_factory=list)       # list of {index, style, text}
    citation_markers: list = field(default_factory=list) # list of CitationMarker
    reference_entries: list = field(default_factory=list) # list of ReferenceEntry
    tables: list = field(default_factory=list)           # list of {index, caption, rows}
    styles_used: list = field(default_factory=list)
    issues: list = field(default_factory=list)           # detected problems
    section_map: dict = field(default_factory=dict)      # section name -> paragraph indices


# ── Citation pattern: matches [1], [1,2], [1-3], [1],[2] etc. ─────────────────
CITATION_RE = re.compile(r'\[(\d+(?:[,–\-]\d+)*)\]')
# ── Reference list entry: starts with [N] at beginning of paragraph ───────────
REF_ENTRY_RE = re.compile(r'^\s*\[(\d+)\]\s+(.+)$', re.DOTALL)
# ── DOI pattern ───────────────────────────────────────────────────────────────
DOI_RE = re.compile(r'10\.\d{4,}/\S+', re.IGNORECASE)
# ── URL pattern ───────────────────────────────────────────────────────────────
URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)


def backup_document(source_path: str, output_dir: str) -> str:
    """
    Creates an untouched backup of the original document.
    Returns the path of the backup file.
    """
    src = Path(source_path)
    dst_dir = Path(output_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    backup_path = dst_dir / f"Original_Research_Paper_Backup{src.suffix}"
    shutil.copy2(src, backup_path)
    logger.info(f"Backup created: {backup_path}")
    return str(backup_path)


def extract_citation_markers(text: str, para_index: int) -> list[CitationMarker]:
    """Extract all [N] citation markers from a paragraph."""
    markers = []
    for m in CITATION_RE.finditer(text):
        raw = m.group(1)
        # Expand ranges like [1-3] -> [1, 2, 3]
        numbers = _expand_citation_range(raw)
        start = m.start()
        context = text[max(0, start - 40): start + len(m.group()) + 40]
        for num in numbers:
            markers.append(CitationMarker(
                number=num,
                paragraph_index=para_index,
                paragraph_text=text[:200],  # truncated for storage
                context_snippet=context.strip(),
                position_in_para=start,
            ))
    return markers


def _expand_citation_range(raw: str) -> list[int]:
    """Convert '1,2,3' or '1-3' or '1–3' into [1, 2, 3]."""
    nums = []
    # Normalize em-dash / en-dash to hyphen
    raw = raw.replace('–', '-').replace('—', '-')
    for part in raw.split(','):
        part = part.strip()
        if '-' in part:
            try:
                a, b = part.split('-', 1)
                nums.extend(range(int(a.strip()), int(b.strip()) + 1))
            except ValueError:
                pass
        else:
            try:
                nums.append(int(part))
            except ValueError:
                pass
    return sorted(set(nums))


def parse_reference_entry(number: int, raw_text: str) -> ReferenceEntry:
    """Best-effort parse of a reference string into structured fields."""
    entry = ReferenceEntry(number=number, raw_text=raw_text.strip())

    # Extract DOI
    doi_match = DOI_RE.search(raw_text)
    if doi_match:
        entry.doi = doi_match.group().rstrip('.,')

    # Extract URL
    url_match = URL_RE.search(raw_text)
    if url_match and not entry.doi:
        entry.url = url_match.group().rstrip('.,')

    # Extract year (4 digits, typically in parentheses or near end)
    year_match = re.search(r'\b(19|20)\d{2}\b', raw_text)
    if year_match:
        entry.year = year_match.group()

    # Very rough author/title split: "Author, A., Title of Paper..."
    # We rely on verify_references.py to do the real parsing via Crossref
    parts = raw_text.split('"')
    if len(parts) >= 2:
        entry.title = parts[1].strip(' ",')

    return entry


def is_reference_section(style_name: str, text: str) -> bool:
    """Heuristic: is this paragraph a reference list entry or heading?"""
    if REF_ENTRY_RE.match(text):
        return True
    lower = text.lower().strip()
    return lower in ('references', 'bibliography', 'works cited', 'reference list')


def extract_document(docx_path: str) -> ExtractedDocument:
    """
    Main extraction function.
    Returns an ExtractedDocument with all structure and citation data.
    """
    doc = Document(docx_path)
    result = ExtractedDocument(file_path=docx_path)

    in_reference_section = False
    styles_seen = set()
    all_citations: list[CitationMarker] = []

    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip()
        style = para.style.name if para.style else "Normal"
        styles_seen.add(style)

        if not text:
            continue

        para_record = {"index": i, "style": style, "text": text}
        result.paragraphs.append(para_record)

        # ── Detect reference section boundary ─────────────────────────────────
        if is_reference_section(style, text) and not REF_ENTRY_RE.match(text):
            in_reference_section = True
            result.section_map.setdefault("References", []).append(i)
            continue

        if in_reference_section:
            ref_match = REF_ENTRY_RE.match(text)
            if ref_match:
                num = int(ref_match.group(1))
                raw = ref_match.group(2)
                entry = parse_reference_entry(num, raw)
                result.reference_entries.append(asdict(entry))
                continue
            elif text:
                # Could be a continuation of previous reference
                if result.reference_entries:
                    result.reference_entries[-1]['raw_text'] += ' ' + text

        # ── Extract section headings ──────────────────────────────────────────
        if 'Heading' in style or style in ('Title', 'Subtitle'):
            result.section_map.setdefault(text, []).append(i)

        # ── Extract citation markers ──────────────────────────────────────────
        markers = extract_citation_markers(text, i)
        all_citations.extend(markers)

    # ── Extract tables ────────────────────────────────────────────────────────
    for t_idx, table in enumerate(doc.tables):
        rows = []
        for row in table.rows:
            rows.append([cell.text.strip() for cell in row.cells])
        result.tables.append({"index": t_idx, "rows": rows})

    result.citation_markers = [asdict(c) for c in all_citations]
    result.styles_used = sorted(styles_seen)

    # ── Validate consistency ──────────────────────────────────────────────────
    cited_numbers = sorted(set(c['number'] for c in result.citation_markers))
    ref_numbers = sorted(set(r['number'] for r in result.reference_entries))

    # Citations without references
    missing_refs = [n for n in cited_numbers if n not in ref_numbers]
    if missing_refs:
        result.issues.append({
            "type": "MISSING_REFERENCE",
            "detail": f"Citations {missing_refs} appear in text but have no reference entry."
        })

    # References never cited
    uncited_refs = [n for n in ref_numbers if n not in cited_numbers]
    if uncited_refs:
        result.issues.append({
            "type": "UNCITED_REFERENCE",
            "detail": f"References {uncited_refs} are in the list but never cited in the text."
        })

    # Numbering gaps
    if ref_numbers:
        expected = list(range(1, max(ref_numbers) + 1))
        gaps = [n for n in expected if n not in ref_numbers]
        if gaps:
            result.issues.append({
                "type": "NUMBERING_GAP",
                "detail": f"Reference numbers {gaps} are missing from the list."
            })

    logger.info(
        f"Extracted {len(result.paragraphs)} paragraphs, "
        f"{len(all_citations)} citation markers, "
        f"{len(result.reference_entries)} reference entries, "
        f"{len(result.issues)} issues detected."
    )
    return result


def save_extraction(result: ExtractedDocument, output_path: str):
    """Save extraction result as JSON for use by downstream scripts."""
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(asdict(result), f, indent=2, ensure_ascii=False)
    logger.info(f"Extraction saved to: {output_path}")


if __name__ == "__main__":
    import sys
    import os
    from dotenv import load_dotenv
    load_dotenv()

    if len(sys.argv) < 2:
        print("Usage: python extract_document.py <path_to_paper.docx> [output_dir]")
        sys.exit(1)

    docx_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else os.getenv("OUTPUT_FOLDER", "outputs")

    # Step 1: Backup
    backup_path = backup_document(docx_path, output_dir)
    print(f"Backup saved: {backup_path}")

    # Step 2: Extract
    result = extract_document(docx_path)

    # Step 3: Save JSON
    out_json = str(Path(output_dir) / "extraction.json")
    save_extraction(result, out_json)

    print(f"Extraction complete. Issues found: {len(result.issues)}")
    for issue in result.issues:
        print(f"  [{issue['type']}] {issue['detail']}")
    print(f"Output: {out_json}")
