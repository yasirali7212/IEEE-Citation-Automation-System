"""
Stage C & D: Citation Mapping and IEEE Formatting
===================================================
Stage C: Maps each in-text [N] marker to its verified reference.
          Detects ordering problems. Flags uncertain mappings.
          NEVER modifies text; only produces a mapping plan.

Stage D: Applies IEEE citation style formatting rules to the mapping.
          Renumbers citations by first appearance order (IEEE standard).
          Produces the final citation_map.json consumed by apply_template.py.

IEEE in-text style: [1], [2], [1]-[3], [1], [3], [5]
"""

import json
import logging
import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CITATION_RE = re.compile(r'\[(\d+(?:[,–\-]\d+)*)\]')


def _expand(raw: str) -> list[int]:
    """Expand '1,2' or '1-3' to [1, 2, 3]."""
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


def build_first_appearance_order(paragraphs: list[dict]) -> list[int]:
    """
    Walk paragraphs in document order and record the first paragraph
    where each citation number appears. Returns a list of citation
    numbers in first-appearance order (IEEE renumbering basis).
    """
    seen = []
    for para in paragraphs:
        text = para.get("text", "")
        for m in CITATION_RE.finditer(text):
            for num in _expand(m.group(1)):
                if num not in seen:
                    seen.append(num)
    return seen


def build_citation_map(extraction: dict, verified_refs: list[dict]) -> dict:
    """
    Stage C+D: Build the complete citation map.

    Returns a dict:
    {
      "old_to_new": {old_num: new_num},           # renumbering map
      "new_to_ref":  {new_num: verified_ref},      # final reference data
      "paragraphs":  [{index, text, replacements}] # what to change per paragraph
      "issues":      [...]
      "unresolved":  [...]
    }
    """
    paragraphs = extraction.get("paragraphs", [])
    ref_list = extraction.get("reference_entries", [])

    # Index verified refs by original number
    verified_by_num: dict[int, dict] = {}
    for vr in verified_refs:
        verified_by_num[vr["number"]] = vr

    # Determine first-appearance order for renumbering
    first_order = build_first_appearance_order(paragraphs)
    logger.info(f"Citation first-appearance order: {first_order}")

    # Build old->new number map
    old_to_new: dict[int, int] = {}
    for new_num, old_num in enumerate(first_order, start=1):
        old_to_new[old_num] = new_num

    # Build new->ref map
    new_to_ref: dict[int, dict] = {}
    for old_num, new_num in old_to_new.items():
        if old_num in verified_by_num:
            new_to_ref[new_num] = verified_by_num[old_num]
        else:
            new_to_ref[new_num] = {
                "number": old_num,
                "flag_for_review": True,
                "review_reason": f"Original citation [{old_num}] has no matching reference entry.",
            }

    # Build per-paragraph replacement plan
    para_plans = []
    issues = []
    unresolved = []

    for para in paragraphs:
        text = para.get("text", "")
        replacements = []

        for m in CITATION_RE.finditer(text):
            old_nums = _expand(m.group(1))
            new_nums = [old_to_new.get(n, n) for n in old_nums]

            # Check all old nums have verified refs
            for on in old_nums:
                vr = verified_by_num.get(on, {})
                if not vr.get("verified", False):
                    unresolved.append({
                        "original_citation": f"[{on}]",
                        "paragraph_index": para["index"],
                        "context": text[:120],
                        "reason": vr.get("review_reason", "Reference not found in list."),
                    })

            # Format the replacement string
            if len(new_nums) == 1:
                replacement = f"[{new_nums[0]}]"
            elif all(
                new_nums[j + 1] == new_nums[j] + 1
                for j in range(len(new_nums) - 1)
            ) and len(new_nums) > 2:
                # Contiguous range -> [1]-[3]
                replacement = f"[{new_nums[0]}]\u2013[{new_nums[-1]}]"
            else:
                replacement = ", ".join(f"[{n}]" for n in new_nums)

            replacements.append({
                "original": m.group(),
                "replacement": replacement,
                "old_numbers": old_nums,
                "new_numbers": new_nums,
                "span_start": m.start(),
                "span_end": m.end(),
            })

        if replacements:
            para_plans.append({
                "index": para["index"],
                "style": para.get("style", ""),
                "original_text": text,
                "replacements": replacements,
            })

    # Detect references in list never cited in text
    all_cited_old = set(first_order)
    all_in_list = set(r["number"] for r in ref_list)
    never_cited = sorted(all_in_list - all_cited_old)
    if never_cited:
        issues.append({
            "type": "UNCITED_REFERENCES",
            "detail": f"References {never_cited} are in the list but not cited in text.",
        })

    result = {
        "old_to_new": {str(k): v for k, v in old_to_new.items()},
        "new_to_ref": {str(k): v for k, v in new_to_ref.items()},
        "paragraph_plans": para_plans,
        "issues": issues,
        "unresolved": unresolved,
        "first_appearance_order": first_order,
    }
    logger.info(
        f"Citation map built: {len(old_to_new)} citations remapped, "
        f"{len(unresolved)} unresolved."
    )
    return result


if __name__ == "__main__":
    output_dir = sys.argv[1] if len(sys.argv) > 1 else os.getenv("OUTPUT_FOLDER", "outputs")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    extraction_path = str(Path(output_dir) / "extraction.json")
    verified_path = str(Path(output_dir) / "verified_references.json")
    output_path = str(Path(output_dir) / "citation_map.json")

    for p in [extraction_path, verified_path]:
        if not Path(p).exists():
            print(f"ERROR: Required file not found: {p}")
            print("Run extract_document.py and verify_references.py first.")
            sys.exit(1)

    with open(extraction_path, encoding='utf-8') as f:
        extraction = json.load(f)
    with open(verified_path, encoding='utf-8') as f:
        verified_refs = json.load(f)

    citation_map = build_citation_map(extraction, verified_refs)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(citation_map, f, indent=2, ensure_ascii=False)

    print(f"Citation map saved: {output_path}")
    print(f"Citations remapped: {len(citation_map['old_to_new'])}")
    print(f"Unresolved:        {len(citation_map['unresolved'])}")
