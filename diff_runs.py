#!/usr/bin/env python3
"""
diff_runs.py
-----------------
Diff two zimmo-scraper output runs by `sku`. No site knowledge -- shared
verbatim across the 2scraper family (CLAUDE.md §1, §9).

Refuses to compare two runs unless BOTH sidecars report status
"complete": a partial run's un-fetched pages would otherwise read as
products that were delisted between runs, which is a false signal about
the SITE rather than a true one about the scrape.

Usage:
    python3 diff_runs.py --old run1.json --new run2.json
    python3 diff_runs.py --old run1.json --new run2.json --fail-on-change
"""

import argparse
import json
import sys
from typing import Dict, List, Optional


def _load_meta(json_path: str) -> Optional[dict]:
    meta_path = json_path.rsplit(".json", 1)[0] + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _load_products(json_path: str) -> List[dict]:
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _dedupe_key(p: dict) -> str:
    """Matches output_writer.Product.dedupe_key() exactly (audit finding
    #9): a row with no `sku` at all previously vanished from the diff
    silently -- excluded from both `old_by_sku` and `new_by_sku`, so it
    could neither be reported as added/removed nor have its own field
    changes tracked. Falls back to the listing's URL, the same as the
    output contract's own deduplication does, so a JSON-LD/CSS-fallback
    result (which sometimes has no sku) is treated identically whether
    it's being deduplicated WITHIN a run or compared ACROSS two runs."""
    sku = p.get("sku")
    return f"sku:{sku}" if sku else f"url:{p.get('url', '')}"


def _index_by_products(products: List[dict]) -> Dict[str, dict]:
    return {_dedupe_key(p): p for p in products}


# Fields where a bare "changed" is misleading -- if the site's own
# schema ever grows a provenance-style field (this family's other repos
# use `price_source` for structured-vs-DOM price reconciliation; zimmo's
# own Product has no such overlay today, so this list is empty for now,
# kept as an explicit, named hook rather than assumed absent forever).
_PROVENANCE_FIELDS: Dict[str, str] = {
    # "price": "price_source",   # example of the shape, from a sibling repo
}

_COMPARE_FIELDS = (
    "title", "brand", "price", "currency", "original_price", "discount_pct",
    "image_url", "listing_type", "property_type", "location",
    "surface_m2", "bedrooms", "epc_label", "epc_value",
)


def diff(old_products: List[dict], new_products: List[dict]) -> dict:
    old_by_key = _index_by_products(old_products)
    new_by_key = _index_by_products(new_products)

    added = [new_by_key[key] for key in new_by_key if key not in old_by_key]
    removed = [old_by_key[key] for key in old_by_key if key not in new_by_key]

    changed = []
    source_changed = []
    for key in old_by_key:
        if key not in new_by_key:
            continue
        old_row, new_row = old_by_key[key], new_by_key[key]
        row_changes = {}
        row_is_source_changed_only = True
        for field_name in _COMPARE_FIELDS:
            old_val, new_val = old_row.get(field_name), new_row.get(field_name)
            if old_val == new_val:
                continue
            row_changes[field_name] = {"old": old_val, "new": new_val}
            provenance_field = _PROVENANCE_FIELDS.get(field_name)
            if provenance_field is None or old_row.get(provenance_field) == new_row.get(provenance_field):
                row_is_source_changed_only = False
        if row_changes:
            # Report the row's real sku (may be None for a URL-keyed
            # row) alongside the dedupe key actually used, so a reader
            # isn't confused by an entry with no sku shown when the key
            # itself says "url:...".
            entry = {"key": key, "sku": old_row.get("sku"), "changes": row_changes}
            if row_is_source_changed_only and any(f in _PROVENANCE_FIELDS for f in row_changes):
                source_changed.append(entry)
            else:
                changed.append(entry)

    return {
        "added_count": len(added), "added": added,
        "removed_count": len(removed), "removed": removed,
        "changed_count": len(changed), "changed": changed,
        "source_changed_count": len(source_changed), "source_changed": source_changed,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--old", required=True, help="Path to the earlier run's .json output")
    p.add_argument("--new", required=True, help="Path to the later run's .json output")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit non-zero if anything actually changed (source_changed-only rows are ignored)")
    args = p.parse_args()

    old_meta, new_meta = _load_meta(args.old), _load_meta(args.new)
    for label, meta, path in (("old", old_meta, args.old), ("new", new_meta, args.new)):
        if meta is None:
            print(f"[!] No .meta.json sidecar found next to {path} -- cannot confirm this "
                  f"run was 'complete'. Refusing to diff. (Looked for "
                  f"{path.rsplit('.json', 1)[0]}.meta.json)")
            return 2
        if meta.get("status") != "complete":
            print(f"[!] The '{label}' run's status is {meta.get('status')!r}, not 'complete' -- "
                  f"refusing to diff. A partial run's un-fetched pages would otherwise read as "
                  f"products delisted between runs, which is false.")
            return 2

    old_products = _load_products(args.old)
    new_products = _load_products(args.new)
    result = diff(old_products, new_products)

    print(f"Added:           {result['added_count']}")
    print(f"Removed:         {result['removed_count']}")
    print(f"Changed:         {result['changed_count']}")
    print(f"Source-changed:  {result['source_changed_count']} (provenance differs, "
          f"ignored by --fail-on-change)")

    for item in result["added"][:10]:
        identifier = item.get("sku") or item.get("url")
        print(f"  + {identifier}: {item.get('title')}")
    for item in result["removed"][:10]:
        identifier = item.get("sku") or item.get("url")
        print(f"  - {identifier}: {item.get('title')}")
    for item in result["changed"][:10]:
        print(f"  ~ {item['key']}: {list(item['changes'].keys())}")

    if args.fail_on_change and (result["changed_count"] > 0
                                 or result["added_count"] > 0
                                 or result["removed_count"] > 0):
        # FIXED (external audit, 2026-09-14, codex.md): the help text
        # says "if anything actually changed" -- a listing appearing or
        # disappearing between two runs is exactly that, to anyone
        # reading the flag's own description, even though internally
        # this tool tracks it as a separate "added"/"removed" count
        # rather than a per-field "changed" one. An earlier version
        # checked only changed_count, so --fail-on-change silently
        # passed through a run where every single listing had been
        # replaced by different ones.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
