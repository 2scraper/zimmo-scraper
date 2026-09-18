#!/usr/bin/env python3
"""
zimmo-scraper — Playwright edition (primary engine)
=======================================================

Scrapes zimmo.be real-estate search-results pages -- houses, apartments,
for sale or for rent, any Belgian city/postcode/category page.

Confirmed live (2026-08-22): zimmo.be search-results pages embed their
listings as a JSON array inline in a <script> tag -- see
product_parser.py's module docstring for the full story, including
which of its three extraction paths are confirmed live versus
best-effort fallbacks.

Pagination: **CORRECTED via a real live run, 2026-09-16** -- an earlier
`discover_search_params`-style probe (from an unrelated MCP-tooling
session, run against the site at an earlier point in time) had found a
`p` query parameter and that confirmation was carried into this file
verbatim; a live run against the CURRENT site found that `?p=N` is
silently ignored (page 2 returned page 1's own listings again,
verbatim, which our own data-based termination correctly read as "the
listing is exhausted" -- an honest but WRONG conclusion caused by a
stale confirmation, not a bug in that termination logic itself). The
real, working parameter, found by inspecting the site's own pagination
link directly, is `page` -- `https://www.zimmo.be/nl/gent-9000/te-koop?page=2`.
`page_url()` below reconstructs `?page=N` on top of whatever the
caller's own --url already carries, preserving every other query
parameter.

Usage
-----
    python3 playwright_scraper.py \\
        --url "https://www.zimmo.be/nl/gent-9000/te-koop/" \\
        --pages 3 --format both \\
        --cdp-endpoint "ws://user:pass@cb.2captcha.com:9222"

Secrets belong in .env (see env_config.py), not on this command line.

Requires: pip install playwright beautifulsoup4 requests --break-system-packages
          && playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from captcha_solver import (
    detect_recaptcha_v3, solve_recaptcha_v3, detect_cloudflare_challenge,
    detect_turnstile_challenge, solve_turnstile,
    INJECT_TOKEN_JS, INJECT_TURNSTILE_TOKEN_JS,
)
from product_parser import parse_products, SELECTORS
from output_writer import Product, finish_run, dedupe_by_sku, EXIT_CRASH
from env_config import apply_env_defaults
from proxy_pool import (
    ProxyPool, load_proxy_file, is_proxy_error,
    warn_if_concurrency_without_pool, refuse_concurrency_with_cdp_endpoint,
    redact_secret_patterns,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")

ITEM_LINK_SELECTOR = SELECTORS["item_link"]
MIN_CARD_MATCHES = 5


def _mask_credentials(url: str) -> str:
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


def page_url(base_url: str, page_num: int) -> str:
    """Reconstruct zimmo.be's own `?page=N` pagination parameter --
    FIXED 2026-09-16, was `?p=N` (a stale confirmation from an earlier,
    unrelated session against a since-changed site; see module
    docstring). Page 1 needs no `page` param at all (matches the site's
    own page-1 links, which omit it)."""
    if page_num <= 1:
        return base_url
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page_num)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def handle_captcha_if_present(page, args) -> bool:
    """Runs after EVERY navigation. Returns True if a challenge was
    detected (regardless of whether solving was attempted or
    succeeded).

    FIXED (external audit, 2026-09-14, codex.md finding #3):
    --solve-captcha=never previously did not prevent the solver from
    being called at all -- detection and solving were unconditional.
    Now: "never" detects (for logging/blocked-status) but never calls a
    solver; "always" solves on every detection; "when-blocked" (the
    default) solves only when the page doesn't already show usable
    listing content -- a stale/leftover challenge marker on an
    otherwise-rendered page is not something worth spending a solve on.
    """
    html = page.content()
    detected_anything = False

    should_attempt_solve = True
    if args.solve_captcha == "never":
        should_attempt_solve = False
    elif args.solve_captcha == "when-blocked":
        # A quick, non-waiting check: if the page already has more than
        # MIN_CARD_MATCHES item links, real content is already present
        # and a captcha marker here is stale/inert -- not worth solving.
        try:
            existing_matches = page.eval_on_selector_all(ITEM_LINK_SELECTOR, "els => els.length")
        except Exception:
            existing_matches = 0
        should_attempt_solve = existing_matches <= MIN_CARD_MATCHES

    if detect_cloudflare_challenge(html):
        logger.warning("Cloudflare interstitial detected on the page.")
        detected_anything = True

    turnstile = detect_turnstile_challenge(html, page.url)
    if turnstile:
        detected_anything = True
        if not should_attempt_solve:
            logger.info("Turnstile/Cloudflare challenge detected, but --solve-captcha=%s -- "
                        "not attempting to solve it.", args.solve_captcha)
        else:
            logger.warning("Cloudflare Turnstile challenge detected (sitekey=%s) -- "
                            "attempting to solve.", turnstile.sitekey)
            try:
                token = solve_turnstile(turnstile, args.twocaptcha_key)
                page.evaluate(INJECT_TURNSTILE_TOKEN_JS, token)
                logger.info("Turnstile token injected. Reloading page to continue.")
                page.wait_for_timeout(1500)
                page.reload(wait_until="domcontentloaded", timeout=60000)
                html = page.content()
            except Exception as e:
                logger.warning("Turnstile solve/injection failed (%s) -- a missing key or "
                                "solver error is a warning here, not a crash.",
                                redact_secret_patterns(str(e)))

    challenge = detect_recaptcha_v3(html, page.url)
    if challenge:
        detected_anything = True
        if not should_attempt_solve:
            logger.info("reCAPTCHA v3 detected, but --solve-captcha=%s -- not attempting "
                        "to solve it.", args.solve_captcha)
        else:
            logger.warning("reCAPTCHA v3 detected (sitekey=%s, action=%s) -- attempting to solve.",
                           challenge.sitekey, challenge.action)
            try:
                token = solve_recaptcha_v3(challenge, args.twocaptcha_key, min_score=args.min_score)
                page.evaluate(INJECT_TOKEN_JS, token)
                logger.info("Token injected. Reloading page to continue.")
                page.wait_for_timeout(1500)
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning("reCAPTCHA solve/injection failed (%s) -- a missing key or "
                                "solver error is a warning here, not a crash.",
                                redact_secret_patterns(str(e)))

    return detected_anything


def _wait_for_listing_markers(page, timeout_ms: int = 45000) -> bool:
    for attempt in range(2):
        start = time.monotonic()
        try:
            page.wait_for_function(
                f"document.querySelectorAll({ITEM_LINK_SELECTOR!r}).length > {MIN_CARD_MATCHES}",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(1000)
            return True
        except Exception as e:
            elapsed = time.monotonic() - start
            if attempt == 0 and elapsed < 10:
                logger.info("Listing-marker wait failed after only %.1fs -- retrying once.", elapsed)
                page.wait_for_timeout(2000)
                continue
            logger.warning("No listing markers appeared (%s) -- parsing whatever loaded.", e)
            return False
    return False


def _fetch_page(url: str, page_num: int, args, proxy_pool: Optional[ProxyPool],
                 worker_offset: int) -> Tuple[List[Product], bool, bool, bool]:
    """Fetch and parse one page. Returns
    (products, success, blocked, remote_api_error).

    CRITICAL FIX (external audit, 2026-09-14, codex.md finding #1): this
    function now creates and tears down its OWN `sync_playwright()`
    context internally, rather than receiving one shared object created
    in a different thread. The previous version created a single
    `sync_playwright()` in the main thread and passed that SAME object
    into worker threads via ThreadPoolExecutor -- Playwright's sync API
    is explicitly documented as not safe to use this way (its sync
    wrapper is built on greenlets pinned to the thread that created
    them), and the audit reproduced the exact failure live:
    `greenlet.error: Cannot switch to a different thread`, on EVERY
    multi-page run, even at --concurrency 1, since pages beyond the
    first always went through the executor regardless of concurrency.
    Confirmed fixed by direct reproduction: launching a browser from
    inside a worker thread's OWN `with sync_playwright() as pw:` block
    works correctly at any concurrency; sharing one across threads does
    not, at any concurrency including 1.

    A worker owns ONE exit for its lifetime."""
    current_exit = proxy_pool.get_exit_for_worker(worker_offset) if proxy_pool else None

    for attempt in range(args.retries + 1):
        browser = None
        try:
            with sync_playwright() as pw:
                if args.cdp_endpoint:
                    logger.info("Connecting to existing browser over CDP: %s",
                                _mask_credentials(args.cdp_endpoint))
                    browser = pw.chromium.connect_over_cdp(args.cdp_endpoint)
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = context.new_page()
                    try:
                        cdp_session = context.new_cdp_session(page)
                        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
                        cdp_session.on("Captcha.detected", lambda *_: logger.info("[Browser API] CAPTCHA detected."))
                        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info("[Browser API] CAPTCHA sent to 2captcha."))
                        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Browser API] CAPTCHA solved."))
                        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Browser API] CAPTCHA auto-solve failed."))
                        logger.info("2captcha Browser API Captcha.setAutoSolve enabled.")
                    except Exception as e:
                        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s).",
                                    redact_secret_patterns(str(e)))
                else:
                    launch_kwargs = {"headless": args.headless}
                    if current_exit:
                        launch_kwargs["proxy"] = {
                            "server": current_exit.server_only(),
                            **({"username": current_exit.auth_tuple()[0],
                                "password": current_exit.auth_tuple()[1]}
                               if current_exit.auth_tuple() else {}),
                        }
                        logger.info("Using proxy exit: %s", current_exit.masked())
                    browser = pw.chromium.launch(**launch_kwargs)
                    context = browser.new_context(locale="nl-BE")
                    page = context.new_page()

                logger.info("Loading %s (page %d, attempt %d/%d)",
                            url, page_num, attempt + 1, args.retries + 1)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)

                detected = handle_captcha_if_present(page, args)
                rendered_ok = _wait_for_listing_markers(page)

                html = page.content()
                products = parse_products(html, page.url, category=args.category)
                logger.info("Parsed %d listing(s) from page %d.", len(products), page_num)

                if args.dump_html:
                    debug_html = f"{args.out}_page{page_num}_debug.html"
                    with open(debug_html, "w", encoding="utf-8") as f:
                        f.write(html)
                    try:
                        page.screenshot(path=f"{args.out}_page{page_num}_debug.png", full_page=True)
                    except Exception as e:
                        logger.warning("Could not capture screenshot: %s", e)
                    logger.info("Dumped page %d to %s (--dump-html).", page_num, debug_html)
                elif not products:
                    debug_html = f"{args.out}_page{page_num}_debug.html"
                    with open(debug_html, "w", encoding="utf-8") as f:
                        f.write(html)
                    try:
                        page.screenshot(path=f"{args.out}_page{page_num}_debug.png", full_page=True)
                    except Exception as e:
                        logger.warning("Could not capture screenshot: %s", e)
                    logger.warning("0 listings parsed on page %d -- saved %s for diagnosis.",
                                    page_num, debug_html)

                if args.cdp_endpoint:
                    page.close()
                else:
                    browser.close()

                page_blocked = detected and not products and not rendered_ok

                # --delay: wired up (audit finding #3 -- previously
                # "reserved" but never actually slept). Only after a
                # SUCCESSFUL fetch, not before the first attempt and not
                # between retries (--retry-delay already covers that).
                if args.delay > 0:
                    time.sleep(args.delay)

                return products, True, page_blocked, False

        except PWTimeout:
            logger.error("Timeout loading %s (attempt %d/%d).", url, attempt + 1, args.retries + 1)
            if attempt < args.retries:
                time.sleep(args.retry_delay)
                continue
            return [], False, False, False

        except Exception as e:
            error_msg = redact_secret_patterns(str(e))
            if is_proxy_error(error_msg) and proxy_pool:
                logger.warning("Proxy error on exit %s (%s) -- rotating to a different exit.",
                                current_exit.masked() if current_exit else "?", error_msg)
                if args.proxy_rotate:
                    next_exit = proxy_pool.rotate_away_from(current_exit, worker_offset)
                    if next_exit is None:
                        return [], False, False, True
                    current_exit = next_exit
                else:
                    logger.info("--no-proxy-rotate is set -- retrying the SAME exit rather "
                                "than rotating away from it.")
                continue
            logger.error("Unexpected error fetching page %d: %s", page_num, error_msg)
            if attempt < args.retries:
                time.sleep(args.retry_delay)
                continue
            return [], False, False, True

    return [], False, False, False


def _fetch_pages_concurrently(fetch_fn, remaining_pages: List[int], concurrency: int,
                               seen_skus_initial=()) -> Tuple[dict, List[int], bool, bool, List[int]]:
    page_results: dict = {}
    failed_pages: List[int] = []
    unattempted_pages: List[int] = []
    any_blocked = False
    any_remote_error = False
    seen_skus = set(seen_skus_initial)
    stop_event = threading.Event()
    workers = max(concurrency, 1)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {}
        idx = 0

        def try_submit():
            nonlocal idx
            while idx < len(remaining_pages) and len(pending) < workers:
                if stop_event.is_set():
                    unattempted_pages.extend(remaining_pages[idx:])
                    idx = len(remaining_pages)
                    return
                page_num = remaining_pages[idx]
                fut = executor.submit(fetch_fn, page_num, idx % workers)
                pending[fut] = page_num
                idx += 1

        try_submit()
        while pending:
            done, _ = wait(list(pending.keys()), return_when=FIRST_COMPLETED)
            for fut in done:
                page_num = pending.pop(fut)
                try:
                    products, ok, blocked, remote_err = fut.result()
                except Exception as e:
                    logger.error("Worker raised while fetching page %d: %s",
                                 page_num, redact_secret_patterns(str(e)))
                    products, ok, blocked, remote_err = [], False, False, False
                page_results[page_num] = products
                any_blocked = any_blocked or blocked
                any_remote_error = any_remote_error or remote_err
                if ok:
                    new_skus = {p.sku for p in products if p.sku} - seen_skus
                    seen_skus |= new_skus
                    if products and not new_skus:
                        logger.info("Page %d added no new sku -- listing appears exhausted, "
                                    "not requesting any later page.", page_num)
                        stop_event.set()
                else:
                    failed_pages.append(page_num)
            try_submit()

    return page_results, failed_pages, any_blocked, any_remote_error, sorted(unattempted_pages)


def scrape(args) -> int:
    started_at = datetime.now(timezone.utc).isoformat()

    proxy_pool = None
    if args.proxy_file:
        urls = load_proxy_file(args.proxy_file)
        proxy_pool = ProxyPool(urls, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries)
    elif args.proxy:
        proxy_pool = ProxyPool([args.proxy], block_retries=args.proxy_block_retries)

    warn_if_concurrency_without_pool(args.concurrency, proxy_pool)
    refuse_concurrency_with_cdp_endpoint(args.concurrency, args.cdp_endpoint)

    # Page 1 is always fetched alone (its own sync_playwright() context
    # -- see _fetch_page's own docstring for why every call, including
    # this one in the main thread, manages its own context uniformly).
    url_1 = page_url(args.url, 1)
    products_1, ok_1, blocked_1, remote_1 = _fetch_page(url_1, 1, args, proxy_pool, worker_offset=0)

    page_results = {1: products_1}
    failed_pages = [] if ok_1 else [1]
    any_blocked = blocked_1
    any_remote_error = remote_1
    pages_completed = 1 if ok_1 else 0
    unattempted_pages: List[int] = []

    remaining_pages = list(range(2, args.pages + 1))
    if remaining_pages and not products_1 and not ok_1:
        logger.warning("Page 1 failed entirely -- not attempting pages 2..%d, since page "
                        "1's own result decides whether the site can be addressed "
                        "independently.", args.pages)
        unattempted_pages = remaining_pages
        remaining_pages = []

    if remaining_pages:
        def fetch_fn(page_num, worker_offset):
            url = page_url(args.url, page_num)
            return _fetch_page(url, page_num, args, proxy_pool, worker_offset)

        initial_skus = {p.sku for p in products_1 if p.sku}
        more_results, more_failed, more_blocked, more_remote, more_unattempted = \
            _fetch_pages_concurrently(fetch_fn, remaining_pages, args.concurrency, initial_skus)
        page_results.update(more_results)
        failed_pages.extend(more_failed)
        any_blocked = any_blocked or more_blocked
        any_remote_error = any_remote_error or more_remote
        pages_completed += len(more_results) - len(more_failed)
        unattempted_pages.extend(more_unattempted)

    # Merge in PAGE ORDER, not arrival order.
    all_products = [p for page_num in sorted(page_results) for p in page_results[page_num]]
    all_products = dedupe_by_sku(all_products)

    return finish_run(
        all_products, args.out, args.format,
        pages_requested=args.pages, pages_completed=pages_completed,
        failed_pages=failed_pages,
        # FIXED (external audit, 2026-09-14, codex.md finding #5):
        # unattempted_pages was computed but never passed into
        # finish_run() -- a run that stopped at page 1 (blocked) with
        # --pages 5 could report status=complete, exit 0. Now passed
        # through so finish_run()'s own fix (accepting and acting on
        # this parameter) actually takes effect for this engine.
        unattempted_pages=unattempted_pages,
        blocked=any_blocked,
        remote_api_error=any_remote_error, allow_empty=args.allow_empty,
        started_at=started_at,
    )


def parse_args():
    p = argparse.ArgumentParser(description="Zimmo.be scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                    help="zimmo.be search-results URL (falls back to ZIMMO_URL from .env)")
    p.add_argument("--category", default=None, help="Label to tag output rows with (optional)")
    p.add_argument("--pages", type=int, default=1, help="Number of result pages to fetch")
    p.add_argument("--delay", type=float, default=0.0,
                    help="Seconds to sleep after each successfully-fetched page (rate limiting)")
    p.add_argument("--retries", type=int, default=2, help="Retries per page on a transient failure")
    p.add_argument("--retry-delay", type=float, default=3.0, help="Seconds to wait between retries")
    p.add_argument("--concurrency", type=int, default=1,
                    help="Parallel workers for pages 2+ (page 1 is always fetched alone). "
                         "Not supported together with --cdp-endpoint.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="zimmo_listings", help="Output file prefix")
    p.add_argument("--proxy", default=None,
                    help="Single proxy URL, e.g. http://user:pass@host:port "
                         "(2prx.com is 2captcha.com's own proxy product)")
    p.add_argument("--proxy-file", default=None, help="Path to a file of proxy URLs, one per line")
    p.add_argument("--proxy-rotate", action="store_true", default=True,
                    help="Rotate to a different pool exit on a dead-proxy error (default: on)")
    p.add_argument("--no-proxy-rotate", dest="proxy_rotate", action="store_false",
                    help="Disable proxy rotation -- retry the SAME exit on every failure instead. "
                         "FIXED (audit finding #3): --proxy-rotate alone was a store_true flag "
                         "defaulting to True, which meant it could never actually be turned off "
                         "from the command line -- this flag is the real, working opt-out.")
    p.add_argument("--proxy-shuffle", action="store_true", help="Shuffle the proxy pool's exit order at startup")
    p.add_argument("--proxy-block-retries", type=int, default=3,
                    help="Treat a proxy exit as blocked after this many failures")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--captcha-api", default=None, help="Alternate captcha-solving API base URL (advanced)")
    p.add_argument("--solve-captcha", choices=["always", "when-blocked", "never"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=None,
                    help="Minimum acceptable reCAPTCHA v3 score to request from the solver")
    p.add_argument("--cdp-endpoint", default=None,
                    help="Connect to an existing browser over CDP instead of launching Playwright's "
                         "bundled Chromium. --proxy and --headless/--headful are ignored when set.")
    p.add_argument("--allow-empty", action="store_true",
                    help="Write output even if zero products were found")
    p.add_argument("--dump-html", action="store_true",
                    help="Always save a debug .html/.png per page, on success too")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()

    apply_env_defaults(args)
    if not args.url:
        p.error("--url is required (or set ZIMMO_URL in .env)")
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(scrape(args))
    except KeyboardInterrupt:
        sys.exit(EXIT_CRASH)
