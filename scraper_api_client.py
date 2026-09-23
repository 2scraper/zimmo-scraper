#!/usr/bin/env python3
"""
scraper_api_client.py — zimmo-scraper via 2captcha's Scraper API
=====================================================================

A FOURTH way to run this scraper, architecturally different from the
three browser-driven engines: this calls 2captcha's separate **Scraper
API** (https://scraper.2captcha.com -- a different product from the
Browser API used elsewhere in this family) over plain HTTP. No browser,
no CDP session to manage.

The HTML that comes back is fed into the exact same `product_parser.py`
used by the other three engines.

UPDATE, confirmed live 2026-09-16 — the picture below turned out to be
less clear-cut than first thought, across THREE separate re-tests the
same day. In order:
  (a) Plain call, no `--cdp-url`, stale wait text ("properties: ["):
      timeout (expected -- that text no longer exists on the site at
      all, see product_parser.py's own 2026-09-16 update).
  (b) Wait text corrected to "€"; still no `--cdp-url`: **timeout
      again** -- ruling out the stale wait text as the ONLY cause.
  (c) `--cdp-url` added (routing through a Browser API session): full
      success, 21 listings, ~9s, $0.0005 -- the opposite of three
      EARLIER documented failures on this exact combination
      (2026-08-22, `profile_locked`/`HTTP 422`).
  (d) Later the same day, a PLAIN call again (no `--cdp-url`, corrected
      "€" wait text, otherwise identical to (b)): **succeeded**, 21
      listings, ~12s, $0.0005 -- the opposite of (b), same
      configuration.

**The honest conclusion is NOT "plain calls are broken, --cdp-url
fixes it" or the reverse -- it is that neither path is deterministic
against zimmo.be's Managed Challenge.** Every technical path this
family has tried against this specific challenge (this file's plain
calls, this file's `--cdp-url` calls, and separately
playwright_scraper.py's own Captcha.setAutoSolve -- see that file's
own reliability-testing notes) has succeeded at least once and failed
at least once, on the same configuration, on different attempts. Do
not read a single success OR a single failure as the final word on any
of these paths; a real deployment should retry on a failure rather
than assume one attempt is representative.

READ THIS BEFORE USING ON zimmo.be — HISTORY OF LIVE FINDINGS
(2026-08-22 original, updated 2026-09-16 above with the fuller
picture):

1. Whether a plain call (no `--cdp-url`) gets through zimmo.be's
   Managed Challenge is INCONSISTENT -- confirmed both ways on
   2026-09-16 alone (see (b) and (d) above), with the wait text held
   constant between the two attempts.
2. Whether `--cdp-url` (routing through a Browser API session) gets
   through is ALSO inconsistent -- three failures on 2026-08-22,
   one success on 2026-09-16 (see (c) above), same technique.

**Net effect: for zimmo.be specifically, budget for retries on
whichever path you use** -- a single failed attempt does not mean the
path is broken, and a single success does not mean it will work next
time either.
Usage
-----
    python3 scraper_api_client.py \\
        --url "https://www.zimmo.be/nl/gent-9000/te-koop/" \\
        --format both --out gent_sale

Secrets belong in .env (TWOCAPTCHA_KEY) -- not on the command line.

Requires: pip install requests --break-system-packages
(No playwright/selenium/pyppeteer needed for this engine at all.)
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

import requests

from product_parser import parse_products
from output_writer import finish_run, dedupe_by_sku, EXIT_REMOTE_API_ERROR
from env_config import apply_env_defaults
from proxy_pool import redact_secret_patterns

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

SCRAPER_API_BASE = "https://scraper.2captcha.com"

# CORRECTED via a real live run, 2026-09-16: "properties: [" was
# confirmed live 2026-08-22, but zimmo.be has since moved to an
# Angular-rendered listing grid -- the `properties` embedded-JSON array
# is now GONE entirely (confirmed via a real Playwright run: 0 matches
# on a fully-rendered 420KB page). Waiting for it here made this
# client time out unconditionally, regardless of whether Cloudflare
# would have let the request through -- the wait condition itself was
# stale, not a real signal about the request's outcome. "€" is used
# instead: confirmed live to appear as genuine rendered price text on
# every real listing tile (21 occurrences on a real captured page), and
# not expected to appear on a genuine Cloudflare block page at all.
DEFAULT_WAIT_FOR_TEXT = "€"


def scrape_via_api(url: str, api_key: str, wait_for_text: str, timeout: int,
                    cdp_url: str = None, proxy: str = None) -> str:
    """Calls the Scraper API's synchronous /tasks/sync endpoint and
    returns the fetched page's raw HTML. Raises on any failure.

    `proxy`: CONFIRMED LIVE 2026-09-16 -- field name and a plain
    `scheme://user:pass@host:port` string value are accepted, AND the
    proxy was genuinely routed through, not just silently accepted:
    independently verified via the proxy provider's own usage
    dashboard, which showed the request in its logs (not inferred from
    this project's own output alone, which -- on this inconsistent
    site -- would look identical whether the proxy was used or
    ignored; see module docstring).
    """
    payload = {
        "task_type": "scrape",
        "url": url,
        "data_format": "raw",
        "format": "json",
        "timeout": timeout,
        # An OBJECT. It was sent as a JSON-encoded string until
        # 2026-09-23, when the live API answered every such request with
        # HTTP 422 "ScrapeParser: params.waitFor must be an object" --
        # still billing $0.0005 for it. The same page with an object:
        # HTTP 200, 412 KB.
        "waitFor": {"text": wait_for_text},
    }
    if cdp_url:
        payload["cdpurl"] = cdp_url
        logger.info("Using --cdp-url: routing through an existing browser session "
                    "instead of the Scraper API's own default browser.")
    if proxy:
        payload["proxy"] = proxy
        logger.info("Using --proxy: %s (UNCONFIRMED payload shape -- see this "
                    "function's own docstring)", redact_secret_patterns(proxy))

    logger.info("Requesting %s via 2captcha Scraper API (timeout=%ss, waitFor text=%r)...",
                url, timeout, wait_for_text)

    try:
        resp = requests.post(
            f"{SCRAPER_API_BASE}/tasks/sync",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout + 15,
        )
    except requests.RequestException as e:
        # The key rides in a header here, not a query string, so this
        # is lower risk than the family's known leak points -- but
        # redact globally regardless (CLAUDE.md §8), and a caller-
        # supplied --cdp-url IS part of the request body and could in
        # principle surface in a verbose client-side error.
        raise RuntimeError(f"Scraper API request failed: {redact_secret_patterns(str(e))}") from None

    debug_header = resp.headers.get("x-debug", "")
    if debug_header:
        try:
            debug_info = json.loads(debug_header)
            logger.info("Scraper API task cost: $%s", debug_info.get("price"))
        except json.JSONDecodeError:
            pass

    if resp.status_code == 408:
        raise TimeoutError(f"Scraper API task timed out after {timeout}s -- the page may need "
                            f"more time, or waitFor text {wait_for_text!r} never appeared "
                            f"(Cloudflare may not have cleared). Response: {resp.text[:300]}")
    if resp.status_code == 402:
        raise RuntimeError("Scraper API: insufficient balance on this 2captcha account.")
    if resp.status_code != 200:
        raise RuntimeError(f"Scraper API returned HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    # Measured 2026-09-23: `status` is the API's own verdict ("success"),
    # and the TARGET's HTTP code is `http_code`. Comparing the string
    # `status` with 400 raised TypeError on every successful call.
    target_status = data.get("http_code")
    if isinstance(target_status, int) and target_status >= 400:
        logger.warning("Target page itself responded with HTTP %s -- parsing whatever HTML "
                        "came back anyway.", target_status)

    html = data.get("body", "")
    if not html:
        raise RuntimeError(f"Scraper API returned no HTML body. Full response: {resp.text[:500]}")
    return html


def scrape(args) -> int:
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        html = scrape_via_api(args.url, args.twocaptcha_key, args.wait_for_text,
                               args.timeout, args.cdp_url, args.proxy)
    except Exception as e:
        logger.error("Scraper API request failed: %s", redact_secret_patterns(str(e)))
        return EXIT_REMOTE_API_ERROR

    logger.info("Received %d chars of HTML.", len(html))

    products = parse_products(html, args.url, category=args.category)
    logger.info("Parsed %d listings.", len(products))

    if not products:
        debug_path = f"{args.out}_debug.html"
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.warning("0 listings parsed -- saved what the Scraper API actually returned to %s. "
                        "There's no browser/screenshot on this path, so check the HTML directly.",
                        debug_path)
    elif args.dump_html:
        debug_path = f"{args.out}_debug.html"
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Dumped page to %s (--dump-html).", debug_path)

    products = dedupe_by_sku(products)

    return finish_run(
        products, args.out, args.format,
        pages_requested=1, pages_completed=1 if products else 0,
        failed_pages=[] if products else [1],
        allow_empty=args.allow_empty,
        started_at=started_at,
    )


def parse_args():
    p = argparse.ArgumentParser(description="Zimmo.be scraper via 2captcha's Scraper API (no browser needed)")
    p.add_argument("--url", default=None, help="zimmo.be search-results URL (falls back to ZIMMO_URL from .env)")
    p.add_argument("--category", default=None, help="Label to tag output rows with (optional)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="zimmo_listings", help="Output file prefix")
    p.add_argument("--twocaptcha-key", default=None,
                    help="Your 2captcha.com API key (falls back to TWOCAPTCHA_KEY from .env -- "
                         "the Scraper API uses the same account/key as the Captcha Solver API "
                         "and Browser API)")
    p.add_argument("--wait-for-text", default=DEFAULT_WAIT_FOR_TEXT,
                    help="Text that must appear in the page before the Scraper API returns")
    p.add_argument("--timeout", type=int, default=90,
                    help="Max seconds to let the Scraper API's own task run (1-120)")
    p.add_argument("--cdp-url", default=None,
                    help="Route the Scraper API's fetch through your OWN existing browser "
                         "session -- see module docstring: confirmed INCONSISTENT against "
                         "zimmo.be (failed 3x on 2026-08-22, succeeded on 2026-09-16).")
    p.add_argument("--proxy", default=None,
                    help="Route the Scraper API's fetch through a proxy, e.g. "
                         "http://user:pass@host:port (falls back to ZIMMO_PROXY from .env). "
                         "UNCONFIRMED payload shape -- see scrape_via_api()'s own docstring.")
    p.add_argument("--allow-empty", action="store_true",
                    help="Write output even if zero products were found")
    p.add_argument("--dump-html", action="store_true",
                    help="Always save a debug .html, on success too")
    args = p.parse_args()

    apply_env_defaults(args)
    if not args.url:
        p.error("--url is required (or set ZIMMO_URL in .env)")
    if not args.twocaptcha_key:
        p.error("--twocaptcha-key is required (or set TWOCAPTCHA_KEY in .env)")
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(scrape(args))
    except KeyboardInterrupt:
        sys.exit(1)
