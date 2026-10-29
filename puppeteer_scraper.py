#!/usr/bin/env python3
"""
zimmo-scraper — Puppeteer edition (via pyppeteer)
=====================================================

Same feature set as playwright_scraper.py -- see its module docstring
for the full strategy, pagination, and captcha-handling story. All
three engines in this family must agree on exit codes, run status, and
whether a run crashes or spends money; this file mirrors
playwright_scraper.py's control flow deliberately, translated into
pyppeteer's async API.

pyppeteer is effectively unmaintained -- kept for
parity, demoted in priority, not in correctness.

Usage
-----
    python3 puppeteer_scraper.py \\
        --url "https://www.zimmo.be/nl/gent-9000/te-koop/" \\
        --pages 3 --format both

Requires: pip install pyppeteer beautifulsoup4 requests --break-system-packages
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

from pyppeteer import launch
from pyppeteer.browser import Browser
from pyppeteer.connection import Connection

from captcha_solver import (
    detect_recaptcha_v3, solve_recaptcha_v3, detect_cloudflare_challenge,
    detect_turnstile_challenge, solve_turnstile, SolveBudget,
    INJECT_TOKEN_JS, INJECT_TURNSTILE_TOKEN_JS,
)
from product_parser import parse_products, page_result_count, SELECTORS, NO_RESULTS_TEXTS
from output_writer import (
    Product, finish_run, finish_without_fetch, dedupe_by_sku, EXIT_INTERRUPTED,
    classify_page, page_outcome_is_ok, should_retry_page, status_rules_out_listings,
    PAGE_CONTENT, PAGE_BLOCKED, PAGE_PARSER_FAILED, split_page_result,
    is_last_page, RunInterrupted,
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
logger = logging.getLogger("puppeteer_scraper")
quiet_late_connection_errors()

ITEM_LINK_SELECTOR = SELECTORS["item_link"]
MIN_CARD_MATCHES = 5

# FIXED (2026-10-07, found on a live run while verifying an external
# audit): pyppeteer gives connect() and the calls after it no timeout of
# their own, and opens its websocket with pings disabled, so a remote
# browser that stops answering left the run hanging -- measured: page 1
# done, then the connect for page 2 still waiting after ten minutes.
# Every remote call is bounded; a timeout lands in the
# ordinary retry path below.
CDP_CALL_TIMEOUT_S = 60


async def _bounded(awaitable, seconds: float, what: str):
    try:
        return await asyncio.wait_for(awaitable, seconds)
    except asyncio.TimeoutError:
        raise TimeoutError(f"{what} did not answer within {seconds:.0f}s") from None


class CdpRefused(RuntimeError):
    """The endpoint answered the websocket upgrade with an HTTP error."""


async def _connect_cdp(endpoint: str, timeout_s: float = CDP_CALL_TIMEOUT_S):
    """pyppeteer.connect(), watching the websocket handshake.

    pyppeteer's connect() opens the websocket in a background task and then
    waits for an answer that a refused handshake never sends: a wrong
    Scraping Browser password (HTTP 401) cost three 60-second "did not
    answer" timeouts, with the 401 visible only in a traceback printed
    later (report of 2026-10-28, #29). Same steps as pyppeteer 2.0's
    connect(), except that the handshake's own failure is raised at once."""
    loop = asyncio.get_event_loop()
    connection = Connection(endpoint, loop, 0)
    recv = connection._recv_fut
    deadline = loop.time() + timeout_s
    # Not connection.send() before the socket is open: its sender task
    # polls for the connection with no timeout of its own.
    while not connection._connected and not recv.done():
        if loop.time() > deadline:
            recv.cancel()
            raise TimeoutError(f"CDP connect did not answer within {timeout_s:.0f}s")
        await asyncio.sleep(0.05)
    if recv.done():
        error = None if recv.cancelled() else recv.exception()
        status = getattr(error, "status_code", None) or getattr(
            getattr(error, "response", None), "status_code", None)
        raise CdpRefused(f"the CDP endpoint refused the connection"
                         f"{f' (HTTP {status})' if status else ''}: "
                         f"{redact_secret_patterns(str(error))[:200]}")
    contexts = await _bounded(connection.send("Target.getBrowserContexts"),
                              max(1.0, deadline - loop.time()), "CDP connect")
    return await Browser.create(connection, (contexts or {}).get("browserContextIds", []),
                                False, {"width": 800, "height": 600}, None,
                                lambda: connection.send("Browser.close"))



async def _authenticate_proxy(page, username, password):
    """Answer the proxy's 407 through the CDP Fetch domain.

    Not page.authenticate(): pyppeteer implements it with
    Network.setRequestInterception, which current Chrome no longer has —
    measured 2026-09-29 in a sibling repo against Chrome for Testing:
    "Protocol error (Network.setRequestInterception): ... wasn't found",
    exit 5 before the first request.
    """
    cdp = await page.target.createCDPSession()

    def paused(event):
        asyncio.ensure_future(cdp.send("Fetch.continueRequest", {"requestId": event["requestId"]}))

    def auth(event):
        asyncio.ensure_future(cdp.send("Fetch.continueWithAuth", {
            "requestId": event["requestId"],
            "authChallengeResponse": {"response": "ProvideCredentials",
                                      "username": username, "password": password}}))

    cdp.on("Fetch.requestPaused", paused)
    cdp.on("Fetch.authRequired", auth)
    await cdp.send("Fetch.enable", {"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]})

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
    """See playwright_scraper.py's page_url() -- identical logic,
    duplicated so this engine has no import-time dependency on
    Playwright being installed."""
    if page_num <= 1:
        return base_url
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page_num)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


async def handle_captcha_if_present(page, args, budget: SolveBudget) -> bool:
    html = await page.content()
    detected_anything = False

    should_attempt_solve = True
    if args.solve_captcha == "never":
        should_attempt_solve = False
    elif args.solve_captcha == "when-blocked":
        try:
            existing_matches = await page.querySelectorAllEval(ITEM_LINK_SELECTOR, "els => els.length")
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
                await page.evaluate(f"({INJECT_TURNSTILE_TOKEN_JS})", token)
                logger.info("Turnstile token injected. Reloading page to continue.")
                await asyncio.sleep(1.5)
                await page.reload({"waitUntil": "domcontentloaded", "timeout": 60000})
                html = await page.content()
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
                await page.evaluate(f"({INJECT_TOKEN_JS})", token)
                logger.info("Token injected. Reloading page to continue.")
                await asyncio.sleep(1.5)
                await page.reload({"waitUntil": "domcontentloaded", "timeout": 60000})
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


async def _wait_for_listing_markers(page, timeout_ms: int = 45000) -> bool:
    for attempt in range(2):
        start = time.monotonic()
        try:
            handle = await page.waitForFunction(_READY_JS, {"timeout": timeout_ms,
                                                            "polling": 500})
            if handle is not None and await handle.jsonValue() == "none":
                logger.info("The page says it has no listing.")
                return False
            await asyncio.sleep(1)
            return True
        except Exception as e:
            elapsed = time.monotonic() - start
            if attempt == 0 and elapsed < 10:
                logger.info("Listing-marker wait failed after only %.1fs -- retrying once.", elapsed)
                await asyncio.sleep(2)
                continue
            logger.warning("No listing markers appeared (%s) -- parsing whatever loaded.", e)
            return False
    return False


class CdpConnection:
    """ONE connection to --cdp-endpoint for the whole run; each page gets
    a fresh tab in it. Mirrors playwright_scraper.CdpConnection, whose
    docstring has the measurement: reconnecting per page made the
    Scraping Browser answer HTTP 500 on the next page's websocket upgrade.
    All pages run on one event loop, so sharing it needs no thread rule.
    A failed attempt drops it (reset()), and the retry reconnects."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self._browser = None
        self.connects = 0

    async def browser(self):
        if self._browser is None:
            logger.info("Connecting to existing browser over CDP: %s (one connection for the run)",
                        _mask_credentials(self.endpoint))
            self._browser = await _connect_cdp(self.endpoint)
            self.connects += 1
        return self._browser

    async def new_page(self):
        browser = await self.browser()
        page = await _bounded(browser.newPage(), CDP_CALL_TIMEOUT_S, "CDP newPage()")
        try:
            cdp_session = await _bounded(page.target.createCDPSession(), CDP_CALL_TIMEOUT_S,
                                         "CDP createCDPSession()")
            await _bounded(cdp_session.send("Captcha.setAutoSolve",
                                            {"autoSolve": True, "options": [{"type": "*"}]}),
                           CDP_CALL_TIMEOUT_S, "Captcha.setAutoSolve")
            cdp_session.on("Captcha.detected", lambda *_: logger.info("[Browser API] CAPTCHA detected."))
            cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Browser API] CAPTCHA solved."))
            cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Browser API] CAPTCHA auto-solve failed."))
            logger.info("2captcha Browser API Captcha.setAutoSolve enabled.")
        except Exception as e:
            logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s).",
                        redact_secret_patterns(str(e)))
        return page

    async def reset(self):
        browser, self._browser = self._browser, None
        if browser is not None:
            try:
                await _bounded(browser.disconnect(), CDP_CALL_TIMEOUT_S, "CDP disconnect")
            except Exception:
                pass


async def _fetch_page(url: str, page_num: int, args, proxy_pool: Optional[ProxyPool],
                       worker_offset: int, cdp: Optional[CdpConnection] = None
                       ) -> Tuple[List[Product], bool, bool, bool, bool]:
    """With --cdp-endpoint the page opens in `cdp`, the run's one
    connection; a caller that passes none gets one for this call only."""
    own_cdp = None
    if args.cdp_endpoint and cdp is None:
        cdp = own_cdp = CdpConnection(args.cdp_endpoint)
    try:
        return await _fetch_page_attempts(url, page_num, args, proxy_pool, worker_offset, cdp)
    finally:
        if own_cdp is not None:
            await own_cdp.reset()


# A local browser that never started is not worth a retry. Measured
# 2026-10-28 on Apple Silicon: pyppeteer's x86_64 Chromium 117 crashes at
# once headless under Rosetta ("Browser closed unexpectedly"), and the run
# retried it and then reported a remote API error (#33).
_LAUNCH_FAILURE_RE = re.compile(r"Browser closed unexpectedly|Failed to launch|"
                                r"No such file or directory.*chrom", re.IGNORECASE)


def _is_timeout(e: BaseException) -> bool:
    return isinstance(e, (TimeoutError, asyncio.TimeoutError)) or type(e).__name__ == "TimeoutError"


async def _fetch_page_attempts(url, page_num, args, proxy_pool, worker_offset, cdp):
    current_exit = proxy_pool.get_exit_for_worker(worker_offset) if proxy_pool else None
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
            if cdp:
                page = await cdp.new_page()
            else:
                launch_args = {
                    "headless": args.headless,
                    "args": ["--disable-blink-features=AutomationControlled", "--window-size=1366,900"],
                    # pyppeteer's own SIGINT handler replaced Python's and
                    # ran the event loop re-entrantly: Ctrl-C ended in a
                    # traceback, exit 1, with the pages already fetched
                    # lost (#8). Chromium gets the terminal's SIGINT anyway.
                    "handleSIGINT": False, "handleSIGTERM": False, "handleSIGHUP": False,
                    # Its atexit hook runs the event loop after asyncio.run
                    # has closed it ("RuntimeError: Event loop is closed" at
                    # the end of the log, #33); this loop closes the browser
                    # itself, on every path.
                    "autoClose": False,
                }
                if current_exit:
                    # Credentials never reach argv: only
                    # the credential-free server string goes on the
                    # launch flag. The credential itself goes through
                    # the DevTools Fetch domain below (_authenticate_proxy).
                    launch_args["args"].append(f"--proxy-server={current_exit.server_only()}")
                    logger.info("Using proxy exit: %s", current_exit.masked())
                browser = await launch(**launch_args)
                pages = await browser.pages()
                page = pages[0] if pages else await browser.newPage()
                if current_exit and current_exit.auth_tuple():
                    username, password = current_exit.auth_tuple()
                    await _authenticate_proxy(page, username, password)
                if proxy_check.block_media(args, current_exit, remote=False):
                    await proxy_check.install_pyppeteer(page)
                await proxy_meter.install_pyppeteer(page, proxy_meter.of(args))

            logger.info("Loading %s (page %d, attempt %d/%d)",
                        url, page_num, attempt + 1, args.retries + 1)
            response = await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": 60000})
            http_status = response.status if response else None

            # The page renders (or clears a challenge by itself) before a
            # challenge is looked for or a solve bought -- the same order
            # in every engine (#4).
            rendered_ok = (False if status_rules_out_listings(http_status)
                           else await _wait_for_listing_markers(page))
            solves_before = budget.used
            detected = await handle_captcha_if_present(page, args, budget)
            if (budget.used > solves_before and not rendered_ok
                    and not status_rules_out_listings(http_status)):
                rendered_ok = await _wait_for_listing_markers(page)

            html = await page.content()
            products = parse_products(html, page.url, category=args.category)
            result_count = page_result_count(html, page_num, len(products))
            logger.info("Parsed %d listing(s) from page %d.", len(products), page_num)

            if args.dump_html:
                debug_html = f"{args.out}_page{page_num}_debug.html"
                with open(debug_html, "w", encoding="utf-8") as f:
                    f.write(html)
                try:
                    await page.screenshot({"path": f"{args.out}_page{page_num}_debug.png", "fullPage": True})
                except Exception as e:
                    logger.warning("Could not capture screenshot: %s", e)
                logger.info("Dumped page %d to %s (--dump-html).", page_num, debug_html)
            elif not products:
                debug_html = f"{args.out}_page{page_num}_debug.html"
                with open(debug_html, "w", encoding="utf-8") as f:
                    f.write(html)
                try:
                    await page.screenshot({"path": f"{args.out}_page{page_num}_debug.png", "fullPage": True})
                except Exception as e:
                    logger.warning("Could not capture screenshot: %s", e)
                logger.warning("0 listings parsed on page %d -- saved %s for diagnosis.",
                                page_num, debug_html)

            if cdp:
                await _bounded(page.close(), CDP_CALL_TIMEOUT_S, "CDP page.close()")
            else:
                await browser.close()

            # FIXED (external audit, 2026-10-07, P1/P2): see
            # output_writer.classify_page() -- shared by all three engines.
            outcome = classify_page(len(products), http_status, detected, rendered_ok,
                                    result_count)
            if outcome != PAGE_CONTENT:
                logger.warning("Page %d: HTTP %s, %s, classified as %s.",
                                page_num, http_status,
                                "no result count on the page" if result_count is None
                                else f"the site counts {result_count} result(s)", outcome)
            if should_retry_page(outcome, http_status) and attempt < args.retries:
                await asyncio.sleep(args.retry_delay)
                continue
            ok = page_outcome_is_ok(outcome)

            if ok and args.delay > 0:
                await asyncio.sleep(args.delay)

            if proxy_pool and current_exit:
                proxy_pool.record_success(current_exit)   # it reached the site
            return (products, ok, outcome == PAGE_BLOCKED, False,
                    outcome == PAGE_PARSER_FAILED, result_count)

        except asyncio.CancelledError:
            # Ctrl-C: close what this attempt opened, then let it through.
            if browser is not None:
                try:
                    await _bounded(browser.close(), 10, "browser.close()")
                except BaseException:
                    pass
            raise

        except Exception as e:
            error_msg = redact_secret_patterns(str(e))
            if cdp:
                # A failed attempt closes its tab and drops the connection,
                # so the retry starts from a fresh one.
                if page is not None:
                    try:
                        await _bounded(page.close(), CDP_CALL_TIMEOUT_S, "CDP page.close()")
                    except Exception:
                        pass
                await cdp.reset()
            elif browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if isinstance(e, CdpRefused):
                if "401" in error_msg:
                    logger.error("The Scraping Browser refused the login in --cdp-endpoint / "
                                 "ZIMMO_CDP_ENDPOINT (HTTP 401): check that endpoint's login "
                                 "and password. Not retrying.")
                    return [], False, False, True
                logger.error("Error fetching page %d (attempt %d/%d): %s",
                             page_num, attempt + 1, args.retries + 1, error_msg)
                if attempt < args.retries:
                    await asyncio.sleep(args.retry_delay)
                    continue
                return [], False, False, True
            if not cdp and browser is None and _LAUNCH_FAILURE_RE.search(error_msg):
                logger.error("The local Chromium did not start, so nothing was fetched -- not "
                             "retrying (%s). pyppeteer downloads an x86_64 Chromium, which on "
                             "Apple Silicon crashes headless under Rosetta: try --headful or "
                             "playwright_scraper.py.", error_msg.splitlines()[0][:200])
                return [], False, False, True
            if proxy_pool and current_exit and (is_proxy_error(error_msg) or _is_timeout(e)):
                # A navigation that times out through a proxy is that exit
                # failing too (#26).
                current_exit = proxy_pool.after_failure(
                    current_exit, worker_offset, args.proxy_rotate,
                    "Timeout" if _is_timeout(e) else "Proxy error", error_msg)
                if current_exit is None:
                    return [], False, False, True
                continue
            logger.error("Error fetching page %d (attempt %d/%d): %s",
                         page_num, attempt + 1, args.retries + 1, error_msg)
            if attempt < args.retries:
                await asyncio.sleep(args.retry_delay)
                continue
            return [], False, False, True

    return [], False, False, False


def _uncancel() -> None:
    """Ctrl-C under asyncio.run (Python 3.11+) cancels the main task; once
    that is caught to write the partial run, the task stops counting as
    cancelled so the remaining awaits (closing the CDP connection) run."""
    task = asyncio.current_task()
    if task is not None and hasattr(task, "uncancel"):
        while task.uncancel() > 0:
            pass


async def _fetch_pages_concurrently(fetch_fn, remaining_pages: List[int], concurrency: int,
                                     seen_skus_initial=(), last_page=None
                                     ) -> Tuple[dict, List[int], bool, bool, List[int]]:
    """asyncio equivalent of playwright_scraper.py's own
    `_fetch_pages_concurrently` -- same contract, same
    data-based termination, reimplemented as a small worker-pool
    pulling from a shared index under a lock rather than a
    ThreadPoolExecutor, since pyppeteer is async-native."""
    page_results: dict = {}
    failed_pages: List[int] = []
    any_blocked = False
    any_remote_error = False
    seen_skus = set(seen_skus_initial)
    stop_event = asyncio.Event()
    lock = asyncio.Lock()
    workers = max(concurrency, 1)
    state = {"idx": 0}

    async def worker(worker_offset: int):
        nonlocal any_blocked, any_remote_error
        while True:
            async with lock:
                if stop_event.is_set() or state["idx"] >= len(remaining_pages):
                    return
                page_num = remaining_pages[state["idx"]]
                state["idx"] += 1

            try:
                products, ok, blocked, remote_err = await fetch_fn(page_num, worker_offset)
            except proxy_meter.ProxyBudgetExceeded as e:
                # One meter for every worker: the first page that meets the
                # cap stops dispatch for all of them. Not recorded, so it is
                # counted as unattempted.
                logger.error("Page %d not fetched: %s.", page_num, e)
                stop_event.set()
                return
            except Exception as e:
                logger.error("Worker raised while fetching page %d: %s",
                             page_num, redact_secret_patterns(str(e)))
                products, ok, blocked, remote_err = [], False, False, False

            page_results[page_num] = products
            any_blocked = any_blocked or blocked
            any_remote_error = any_remote_error or remote_err
            if ok:
                async with lock:
                    new_skus = {p.sku for p in products if p.sku} - seen_skus
                    seen_skus.update(new_skus)
                    page_added_nothing_new = products and not new_skus
                if page_added_nothing_new:
                    logger.info("Page %d added no new sku -- listing appears exhausted, "
                                "not requesting any later page.", page_num)
                    stop_event.set()
                elif last_page is not None and last_page(page_num, products, seen_skus):
                    logger.info("Page %d is the last page of the listing -- not requesting "
                                "any later page.", page_num)
                    stop_event.set()
            else:
                failed_pages.append(page_num)

    try:
        await asyncio.gather(*(worker(i) for i in range(workers)))
    except asyncio.CancelledError:
        _uncancel()
        raise RunInterrupted(page_results, failed_pages, any_blocked, any_remote_error,
                             [p for p in remaining_pages if p not in page_results]) from None

    unattempted_pages = [p for p in remaining_pages if p not in page_results]
    return page_results, failed_pages, any_blocked, any_remote_error, unattempted_pages


async def scrape_async(args) -> int:
    cdp = CdpConnection(args.cdp_endpoint) if args.cdp_endpoint else None
    try:
        return await _scrape_async(args, cdp)
    finally:
        if cdp is not None:
            logger.info("CDP connections opened this run: %d.", cdp.connects)
            await cdp.reset()


async def _scrape_async(args, cdp: Optional[CdpConnection]) -> int:
    started_at = datetime.now(timezone.utc).isoformat()

    proxy_pool, refused = _proxy_pool(args)
    if refused:
        return finish_without_fetch(
            args.out, stop_reason=refused.stop_reason,
            pages_requested=args.pages, url=args.url, started_at=started_at)
    meter = proxy_meter.start(args, proxy_pool, remote=bool(args.cdp_endpoint))

    interrupted = False
    result_counts: dict = {}

    url_1 = page_url(args.url, 1)
    try:
        (products_1, ok_1, blocked_1, remote_1), parser_failed_1, result_counts[1] = \
            split_page_result(await _fetch_page(url_1, 1, args, proxy_pool, worker_offset=0,
                                                cdp=cdp))
    except proxy_meter.ProxyBudgetExceeded as e:
        logger.error("Stopping: %s.", e)
        (products_1, ok_1, blocked_1, remote_1), parser_failed_1 = \
            ([], False, False, False), False
    except asyncio.CancelledError:
        _uncancel()
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
        logger.warning("Page 1 failed entirely -- not attempting pages 2..%d.", args.pages)
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
        async def fetch_fn(page_num, worker_offset):
            url = page_url(args.url, page_num)
            result, parser_failed, result_counts[page_num] = split_page_result(
                await _fetch_page(url, page_num, args, proxy_pool, worker_offset, cdp=cdp))
            if parser_failed:
                parser_failed_pages.append(page_num)
            return result

        def last_page(page_num, products, seen_skus):
            return is_last_page(products, len(products_1), result_counts.get(page_num),
                                len(seen_skus))

        initial_skus = {p.sku for p in products_1 if p.sku}
        try:
            more_results, more_failed, more_blocked, more_remote, more_unattempted = \
                await _fetch_pages_concurrently(fetch_fn, remaining_pages, args.concurrency,
                                                initial_skus, last_page=last_page)
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

    all_products = [p for page_num in sorted(page_results) for p in page_results[page_num]]
    all_products = dedupe_by_sku(all_products)

    return finish_run(
        all_products, args.out, args.format,
        pages_requested=args.pages, pages_completed=pages_completed,
        failed_pages=failed_pages, unattempted_pages=unattempted_pages, blocked=any_blocked,
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
    """See playwright_scraper.py's own copy."""
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
    """See playwright_scraper.py's own copy: every exit is pre-checked."""
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
    p = argparse.ArgumentParser(description="Zimmo.be scraper (Puppeteer/pyppeteer edition)")
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
                    help="Disable proxy rotation -- retry the SAME exit on every failure instead")
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
                    help="Connect to an existing browser over CDP instead of launching pyppeteer's "
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
    try:
        sys.exit(asyncio.run(scrape_async(args)))
    except KeyboardInterrupt:
        # Python 3.9/3.10, or a Ctrl-C outside the page loop: nothing more to
        # save. Not exit 1 ("crash").
        sys.exit(EXIT_INTERRUPTED)
