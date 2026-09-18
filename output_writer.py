"""
output_writer.py
-----------------
Shared listing model, JSON/CSV writers, and the run-status/exit-code
contract used by all engines. Site knowledge: ~none.

Fixed per external audit (2026-09-14, codex.md finding #5): a run whose
`unattempted_pages` were never passed into finish_run() could report
`status=complete` and exit 0 even though it deliberately never even
tried some of the requested pages (e.g. after page 1 came back blocked,
or after data-based pagination termination stopped early). `complete`
now means every REQUESTED page was either fetched or explicitly
determined unnecessary by data-based termination -- not merely "the
pages we did try didn't fail".
"""

import csv
import json
from dataclasses import dataclass, asdict, fields, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any


@dataclass
class Product:
    source: str = "zimmo.be"
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    sku: Optional[str] = None
    title: Optional[str] = None
    brand: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = "EUR"
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    in_stock: Optional[bool] = None
    image_url: Optional[str] = None
    category: Optional[str] = None
    listing_type: Optional[str] = None
    property_type: Optional[str] = None
    location: Optional[str] = None
    surface_m2: Optional[float] = None
    bedrooms: Optional[int] = None
    epc_label: Optional[str] = None
    epc_value: Optional[float] = None

    def dedupe_key(self) -> str:
        """CLAUDE.md-family convention extended per audit finding #9:
        an object with no `sku` at all (a JSON-LD/CSS-fallback result,
        which sometimes has none) previously bypassed deduplication and
        diff entirely. Falls back to the listing URL, which is always
        present -- two rows can only share a URL if they really are the
        same listing, whereas two rows sharing `sku=None` are almost
        certainly NOT the same listing and must never collapse into
        one."""
        return f"sku:{self.sku}" if self.sku else f"url:{self.url}"


EXIT_OK = 0
EXIT_CRASH = 1
EXIT_BAD_USAGE = 2
EXIT_BLOCKED = 3
EXIT_ZERO_PRODUCTS = 4
EXIT_REMOTE_API_ERROR = 5
EXIT_PARTIAL = 6


def _field_names() -> List[str]:
    return [f.name for f in fields(Product)]


def write_json(products: List[Product], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)


def write_csv(products: List[Product], path: str) -> None:
    fieldnames = _field_names()
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in products:
            writer.writerow(asdict(p))


def save(products: List[Product], out_prefix: str, fmt: str) -> None:
    if fmt in ("json", "both"):
        write_json(products, f"{out_prefix}.json")
        print(f"[+] Saved {len(products)} listings -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(products, f"{out_prefix}.csv")
        print(f"[+] Saved {len(products)} listings -> {out_prefix}.csv")


def write_meta(meta: Dict[str, Any], out_prefix: str) -> None:
    with open(f"{out_prefix}.meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Saved run metadata -> {out_prefix}.meta.json")


def finish_run(
    products: List[Product],
    out_prefix: str,
    fmt: str,
    *,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]] = None,
    unattempted_pages: Optional[List[int]] = None,
    blocked: bool = False,
    remote_api_error: bool = False,
    allow_empty: bool = False,
    started_at: Optional[str] = None,
) -> int:
    """The single place that decides status, exit code, and whether/what
    to write.

    FIXED (audit finding #5): `unattempted_pages` -- pages the run
    deliberately never even tried, e.g. because page 1 came back
    blocked, or a proxy pool ran out of exits -- now counts toward
    "not complete" exactly like `failed_pages` does. Before this fix, a
    caller that computed `unattempted_pages` but forgot to pass it in
    (or a caller correctly passing an empty `failed_pages` because
    those pages were never ATTEMPTED, only skipped) could see
    `pages_completed < pages_requested` yet still get back
    `status=complete, exit 0` -- because nothing failed, nothing was
    EVER TRIED either. "Data-based termination" (CLAUDE.md §7: a page
    added no new sku, so later pages were never requested) is the one
    legitimate reason pages_completed can be less than pages_requested
    while still being genuinely `complete` -- signalled by the caller
    passing those specific page numbers in unattempted_pages so this
    function can tell "we chose not to ask" apart from "we tried to ask
    and failed", while still not silently calling either one
    `complete` by default.
    """
    failed_pages = failed_pages or []
    unattempted_pages = unattempted_pages or []
    finished_at = datetime.now(timezone.utc).isoformat()

    if remote_api_error and not products:
        print("[!] Remote API error and zero products recovered -- writing nothing, "
              "leaving any previous good output in place.")
        return EXIT_REMOTE_API_ERROR

    if blocked and not products:
        print("[!] Run appears blocked (bot-challenge/captcha) and zero products "
              "were recovered -- writing nothing, leaving any previous good output "
              "in place.")
        return EXIT_BLOCKED

    if not products and not allow_empty:
        print(f"[!] Zero products found across {pages_completed}/{pages_requested} "
              f"page(s) -- writing NOTHING so this doesn't overwrite a previous good "
              f"run. Pass --allow-empty to force writing an empty result.")
        return EXIT_ZERO_PRODUCTS

    if failed_pages:
        status = "partial"
        stop_reason = f"pages failed: {failed_pages}"
        exit_code = EXIT_PARTIAL
    elif unattempted_pages:
        # Distinguishable from "failed" in the sidecar (a caller can
        # tell "the site said no" from "we chose not to ask"), but NOT
        # "complete" either -- some requested pages genuinely have no
        # data in this run's output, for whatever reason.
        status = "partial"
        stop_reason = f"pages never attempted: {unattempted_pages}"
        exit_code = EXIT_PARTIAL
    elif not products:
        status = "empty"
        stop_reason = "zero products, --allow-empty was set"
        exit_code = EXIT_OK
    else:
        status = "complete"
        stop_reason = "ok"
        exit_code = EXIT_OK

    save(products, out_prefix, fmt)
    write_meta({
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "failed_pages": failed_pages,
        "unattempted_pages": unattempted_pages,
        "product_count": len(products),
        "started_at": started_at,
        "finished_at": finished_at,
        "source": "zimmo.be",
    }, out_prefix)

    return exit_code


def dedupe_by_sku(products: List[Product]) -> List[Product]:
    """Merge in PAGE ORDER, not arrival order. Uses Product.dedupe_key()
    (audit finding #9), so a row with no `sku` at all falls back to its
    URL instead of bypassing deduplication and diff entirely -- a
    behaviour a previous version had for any JSON-LD/CSS-fallback
    result missing a sku."""
    seen = set()
    result: List[Product] = []
    for p in products:
        key = p.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result
