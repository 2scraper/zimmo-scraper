#!/usr/bin/env python3
"""
zimmo-scraper — Playwright edition (primary engine)
=======================================================

Scrapes zimmo.be real-estate search-results pages -- houses, apartments,
for sale or for rent, any Belgian city/postcode/category page.

Since 2026-09-15 zimmo.be renders its listing grid with Angular, and the
tiles themselves are what gets parsed (the inline JSON array confirmed on
2026-08-22 is gone) -- see product_parser.py's module docstring for which
of its three extraction paths fires on the live site today.

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
import contextlib
import json
import logging
import re
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
    detect_turnstile_challenge, solve_turnstile, SolveBudget,
    INJECT_TOKEN_JS, INJECT_TURNSTILE_TOKEN_JS,
)
from product_parser import parse_products, parse_result_count, SELECTORS, NO_RESULTS_TEXTS
from output_writer import (
    Product, finish_run, finish_without_fetch, dedupe_by_sku, EXIT_INTERRUPTED,
    classify_page, page_outcome_is_ok, should_retry_page, status_rules_out_listings,
    PAGE_CONTENT, PAGE_BLOCKED, PAGE_PARSER_FAILED, split_page_result,
    is_last_page, RunInterrupted, install_interrupt_flag, interrupt_seen,
)
import proxy_check
import proxy_meter
from env_config import (
    apply_env_defaults, non_negative_int, positive_int, non_negative_float,
    recaptcha_score, validate_run_args,
)
from proxy_pool import (
    ProxyPool, is_proxy_error, warn_if_concurrency_without_pool,
    redact_secret_patterns, quiet_late_connection_errors, proxy_urls_from_args,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")
quiet_late_connection_errors()

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


def handle_captcha_if_present(page, args, budget: SolveBudget) -> bool:
    """Runs after EVERY navigation. Returns True if a challenge was
    detected (regardless of whether solving was attempted or
    succeeded).

    FIXED (external audit, 2026-09-14, finding #3):
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
                token = solve_turnstile(turnstile, args.twocaptcha_key, budget)
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
                token = solve_recaptcha_v3(challenge, args.twocaptcha_key, budget,
                                           min_score=args.min_score)
                page.evaluate(INJECT_TOKEN_JS, token)
                logger.info("Token injected. Reloading page to continue.")
                page.wait_for_timeout(1500)
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning("reCAPTCHA solve/injection failed (%s) -- a missing key or "
                                "solver error is a warning here, not a crash.",
                                redact_secret_patterns(str(e)))

    return detected_anything


# Truthy once the grid has rendered ("listings") or the page says it has
# none ("none"), so a page past the end of a listing does not wait 45 s.
_READY_JS = (
    "() => document.querySelectorAll(%s).length > %d ? 'listings' : "
    "(document.body && %s.some(t => document.body.innerText.includes(t)) ? 'none' : false)"
    % (json.dumps(ITEM_LINK_SELECTOR), MIN_CARD_MATCHES, json.dumps(list(NO_RESULTS_TEXTS))))


def _wait_for_listing_markers(page, timeout_ms: int = 45000) -> bool:
    for attempt in range(2):
        start = time.monotonic()
        try:
            handle = page.wait_for_function(_READY_JS, timeout=timeout_ms, polling=500)
            if handle is not None and handle.json_value() == "none":
                logger.info("The page says it has no listing.")
                return False
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


class CdpConnection:
    """ONE connection to --cdp-endpoint for the whole run.

    FIXED (2026-10-07, found on live runs while verifying an external
    audit): every page used to open its own connection and drop it
    again. The Scraping Browser API allows one live connection per
    profile, and a reconnect made a moment after the previous page let
    go was answered with HTTP 500 on the websocket upgrade -- measured on
    pages 2 and 3 of a 3-page run, each costing a retry. Each page now
    gets a fresh TAB in the one connected browser instead.

    Playwright's sync objects belong to the thread that created them, so
    a CdpConnection is only ever used from the main thread: scrape()
    fetches every page in that thread when --cdp-endpoint is set, which
    costs nothing, because --concurrency above 1 is refused there anyway.

    A failed attempt drops the connection (reset()), so the retry starts
    from a clean one instead of inheriting whatever broke."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self._pw = None
        self._browser = None
        self.connects = 0

    def browser(self):
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        self.reset()
        logger.info("Connecting to existing browser over CDP: %s (one connection for the run)",
                    _mask_credentials(self.endpoint))
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(self.endpoint)
        except Exception:
            self.reset()
            raise
        self.connects += 1
        return self._browser

    def new_page(self):
        browser = self.browser()
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
        return page

    def reset(self):
        """Disconnect. Stopping the driver drops the websocket and leaves the
        remote browser running, exactly as the per-page version did."""
        self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None


def _fetch_page(url: str, page_num: int, args, proxy_pool: Optional[ProxyPool],
                 worker_offset: int, cdp: Optional[CdpConnection] = None
                 ) -> Tuple[List[Product], bool, bool, bool, bool]:
    """Fetch and parse one page. Returns
    (products, success, blocked, remote_api_error, parser_failed); a
    failure path returns the first four only.

    CRITICAL FIX (external audit, 2026-09-14, finding #1): this
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

    A worker owns ONE exit for its lifetime.

    With --cdp-endpoint the page opens in `cdp`, the run's one connection
    (see CdpConnection); a caller that passes none gets a connection that
    lasts for this call only."""
    current_exit = proxy_pool.get_exit_for_worker(worker_offset) if proxy_pool else None
    own_cdp = None
    if args.cdp_endpoint and cdp is None:
        cdp = own_cdp = CdpConnection(args.cdp_endpoint)
    try:
        return _fetch_page_attempts(url, page_num, args, proxy_pool, worker_offset,
                                    current_exit, cdp)
    finally:
        if own_cdp is not None:
            own_cdp.reset()


# A local browser that never started is not worth a retry (report of
# 2026-10-28, #33: the same crash three times, then "Remote API error").
_LAUNCH_FAILURE_RE = re.compile(r"BrowserType\.launch|Executable doesn't exist|"
                                r"Browser closed unexpectedly|Failed to launch")
# The Scraping Browser refusing the login is final for the run (#29).
_CDP_REFUSED_RE = re.compile(r"\b401\b|Unauthorized")


def _fetch_page_attempts(url, page_num, args, proxy_pool, worker_offset, current_exit, cdp):
    if proxy_pool and current_exit is None:
        logger.error("Page %d: every proxy exit is retired for this run -- not fetching it "
                     "without a proxy.", page_num)
        return [], False, False, True
    # One paid-solve allowance for this page, shared by every attempt and
    # every proxy rotation below (captcha_solver.SolveBudget).
    budget = SolveBudget()
    for attempt in range(args.retries + 1):
        # Before a browser is even launched for this attempt.
        proxy_meter.check(args)
        browser = None
        page = None
        try:
            # Over CDP the driver is already running inside `cdp`; a second
            # sync_playwright() in the same thread is refused by Playwright.
            with (contextlib.nullcontext() if cdp else sync_playwright()) as pw:
                if cdp:
                    page = cdp.new_page()
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
                    if proxy_check.block_media(args, current_exit, remote=False):
                        proxy_check.install_playwright(context)
                    # After the media route, so the budget route runs first
                    # (Playwright runs routes in reverse registration order).
                    proxy_meter.install_playwright(context, proxy_meter.of(args))
                    page = context.new_page()

                logger.info("Loading %s (page %d, attempt %d/%d)",
                            url, page_num, attempt + 1, args.retries + 1)
                response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                http_status = response.status if response else None

                # The page gets its chance to render -- or to clear a
                # challenge by itself -- BEFORE a challenge is looked for
                # or a solve is bought. Checked straight after goto, the
                # Turnstile field was not there yet, and a Cloudflare page
                # ended blocked with nothing in the log about the solver
                # (report of 2026-10-28, #4; Selenium, whose get() returns
                # later, did report it).
                rendered_ok = (False if status_rules_out_listings(http_status)
                               else _wait_for_listing_markers(page))
                solves_before = budget.used
                detected = handle_captcha_if_present(page, args, budget)
                if (budget.used > solves_before and not rendered_ok
                        and not status_rules_out_listings(http_status)):
                    rendered_ok = _wait_for_listing_markers(page)

                html = page.content()
                products = parse_products(html, page.url, category=args.category)
                result_count = parse_result_count(html)
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

                if cdp:
                    page.close()
                else:
                    browser.close()

                # FIXED (external audit, 2026-10-07, P1/P2): this used to
                # return success for ANY page that loaded, so a 403 on
                # page 2 read as a complete run and a 500 as an empty
                # catalogue. classify_page() is shared by all engines.
                outcome = classify_page(len(products), http_status, detected, rendered_ok,
                                        result_count)
                if outcome != PAGE_CONTENT:
                    logger.warning("Page %d: HTTP %s, %s, classified as %s.",
                                    page_num, http_status,
                                    "no result count on the page" if result_count is None
                                    else f"the site counts {result_count} result(s)", outcome)
                if should_retry_page(outcome, http_status) and attempt < args.retries:
                    time.sleep(args.retry_delay)
                    continue
                ok = page_outcome_is_ok(outcome)

                # --delay: wired up (audit finding #3 -- previously
                # "reserved" but never actually slept). Only after a
                # SUCCESSFUL fetch, not before the first attempt and not
                # between retries (--retry-delay already covers that).
                if ok and args.delay > 0:
                    time.sleep(args.delay)

                return (products, ok, outcome == PAGE_BLOCKED, False,
                        outcome == PAGE_PARSER_FAILED, result_count)

        except PWTimeout:
            _drop_cdp_after_failure(cdp, page)
            if interrupt_seen():
                raise KeyboardInterrupt
            if proxy_pool and current_exit:
                # A navigation that times out through a proxy is that exit
                # failing too: a silent exit used to be retried as-is, page
                # after page, while a working one sat unused (#26).
                current_exit = proxy_pool.after_failure(
                    current_exit, worker_offset, args.proxy_rotate,
                    "Timeout loading page %d" % page_num, url)
                if current_exit is None:
                    return [], False, False, True
            else:
                logger.error("Timeout loading %s (attempt %d/%d).", url, attempt + 1, args.retries + 1)
            if attempt < args.retries:
                time.sleep(args.retry_delay)
                continue
            return [], False, False, False

        except Exception as e:
            _drop_cdp_after_failure(cdp, page)
            if interrupt_seen():
                raise KeyboardInterrupt
            error_msg = redact_secret_patterns(str(e))
            if cdp and _CDP_REFUSED_RE.search(error_msg):
                logger.error("The Scraping Browser refused the login in --cdp-endpoint / "
                             "ZIMMO_CDP_ENDPOINT (HTTP 401): check that endpoint's login and "
                             "password. Not retrying. (%s)", error_msg.splitlines()[0][:200])
                return [], False, False, True
            if not cdp and _LAUNCH_FAILURE_RE.search(error_msg):
                logger.error("The local Chromium did not start, so nothing was fetched -- not "
                             "retrying. Run `playwright install chromium` if it is missing. "
                             "(%s)", error_msg.splitlines()[0][:300])
                return [], False, False, True
            if is_proxy_error(error_msg) and proxy_pool and current_exit:
                current_exit = proxy_pool.after_failure(
                    current_exit, worker_offset, args.proxy_rotate, "Proxy error", error_msg)
                if current_exit is None:
                    return [], False, False, True
                continue
            logger.error("Unexpected error fetching page %d: %s", page_num, error_msg)
            if attempt < args.retries:
                time.sleep(args.retry_delay)
                continue
            return [], False, False, True

    return [], False, False, False


def _drop_cdp_after_failure(cdp: Optional[CdpConnection], page) -> None:
    """A failed attempt over CDP closes its tab and drops the connection,
    so the retry starts from a fresh one."""
    if cdp is None:
        return
    if page is not None:
        try:
            page.close()
        except Exception:
            pass
    cdp.reset()


def _fetch_pages_concurrently(fetch_fn, remaining_pages: List[int], concurrency: int,
                               seen_skus_initial=(), inline: bool = False,
                               last_page=None
                               ) -> Tuple[dict, List[int], bool, bool, List[int]]:
    """`inline=True` fetches the pages one after another in THIS thread,
    with the same bookkeeping -- for --cdp-endpoint, whose one connection
    belongs to the main thread (see CdpConnection).

    `last_page(page_num, products, seen_skus)` says whether a good page
    ended the listing (output_writer.is_last_page); a page that added no
    new sku always does. Ctrl-C raises RunInterrupted with what was
    gathered."""
    page_results: dict = {}
    failed_pages: List[int] = []
    unattempted_pages: List[int] = []
    any_blocked = False
    any_remote_error = False
    seen_skus = set(seen_skus_initial)
    stop_event = threading.Event()
    workers = max(concurrency, 1)

    def record(page_num, result):
        nonlocal any_blocked, any_remote_error, seen_skus
        if result is None:
            # Stopped by the proxy budget before it was fetched.
            unattempted_pages.append(page_num)
            return
        products, ok, blocked, remote_err = result
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
            elif last_page is not None and last_page(page_num, products, seen_skus):
                logger.info("Page %d is the last page of the listing -- not requesting "
                            "any later page.", page_num)
                stop_event.set()
        else:
            failed_pages.append(page_num)

    def interrupted(not_fetched):
        unattempted_pages.extend(not_fetched)
        return RunInterrupted(page_results, failed_pages, any_blocked, any_remote_error,
                              sorted(unattempted_pages))

    def call(page_num, result_fn):
        try:
            return result_fn()
        except proxy_meter.ProxyBudgetExceeded as e:
            # One meter for every worker: the first page that meets the cap
            # stops dispatch for all of them.
            logger.error("Page %d not fetched: %s.", page_num, e)
            stop_event.set()
            return None
        except Exception as e:
            logger.error("Worker raised while fetching page %d: %s",
                         page_num, redact_secret_patterns(str(e)))
            return [], False, False, False

    if inline:
        for idx, page_num in enumerate(remaining_pages):
            if stop_event.is_set():
                unattempted_pages.extend(remaining_pages[idx:])
                break
            try:
                record(page_num, call(page_num, lambda: fetch_fn(page_num, 0)))
            except KeyboardInterrupt:
                raise interrupted(remaining_pages[idx:]) from None
        return page_results, failed_pages, any_blocked, any_remote_error, sorted(unattempted_pages)

    executor = ThreadPoolExecutor(max_workers=workers)
    pending = {}
    idx = 0
    try:

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
                # Popped only once recorded: a Ctrl-C while recording leaves
                # the page among the unattempted ones.
                page_num = pending[fut]
                record(page_num, call(page_num, fut.result))
                del pending[fut]
            try_submit()
    except KeyboardInterrupt:
        stop_event.set()
        executor.shutdown(wait=False, cancel_futures=True)
        raise interrupted(sorted(set(pending.values()) | set(remaining_pages[idx:]))) from None
    executor.shutdown(wait=True)

    return page_results, failed_pages, any_blocked, any_remote_error, sorted(unattempted_pages)


def scrape(args) -> int:
    # One connection for the whole run over --cdp-endpoint (CdpConnection),
    # released however the run ends.
    cdp = CdpConnection(args.cdp_endpoint) if args.cdp_endpoint else None
    try:
        return _scrape(args, cdp)
    finally:
        if cdp is not None:
            logger.info("CDP connections opened this run: %d.", cdp.connects)
            cdp.reset()


def _scrape(args, cdp: Optional[CdpConnection]) -> int:
    started_at = datetime.now(timezone.utc).isoformat()

    proxy_pool, refused = _proxy_pool(args)
    if refused:
        return finish_without_fetch(
            args.out, stop_reason=refused.stop_reason,
            pages_requested=args.pages, url=args.url, started_at=started_at)
    meter = proxy_meter.start(args, proxy_pool, remote=bool(args.cdp_endpoint))

    interrupted = False
    result_counts: dict = {}

    # Page 1 is always fetched alone (its own sync_playwright() context
    # -- see _fetch_page's own docstring for why every call, including
    # this one in the main thread, manages its own context uniformly).
    url_1 = page_url(args.url, 1)
    try:
        (products_1, ok_1, blocked_1, remote_1), parser_failed_1, result_counts[1] = \
            split_page_result(_fetch_page(url_1, 1, args, proxy_pool, worker_offset=0, cdp=cdp))
    except proxy_meter.ProxyBudgetExceeded as e:
        logger.error("Stopping: %s.", e)
        (products_1, ok_1, blocked_1, remote_1), parser_failed_1 = \
            ([], False, False, False), False
    except KeyboardInterrupt:
        logger.warning("Interrupted (Ctrl-C) while fetching page 1.")
        (products_1, ok_1, blocked_1, remote_1), parser_failed_1 = \
            ([], False, False, False), False
        interrupted = True

    page_results = {1: products_1}
    failed_pages = [] if ok_1 or interrupted else [1]
    any_blocked = blocked_1
    any_remote_error = remote_1
    pages_completed = 1 if ok_1 else 0
    parser_failed_pages: List[int] = [1] if parser_failed_1 else []
    unattempted_pages: List[int] = []
    exhausted_pages: List[int] = []

    remaining_pages = list(range(2, args.pages + 1))
    if interrupted:
        unattempted_pages = list(range(1, args.pages + 1))
        remaining_pages = []
    if remaining_pages and not products_1 and not ok_1:
        logger.warning("Page 1 failed entirely -- not attempting pages 2..%d, since page "
                        "1's own result decides whether the site can be addressed "
                        "independently.", args.pages)
        unattempted_pages = remaining_pages
        remaining_pages = []
    if remaining_pages and meter is not None and meter.exceeded:
        logger.error("Stopping after page 1: the proxy budget is spent; pages "
                     "2..%d are not requested.", args.pages)
        unattempted_pages = remaining_pages
        remaining_pages = []
    if remaining_pages and ok_1 and is_last_page(products_1, len(products_1), result_counts[1],
                                                 len({p.sku for p in products_1 if p.sku})):
        logger.info("Page 1 holds every result the site counts (%s) -- not requesting "
                    "pages 2..%d.", result_counts[1], args.pages)
        exhausted_pages = remaining_pages
        remaining_pages = []

    if remaining_pages:
        def fetch_fn(page_num, worker_offset):
            url = page_url(args.url, page_num)
            result, parser_failed, result_counts[page_num] = split_page_result(
                _fetch_page(url, page_num, args, proxy_pool, worker_offset, cdp=cdp))
            if parser_failed:
                parser_failed_pages.append(page_num)
            return result

        def last_page(page_num, products, seen_skus):
            return is_last_page(products, len(products_1), result_counts.get(page_num),
                                len(seen_skus))

        initial_skus = {p.sku for p in products_1 if p.sku}
        try:
            more_results, more_failed, more_blocked, more_remote, more_unattempted = \
                _fetch_pages_concurrently(fetch_fn, remaining_pages, args.concurrency, initial_skus,
                                          inline=cdp is not None, last_page=last_page)
        except RunInterrupted as e:
            logger.warning("Interrupted (Ctrl-C): keeping the pages already fetched.")
            more_results, more_failed, more_blocked, more_remote, more_unattempted = e.partial
            interrupted = True
        page_results.update(more_results)
        failed_pages.extend(more_failed)
        any_blocked = any_blocked or more_blocked
        any_remote_error = any_remote_error or more_remote
        pages_completed += len(more_results) - len(more_failed)
        # Pages left undispatched because the listing ENDED: a complete
        # answer, not a gap in one.
        exhausted_pages = more_unattempted
        if interrupted or (meter is not None and meter.exceeded):
            # ...unless Ctrl-C or the proxy budget stopped dispatch: then
            # they were simply never asked for.
            unattempted_pages = sorted(set(unattempted_pages)
                                       | set(more_unattempted))
            exhausted_pages = []

    # Merge in PAGE ORDER, not arrival order.
    all_products = [p for page_num in sorted(page_results) for p in page_results[page_num]]
    all_products = dedupe_by_sku(all_products)

    return finish_run(
        all_products, args.out, args.format,
        pages_requested=args.pages, pages_completed=pages_completed,
        failed_pages=failed_pages,
        # FIXED (external audit, 2026-09-14, finding #5):
        # unattempted_pages was computed but never passed into
        # finish_run() -- a run that stopped at page 1 (blocked) with
        # --pages 5 could report status=complete, exit 0. Now passed
        # through so finish_run()'s own fix (accepting and acting on
        # this parameter) actually takes effect for this engine.
        unattempted_pages=unattempted_pages,
        blocked=any_blocked,
        remote_api_error=any_remote_error, allow_empty=args.allow_empty,
        started_at=started_at,
        exhausted_pages=exhausted_pages, url=args.url,
        parser_failed_pages=parser_failed_pages,
        proxy=proxy_meter.finish(args),
        listing_end_seen=_listing_end_seen(page_results, failed_pages, result_counts,
                                           len(products_1)),
        interrupted=interrupted,
    )


def _listing_end_seen(page_results, failed_pages, result_counts, first_page_size) -> bool:
    """Did the run see the end of the listing, even on its last requested
    page? (A page past the end, or the count reached on a short page.)"""
    seen = set()
    for page_num in sorted(page_results):
        products = page_results[page_num]
        seen |= {p.sku for p in products if p.sku}
        if page_num in failed_pages:
            continue
        if page_num > 1 and is_last_page(products, first_page_size,
                                         result_counts.get(page_num), len(seen)):
            return True
    return False


def _proxy_pool(args):
    """The run's pool (None without a proxy) and the pre-check's fatal
    answer, if every exit got one. Each exit is pre-checked: a pool used to
    be refused whole when its FIRST exit answered 407, even with a working
    exit behind it (report of 2026-10-28, #6)."""
    urls = getattr(args, "proxy_urls", None)
    if urls is None:
        urls = proxy_urls_from_args(args)   # args not built by parse_args()
    if not urls:
        return None, None
    pool = ProxyPool(urls, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries)
    # One request through the proxy before a browser exists: a 407 or 402
    # from the gateway ends the run here, in seconds, instead of surfacing
    # as a navigation timeout after every retry.
    return pool, proxy_check.gate(pool, args.url)


def parse_args():
    p = argparse.ArgumentParser(description="Zimmo.be scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                    help="zimmo.be search-results URL (falls back to ZIMMO_URL from .env)")
    p.add_argument("--category", default=None, help="Label to tag output rows with (optional)")
    p.add_argument("--pages", type=positive_int, default=1, help="Number of result pages to fetch")
    p.add_argument("--delay", type=non_negative_float, default=0.0,
                    help="Seconds to sleep after each successfully-fetched page (rate limiting)")
    p.add_argument("--retries", type=non_negative_int, default=2,
                    help="Extra attempts per page on a transient failure (0 = one attempt)")
    p.add_argument("--retry-delay", type=non_negative_float, default=3.0, help="Seconds to wait between retries")
    p.add_argument("--concurrency", type=positive_int, default=1,
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
    p.add_argument("--proxy-block-retries", type=positive_int, default=3,
                    help="Treat a proxy exit as blocked after this many failures")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--solve-captcha", choices=["always", "when-blocked", "never"], default="when-blocked")
    p.add_argument("--min-score", type=recaptcha_score, default=None,
                    help="Minimum acceptable reCAPTCHA v3 score to request from the solver")
    proxy_check.add_media_flags(p)
    proxy_meter.add_budget_flag(p)
    p.add_argument("--cdp-endpoint", default=None,
                    help="Connect to an existing browser over CDP instead of launching Playwright's "
                         "bundled Chromium. Refused together with --proxy/--proxy-file (the remote "
                         "browser has its own exit); --headless/--headful are ignored when set.")
    p.add_argument("--allow-empty", action="store_true",
                    help="Write output even if zero products were found")
    p.add_argument("--dump-html", action="store_true",
                    help="Always save a debug .html/.png per page, on success too")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()

    apply_env_defaults(args)
    validate_run_args(p, args)
    if not args.proxy_urls:
        warn_if_concurrency_without_pool(args.concurrency, None)
    return args


if __name__ == "__main__":
    args = parse_args()
    install_interrupt_flag()
    try:
        sys.exit(scrape(args))
    except KeyboardInterrupt:
        # Outside the page loop (a second Ctrl-C, or one while writing):
        # nothing more to save. Not exit 1, which means "crash".
        sys.exit(EXIT_INTERRUPTED)
