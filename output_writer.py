"""
output_writer.py
-----------------
Shared listing model, JSON/CSV writers, and the run-status/exit-code
contract used by all engines. Site knowledge: ~none.

Fixed per external audit (2026-09-14, codex.md finding #5): a run whose
`unattempted_pages` were never passed into finish_run() could report
`status=complete` and exit 0 even though it deliberately never even
tried some of the requested pages (e.g. after page 1 came back blocked).
`complete` now means every REQUESTED page was either fetched and
trusted, or determined unnecessary by data-based termination (passed
separately as `exhausted_pages`) -- not merely "the pages we did try
didn't fail". Which pages count as trusted is `classify_page()`'s call.
"""

import csv
import json
from dataclasses import dataclass, asdict, fields, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


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
# The same code, under the name the rest of this family uses for it as of
# 2026-09-21: 5 means "the content was never obtained" -- a remote API
# error is one way for that to happen, a dead proxy or a load timeout is
# another. See CLAUDE.md §25.
EXIT_FETCH_FAILED = EXIT_REMOTE_API_ERROR
EXIT_PARTIAL = 6


# --- What one fetched page turned out to be -------------------------------
#
# FIXED (external audit, 2026-10-07, P1/P2): every engine returned
# "success" for any page that loaded at all, and dropped the HTTP status.
# A Cloudflare 403 on page 2 behind a good page 1 therefore finished as
# status=complete, exit 0, with page 2's listings silently missing; an
# HTTP 500 on every page finished as exit 4, "the catalogue is empty". The
# classification lives HERE, once, so the three engines cannot disagree
# about it -- they only supply the facts.
#
# Measured live 2026-10-07 through a residential exit: a served search page
# is HTTP 200; Cloudflare's "Even geduld..." challenge is HTTP 403; a route
# the site does not serve is HTTP 404 ("Pagina niet gevonden"); and a page
# number past the end of the listing is HTTP 200 carrying page 1 again --
# which the data-based "no new sku" rule, not the status, recognises.
PAGE_CONTENT = "content"          # listings were parsed
PAGE_EMPTY = "empty"              # served normally, no listings on it
PAGE_BLOCKED = "blocked"          # 401/403, or a challenge that never cleared
PAGE_NOT_FOUND = "not_found"      # 404/410 -- retrying will not change it
PAGE_FETCH_ERROR = "fetch_error"  # 429/5xx/other 4xx -- the content was never obtained

RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def classify_page(products_found: int, http_status: Optional[int],
                  challenge_detected: bool, rendered_ok: bool) -> str:
    """Order matters: parsed listings win over everything (a challenge that
    the solver or the browser cleared after a 403 still yields a good
    page), then the status, then the challenge heuristic. `http_status` is
    None when the engine could not read it; the challenge heuristic is
    then the only signal, exactly as before this fix."""
    if products_found:
        return PAGE_CONTENT
    if http_status in (401, 403):
        return PAGE_BLOCKED
    if http_status in (404, 410):
        return PAGE_NOT_FOUND
    if http_status is not None and http_status >= 400:
        return PAGE_FETCH_ERROR
    if challenge_detected and not rendered_ok:
        return PAGE_BLOCKED
    return PAGE_EMPTY


def status_rules_out_listings(http_status: Optional[int]) -> bool:
    """True when the status alone says no listing grid is coming, so the
    45-second readiness wait would only delay the verdict (three times
    over, with retries). 401/403 are NOT included: a Cloudflare challenge
    answers 403 and can still clear itself into the real page."""
    return http_status is not None and http_status >= 400 and http_status not in (401, 403)


def page_outcome_is_ok(outcome: str) -> bool:
    """True for a page whose answer can be trusted -- listings, or a page
    the site served normally with none on it."""
    return outcome in (PAGE_CONTENT, PAGE_EMPTY)


def should_retry_page(outcome: str, http_status: Optional[int]) -> bool:
    """Only a transient server answer earns another attempt. A block is
    not retried from the same exit (the next answer is the same 403), and
    a 404 is a fact about the URL."""
    return outcome == PAGE_FETCH_ERROR and http_status in RETRYABLE_STATUSES


def selection_url(url: str) -> str:
    """The search a run covered, independent of which page was asked for:
    scheme and host lower-cased, the `page` parameter dropped, the rest of
    the query sorted, and a trailing slash normalised. Two runs are only
    comparable by diff_runs.py when these agree."""
    parsed = urlparse(url.strip())
    query = sorted((k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                   if k != "page")
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "",
                       urlencode(query), ""))


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
    exhausted_pages: Optional[List[int]] = None,
    url: Optional[str] = None,
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
    while still being genuinely `complete`. Those pages arrive in
    `exhausted_pages`, NOT in `unattempted_pages`.

    FIXED (2026-10-07, found while verifying the external audit): the
    engines used to pass exhausted pages in `unattempted_pages`, so
    `--pages 4` on a two-page listing finished `partial`, exit 6 -- and
    diff_runs.py then refused every such run. The listing ending is the
    answer to the question the run asked, not a gap in it.

    `url` is recorded as the run's `selection` so diff_runs.py can refuse
    to compare two different searches, and `listing_exhausted` says
    whether the run saw the END of that listing or only a window onto
    it -- which decides whether a listing missing from a later run was
    removed from the site or merely moved past --pages.
    """
    failed_pages = failed_pages or []
    unattempted_pages = unattempted_pages or []
    exhausted_pages = exhausted_pages or []
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

    # Pages were attempted and NONE completed: the content was never
    # obtained -- a dead proxy, a load timeout -- which is a different fact
    # from "we read the listing and it held nothing". Without this it fell
    # through to EXIT_ZERO_PRODUCTS and told a pipeline the catalogue was
    # empty on a run that never reached the site. Exit 5 has meant "the
    # content was never obtained" family-wide since 2026-09-21 (CLAUDE.md
    # §25); this repo predates that pass.
    if not products and failed_pages and pages_completed == 0:
        print(f"[!] None of the {len(failed_pages)} attempted page(s) could be fetched -- "
              f"the content was never obtained. Writing nothing, leaving any previous "
              f"good output in place.")
        return EXIT_FETCH_FAILED

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
    elif exhausted_pages:
        status = "complete"
        stop_reason = (f"listing exhausted: page {min(exhausted_pages) - 1} added no new "
                       f"listing, so pages {exhausted_pages} were not requested")
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
        "exhausted_pages": exhausted_pages,
        "listing_exhausted": bool(exhausted_pages),
        "selection": {"url": selection_url(url) if url else None,
                      "pages_requested": pages_requested},
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
