"""
output_writer.py
-----------------
Shared listing model, JSON/CSV writers, and the run-status/exit-code
contract used by all engines. Site knowledge: ~none.

Fixed per external audit (2026-09-14, finding #5): a run whose
`unattempted_pages` were never passed into finish_run() could report
`status=complete` and exit 0 even though it deliberately never even
tried some of the requested pages (e.g. after page 1 came back blocked).
`complete` now means every REQUESTED page was either fetched and
trusted, or determined unnecessary by data-based termination (passed
separately as `exhausted_pages`) -- not merely "the pages we did try
didn't fail". Which pages count as trusted is `classify_page()`'s call.
"""

import contextlib
import csv
import io
import json
import os
import tempfile
from dataclasses import dataclass, asdict, fields, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
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
    currency: Optional[str] = None
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
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
        """The family's dedupe-by-sku convention, extended per audit finding #9:
        an object with no `sku` at all (a JSON-LD/CSS-fallback result,
        which sometimes has none) previously bypassed deduplication and
        diff entirely. Falls back to the listing URL, which is always
        present -- two rows can only share a URL if they really are the
        same listing, whereas two rows sharing `sku=None` are almost
        certainly NOT the same listing and must never collapse into
        one."""
        return f"sku:{self.sku}" if self.sku else f"url:{self.url}"


# Columns that were REMOVED in 1.3.0, kept as a named list so a check can
# assert they stay gone. All three came with the family's shop-shaped row
# and nothing in this repo ever assigned them: 0 of 21 rows in the real
# 2026-09-23 capture, and no assignment anywhere in product_parser.py.
# Put one back only with a measurement that it is populated.
REMOVED_COLUMNS = ("rating", "review_count", "in_stock")

SCHEMA_VERSION = 2   # 1 = the 1.0-1.2 row with the three columns above


def json_schema() -> dict:
    """JSON Schema (draft 2020-12) for one row, generated from `Product` so
    it cannot drift from the dataclass. Published as product.schema.json;
    smoke_test.py fails if that file differs from this output."""
    import typing
    hints = typing.get_type_hints(Product)
    props, required = {}, []
    for f in fields(Product):
        hint = hints[f.name]
        inner = [a for a in typing.get_args(hint) if a is not type(None)] or [hint]
        t = {str: "string", float: "number", int: "integer", bool: "boolean"}[inner[0]]
        nullable = type(None) in typing.get_args(hint)
        props[f.name] = {"type": [t, "null"] if nullable else t}
        required.append(f.name)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "zimmo-scraper row",
        "description": ("One listing, as written to <out>.json and <out>.csv. "
                        "Every key is always present; an unknown value is null."),
        "x-schema-version": SCHEMA_VERSION,
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


EXIT_OK = 0
EXIT_CRASH = 1
EXIT_BAD_USAGE = 2
EXIT_BLOCKED = 3
EXIT_ZERO_PRODUCTS = 4
EXIT_REMOTE_API_ERROR = 5
# The same code, under the name the rest of this family uses for it as of
# 2026-09-21: 5 means "the content was never obtained" -- a remote API
# error is one way for that to happen, a dead proxy or a load timeout is
# another.
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
# The page rendered its listing grid (the engine saw more than
# MIN_CARD_MATCHES listing links before parsing) and the parser still
# returned nothing. That is a parser failure, never "the search is empty":
# reporting it as an empty catalogue (exit 4) would tell a pipeline the
# listings are gone when they are on the screen.
PAGE_PARSER_FAILED = "parser_failed"

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
    if rendered_ok:
        # Engines only report rendered_ok after the listing grid appeared,
        # and never for a status that rules listings out (404, 5xx), so a
        # cleared 403 challenge followed by an unparseable grid lands here
        # too.
        return PAGE_PARSER_FAILED
    if http_status in (401, 403):
        return PAGE_BLOCKED
    if http_status in (404, 410):
        return PAGE_NOT_FOUND
    if http_status is not None and http_status >= 400:
        return PAGE_FETCH_ERROR
    if challenge_detected:
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


def split_page_result(result) -> Tuple[tuple, bool]:
    """An engine's _fetch_page returns (products, ok, blocked,
    remote_api_error, parser_failed); the page dispatchers only consume
    the first four. Returns those four plus the parser-failure flag
    (False for a four-element result)."""
    result = tuple(result)
    return result[:4], bool(result[4]) if len(result) > 4 else False


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


# --- Writing files --------------------------------------------------------
#
# Every output file is written to a temporary file in the destination's own
# directory and moved over the destination only once it is complete
# (flush + fsync + os.replace). A crash, a full disk or a serialisation
# error mid-write therefore leaves the previous good file byte-identical
# instead of truncated. The final mode is the existing file's own mode, or
# for a new file what open() would have produced under the current umask
# (0o666 & ~umask) -- a temp file starts at 0600, which must not leak into
# the result.

def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


@contextlib.contextmanager
def _atomic_open(path: str, newline: Optional[str] = None):
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o666 & ~_current_umask()
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with io.open(fd, "w", encoding="utf-8", newline=newline) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# A spreadsheet treats a cell starting with one of these as a formula
# (title and location are text the site's advertisers write). In the CSV
# only, such a cell gets a leading apostrophe; numbers are left alone and
# the JSON output keeps the exact value.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def _csv_cell(value: Any) -> Tuple[Any, bool]:
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value, True
    return value, False


def write_json(products: List[Product], path: str) -> None:
    with _atomic_open(path) as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)


def write_csv(products: List[Product], path: str) -> int:
    """Returns how many cells were escaped as formula-shaped."""
    fieldnames = _field_names()
    escaped = 0
    with _atomic_open(path, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in products:
            row = {}
            for k, v in asdict(p).items():
                row[k], was_escaped = _csv_cell(v)
                escaped += was_escaped
            writer.writerow(row)
    return escaped


def save(products: List[Product], out_prefix: str, fmt: str) -> int:
    """Writes the requested formats; returns the CSV escape count (0 when
    no CSV was written)."""
    escaped = 0
    if fmt in ("json", "both"):
        write_json(products, f"{out_prefix}.json")
        print(f"[+] Saved {len(products)} listings -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        escaped = write_csv(products, f"{out_prefix}.csv")
        print(f"[+] Saved {len(products)} listings -> {out_prefix}.csv")
        if escaped:
            print(f"[+] {escaped} CSV cell(s) started with = + - @ or a control character "
                  f"and were prefixed with an apostrophe (JSON keeps the raw value).")
    return escaped


def write_meta(meta: Dict[str, Any], out_prefix: str) -> None:
    with _atomic_open(f"{out_prefix}.meta.json") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Saved run metadata -> {out_prefix}.meta.json")


ATTEMPT_META_SUFFIX = ".latest_attempt.meta.json"


def finish_without_fetch(out_prefix: str, *, stop_reason: str,
                         pages_requested: int, url: Optional[str] = None,
                         started_at: Optional[str] = None) -> int:
    """End a run that was stopped before anything was fetched; exit 5.

    Used by the proxy pre-check (a 407 or 402 from the proxy). The record
    goes to `<out>.latest_attempt.meta.json`, NOT `<out>.meta.json`: the
    latter describes the data file beside it, and a previous run's good
    output is still there, untouched, so overwriting its metadata with
    `status: failed` would make the pair contradict each other.
    """
    path = f"{out_prefix}{ATTEMPT_META_SUFFIX}"
    with _atomic_open(path) as f:
        json.dump({
            "status": "failed",
            "stop_reason": stop_reason,
            "pages_requested": pages_requested,
            "pages_completed": 0,
            "failed_pages": [],
            "unattempted_pages": list(range(1, pages_requested + 1)),
            "selection": {"url": selection_url(url) if url else None,
                          "pages_requested": pages_requested},
            "product_count": 0,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "source": "zimmo.be",
        }, f, ensure_ascii=False, indent=2)
    print(f"[+] Saved attempt metadata -> {path} (status=failed, "
          f"stop_reason={stop_reason})")
    print(f"[!] Failed run: nothing was fetched ({stop_reason}). Any previous "
          f"output and its .meta.json are untouched.")
    return EXIT_FETCH_FAILED


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
    parser_failed_pages: Optional[List[int]] = None,
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
    EVER TRIED either. "Data-based termination" (a page
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
    # A parser-failed page is a failed page: its listings were on the
    # screen and are not in the output.
    parser_failed_pages = sorted(set(parser_failed_pages or []))
    failed_pages = sorted(set(failed_pages) | set(parser_failed_pages))
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

    if not products and parser_failed_pages:
        print(f"[!] Page(s) {parser_failed_pages} rendered a listing grid but the parser "
              f"read zero listings from it -- a parser failure, not an empty search. "
              f"Writing nothing, leaving any previous good output in place.")
        return EXIT_FETCH_FAILED

    # A page was attempted and failed, and no listing was recovered from
    # any page: the content was never obtained (a dead proxy, a load
    # timeout), which is a different fact from "we read the listing and it
    # held nothing". This holds even when another page was served empty:
    # an empty page next to a failed one does not prove the search is
    # empty, so it must not be reported as exit 4.
    if not products and failed_pages:
        print(f"[!] {len(failed_pages)} attempted page(s) {failed_pages} could not be fetched "
              f"and no listing was recovered -- the content was never obtained. Writing "
              f"nothing, leaving any previous good output in place.")
        return EXIT_FETCH_FAILED

    if not products and not allow_empty:
        print(f"[!] Zero products found across {pages_completed}/{pages_requested} "
              f"page(s) -- writing NOTHING so this doesn't overwrite a previous good "
              f"run. Pass --allow-empty to force writing an empty result.")
        return EXIT_ZERO_PRODUCTS

    if parser_failed_pages:
        status = "partial"
        other = [p for p in failed_pages if p not in parser_failed_pages]
        stop_reason = (f"parser found nothing on rendered page(s) {parser_failed_pages}"
                       + (f"; pages failed: {other}" if other else ""))
        exit_code = EXIT_PARTIAL
    elif failed_pages:
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

    csv_cells_escaped = save(products, out_prefix, fmt)
    write_meta({
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "failed_pages": failed_pages,
        "parser_failed_pages": parser_failed_pages,
        "unattempted_pages": unattempted_pages,
        "exhausted_pages": exhausted_pages,
        "listing_exhausted": bool(exhausted_pages),
        "selection": {"url": selection_url(url) if url else None,
                      "pages_requested": pages_requested},
        "product_count": len(products),
        "started_at": started_at,
        "finished_at": finished_at,
        "source": "zimmo.be",
        "csv_cells_escaped": csv_cells_escaped,
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


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(json_schema(), indent=2))
