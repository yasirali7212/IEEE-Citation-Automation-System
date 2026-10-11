"""
Stage D & E: Format Citations and IEEE Bibliography
=====================================================
Reads the citation_map and verified_references, then:
  - Formats every in-text [N] marker in IEEE style
  - Generates the complete IEEE-formatted bibliography
  - Writes the output Word document with ONLY citation-marker
    changes applied to the original text

ABSOLUTE RULE: The ONLY permitted text changes are replacing old
citation markers [N] with renumbered [N] markers. No other text
changes whatsoever. If the old and new number are the same, no
change is made.

IEEE Bibliography Format (journal article example):
  [1] A. B. Author and C. D. Author, "Title of paper," *Journal Name*,
      vol. X, no. Y, pp. Z1–Z2, Mon. YYYY, doi: 10.xxxx/xxxx.
"""

import json
import logging
import os
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Optional

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── IEEE month abbreviations ───────────────────────────────────────────────────
MONTH_MAP = {
    "01": "Jan.", "02": "Feb.", "03": "Mar.", "04": "Apr.",
    "05": "May", "06": "Jun.", "07": "Jul.", "08": "Aug.",
    "09": "Sep.", "10": "Oct.", "11": "Nov.", "12": "Dec.",
}

CITATION_RE = re.compile(r'\[(\d+(?:[,–\-]\d+)*)\]')


# ─────────────────────────────────────────────────────────────────────────────
# IEEE bibliography formatting functions
# ─────────────────────────────────────────────────────────────────────────────

def _format_authors_ieee(authors: list[str]) -> str:
    """
    Format author list in IEEE style:
      Single: A. B. Smith
      Two:    A. B. Smith and C. D. Jones
      Three+: A. B. Smith, C. D. Jones, and E. F. Brown
      Six+:   First six + "et al."
    """
    # Filter empty author strings
    authors = [a.strip() for a in authors if a and a.strip()]
    if not authors:
        return ""
    # Reformat "Last, F." -> "F. Last"
    formatted = []
    for a in authors:
        if ',' in a:
            parts = a.split(',', 1)
            last = parts[0].strip()
            initials = parts[1].strip()
            formatted.append(f"{initials} {last}".strip())
        else:
            formatted.append(a.strip())

    # Filter out empty or whitespace results
    formatted = [f for f in formatted if f]
    if not formatted:
        return ""
    if len(formatted) > 6:
        return ", ".join(formatted[:6]) + " et al."
    if len(formatted) == 1:
        return formatted[0]
    if len(formatted) == 2:
        return f"{formatted[0]} and {formatted[1]}"
    return ", ".join(formatted[:-1]) + f", and {formatted[-1]}"


def _format_year(year_str: str) -> str:
    """Return just the 4-digit year portion."""
    m = re.search(r'\b(19|20)\d{2}\b', str(year_str))
    return m.group() if m else str(year_str)


def format_ieee_journal(num: int, meta: dict) -> str:
    """IEEE format for journal article."""
    authors = _format_authors_ieee(meta.get("authors", []))
    title = meta.get("title", "Untitled").strip('"')
    journal = meta.get("journal", "")
    volume = meta.get("volume", "")
    issue = meta.get("issue", "")
    pages = meta.get("pages", "").replace("--", "-")
    year = _format_year(meta.get("year", ""))
    doi = meta.get("doi", "")

    parts = [f"[{num}] {authors},"] if authors else [f"[{num}]"]
    parts.append(f'"{title},"')
    if journal:
        parts.append(f"*{journal}*,")
    if volume:
        parts.append(f"vol. {volume},")
    if issue:
        parts.append(f"no. {issue},")
    if pages:
        parts.append(f"pp. {pages},")
    if year:
        parts.append(f"{year},")
    if doi:
        parts.append(f"doi: {doi}.")

    return " ".join(parts).rstrip(",").rstrip(" ") + ("." if not doi else "")


def format_ieee_conference(num: int, meta: dict) -> str:
    """IEEE format for conference paper."""
    authors = _format_authors_ieee(meta.get("authors", []))
    title = meta.get("title", "Untitled").strip('"')
    conference = meta.get("journal", meta.get("publisher", ""))
    pages = meta.get("pages", "").replace("--", "\u2013").replace("-", "\u2013")
    year = _format_year(meta.get("year", ""))
    doi = meta.get("doi", "")

    parts = [f"[{num}] {authors},"] if authors else [f"[{num}]"]
    parts.append(f'"{title},"')
    if conference:
        parts.append(f"in *{conference}*,")
    if year:
        parts.append(f"{year},")
    if pages:
        parts.append(f"pp. {pages},")
    if doi:
        parts.append(f"doi: {doi}.")

    return " ".join(parts).rstrip(",") + "."


def format_ieee_book(num: int, meta: dict) -> str:
    """IEEE format for a book."""
    authors = _format_authors_ieee(meta.get("authors", []))
    title = meta.get("title", "Untitled")
    publisher = meta.get("publisher", "")
    year = _format_year(meta.get("year", ""))

    parts = [f"[{num}] {authors},"] if authors else [f"[{num}]"]
    parts.append(f"*{title}*.")
    if publisher:
        parts.append(f"{publisher},")
    if year:
        parts.append(f"{year}.")

    return " ".join(parts).rstrip(",") + "."


def format_reference_ieee(num: int, ref: dict) -> str:
    """
    Select the correct IEEE format function based on item type,
    or fall back to the verified metadata if available, or raw text.
    """
    meta = ref.get("verified_metadata") or {}
    if not meta and ref.get("verified"):
        meta = ref

    item_type = meta.get("item_type", "").lower()

    if not meta or not meta.get("title"):
        # No verified metadata — use raw text with number updated
        raw = ref.get("raw_text", "Unknown reference.")
        return f"[{num}] {raw}"

    if "conference" in item_type or "proceedings" in item_type:
        return format_ieee_conference(num, meta)
    elif "book" in item_type:
        return format_ieee_book(num, meta)
    else:
        return format_ieee_journal(num, meta)


# ─────────────────────────────────────────────────────────────────────────────
# Document writing
# ─────────────────────────────────────────────────────────────────────────────

def _expand_citation_range(raw: str) -> list[int]:
    nums = []
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


def _get_reference_tooltip(new_num: int, citation_map: dict) -> str:
    """Generate a descriptive tooltip string for hover ScreenTip."""
    new_to_ref = citation_map.get("new_to_ref", {})
    ref = new_to_ref.get(str(new_num), {})
    meta = ref.get("verified_metadata") or ref

    title = meta.get("title", "")
    authors = meta.get("authors", [])
    year = meta.get("year", "")
    journal = meta.get("journal", "")

    author_str = ""
    if authors:
        first = authors[0].split(",")[0].strip() if "," in authors[0] else authors[0].strip()
        author_str = f"{first} et al." if len(authors) > 1 else first

    parts = [f"[{new_num}]"]
    if author_str:
        parts.append(author_str)
    if year:
        parts.append(f"({year})")
    if title:
        parts.append(f'- "{title}"')
    if journal:
        parts.append(f"({journal})")

    return " ".join(parts)[:240]


def _make_citation_hyperlink(num: int, tooltip: str, font_name: Optional[str] = None,
                              font_size = None, link_color: str = "0563C1") -> OxmlElement:
    """Create an internal <w:hyperlink> element jumping to bookmark _Ref{num} with ScreenTip tooltip."""
    hl = OxmlElement('w:hyperlink')
    hl.set(qn('w:anchor'), f'_Ref{num}')
    hl.set(qn('w:history'), '1')
    if tooltip:
        hl.set(qn('w:tooltip'), tooltip)

    r = OxmlElement('w:r')
    rPr = OxmlElement('w:rPr')
    if link_color:
        color = OxmlElement('w:color')
        color.set(qn('w:val'), link_color)
        rPr.append(color)
    if font_name:
        rFonts = OxmlElement('w:rFonts')
        rFonts.set(qn('w:ascii'), font_name)
        rFonts.set(qn('w:hAnsi'), font_name)
        rPr.append(rFonts)
    if font_size:
        sz = OxmlElement('w:sz')
        sz.set(qn('w:val'), str(int(font_size.pt * 2)))
        rPr.append(sz)
    r.append(rPr)

    t = OxmlElement('w:t')
    t.text = f'[{num}]'
    r.append(t)
    hl.append(r)
    return hl


def apply_citation_changes(doc: Document, paragraph_plans: list[dict], citation_map: dict) -> int:
    """
    Apply citation renumbering AND convert citations into interactive hyperlinks.
    When users Ctrl+Click on [N], Word jumps to reference [N] in the bibliography.
    When users hover mouse over [N], Word displays a tooltip showing reference details.
    """
    plan_by_index = {p["index"]: p for p in paragraph_plans}
    new_to_ref = citation_map.get("new_to_ref", {})
    replacements_made = 0
    cite_pattern = re.compile(r'\[(\d+)\]')

    # Detect reference section so we don't convert numbers inside bibliography
    ref_heading_re = re.compile(r'^\s*(references|bibliography|works cited)\s*$', re.I)
    in_ref_section = False

    for para_index, para in enumerate(doc.paragraphs):
        if ref_heading_re.match(para.text.strip()):
            in_ref_section = True
            continue
        if in_ref_section:
            continue

        full_text = para.text
        if not full_text.strip():
            continue

        # Step 1: apply planned text replacements
        new_text = full_text
        if para_index in plan_by_index:
            plan = plan_by_index[para_index]
            for rep in plan.get("replacements", []):
                old = rep["original"]
                new = rep["replacement"]
                if old != new:
                    new_text = new_text.replace(old, new, 1)
                    replacements_made += 1

        # Step 2: if paragraph contains citation markers [N], rebuild runs with interactive hyperlinks
        if cite_pattern.search(new_text):
            first_run = para.runs[0] if para.runs else None
            font_name = first_run.font.name if first_run and first_run.font else None
            font_size = first_run.font.size if first_run and first_run.font else None

            pieces = []
            last_end = 0
            for m in cite_pattern.finditer(new_text):
                start, end = m.span()
                num_str = m.group(1)
                if num_str in new_to_ref:
                    if start > last_end:
                        pieces.append(('text', new_text[last_end:start]))
                    pieces.append(('cite', int(num_str)))
                    last_end = end
            if last_end < len(new_text):
                pieces.append(('text', new_text[last_end:]))

            if any(p[0] == 'cite' for p in pieces):
                para._p.clear_content()
                for kind, val in pieces:
                    if kind == 'text':
                        if val:
                            r = para.add_run(val)
                            if font_name:
                                r.font.name = font_name
                            if font_size:
                                r.font.size = font_size
                    elif kind == 'cite':
                        tooltip = _get_reference_tooltip(val, citation_map)
                        hl = _make_citation_hyperlink(val, tooltip, font_name, font_size)
                        para._p.append(hl)
        elif new_text != full_text:
            if para.runs:
                para.runs[0].text = new_text
                for r in para.runs[1:]:
                    r.text = ""
            else:
                para.add_run(new_text)

    return replacements_made


def write_ieee_bibliography(doc: Document, citation_map: dict) -> None:
    """
    Replace or append the reference section with the IEEE-formatted bibliography.
    Creates bookmarks (_Ref1, _Ref2, etc.) so in-text citations can jump directly to them.
    Also creates clickable external hyperlinks for DOIs and URLs.
    """
    new_to_ref = citation_map.get("new_to_ref", {})
    if not new_to_ref:
        logger.warning("No references to write.")
        return

    # Find the references section heading in the document
    ref_section_start = None
    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip().lower()
        if text in ('references', 'bibliography', 'works cited'):
            ref_section_start = i
            break

    # Remove existing reference paragraphs (paragraphs starting with [N])
    ref_entry_re = re.compile(r'^\s*\[\d+\]')
    elements_to_remove = []
    if ref_section_start is not None:
        for i in range(ref_section_start + 1, len(doc.paragraphs)):
            para = doc.paragraphs[i]
            if ref_entry_re.match(para.text):
                elements_to_remove.append(para._element)

    for elem in elements_to_remove:
        parent = elem.getparent()
        if parent is not None:
            parent.remove(elem)

    # If no reference section exists, add a heading
    if ref_section_start is None:
        doc.add_paragraph()  # spacing
        heading = doc.add_paragraph("References")
        heading.style = doc.styles["Heading 1"] if "Heading 1" in [s.name for s in doc.styles] else heading.style

    # Write sorted bibliography with bookmarks and clickable DOI/URLs
    for new_num_str in sorted(new_to_ref.keys(), key=lambda x: int(x)):
        ref = new_to_ref[new_num_str]
        new_num = int(new_num_str)
        formatted = format_reference_ieee(new_num, ref)
        p = doc.add_paragraph()

        # Add bookmark start so in-text citations can jump directly here
        bm_start = OxmlElement('w:bookmarkStart')
        bm_start.set(qn('w:id'), str(new_num))
        bm_start.set(qn('w:name'), f'_Ref{new_num}')
        p._p.append(bm_start)

        # Check for DOI to make it a clickable external hyperlink
        doi_match = re.search(r'doi:\s*([^\s\.]+(?:/[^\s\.]+)*)', formatted, re.I)
        url_match = re.search(r'https?://[^\s]+', formatted)

        if doi_match:
            doi_val = doi_match.group(1).rstrip('.')
            doi_url = f"https://doi.org/{doi_val}" if not doi_val.startswith('http') else doi_val
            prefix = formatted[:doi_match.start()]
            suffix = formatted[doi_match.end():]
            p.add_run(prefix)

            # External hyperlink for DOI
            try:
                r_id = p.part.relate_to(doi_url, 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink', is_external=True)
                hl = OxmlElement('w:hyperlink')
                hl.set(qn('r:id'), r_id)
                r_hl = OxmlElement('w:r')
                rPr = OxmlElement('w:rPr')
                color = OxmlElement('w:color')
                color.set(qn('w:val'), '0563C1')
                rPr.append(color)
                u = OxmlElement('w:u')
                u.set(qn('w:val'), 'single')
                rPr.append(u)
                r_hl.append(rPr)
                t = OxmlElement('w:t')
                t.text = f"doi: {doi_val}"
                r_hl.append(t)
                hl.append(r_hl)
                p._p.append(hl)
            except Exception:
                p.add_run(f"doi: {doi_val}")

            if suffix:
                p.add_run(suffix)
        elif url_match:
            url_val = url_match.group().rstrip('.')
            prefix = formatted[:url_match.start()]
            suffix = formatted[url_match.end():]
            p.add_run(prefix)

            try:
                r_id = p.part.relate_to(url_val, 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink', is_external=True)
                hl = OxmlElement('w:hyperlink')
                hl.set(qn('r:id'), r_id)
                r_hl = OxmlElement('w:r')
                rPr = OxmlElement('w:rPr')
                color = OxmlElement('w:color')
                color.set(qn('w:val'), '0563C1')
                rPr.append(color)
                u = OxmlElement('w:u')
                u.set(qn('w:val'), 'single')
                rPr.append(u)
                r_hl.append(rPr)
                t = OxmlElement('w:t')
                t.text = url_val
                r_hl.append(t)
                hl.append(r_hl)
                p._p.append(hl)
            except Exception:
                p.add_run(url_val)

            if suffix:
                p.add_run(suffix)
        else:
            p.add_run(formatted)

        # Add bookmark end
        bm_end = OxmlElement('w:bookmarkEnd')
        bm_end.set(qn('w:id'), str(new_num))
        p._p.append(bm_end)

        # Apply 8pt hanging indent style if possible
        try:
            p.paragraph_format.first_line_indent = Pt(-8)
        except Exception:
            pass

    logger.info(f"Bibliography written with {len(new_to_ref)} entries and bookmarks.")


def format_document(
    original_docx: str,
    citation_map_path: str,
    output_path: str,
) -> str:
    """
    Main function for Stages D & E.
    Reads the original .docx, applies only citation-marker changes,
    writes the new IEEE bibliography, and saves the output file.
    """
    with open(citation_map_path, 'r', encoding='utf-8') as f:
        citation_map = json.load(f)

    doc = Document(original_docx)

    # Stage D: Apply in-text citation renumbering and interactive hyperlinks
    changes = apply_citation_changes(doc, citation_map.get("paragraph_plans", []), citation_map)
    logger.info(f"In-text citation markers updated: {changes}")

    # Stage E: Write IEEE bibliography with bookmarks and external links
    write_ieee_bibliography(doc, citation_map)

    # Save output (never overwrite the original)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)
    logger.info(f"Formatted document saved: {output_path}")
    return output_path


if __name__ == "__main__":
    output_dir = sys.argv[1] if len(sys.argv) > 1 else os.getenv("OUTPUT_FOLDER", "outputs")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    original = sys.argv[2] if len(sys.argv) > 2 else str(Path(output_dir) / "paper.docx")
    citation_map_path = str(Path(output_dir) / "citation_map.json")
    out_path = str(Path(output_dir) / "IEEE_Formatted_Research_Paper.docx")

    if not Path(citation_map_path).exists():
        print(f"ERROR: citation_map.json not found at {citation_map_path}")
        print("Run match_citations.py first.")
        sys.exit(1)

    result = format_document(original, citation_map_path, out_path)
    print(f"Formatted document saved: {result}")
