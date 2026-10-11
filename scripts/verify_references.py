"""
Stage B: Verify References Against Online Sources
==================================================
Loads extraction.json and zotero_library.json, then verifies every
reference entry against:
  1. Zotero library (exact/fuzzy title match)
  2. Crossref REST API (DOI or title search)
  3. Semantic Scholar (fallback)

RULE: Never invent metadata. Flag unverifiable references.
RULE: Never modify the original document text.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _title_similarity(a: str, b: str) -> float:
    """Token overlap similarity between two titles / strings."""
    import re
    a_tok = set(re.sub(r'[^a-z0-9 ]', '', a.lower()).split())
    b_tok = set(re.sub(r'[^a-z0-9 ]', '', b.lower()).split())
    if not a_tok or not b_tok:
        return 0.0
    jaccard = len(a_tok & b_tok) / len(a_tok | b_tok)
    # If b (Zotero title) is largely contained within a (raw reference text)
    b_coverage = len(a_tok & b_tok) / len(b_tok)
    return max(jaccard, b_coverage * 0.95 if b_coverage >= 0.8 else 0.0)


def match_zotero(ref: dict, zotero_index: dict, threshold: float = 0.55) -> Optional[dict]:
    """
    Try to find a Zotero item matching this reference.
    Returns the normalized Zotero item if confidence >= threshold, else None.
    """
    raw_title = (ref.get("title") or ref.get("raw_text", ""))[:200]
    if not raw_title:
        return None

    best_key = None
    best_score = 0.0
    for z_title, z_item in zotero_index.items():
        score = _title_similarity(raw_title, z_title)
        if score > best_score:
            best_score = score
            best_key = z_title

    if best_score >= threshold and best_key:
        match = zotero_index[best_key].copy()
        match["zotero_match_score"] = round(best_score, 3)
        return match
    return None


def verify_all_references(extraction_path: str, zotero_library_path: Optional[str],
                          output_path: str) -> list[dict]:
    """
    Main function: verify all references from extraction JSON.
    Saves results to output_path (JSON).
    Returns list of verified reference dicts.
    """
    # Load extraction data
    with open(extraction_path, 'r', encoding='utf-8') as f:
        extraction = json.load(f)

    references = extraction.get("reference_entries", [])
    logger.info(f"Loaded {len(references)} reference entries to verify.")

    # Load Zotero library index (optional)
    zotero_index = {}
    if zotero_library_path and Path(zotero_library_path).exists():
        with open(zotero_library_path, 'r', encoding='utf-8') as f:
            zotero_index = json.load(f)
        logger.info(f"Loaded Zotero index with {len(zotero_index)} entries.")
    else:
        logger.warning("No Zotero library found. Skipping Zotero matching.")

    # Import online lookup (lazy to allow offline runs)
    try:
        from crossref_client import verify_reference
        online_available = True
    except ImportError:
        logger.warning("crossref_client not found. Online verification disabled.")
        online_available = False

    verified_refs = []
    for i, ref in enumerate(references):
        logger.info(f"Verifying [{ref['number']}] {ref.get('raw_text', '')[:60]}...")

        result = ref.copy()
        result["zotero_match"] = None
        result["verified"] = False
        result["verification_source"] = "none"
        result["verified_metadata"] = {}
        result["flag_for_review"] = False
        result["review_reason"] = ""

        # --- Try Zotero first ---
        if zotero_index:
            z_match = match_zotero(ref, zotero_index)
            if z_match:
                result["zotero_match"] = z_match
                result["verified"] = True
                result["verification_source"] = "zotero"
                result["verified_metadata"] = z_match
                logger.info(f"  -> Matched in Zotero (score={z_match.get('zotero_match_score')})")

        # --- Online lookup if not yet verified ---
        if not result["verified"] and online_available:
            online = verify_reference(ref)
            if online.get("verified"):
                result["verified"] = True
                result["verification_source"] = online.get("verification_source", "online")
                result["verified_metadata"] = online.get("verified_metadata", {})

        # --- Flag unverified for manual review ---
        if not result["verified"]:
            result["flag_for_review"] = True
            result["review_reason"] = (
                "Could not verify this reference against Zotero, Crossref, "
                "or Semantic Scholar. Manual review required before submission."
            )

        verified_refs.append(result)

    # Save results
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(verified_refs, f, indent=2, ensure_ascii=False)

    verified_count = sum(1 for r in verified_refs if r["verified"])
    flagged_count = sum(1 for r in verified_refs if r["flag_for_review"])
    logger.info(
        f"Verification complete: {verified_count}/{len(verified_refs)} verified, "
        f"{flagged_count} flagged for review."
    )
    return verified_refs


if __name__ == "__main__":
    output_dir = sys.argv[1] if len(sys.argv) > 1 else os.getenv("OUTPUT_FOLDER", "outputs")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    extraction_path = str(Path(output_dir) / "extraction.json")
    zotero_path = str(Path(output_dir) / "zotero_library.json")
    output_path = str(Path(output_dir) / "verified_references.json")

    if not Path(extraction_path).exists():
        print(f"ERROR: extraction.json not found at {extraction_path}")
        print("Run extract_document.py first.")
        sys.exit(1)

    results = verify_all_references(extraction_path, zotero_path, output_path)
    print(f"\nVerification saved to: {output_path}")
    print(f"Verified: {sum(1 for r in results if r['verified'])}/{len(results)}")
    print(f"Flagged:  {sum(1 for r in results if r['flag_for_review'])}")
