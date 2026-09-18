#!/usr/bin/env python3
"""
zimmo-scraper — Puppeteer edition (via pyppeteer)
=====================================================

Same feature set as playwright_scraper.py -- see its module docstring
for the full strategy, pagination, and captcha-handling story. All
three engines in this family must agree on exit codes, run status, and
whether a run crashes or spends money (CLAUDE.md §6); this file mirrors
playwright_scraper.py's control flow deliberately, translated into
pyppeteer's async API.

pyppeteer is effectively unmaintained (CLAUDE.md §6) -- kept for
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
import logging
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

from pyppeteer import launch, connect

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
logger = logging.getLogger("puppeteer_scraper")

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
    """See playwright_scraper.py's page_url() -- identical logic,
    duplicated so this engine has no import-time dependency on
    Playwright being installed (CLAUDE.md §10)."""
    if page_num <= 1:
        return base_url
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page_num)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


async def handle_captcha_if_present(page, args) -> bool:
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
                token = solve_turnstile(turnstile, args.twocaptcha_key)
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
                token = solve_recaptcha_v3(challenge, args.twocaptcha_key, min_score=args.min_score)
                await page.evaluate(f"({INJECT_TOKEN_JS})", token)
                logger.info("Token injected. Reloading page to continue.")
                await asyncio.sleep(1.5)
                await page.reload({"waitUntil": "domcontentloaded", "timeout": 60000})
            except Exception as e:
                logger.warning("reCAPTCHA solve/injection failed (%s) -- a missing key or "
                                "solver error is a warning here, not a crash.",
                                redact_secret_patterns(str(e)))

    return detected_anything


async def _wait_for_listing_markers(page, timeout_ms: int = 45000) -> bool:
    for attempt in range(2):
        start = time.monotonic()
        try:
            await page.waitForFunction(
                f"document.querySelectorAll('{ITEM_LINK_SELECTOR}').length > {MIN_CARD_MATCHES}",
                {"timeout": timeout_ms},
            )
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


async def _fetch_page(url: str, page_num: int, args, proxy_pool: Optional[ProxyPool],
                       worker_offset: int) -> Tuple[List[Product], bool, bool, bool]:
    current_exit = proxy_pool.get_exit_for_worker(worker_offset) if proxy_pool else None

    for attempt in range(args.retries + 1):
        browser = None
        try:
            if args.cdp_endpoint:
                logger.info("Connecting to existing browser over CDP: %s",
                            _mask_credentials(args.cdp_endpoint))
                browser = await connect(browserWSEndpoint=args.cdp_endpoint)
                pages = await browser.pages()
                page = pages[0] if pages else await browser.newPage()
                try:
                    cdp_session = await page.target.createCDPSession()
                    await cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
                    cdp_session.on("Captcha.detected", lambda *_: logger.info("[Browser API] CAPTCHA detected."))
                    cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Browser API] CAPTCHA solved."))
                    cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Browser API] CAPTCHA auto-solve failed."))
                    logger.info("2captcha Browser API Captcha.setAutoSolve enabled.")
                except Exception as e:
                    logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s).",
                                redact_secret_patterns(str(e)))
            else:
                launch_args = {
                    "headless": args.headless,
                    "args": ["--disable-blink-features=AutomationControlled", "--window-size=1366,900"],
                }
                if current_exit:
                    # Credentials never reach argv (CLAUDE.md §8): only
                    # the credential-free server string goes on the
                    # launch flag. The credential itself goes through
                    # page.authenticate() below instead.
                    launch_args["args"].append(f"--proxy-server={current_exit.server_only()}")
                    logger.info("Using proxy exit: %s", current_exit.masked())
                browser = await launch(**launch_args)
                pages = await browser.pages()
                page = pages[0] if pages else await browser.newPage()
                if current_exit and current_exit.auth_tuple():
                    username, password = current_exit.auth_tuple()
                    await page.authenticate({"username": username, "password": password})

            logger.info("Loading %s (page %d, attempt %d/%d)",
                        url, page_num, attempt + 1, args.retries + 1)
            await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": 60000})

            detected = await handle_captcha_if_present(page, args)
            rendered_ok = await _wait_for_listing_markers(page)

            html = await page.content()
            products = parse_products(html, page.url, category=args.category)
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

            if args.cdp_endpoint:
                await page.close()
                await browser.disconnect()
            else:
                await browser.close()

            page_blocked = detected and not products and not rendered_ok

            if args.delay > 0:
                await asyncio.sleep(args.delay)

            return products, True, page_blocked, False

        except Exception as e:
            error_msg = redact_secret_patterns(str(e))
            if browser:
                try:
                    if args.cdp_endpoint:
                        await browser.disconnect()
                    else:
                        await browser.close()
                except Exception:
                    pass
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
            logger.error("Error fetching page %d (attempt %d/%d): %s",
                         page_num, attempt + 1, args.retries + 1, error_msg)
            if attempt < args.retries:
                await asyncio.sleep(args.retry_delay)
                continue
            return [], False, False, True

    return [], False, False, False


async def _fetch_pages_concurrently(fetch_fn, remaining_pages: List[int], concurrency: int,
                                     seen_skus_initial=()) -> Tuple[dict, List[int], bool, bool, List[int]]:
    """asyncio equivalent of playwright_scraper.py's own
    `_fetch_pages_concurrently` -- same contract, same CLAUDE.md §7
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
            else:
                failed_pages.append(page_num)

    await asyncio.gather(*(worker(i) for i in range(workers)))

    unattempted_pages = [p for p in remaining_pages if p not in page_results]
    return page_results, failed_pages, any_blocked, any_remote_error, unattempted_pages


async def scrape_async(args) -> int:
    started_at = datetime.now(timezone.utc).isoformat()

    proxy_pool = None
    if args.proxy_file:
        urls = load_proxy_file(args.proxy_file)
        proxy_pool = ProxyPool(urls, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries)
    elif args.proxy:
        proxy_pool = ProxyPool([args.proxy], block_retries=args.proxy_block_retries)

    warn_if_concurrency_without_pool(args.concurrency, proxy_pool)
    refuse_concurrency_with_cdp_endpoint(args.concurrency, args.cdp_endpoint)

    url_1 = page_url(args.url, 1)
    products_1, ok_1, blocked_1, remote_1 = await _fetch_page(url_1, 1, args, proxy_pool, worker_offset=0)

    page_results = {1: products_1}
    failed_pages = [] if ok_1 else [1]
    any_blocked = blocked_1
    any_remote_error = remote_1
    pages_completed = 1 if ok_1 else 0
    unattempted_pages: List[int] = []

    remaining_pages = list(range(2, args.pages + 1))
    if remaining_pages and not products_1 and not ok_1:
        logger.warning("Page 1 failed entirely -- not attempting pages 2..%d.", args.pages)
        unattempted_pages = remaining_pages
        remaining_pages = []

    if remaining_pages:
        async def fetch_fn(page_num, worker_offset):
            url = page_url(args.url, page_num)
            return await _fetch_page(url, page_num, args, proxy_pool, worker_offset)

        initial_skus = {p.sku for p in products_1 if p.sku}
        more_results, more_failed, more_blocked, more_remote, more_unattempted = \
            await _fetch_pages_concurrently(fetch_fn, remaining_pages, args.concurrency, initial_skus)
        page_results.update(more_results)
        failed_pages.extend(more_failed)
        any_blocked = any_blocked or more_blocked
        any_remote_error = any_remote_error or more_remote
        pages_completed += len(more_results) - len(more_failed)
        unattempted_pages.extend(more_unattempted)

    all_products = [p for page_num in sorted(page_results) for p in page_results[page_num]]
    all_products = dedupe_by_sku(all_products)

    return finish_run(
        all_products, args.out, args.format,
        pages_requested=args.pages, pages_completed=pages_completed,
        failed_pages=failed_pages, unattempted_pages=unattempted_pages, blocked=any_blocked,
        remote_api_error=any_remote_error, allow_empty=args.allow_empty,
        started_at=started_at,
    )


def parse_args():
    p = argparse.ArgumentParser(description="Zimmo.be scraper (Puppeteer/pyppeteer edition)")
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
                    help="Disable proxy rotation -- retry the SAME exit on every failure instead")
    p.add_argument("--proxy-shuffle", action="store_true", help="Shuffle the proxy pool's exit order at startup")
    p.add_argument("--proxy-block-retries", type=int, default=3,
                    help="Treat a proxy exit as blocked after this many failures")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--captcha-api", default=None, help="Alternate captcha-solving API base URL (advanced)")
    p.add_argument("--solve-captcha", choices=["always", "when-blocked", "never"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=None,
                    help="Minimum acceptable reCAPTCHA v3 score to request from the solver")
    p.add_argument("--cdp-endpoint", default=None,
                    help="Connect to an existing browser over CDP instead of launching pyppeteer's "
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
        sys.exit(asyncio.run(scrape_async(args)))
    except KeyboardInterrupt:
        sys.exit(EXIT_CRASH)
