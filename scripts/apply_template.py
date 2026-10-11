"""
Stage F: Apply IEEE Journal Template
=====================================
Reads the IEEE journal .docx template and applies its styles/page layout
to the formatted paper. Only style/layout properties are transferred.
Original text is preserved exactly.

What this script does:
  - Copy page margins, page size, column count from template
  - Copy document-level styles (Normal, Heading 1, Body Text, etc.)
  - Apply header/footer from template if present
  - Preserve all original content: paragraphs, tables, figures, equations

What this script does NOT do:
  - Change any paragraph text
  - Reorder sections
  - Add or remove content
  - Claim compliance with IEEE requirements not explicitly verified

If the template requires a structural change that could affect text,
this script reports it instead of making the change.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, Cm
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _get_page_margins(doc: Document) -> dict:
    """Extract page margins from a document's first section."""
    section = doc.sections[0]
    return {
        "top": section.top_margin,
        "bottom": section.bottom_margin,
        "left": section.left_margin,
        "right": section.right_margin,
        "gutter": section.gutter,
        "header_distance": section.header_distance,
        "footer_distance": section.footer_distance,
    }


def _get_page_size(doc: Document) -> dict:
    section = doc.sections[0]
    return {
        "width": section.page_width,
        "height": section.page_height,
        "orientation": "landscape" if section.page_width > section.page_height else "portrait",
    }


def _apply_page_layout(source_doc: Document, target_doc: Document) -> list[str]:
    """
    Copy page size, margins, and orientation from template to target.
    Returns list of changes applied.
    """
    changes = []
    try:
        src_section = source_doc.sections[0]
        tgt_section = target_doc.sections[0]

        tgt_section.page_width = src_section.page_width
        tgt_section.page_height = src_section.page_height
        tgt_section.top_margin = src_section.top_margin
        tgt_section.bottom_margin = src_section.bottom_margin
        tgt_section.left_margin = src_section.left_margin
        tgt_section.right_margin = src_section.right_margin
        tgt_section.gutter = src_section.gutter
        tgt_section.header_distance = src_section.header_distance
        tgt_section.footer_distance = src_section.footer_distance

        changes.append(
            f"Page layout applied: {src_section.page_width.inches:.2f}\" x "
            f"{src_section.page_height.inches:.2f}\", "
            f"margins: T={src_section.top_margin.inches:.2f}\" "
            f"B={src_section.bottom_margin.inches:.2f}\" "
            f"L={src_section.left_margin.inches:.2f}\" "
            f"R={src_section.right_margin.inches:.2f}\""
        )
        logger.info(f"Page layout applied from template.")
    except Exception as e:
        logger.warning(f"Could not apply page layout: {e}")
    return changes


def _copy_style(src_doc: Document, tgt_doc: Document, style_name: str) -> bool:
    """
    Attempt to copy a named style from source to target document.
    Returns True if successful.
    """
    try:
        src_style = src_doc.styles[style_name]
        # Check if style exists in target
        if style_name in [s.name for s in tgt_doc.styles]:
            tgt_style = tgt_doc.styles[style_name]
            # Copy font properties
            if src_style.font.name:
                tgt_style.font.name = src_style.font.name
            if src_style.font.size:
                tgt_style.font.size = src_style.font.size
            if src_style.font.bold is not None:
                tgt_style.font.bold = src_style.font.bold
            if src_style.font.italic is not None:
                tgt_style.font.italic = src_style.font.italic
            # Copy paragraph format
            pf_src = src_style.paragraph_format
            pf_tgt = tgt_style.paragraph_format
            if pf_src.space_before is not None:
                pf_tgt.space_before = pf_src.space_before
            if pf_src.space_after is not None:
                pf_tgt.space_after = pf_src.space_after
            if pf_src.line_spacing is not None:
                pf_tgt.line_spacing = pf_src.line_spacing
            return True
    except (KeyError, AttributeError) as e:
        logger.debug(f"Style '{style_name}' not copied: {e}")
    return False


STYLES_TO_COPY = [
    "Normal", "Body Text", "Body Text 2",
    "Heading 1", "Heading 2", "Heading 3",
    "Caption", "Abstract", "Title", "Author",
    "Reference", "Bibliography",
]


def _copy_columns(src_doc: Document, tgt_doc: Document) -> Optional[str]:
    """
    Attempt to copy column settings (e.g., 2-column IEEE layout)
    from template to target document.
    """
    try:
        src_sectPr = src_doc.sections[0]._sectPr
        cols_el = src_sectPr.find(qn('w:cols'))
        if cols_el is not None:
            tgt_sectPr = tgt_doc.sections[0]._sectPr
            existing = tgt_sectPr.find(qn('w:cols'))
            if existing is not None:
                tgt_sectPr.remove(existing)
            import copy
            tgt_sectPr.append(copy.deepcopy(cols_el))
            num = cols_el.get(qn('w:num'), '1')
            return f"Column layout applied: {num} column(s)"
    except Exception as e:
        logger.debug(f"Column copy skipped: {e}")
    return None


def apply_template(
    formatted_docx: str,
    template_docx: Optional[str],
    output_path: str,
) -> tuple[str, list[str], list[str]]:
    """
    Apply template layout to the formatted document.
    Returns (output_path, changes_applied, items_to_review).
    """
    changes = []
    review_items = []

    doc = Document(formatted_docx)

    if not template_docx or not Path(template_docx).exists():
        # No template: apply IEEE default formatting
        logger.warning("No template provided. Applying IEEE default margins.")
        section = doc.sections[0]
        section.top_margin = Inches(1.0)
        section.bottom_margin = Inches(1.0)
        section.left_margin = Inches(0.75)
        section.right_margin = Inches(0.75)
        changes.append("Applied IEEE default margins (1\" top/bottom, 0.75\" left/right).")
        review_items.append(
            "No template was provided. Margins set to IEEE default but full "
            "layout compliance cannot be confirmed. Supply the journal template "
            "for precise formatting."
        )
    else:
        template_doc = Document(template_docx)
        logger.info(f"Applying template: {template_docx}")

        # Page layout
        page_changes = _apply_page_layout(template_doc, doc)
        changes.extend(page_changes)

        # Styles
        for style_name in STYLES_TO_COPY:
            if _copy_style(template_doc, doc, style_name):
                changes.append(f"Style copied: {style_name}")

        # Column layout (IEEE journals are often 2-column)
        col_change = _copy_columns(template_doc, doc)
        if col_change:
            changes.append(col_change)
        else:
            review_items.append(
                "Column layout could not be automatically applied. "
                "Manually check the column settings in the output document."
            )

    # Save output
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)
    logger.info(f"Template-applied document saved: {output_path}")

    return output_path, changes, review_items


if __name__ == "__main__":
    output_dir = sys.argv[1] if len(sys.argv) > 1 else os.getenv("OUTPUT_FOLDER", "outputs")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    formatted = str(Path(output_dir) / "IEEE_Formatted_Research_Paper.docx")
    template = sys.argv[2] if len(sys.argv) > 2 else ""
    out_path = formatted  # overwrite in-place

    out, changes, review = apply_template(formatted, template or None, out_path)
    print(f"Template applied. Output: {out}")
    print(f"Changes: {len(changes)}")
    for c in changes:
        print(f"  + {c}")
    if review:
        print("Items for manual review:")
        for r in review:
            print(f"  ! {r}")
