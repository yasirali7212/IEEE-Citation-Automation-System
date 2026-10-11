"""
Report Generator
================
Generates all output reports:
  1. Citation_Verification_Report.csv  — per-reference verification status
  2. Unresolved_Citations_Report.txt   — citations needing manual review
"""

import csv
import json
import logging
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def write_verification_csv(verified_refs: list[dict], output_path: str) -> None:
    """Write Citation_Verification_Report.csv."""
    fieldnames = [
        "Reference_Number",
        "Raw_Text",
        "Verified",
        "Verification_Source",
        "Verified_Title",
        "Verified_Authors",
        "Verified_Year",
        "Verified_Journal",
        "Verified_DOI",
        "Flag_For_Review",
        "Review_Reason",
    ]
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for ref in verified_refs:
            meta = ref.get("verified_metadata") or {}
            if isinstance(meta, dict):
                authors = "; ".join(meta.get("authors", []))
            else:
                authors = ""
            writer.writerow({
                "Reference_Number": ref.get("number", ""),
                "Raw_Text": ref.get("raw_text", "")[:200],
                "Verified": "YES" if ref.get("verified") else "NO",
                "Verification_Source": ref.get("verification_source", ""),
                "Verified_Title": (meta.get("title", "")[:100] if isinstance(meta, dict) else ""),
                "Verified_Authors": authors[:150],
                "Verified_Year": (meta.get("year", "") if isinstance(meta, dict) else ""),
                "Verified_Journal": (meta.get("journal", "")[:100] if isinstance(meta, dict) else ""),
                "Verified_DOI": (meta.get("doi", "") if isinstance(meta, dict) else ""),
                "Flag_For_Review": "YES" if ref.get("flag_for_review") else "NO",
                "Review_Reason": ref.get("review_reason", "")[:200],
            })
    logger.info(f"Verification CSV saved: {output_path}")


def write_unresolved_report(
    citation_map: dict,
    verified_refs: list[dict],
    output_path: str,
) -> None:
    """Write Unresolved_Citations_Report.txt."""
    unresolved_cites = citation_map.get("unresolved", [])
    unverified_refs = [r for r in verified_refs if r.get("flag_for_review")]
    issues = citation_map.get("issues", [])

    lines = [
        "=" * 70,
        "UNRESOLVED CITATIONS AND REFERENCES REPORT",
        "IEEE Citation Automation Workflow",
        "=" * 70,
        "",
        "IMPORTANT: Review every item in this report before submitting.",
        "The workflow did not modify your text for any of these items.",
        "",
    ]

    if not unresolved_cites and not unverified_refs and not issues:
        lines += [
            "No unresolved items found.",
            "All citations were verified and properly mapped.",
        ]
    else:
        if unverified_refs:
            lines += [
                f"UNVERIFIED REFERENCES ({len(unverified_refs)} items)",
                "-" * 40,
                "These references could not be confirmed against Crossref,",
                "Semantic Scholar, or your Zotero library.",
                "",
            ]
            for ref in unverified_refs:
                lines += [
                    f"  [{ref.get('number', '?')}] {ref.get('raw_text', '')[:150]}",
                    f"  Reason: {ref.get('review_reason', '')}",
                    "",
                ]

        if unresolved_cites:
            lines += [
                f"",
                f"IN-TEXT CITATIONS WITHOUT VERIFIED REFERENCES ({len(unresolved_cites)} items)",
                "-" * 40,
                "",
            ]
            for item in unresolved_cites:
                lines += [
                    f"  Citation: {item.get('original_citation', '')}",
                    f"  Context : {item.get('context', '')[:120]}",
                    f"  Reason  : {item.get('reason', '')}",
                    f"  Para #  : {item.get('paragraph_index', '')}",
                    "",
                ]

        if issues:
            lines += [
                "",
                f"STRUCTURAL ISSUES ({len(issues)} items)",
                "-" * 40,
                "",
            ]
            for issue in issues:
                lines += [
                    f"  [{issue.get('type', '')}] {issue.get('detail', '')}",
                    "",
                ]

    lines += [
        "",
        "=" * 70,
        "END OF REPORT",
        "=" * 70,
    ]

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    logger.info(f"Unresolved Citations Report saved: {output_path}")


def write_bibtex_and_ris(citation_map: dict, output_dir: Path) -> None:
    """Generate references.bib and references.ris for journals or reference managers."""
    new_to_ref = citation_map.get("new_to_ref", {})
    if not new_to_ref:
        return

    bib_entries = []
    ris_entries = []

    for num_str in sorted(new_to_ref.keys(), key=lambda x: int(x)):
        item = new_to_ref[num_str]
        meta = item.get("verified_metadata") or item
        title = meta.get("title", "Untitled").strip('"')
        authors = meta.get("authors", [])
        year = meta.get("year", "")
        journal = meta.get("journal", meta.get("publisher", ""))
        volume = meta.get("volume", "")
        issue = meta.get("issue", "")
        pages = meta.get("pages", "")
        doi = meta.get("doi", "")
        url = meta.get("url", "")
        item_type = str(meta.get("item_type", "article")).lower()

        key = f"ref{num_str}"
        entry_type = "inproceedings" if "conference" in item_type or "proceedings" in item_type else ("book" if "book" in item_type else "article")

        # BibTeX
        bib = [f"@{entry_type}{{{key},"]
        bib.append(f"  title = {{{title}}},")
        if authors:
            bib.append(f"  author = {{{' and '.join(authors)}}},")
        if year:
            bib.append(f"  year = {{{year}}},")
        if journal:
            field = "booktitle" if entry_type == "inproceedings" else ("publisher" if entry_type == "book" else "journal")
            bib.append(f"  {field} = {{{journal}}},")
        if volume:
            bib.append(f"  volume = {{{volume}}},")
        if issue:
            bib.append(f"  number = {{{issue}}},")
        if pages:
            bib.append(f"  pages = {{{pages}}},")
        if doi:
            bib.append(f"  doi = {{{doi}}},")
        if url:
            bib.append(f"  url = {{{url}}},")
        bib.append("}")
        bib_entries.append("\n".join(bib))

        # RIS (EndNote / Zotero)
        ris_type = "JOUR" if entry_type == "article" else ("CONF" if entry_type == "inproceedings" else "BOOK")
        ris = [f"TY  - {ris_type}"]
        ris.append(f"ID  - {key}")
        ris.append(f"TI  - {title}")
        for a in authors:
            ris.append(f"AU  - {a}")
        if year:
            ris.append(f"PY  - {year}")
        if journal:
            ris.append(f"JO  - {journal}")
        if volume:
            ris.append(f"VL  - {volume}")
        if issue:
            ris.append(f"IS  - {issue}")
        if pages:
            ris.append(f"SP  - {pages}")
        if doi:
            ris.append(f"DO  - {doi}")
        if url:
            ris.append(f"UR  - {url}")
        ris.append("ER  - \n")
        ris_entries.append("\n".join(ris))

    bib_path = output_dir / "references.bib"
    ris_path = output_dir / "references.ris"

    with open(bib_path, 'w', encoding='utf-8') as f:
        f.write("\n\n".join(bib_entries))
    with open(ris_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(ris_entries))

    logger.info(f"BibTeX saved: {bib_path}")
    logger.info(f"RIS saved: {ris_path}")


def generate_all_reports(output_dir: str) -> None:
    """Generate all reports from the output directory's JSON files."""
    od = Path(output_dir)

    verified_path = od / "verified_references.json"
    citation_map_path = od / "citation_map.json"

    if not verified_path.exists():
        logger.error(f"verified_references.json not found in {output_dir}")
        return
    if not citation_map_path.exists():
        logger.error(f"citation_map.json not found in {output_dir}")
        return

    with open(verified_path, encoding='utf-8') as f:
        verified_refs = json.load(f)
    with open(citation_map_path, encoding='utf-8') as f:
        citation_map = json.load(f)

    write_verification_csv(
        verified_refs,
        str(od / "Citation_Verification_Report.csv"),
    )
    write_unresolved_report(
        citation_map,
        verified_refs,
        str(od / "Unresolved_Citations_Report.txt"),
    )
    write_bibtex_and_ris(
        citation_map,
        od,
    )
    logger.info("All reports and bibliography exports generated.")


if __name__ == "__main__":
    output_dir = sys.argv[1] if len(sys.argv) > 1 else os.getenv("OUTPUT_FOLDER", "outputs")
    generate_all_reports(output_dir)
    print(f"Reports saved to: {output_dir}")
