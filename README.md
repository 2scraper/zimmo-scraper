# zimmo-scraper

![release](https://img.shields.io/github/v/release/2scraper/zimmo-scraper?include_prereleases)
![tests](https://img.shields.io/github/actions/workflow/status/2scraper/zimmo-scraper/tests.yml?branch=main&label=tests)
![canary](https://img.shields.io/github/actions/workflow/status/2scraper/zimmo-scraper/canary.yml?branch=main&label=canary)
![python](https://img.shields.io/badge/python-3.9%E2%80%933.12-blue)
![license](https://img.shields.io/github/license/2scraper/zimmo-scraper)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![no account needed](https://img.shields.io/badge/runs%20without%20an%20account-yes-success)

Zimmo.be real-estate listing-page scraper (Playwright, Selenium, Puppeteer,
or a cloud browser via CDP) — embedded-JSON parsing, Cloudflare Turnstile /
reCAPTCHA v3 solving, proxies, fingerprints.

---

## What this actually does

Zimmo.be search-results pages embed their entire current page of listings
as a JSON array inline in a `<script>` tag (confirmed live, 2026-08-22 —
see `product_parser.py`'s own module docstring for the exact markup and
field names). This scraper reads that array directly rather than parsing
rendered HTML text — the primary path never has to guess a CSS selector
for price, EPC label, surface area, or bedroom count.

**You do not need a paid proxy or a captcha-solving key to try this.**
Whether zimmo.be's own Cloudflare protection triggers on your IP varies —
run `python3 playwright_scraper.py --url "..." --headful` first and see
what you get before reaching for `--cdp-endpoint`/`--twocaptcha-key`. What
those buy you when the site *does* challenge you: automatic Cloudflare
Turnstile solving via the 2Captcha Scraping Browser API's
`Captcha.setAutoSolve`, and a consistent device identity across a run
instead of your own IP repeatedly re-triggering the challenge.

## Honesty about what's confirmed live

- **The `properties`-JSON extraction path is CONFIRMED ABSENT on the
  live site as of 2026-09-15.** It was confirmed present 2026-08-22,
  but zimmo.be has since moved to an Angular-rendered listing grid --
  a real, fully-rendered page (420KB+) now has zero matches for it.
  The code path is kept (in case a future page type or a reversion
  uses it again) but is not what actually runs today.
- **The JSON-LD path is CONFIRMED ABSENT for listing data as of
  2026-09-15.** A live page's only JSON-LD block is a `BreadcrumbList`
  (navigation only, no product schema at all).
- **The CSS/price-anchored fallback is CONFIRMED LIVE AND ACTIVE** --
  it is the path actually used on every real run today, not a last-
  resort fallback. It found a real, serious bug the same day it first
  ran against production (a correctly-widened DOM scope being
  discarded and silently recomputed narrower, causing 0 products on
  every real page) and a second one shortly after (a price regex
  swallowing an adjacent unrelated number across a flattened space,
  producing prices in the billions) -- both fixed and locked in with
  regression tests; see CHANGELOG 0.4.0 for the full list of six
  live-run fixes.
- **Multi-page runs are confirmed working end-to-end against the real
  site**, across all three engines, with real, clean data verified by
  hand (63 unique listings across 3 pages, zero duplicates, realistic
  €19,000–€2,500,000 price range, zero missing titles/locations). This
  took two separate live-run bugs to get right: a Playwright sync-API
  threading bug (fixed and verified with 3 concurrent real browser
  launches) and a stale pagination parameter -- `page_url()` built
  `?p=N`, confirmed live in an earlier, unrelated session against the
  site at an earlier point in time, but the site's real parameter,
  found by inspecting its own pagination link directly, is `page`
  (`?page=N`). Every multi-page run before that second fix silently
  returned only page 1's own listings, repeated.
- **The current live Cloudflare protection on zimmo.be is a Managed
  Challenge**, not a standalone Turnstile widget -- confirmed live,
  and confirmed INCONSISTENT: whether a plain HTTP call, a browser
  session's own auto-solve, or any other path gets through it varies
  run to run, on the identical configuration, across every technical
  path this project has tried (see `scraper_api_client.py`'s own
  module docstring and `playwright_scraper.py`'s reliability-testing
  notes for the dated history). Budget for retries in production;
  a single success or failure on one attempt is not representative.
- **`fingerprint_client.py` is not wired into any engine's browser-
  launch path yet.** It exists and is tested against a real Playwright
  installation's own `new_context()` signature, but no engine calls it.
- **The Scraper API path (`scraper_api_client.py`) has succeeded
  multiple times against zimmo.be** -- plain calls, `--cdp-url`-routed
  calls, and calls with `--proxy` (the last independently confirmed
  via the proxy provider's own usage dashboard) have all worked at
  least once. It is subject to the same run-to-run inconsistency noted
  above, not a distinct limitation of this specific path -- see its
  own module docstring for the dated history of both successes and
  failures on identical configurations.

## Quick start

```bash
git clone https://github.com/2scraper/zimmo-scraper.git
cd zimmo-scraper
pip install -r requirements.txt -r requirements-playwright.txt --break-system-packages
playwright install chromium

cp .env.example .env   # then fill in TWOCAPTCHA_KEY if you have one

python3 playwright_scraper.py --url "https://www.zimmo.be/nl/gent-9000/te-koop/" --headful
```

Selenium and Puppeteer (pyppeteer) work the same way, with their own
`requirements-{engine}.txt` — see [Engines](#engines) for why you
should install exactly one at a time.

## Usage

```bash
python3 playwright_scraper.py \
  --url "https://www.zimmo.be/nl/gent-9000/te-koop/" \
  --pages 3 \
  --category gent-koop \
  --format both \
  --out gent_listings
```

Against zimmo.be's own Cloudflare protection, with the 2Captcha Scraping
Browser API:

```bash
python3 playwright_scraper.py \
  --url "https://www.zimmo.be/nl/gent-9000/te-koop/" \
  --cdp-endpoint "ws://login-zone-scraping_browser-country-be-pid-1:PASSWORD@cb.2captcha.com:9222" \
  --pages 3
```

Reuse the same `pid` across runs rather than minting a new one each time
— the Scraping Browser API caps live connections per profile, and a
persistent `pid` keeps its cookies (and therefore its trust with
Cloudflare) between runs.

Without a browser at all, via the Scraper API (see the honesty section
above for why this has not yet worked against zimmo.be specifically):

```bash
python3 scraper_api_client.py --url "https://www.zimmo.be/nl/gent-9000/te-koop/"
```

### All flags

| Flag | Default | Notes |
|---|---|---|
| `--url` | — (or `ZIMMO_URL`) | A zimmo.be search-results URL |
| `--category` | none | Label written into every row |
| `--pages` | 1 | Number of result pages |
| `--delay` | 0.0s | Sleep this long after each successfully-fetched page (rate limiting) |
| `--retries` / `--retry-delay` | 2 / 3.0s | Per-page retry budget |
| `--concurrency` | 1 | Parallel workers for pages 2+ (page 1 always fetched alone; refused together with `--cdp-endpoint`) |
| `--format` | both | `json`, `csv`, or `both` |
| `--out` | `zimmo_listings` | Output file prefix |
| `--proxy` / `--proxy-file` | none (or `ZIMMO_PROXY`) | A single proxy, or one-per-line file |
| `--proxy-rotate` / `--no-proxy-rotate` | rotate ON | Rotate to a different exit immediately on any proxy failure, or retry the same one |
| `--proxy-shuffle` / `--proxy-block-retries` | off / 3 | Pool behaviour |
| `--twocaptcha-key` | none (or `TWOCAPTCHA_KEY`) | Needed only if Cloudflare/reCAPTCHA actually appears |
| `--solve-captcha` | `when-blocked` | `always` solves on every detection; `when-blocked` only when the page has no usable content yet; `never` detects but never solves |
| `--min-score` | solver default (0.7) | Minimum reCAPTCHA v3 score to request |
| `--cdp-endpoint` | none (or `ZIMMO_CDP_ENDPOINT`) | Connect to an existing browser instead of launching one |
| `--allow-empty` | off | Write output even if zero products were found |
| `--dump-html` | off | Always save a per-page debug `.html`/`.png`, including on success |
| `--headless` / `--headful` | headless | Toggle a visible browser window |

The full set above is identical across all three engines — asserted
automatically by `smoke_test.py`, not just by having copied one file
into the other two.

## Configuration

Secrets belong in `.env`, never on a shared command line or in shell
history — copy `.env.example` to `.env` and fill in real values. An
explicit CLI flag always overrides `.env`; an already-exported shell
variable always overrides `.env` too. A value still containing `{...}`
(a vendor's own placeholder braces, copied verbatim from documentation)
is treated as unset, the same as an empty value. Run `python3
env_config.py` to see which source each variable actually came from,
without ever printing a secret's real value.

## Engines

**Playwright is primary.** Selenium and Puppeteer (via `pyppeteer`) exist
for parity, not as equally-recommended defaults — `pyppeteer` itself is
effectively unmaintained.

Install exactly **one** engine at a time (or use a separate virtualenv
per engine): Playwright and Puppeteer declare mutually unsatisfiable
`pyee` pins, and Puppeteer/Selenium collide on `urllib3`. `pip check`
will tell you if your environment has both — confirmed in this repo's
own development sandbox, where installing all three at once reproduced
both conflicts exactly as described.

Two confirmed engine-specific limits:

- **Selenium cannot use an authenticated remote CDP endpoint.**
  `debuggerAddress` was designed for a local, unauthenticated debug
  port and does not forward a `user:pass` embedded in a remote CDP URL.
  Use `playwright_scraper.py` or `puppeteer_scraper.py` instead if you
  need `--cdp-endpoint` against an authenticated endpoint.
- **Selenium's `--proxy-server` cannot authenticate at all.** A
  credential embedded in `--proxy` is stripped, with a warning, rather
  than silently doing nothing useful.

## Output

Each run writes `<out>.json` / `<out>.csv` (per `--format`) plus
`<out>.meta.json`, a sidecar recording `status` (`complete` / `partial`
/ `empty`), `stop_reason`, which pages failed by number, and the product
count. **A run that finds zero products writes nothing at all by
default** — pass `--allow-empty` to force writing (an empty CSV still
carries its header row either way).

Exit codes, identical across all engines: `0` ok · `1` crash ·
`2` bad usage · `3` blocked · `4` zero products (no `--allow-empty`) ·
`5` remote API error · `6` partial (some pages failed).

Compare two runs: `python3 diff_runs.py --old run1.json --new run2.json`
— refuses to compare a run whose sidecar isn't `status: complete`.

## Testing

```bash
python3 smoke_test.py     # or: pytest tests/
```

Zero-network, zero-browser. Validates the parser against real captured
markup (see caveats above), the output contract, banned wording,
`.env.example` correctness (including a round-trip through the real
loader), that every engine's driver import is at module level, a
coarse AST name-resolution walk, that all three engines' flag sets
match the contract and each other, and that every shared-module
function call across every engine binds against that function's real
signature.

CI runs this offline suite on Python 3.9 and 3.12, an `engine-smoke`
job that installs each engine into its own clean venv, and a
`docker-build` job that actually builds the image, runs its entrypoint,
confirms Chromium launches inside it, and confirms no `.env`/tests/
fixtures ended up in it. A separate `canary` workflow runs live against
a real filtered category once a day, and skips (rather than failing)
when no `TWOCAPTCHA_KEY` secret is configured.

## Repo maintenance notes (not shipped behaviour)

- A per-clone local `CLAUDE.md`, if you keep one, belongs in
  `.git/info/exclude`, not `.gitignore`.
- Before making this repo public, scan every blob that ever existed
  (`git rev-list --objects --all`) for credentials, not just the
  current working tree.
- Add this repo as a row in `2scraper/.github`'s `profile/README.md`
  once it's public, and check that row with a logged-out request.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: see
[SECURITY.md](SECURITY.md), not a public issue.

## License

MIT
