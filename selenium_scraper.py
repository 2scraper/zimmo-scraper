#!/usr/bin/env python3
"""
zimmo-scraper — Selenium edition
====================================

Same feature set as playwright_scraper.py -- see its module docstring
for the full strategy. All three engines must agree on exit codes, run
status, and whether a run crashes or spends money.

TWO CONFIRMED ENGINE LIMITS:

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

Requires: pip install selenium beautifulsoup4 requests --break-system-packages
"""

import argparse
import logging
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait

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
logger = logging.getLogger("selenium_scraper")
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
    # UA comes from the browser, not a hardcoded literal.

    if proxy_server_only:
        options.add_argument(f"--proxy-server={proxy_server_only}")
    # The proxy meter reads chromedriver's performance log, which can only
    # be asked for before the driver starts.
    proxy_meter.selenium_options(
        options, proxy_meter.of(args) if proxy_server_only else None)

    # Selenium Manager (built into selenium>=4.6) resolves a chromedriver
    # that matches the browser actually installed. webdriver-manager, used
    # here until 2026-09-23, fetched the LATEST chromedriver for Google
    # Chrome instead: on a machine with Chromium 152 it downloaded 154 and
    # every session died with "This version of ChromeDriver only supports
    # Chrome version 154" -- reported as exit 5 on every run.
    driver = webdriver.Chrome(options=options)
    proxy_meter.install_selenium(
        driver, proxy_meter.of(args) if proxy_server_only else None)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    if proxy_check.block_media(args, proxy_server_only, remote=False):
        proxy_check.install_selenium(driver)
    return driver


def handle_captcha_if_present(driver, args, budget: SolveBudget) -> bool:
    """Selenium's execute_script takes a function BODY with an
    explicit call, not Playwright/pyppeteer's `() => expr` convention
    (which is why no JavaScript string crosses an engine boundary)."""
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
                token = solve_turnstile(turnstile, args.twocaptcha_key, budget)
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
                token = solve_recaptcha_v3(challenge, args.twocaptcha_key, budget,
                                           min_score=args.min_score)
                driver.execute_script(f"({INJECT_TOKEN_JS})(arguments[0]);", token)
                logger.info("Token injected. Reloading page to continue.")
                time.sleep(1.5)
                driver.refresh()
            except Exception as e:
                logger.warning("reCAPTCHA solve/injection failed (%s) -- a missing key or "
                                "solver error is a warning here, not a crash.",
                                redact_secret_patterns(str(e)))

    return detected_anything


def _load_complete(d) -> bool:
    proxy_meter.drain_selenium(d)   # the budget is enforced while the page loads
    return d.execute_script("return document.readyState") == "complete"


def _ready_state(d) -> str:
    """"listings", "none" (the page says it has no listing) or "" -- and
    the proxy meter is fed on every poll, so a budget that runs out while
    the page is still loading is enforced then, not after the page."""
    proxy_meter.drain_selenium(d)
    if len(d.find_elements(By.CSS_SELECTOR, ITEM_LINK_SELECTOR)) > MIN_CARD_MATCHES:
        return "listings"
    try:
        text = d.execute_script("return document.body ? document.body.innerText : ''") or ""
    except Exception:
        text = ""
    return "none" if any(t in text for t in NO_RESULTS_TEXTS) else ""


def _wait_for_listing_markers(driver, timeout_s: int = 45) -> bool:
    try:
        state = WebDriverWait(driver, timeout_s, poll_frequency=0.5).until(_ready_state)
    except TimeoutException:
        logger.warning("No listing markers appeared within %ds -- parsing whatever loaded.", timeout_s)
        return False
    if state != "listings":
        logger.info("The page says it has no listing.")
        return False
    time.sleep(1)
    return True


def _navigation_status(driver) -> Optional[int]:
    """WebDriver has no API for the HTTP status of a navigation, so read
    it from the page's own Navigation Timing entry (`responseStatus`,
    Chrome 109+). Measured live 2026-10-07 to agree with Playwright's
    response.status on 200, 403 and 404 pages. 0 or an older browser
    reads as None -- unknown, which classify_page() handles."""
    try:
        status = driver.execute_script(
            "var e = performance.getEntriesByType('navigation')[0];"
            "return e ? e.responseStatus : null;")
    except Exception:
        return None
    return status if isinstance(status, int) and status > 0 else None


# A local browser that never started is not worth a retry (#33).
_LAUNCH_FAILURE_RE = re.compile(r"SessionNotCreated|session not created|cannot find Chrome binary|"
                                r"Unable to obtain driver|chrome not reachable", re.IGNORECASE)


def _fetch_page(url: str, page_num: int, args, proxy_pool: Optional[ProxyPool],
                 worker_offset: int) -> Tuple[List[Product], bool, bool, bool, bool]:
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
        driver = None
        try:
            if current_exit and current_exit.auth_tuple():
                # Selenium's --proxy-server cannot authenticate at all
                # -- strip the credential and warn.
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
            proxy_meter.drain_selenium(driver)
            try:
                wait_obj.until(_load_complete)
            except TimeoutException:
                proxy_meter.drain_selenium(driver)
                if args.cdp_endpoint:
                    driver.close()
                else:
                    driver.quit()
                if proxy_pool and current_exit:
                    # A load that times out through a proxy is that exit
                    # failing too (#26).
                    current_exit = proxy_pool.after_failure(
                        current_exit, worker_offset, args.proxy_rotate,
                        "Timeout loading page %d" % page_num, url)
                    if current_exit is None:
                        return [], False, False, True
                else:
                    logger.error("Timeout loading %s.", url)
                if attempt < args.retries:
                    time.sleep(args.retry_delay)
                    continue
                return [], False, False, False

            http_status = _navigation_status(driver)
            # The page renders (or clears a challenge by itself) before a
            # challenge is looked for or a solve bought -- the same order
            # in every engine (#4).
            rendered_ok = (False if status_rules_out_listings(http_status)
                           else _wait_for_listing_markers(driver))
            solves_before = budget.used
            detected = handle_captcha_if_present(driver, args, budget)
            if (budget.used > solves_before and not rendered_ok
                    and not status_rules_out_listings(http_status)):
                rendered_ok = _wait_for_listing_markers(driver)

            html = driver.page_source
            # Everything the page loaded is in the performance log by now.
            proxy_meter.drain_selenium(driver)
            products = parse_products(html, driver.current_url, category=args.category)
            result_count = parse_result_count(html)
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
                time.sleep(args.retry_delay)
                continue
            ok = page_outcome_is_ok(outcome)

            if ok and args.delay > 0:
                time.sleep(args.delay)

            return (products, ok, outcome == PAGE_BLOCKED, False,
                    outcome == PAGE_PARSER_FAILED, result_count)

        except Exception as e:
            error_msg = redact_secret_patterns(str(e))
            if driver:
                proxy_meter.drain_selenium(driver)
                try:
                    driver.quit()
                except Exception:
                    pass
            if interrupt_seen():
                raise KeyboardInterrupt
            if not args.cdp_endpoint and _LAUNCH_FAILURE_RE.search(error_msg):
                logger.error("The local Chrome/chromedriver did not start, so nothing was fetched "
                             "-- not retrying. (%s)", error_msg.splitlines()[0][:300])
                return [], False, False, True
            if (proxy_pool and current_exit
                    and (is_proxy_error(error_msg) or isinstance(e, TimeoutException))):
                current_exit = proxy_pool.after_failure(
                    current_exit, worker_offset, args.proxy_rotate,
                    "Timeout" if isinstance(e, TimeoutException) else "Proxy error", error_msg)
                if current_exit is None:
                    return [], False, False, True
                continue
            logger.error("Error fetching page %d (attempt %d/%d): %s",
                         page_num, attempt + 1, args.retries + 1, error_msg)
            if attempt < args.retries:
                time.sleep(args.retry_delay)
                continue
            return [], False, False, True

    return [], False, False, False


def _fetch_pages_concurrently(fetch_fn, remaining_pages: List[int], concurrency: int,
                               seen_skus_initial=(), last_page=None
                               ) -> Tuple[dict, List[int], bool, bool, List[int]]:
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
                # Popped only once its result is read: a Ctrl-C there leaves
                # the page among the unattempted ones.
                page_num = pending[fut]
                try:
                    products, ok, blocked, remote_err = fut.result()
                except proxy_meter.ProxyBudgetExceeded as e:
                    # One meter for every worker: the first page that meets
                    # the cap stops dispatch for all of them.
                    logger.error("Page %d not fetched: %s.", page_num, e)
                    stop_event.set()
                    unattempted_pages.append(page_num)
                    del pending[fut]
                    continue
                except Exception as e:
                    logger.error("Worker raised while fetching page %d: %s",
                                 page_num, redact_secret_patterns(str(e)))
                    products, ok, blocked, remote_err = [], False, False, False
                del pending[fut]
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
                        logger.info("Page %d is the last page of the listing -- not "
                                    "requesting any later page.", page_num)
                        stop_event.set()
                else:
                    failed_pages.append(page_num)
            try_submit()
    except KeyboardInterrupt:
        stop_event.set()
        executor.shutdown(wait=False, cancel_futures=True)
        unattempted_pages.extend(sorted(set(pending.values()) | set(remaining_pages[idx:])))
        raise RunInterrupted(page_results, failed_pages, any_blocked, any_remote_error,
                             sorted(unattempted_pages)) from None
    executor.shutdown(wait=True)

    return page_results, failed_pages, any_blocked, any_remote_error, sorted(unattempted_pages)


def scrape(args) -> int:
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
            split_page_result(_fetch_page(url_1, 1, args, proxy_pool, worker_offset=0))
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
        def fetch_fn(page_num, worker_offset):
            url = page_url(args.url, page_num)
            result, parser_failed, result_counts[page_num] = split_page_result(
                _fetch_page(url, page_num, args, proxy_pool, worker_offset))
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
                                          last_page=last_page)
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
    p = argparse.ArgumentParser(description="Zimmo.be scraper (Selenium edition)")
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
                    help="Proxy URL. Credentials, if present, are STRIPPED with a warning -- "
                         "Selenium's --proxy-server cannot authenticate at all.")
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
    # A login in --cdp-endpoint cannot reach the remote browser through
    # debuggerAddress: refused here instead of three ~65 s attempts that
    # end in SessionNotCreatedException (report of 2026-10-28, #5).
    validate_run_args(p, args, cdp_credentials_supported=False)
    if not args.proxy_urls:
        warn_if_concurrency_without_pool(args.concurrency, None)
    return args


if __name__ == "__main__":
    args = parse_args()
    install_interrupt_flag()
    try:
        sys.exit(scrape(args))
    except KeyboardInterrupt:
        # Outside the page loop: nothing more to save. Not exit 1 ("crash").
        sys.exit(EXIT_INTERRUPTED)
