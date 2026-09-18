#!/usr/bin/env python3
"""
zimmo-scraper — Selenium edition
====================================

Same feature set as playwright_scraper.py -- see its module docstring
for the full strategy. All three engines must agree on exit codes, run
status, and whether a run crashes or spends money (CLAUDE.md §6).

TWO CONFIRMED ENGINE LIMITS (CLAUDE.md §6):

  1. Selenium's `debuggerAddress` was designed for a LOCAL,
     UNAUTHENTICATED debug port. It does NOT forward a `user:pass`
     embedded in a remote CDP URL -- confirmed with
     `SessionNotCreatedException` on multiple sites in this family. Use
     playwright_scraper.py or puppeteer_scraper.py instead if you need
     `--cdp-endpoint` against an authenticated remote endpoint.
  2. Selenium's `--proxy-server` cannot authenticate at all. A
     credential embedded in `--proxy` is stripped and a warning logged
     -- never silently baked into a CLI flag that looks like it is
     doing something it is not.

Usage
-----
    python3 selenium_scraper.py \\
        --url "https://www.zimmo.be/nl/gent-9000/te-koop/" \\
        --pages 3 --format both

Requires: pip install selenium webdriver-manager beautifulsoup4 requests --break-system-packages
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

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

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
logger = logging.getLogger("selenium_scraper")

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
    """See playwright_scraper.py's page_url() -- identical logic and
    the same 2026-09-16 fix (?p=N -> ?page=N)."""
    if page_num <= 1:
        return base_url
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page_num)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def build_driver(args, proxy_server_only: Optional[str] = None) -> webdriver.Chrome:
    options = Options()

    if args.cdp_endpoint:
        parsed = urlparse(args.cdp_endpoint)
        host_port = parsed.netloc.split("@")[-1]  # drop user:pass@ if present
        logger.warning("Connecting via Selenium's debuggerAddress to %s -- this does NOT "
                        "forward URL-embedded credentials. If your endpoint requires auth, "
                        "this will likely fail with SessionNotCreatedException; use "
                        "playwright_scraper.py or puppeteer_scraper.py instead.", host_port)
        options.add_experimental_option("debuggerAddress", host_port)
        driver = webdriver.Chrome(options=options)
        try:
            driver.execute_cdp_cmd("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
            logger.info("2captcha Browser API Captcha.setAutoSolve enabled.")
        except Exception as e:
            logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s).",
                        redact_secret_patterns(str(e)))
        return driver

    if args.headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1366,900")
    # UA comes from the browser, not a hardcoded literal (CLAUDE.md §8).

    if proxy_server_only:
        options.add_argument(f"--proxy-server={proxy_server_only}")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    return driver


def handle_captcha_if_present(driver, args) -> bool:
    """Selenium's execute_script takes a function BODY with an
    explicit call, not Playwright/pyppeteer's `() => expr` convention
    (CLAUDE.md §1 on why no JavaScript crosses an engine boundary)."""
    html = driver.page_source
    detected_anything = False

    should_attempt_solve = True
    if args.solve_captcha == "never":
        should_attempt_solve = False
    elif args.solve_captcha == "when-blocked":
        existing_matches = len(driver.find_elements(By.CSS_SELECTOR, ITEM_LINK_SELECTOR))
        should_attempt_solve = existing_matches <= MIN_CARD_MATCHES

    if detect_cloudflare_challenge(html):
        logger.warning("Cloudflare interstitial detected on the page.")
        detected_anything = True

    turnstile = detect_turnstile_challenge(html, driver.current_url)
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
                driver.execute_script(f"({INJECT_TURNSTILE_TOKEN_JS})(arguments[0]);", token)
                logger.info("Turnstile token injected. Reloading page to continue.")
                time.sleep(1.5)
                driver.refresh()
                html = driver.page_source
            except Exception as e:
                logger.warning("Turnstile solve/injection failed (%s) -- a missing key or "
                                "solver error is a warning here, not a crash.",
                                redact_secret_patterns(str(e)))

    challenge = detect_recaptcha_v3(html, driver.current_url)
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
                driver.execute_script(f"({INJECT_TOKEN_JS})(arguments[0]);", token)
                logger.info("Token injected. Reloading page to continue.")
                time.sleep(1.5)
                driver.refresh()
            except Exception as e:
                logger.warning("reCAPTCHA solve/injection failed (%s) -- a missing key or "
                                "solver error is a warning here, not a crash.",
                                redact_secret_patterns(str(e)))

    return detected_anything


def _wait_for_listing_markers(driver, timeout_s: int = 45) -> bool:
    try:
        WebDriverWait(driver, timeout_s).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, ITEM_LINK_SELECTOR)) > MIN_CARD_MATCHES
        )
        time.sleep(1)
        return True
    except TimeoutException:
        logger.warning("No listing markers appeared within %ds -- parsing whatever loaded.", timeout_s)
        return False


def _fetch_page(url: str, page_num: int, args, proxy_pool: Optional[ProxyPool],
                 worker_offset: int) -> Tuple[List[Product], bool, bool, bool]:
    current_exit = proxy_pool.get_exit_for_worker(worker_offset) if proxy_pool else None

    for attempt in range(args.retries + 1):
        driver = None
        try:
            if current_exit and current_exit.auth_tuple():
                # Selenium's --proxy-server cannot authenticate at all
                # (CLAUDE.md §6) -- strip the credential and warn.
                logger.warning("Proxy exit %s has credentials, but Selenium's --proxy-server "
                                "cannot authenticate at all -- connecting WITHOUT credentials, "
                                "which will likely fail against an authenticated proxy. Use "
                                "playwright_scraper.py or puppeteer_scraper.py for an "
                                "authenticated proxy exit.", current_exit.masked())
            proxy_server_only = current_exit.server_only() if current_exit else None
            if proxy_server_only:
                logger.info("Using proxy exit (unauthenticated only): %s",
                            current_exit.masked())

            driver = build_driver(args, proxy_server_only=proxy_server_only)
            wait_obj = WebDriverWait(driver, 20)

            logger.info("Loading %s (page %d, attempt %d/%d)",
                        url, page_num, attempt + 1, args.retries + 1)
            driver.get(url)
            try:
                wait_obj.until(lambda d: d.execute_script("return document.readyState") == "complete")
            except TimeoutException:
                logger.error("Timeout loading %s.", url)
                if args.cdp_endpoint:
                    driver.close()
                else:
                    driver.quit()
                if attempt < args.retries:
                    time.sleep(args.retry_delay)
                    continue
                return [], False, False, False

            detected = handle_captcha_if_present(driver, args)
            rendered_ok = _wait_for_listing_markers(driver)

            html = driver.page_source
            products = parse_products(html, driver.current_url, category=args.category)
            logger.info("Parsed %d listing(s) from page %d.", len(products), page_num)

            if args.dump_html:
                debug_html = f"{args.out}_page{page_num}_debug.html"
                with open(debug_html, "w", encoding="utf-8") as f:
                    f.write(html)
                try:
                    driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
                except Exception as e:
                    logger.warning("Could not capture screenshot: %s", e)
                logger.info("Dumped page %d to %s (--dump-html).", page_num, debug_html)
            elif not products:
                debug_html = f"{args.out}_page{page_num}_debug.html"
                with open(debug_html, "w", encoding="utf-8") as f:
                    f.write(html)
                try:
                    driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
                except Exception as e:
                    logger.warning("Could not capture screenshot: %s", e)
                logger.warning("0 listings parsed on page %d -- saved %s for diagnosis.",
                                page_num, debug_html)

            if args.cdp_endpoint:
                driver.close()
            else:
                driver.quit()

            page_blocked = detected and not products and not rendered_ok

            if args.delay > 0:
                time.sleep(args.delay)

            return products, True, page_blocked, False

        except Exception as e:
            error_msg = redact_secret_patterns(str(e))
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            if is_proxy_error(error_msg) and proxy_pool and current_exit:
                logger.warning("Proxy error on exit %s (%s) -- rotating to a different exit.",
                                current_exit.masked(), error_msg)
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
                time.sleep(args.retry_delay)
                continue
            return [], False, False, True

    return [], False, False, False


def _fetch_pages_concurrently(fetch_fn, remaining_pages: List[int], concurrency: int,
                               seen_skus_initial=()) -> Tuple[dict, List[int], bool, bool, List[int]]:
    """See playwright_scraper.py's own copy for the full reasoning --
    identical contract and implementation."""
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
        logger.warning("Page 1 failed entirely -- not attempting pages 2..%d.", args.pages)
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
    p = argparse.ArgumentParser(description="Zimmo.be scraper (Selenium edition)")
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
                    help="Proxy URL. Credentials, if present, are STRIPPED with a warning -- "
                         "Selenium's --proxy-server cannot authenticate at all (CLAUDE.md §6).")
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
                    help="BEST-EFFORT: attach via Selenium's debuggerAddress. Does NOT forward "
                         "URL-embedded credentials -- for an authenticated remote endpoint use "
                         "playwright_scraper.py or puppeteer_scraper.py instead.")
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
