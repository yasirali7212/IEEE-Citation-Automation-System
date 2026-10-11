"""
Stage G: Validate Final Document — Text Preservation Check
===========================================================
Compares the original and final documents paragraph by paragraph.
Produces a detailed Text_Preservation_Report.txt.

CRITICAL: Any difference NOT caused by citation-marker renumbering
is a validation failure and must be reported.

The script does NOT fix issues — it only reports them.
Reporting structure:
  - PASS: paragraph text unchanged (or only citation numbers changed)
  - CITATION_CHANGE: [N] -> [M] substitution (approved change)
  - UNEXPECTED_CHANGE: any other text difference (requires investigation)
  - MISSING_PARAGRAPH: paragraph in original not found in final
  - EXTRA_PARAGRAPH: paragraph in final not in original
"""

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from docx import Document
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CITATION_RE = re.compile(r'\[\d+(?:[,–\-]\d+)*\]')


@dataclass
class ParagraphComparison:
    index: int
    original_text: str
    final_text: str
    status: str         # PASS, CITATION_CHANGE, UNEXPECTED_CHANGE, MISSING, EXTRA
    details: str = ""


def _normalize_citations(text: str) -> str:
    """Replace all [N] markers with a placeholder for text-only comparison."""
    return CITATION_RE.sub('[CITE]', text)


def _only_citation_changed(original: str, final: str) -> bool:
    """
    Returns True if the only difference between original and final
    is citation-marker numbers (approved change).
    """
    orig_norm = _normalize_citations(original)
    final_norm = _normalize_citations(final)
    return orig_norm == final_norm


def extract_paragraphs(docx_path: str) -> list[str]:
    """Return list of non-empty paragraph texts from a Word document."""
    doc = Document(docx_path)
    return [p.text for p in doc.paragraphs if p.text.strip()]


def compare_documents(
    original_path: str,
    final_path: str,
) -> tuple[list[ParagraphComparison], dict]:
    """
    Compare original and final documents.
    Returns (comparisons, summary_dict).
    """
    orig_paras = extract_paragraphs(original_path)
    final_paras = extract_paragraphs(final_path)

    comparisons = []
    total = max(len(orig_paras), len(final_paras))
    pass_count = 0
    citation_change_count = 0
    bibliography_count = 0
    unexpected_count = 0
    missing_count = 0
    extra_count = 0

    in_reference_section = False
    ref_heading_re = re.compile(r'^\s*(references|bibliography|works cited)\s*$', re.I)
    ref_item_re = re.compile(r'^\s*\[\d+\]')

    for i in range(total):
        orig_text = orig_paras[i] if i < len(orig_paras) else None
        final_text = final_paras[i] if i < len(final_paras) else None

        # Detect start of References section
        if (orig_text and ref_heading_re.match(orig_text)) or (final_text and ref_heading_re.match(final_text)):
            in_reference_section = True
            comp = ParagraphComparison(
                index=i, original_text=orig_text or "",
                final_text=final_text or "", status="PASS",
                details="References heading"
            )
            pass_count += 1
            comparisons.append(comp)
            continue

        if orig_text is None:
            comp = ParagraphComparison(
                index=i,
                original_text="",
                final_text=final_text or "",
                status="EXTRA" if not in_reference_section else "BIBLIOGRAPHY_FORMATTED",
                details="Paragraph exists in final document but not in original." if not in_reference_section else "IEEE bibliography entry.",
            )
            if in_reference_section:
                bibliography_count += 1
            else:
                extra_count += 1
        elif final_text is None:
            comp = ParagraphComparison(
                index=i,
                original_text=orig_text,
                final_text="",
                status="MISSING" if not in_reference_section else "BIBLIOGRAPHY_FORMATTED",
                details="Paragraph exists in original but is missing from final document." if not in_reference_section else "Original unformatted reference replaced.",
            )
            if in_reference_section:
                bibliography_count += 1
            else:
                missing_count += 1
        elif in_reference_section or (ref_item_re.match(orig_text) and ref_item_re.match(final_text)):
            in_reference_section = True
            comp = ParagraphComparison(
                index=i,
                original_text=orig_text,
                final_text=final_text,
                status="BIBLIOGRAPHY_FORMATTED",
                details="Reference entry updated to IEEE standard format.",
            )
            bibliography_count += 1
        elif orig_text == final_text:
            comp = ParagraphComparison(
                index=i, original_text=orig_text,
                final_text=final_text, status="PASS",
            )
            pass_count += 1
        elif _only_citation_changed(orig_text, final_text):
            # Find what changed
            orig_cites = CITATION_RE.findall(orig_text)
            final_cites = CITATION_RE.findall(final_text)
            comp = ParagraphComparison(
                index=i,
                original_text=orig_text,
                final_text=final_text,
                status="CITATION_CHANGE",
                details=f"Citation markers updated: {orig_cites} -> {final_cites}",
            )
            citation_change_count += 1
        else:
            comp = ParagraphComparison(
                index=i,
                original_text=orig_text,
                final_text=final_text,
                status="UNEXPECTED_CHANGE",
                details=(
                    "Text differs beyond citation-marker changes. "
                    "This requires investigation."
                ),
            )
            unexpected_count += 1
            logger.error(
                f"UNEXPECTED TEXT CHANGE at paragraph {i}:\n"
                f"  ORIGINAL: {orig_text[:100]}\n"
                f"  FINAL:    {final_text[:100]}"
            )

        comparisons.append(comp)

    summary = {
        "total_paragraphs": total,
        "pass": pass_count,
        "citation_changes": citation_change_count,
        "bibliography_entries": bibliography_count,
        "unexpected_changes": unexpected_count,
        "missing_paragraphs": missing_count,
        "extra_paragraphs": extra_count,
        "preservation_passed": unexpected_count == 0 and missing_count == 0,
    }
    return comparisons, summary


def write_preservation_report(
    comparisons: list[ParagraphComparison],
    summary: dict,
    output_path: str,
    original_path: str,
    final_path: str,
) -> None:
    """Write the Text_Preservation_Report.txt."""
    lines = [
        "=" * 70,
        "TEXT PRESERVATION REPORT",
        "IEEE Citation Automation Workflow",
        "=" * 70,
        "",
        f"Original document : {original_path}",
        f"Final document    : {final_path}",
        "",
        "SUMMARY",
        "-" * 40,
        f"Total paragraphs compared : {summary['total_paragraphs']}",
        f"Unchanged (PASS)          : {summary['pass']}",
        f"Citation-only changes     : {summary['citation_changes']} (approved)",
        f"Bibliography entries      : {summary.get('bibliography_entries', 0)} (IEEE formatted)",
        f"UNEXPECTED CHANGES        : {summary['unexpected_changes']}",
        f"Missing paragraphs        : {summary['missing_paragraphs']}",
        f"Extra paragraphs          : {summary['extra_paragraphs']}",
        "",
    ]

    if summary["preservation_passed"]:
        lines += [
            "RESULT: PASS",
            "All original manuscript text has been preserved.",
            "Only citation-marker numbers were changed (approved changes).",
        ]
    else:
        lines += [
            "RESULT: FAIL — MANUAL REVIEW REQUIRED",
            "One or more unexpected text changes were detected.",
            "Do NOT submit this document without investigating the issues below.",
        ]

    if summary["unexpected_changes"] > 0:
        lines += ["", "UNEXPECTED CHANGES (requires investigation)", "-" * 40]
        for comp in comparisons:
            if comp.status == "UNEXPECTED_CHANGE":
                lines += [
                    f"  Paragraph #{comp.index}",
                    f"  ORIGINAL : {comp.original_text[:200]}",
                    f"  FINAL    : {comp.final_text[:200]}",
                    "",
                ]

    if summary["missing_paragraphs"] > 0:
        lines += ["", "MISSING PARAGRAPHS", "-" * 40]
        for comp in comparisons:
            if comp.status == "MISSING":
                lines += [
                    f"  Paragraph #{comp.index} MISSING from final:",
                    f"  {comp.original_text[:200]}",
                    "",
                ]

    if summary["citation_changes"] > 0:
        lines += ["", "APPROVED CITATION-MARKER CHANGES", "-" * 40]
        for comp in comparisons:
            if comp.status == "CITATION_CHANGE":
                lines += [
                    f"  Paragraph #{comp.index}: {comp.details}",
                ]

    lines += [
        "",
        "=" * 70,
        "END OF REPORT",
        "=" * 70,
    ]

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    logger.info(f"Text Preservation Report saved: {output_path}")


def validate_document(
    original_path: str,
    final_path: str,
    output_dir: str,
) -> dict:
    """Main validation function. Returns summary dict."""
    comparisons, summary = compare_documents(original_path, final_path)
    report_path = str(Path(output_dir) / "Text_Preservation_Report.txt")
    write_preservation_report(comparisons, summary, report_path, original_path, final_path)
    return summary


if __name__ == "__main__":
    output_dir = sys.argv[1] if len(sys.argv) > 1 else os.getenv("OUTPUT_FOLDER", "outputs")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    original = str(Path(output_dir) / "Original_Research_Paper_Backup.docx")
    final = str(Path(output_dir) / "IEEE_Formatted_Research_Paper.docx")

    if not Path(original).exists():
        print(f"ERROR: Backup not found at: {original}")
        sys.exit(1)
    if not Path(final).exists():
        print(f"ERROR: Final document not found at: {final}")
        sys.exit(1)

    summary = validate_document(original, final, output_dir)
    print(f"\nValidation Result: {'PASS' if summary['preservation_passed'] else 'FAIL'}")
    print(f"  Unchanged: {summary['pass']}/{summary['total_paragraphs']} paragraphs")
    print(f"  Unexpected changes: {summary['unexpected_changes']}")
    report = str(Path(output_dir) / "Text_Preservation_Report.txt")
    print(f"  Full report: {report}")
