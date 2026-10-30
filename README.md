# Zimmo Scraper — Belgian Property Listings in Python

![release](https://img.shields.io/github/v/release/2scraper/zimmo-scraper?include_prereleases)
![tests](https://img.shields.io/github/actions/workflow/status/2scraper/zimmo-scraper/tests.yml?branch=main&label=tests)
![canary](https://img.shields.io/github/actions/workflow/status/2scraper/zimmo-scraper/canary.yml?branch=main&label=canary)
![python](https://img.shields.io/badge/python-3.9%E2%80%933.12-blue)
![license](https://img.shields.io/github/license/2scraper/zimmo-scraper?cacheSeconds=3600)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![what you need](https://img.shields.io/badge/what%20you%20need-a%20residential%20exit%20%2B%20headful%20browser-orange)

**Zimmo scraper** — an open-source (MIT) Python tool that reads zimmo.be search results, for sale and for rent, on the Dutch and French sites, and writes one row per listing — price, address, property type, living area, bedrooms and energy grade (EPC) — to JSON or CSV. zimmo.be sits behind Cloudflare: when measured on 2026-09-23, a headful browser on a residential exit (or the 2Captcha Scraping Browser API) was served, while datacentre addresses and headless Chromium were refused. Use it to build a dataset of Belgian property listings, or to compare two runs of the same search with the bundled `diff_runs.py`.

Zimmo.be real-estate listing scraper (Playwright, Selenium, Puppeteer, or
the 2Captcha Scraping Browser API via CDP) — sale and rent listings, prices,
living area, bedrooms, EPC labels, proxies, captcha solving.

---

## What this actually does

It reads zimmo.be search-results pages — sale and rent, the Dutch
(`/nl/…/te-koop/`) and French (`/fr/…/a-vendre/`) sites — and writes one
row per listing: price, address, property type, living area, bedrooms and
energy grade, plus the listing's own Zimmo code as `sku`.

The site serves an Angular grid with no listing data in any structured
block (its only JSON-LD is a `BreadcrumbList`, and its embedded Angular
state carries headings and counts, not rows). So the rows come from the
rendered tiles, anchored on markup the site uses as a contract rather than
on build-generated classes: each listing is its own `<zimmo-listing>`
element, the listing code sits in `.zimmo-code`, and bedrooms, living area
and energy grade are stated by **icon file** (`bedrooms.svg`,
`floorspace-surface.svg`, `energy-labels/epc_b.svg`), which is identical on
the Dutch and French sites while the labels beside it are translated.

Measured 2026-09-23 on 105 tiles over 7 live pages (gent-9000/te-koop
pages 1–3, antwerpen-2000/te-huur, bruxelles-1000/a-vendre), checked field
by field against the tiles' own accessibility labels: **0 mismatches**.
Coverage on those pages: price 99% (the rest are "Prijs op aanvraag",
written as a row with `price` and `currency` null), property and listing
type 100%, living area 90%, bedrooms 72%, energy grade 70% — the gaps are
listings that state no area, no bedrooms (garages, commercial property) or
no grade.

Things to know about the columns:

- **`brand`, `original_price`, `discount_pct` and `epc_value` are always
  null on today's site.** They are filled only from the embedded
  `properties` JSON the site served until 2026-09, which the parser still
  reads if it comes back. In a manual test on 2026-10-27/28 they were null
  in all 168 rows of the live Gent, Brussels and Antwerp runs.
- **A new-build project's price is its lowest unit price.** Project tiles
  read "Vanaf € 499.782" ("from"); `price` is that lower bound, with
  nothing else in the row marking it as one beyond `title: "Project"`, so
  filter those rows out before using prices as asking prices.
- Energy grades include Brussels' minus grades (`E-`, from
  `epb_e_minus.svg`) as well as `A+`/`A++`.

## What you need

**No 2Captcha key. But a residential exit and a visible browser.** Every
run below is one attempt, 2026-09-23, same URL
(`/nl/gent-9000/te-koop/`):

| From | Browser | Result |
|---|---|---|
| datacentre (Hetzner), no proxy | curl, any User-Agent | HTTP 403 "Just a moment…" |
| datacentre (Hetzner), no proxy | Chromium headless / headful | Cloudflare challenge, exit 3 |
| GitHub Actions runner, no proxy | Chromium headful under xvfb | Cloudflare challenge, exit 3 |
| Belgian residential exit | Chromium **headless** | Cloudflare challenge, exit 3 |
| Belgian residential exit | Chromium **headful** | served — 63 rows, 3 of 3 pages, exit 0 |
| German residential exit | Chromium headful | served — 21 rows, exit 0 |
| 2Captcha Scraping Browser API, `country-be` | its own | served — 63 rows, 3 of 3 pages, exit 0 |
| 2Captcha Scraper API | its own | served — 21 rows, exit 0 (2026-09-23); **not served** on 2026-10-28, see below |

On 2026-10-28 (manual test) the Scraper API returned the site's own page
reading "0 resultaten" for `/nl/gent-9000/te-koop/`, twice, while the
Scraping Browser API read 63 listings from the same URL; a Brussels request
timed out. A page with no listing from the Scraper API is therefore
reported as **content not obtained (exit 5)**, never as an empty search.

So on a home connection, `--headful` is the whole requirement — the exit
does not have to be Belgian. On a server, you need a residential proxy
(`ZIMMO_PROXY` in `.env`) **and** a display for the headful browser
(`xvfb-run`), or the Scraping Browser API, which brings both. Headless is
refused even from a residential address, which is why every example below
passes `--headful`; the flag's default stays headless for parity with the
rest of this family.

All three engines were run live through the Belgian exit, headful, 3 pages:
Playwright, Puppeteer and Selenium each returned the same 63 listings at
the same prices. (Selenium cannot authenticate a proxy, so it was given a
local forwarder; see [Engines](#engines).)

What the paid products buy here: the **Scraping Browser API** is the one
path that needs neither a residential address nor a display of your own,
which is what a server-side pipeline lacks; **residential proxies** give a
local browser the address it needs; the **Scraper API** returns a page
with no browser at all, one request per page ($0.0005 per request on
2026-09-23). The **captcha solver** was not exercised in these runs: no
challenge appeared on a residential exit with a headful browser, and a
solve against the Cloudflare interstitial the other paths meet was not
tested here.

`fingerprint_client.py` is shipped but not wired into any engine yet.

## Quick start

```bash
git clone https://github.com/2scraper/zimmo-scraper.git
cd zimmo-scraper
python3 --version   # 3.9-3.12, see below
pip install -r requirements.txt -r requirements-playwright.txt --break-system-packages
playwright install chromium

cp .env.example .env   # on a server: set ZIMMO_PROXY to a residential proxy

python3 playwright_scraper.py --url "https://www.zimmo.be/nl/gent-9000/te-koop/" --headful
# on a server with no display:
xvfb-run -a python3 playwright_scraper.py --url "https://www.zimmo.be/nl/gent-9000/te-koop/" --headful
```

**Python 3.13 and newer:** the pinned `playwright==1.44.0` needs
`greenlet==3.0.3`, which has no wheel and does not build there ("Failed to
build installable wheels ... greenlet"). Use Python 3.12 or older (e.g.
`python3.12 -m venv .venv`), or install a current greenlet first and the
pin without its dependencies: `pip install greenlet pyee==11.1.0` then
`pip install --no-deps playwright==1.44.0`.

Selenium and Puppeteer (pyppeteer) work the same way, with their own
`requirements-{engine}.txt` — see [Engines](#engines) for why you
should install exactly one at a time.

## Usage

```bash
python3 playwright_scraper.py \
  --url "https://www.zimmo.be/nl/gent-9000/te-koop/" \
  --pages 3 \
  --headful \
  --category gent-koop \
  --format both \
  --out gent_listings
```

With the 2Captcha Scraping Browser API instead of a local browser — put
the endpoint in `.env`, never on the command line, since a password in
`argv` is readable by anything that can run `ps`:

```bash
# .env
ZIMMO_CDP_ENDPOINT=ws://{login}-zone-scraping_browser-country-be-pid-{profileId}:{password}@cb.2captcha.com:9222
```

```bash
python3 playwright_scraper.py --url "https://www.zimmo.be/nl/gent-9000/te-koop/" --pages 3
```

A profile's credentials can stop being valid (no fixed lifetime is
promised here): an HTTP 401 `deny_no_user` on connect usually means the
endpoint's credentials are no longer accepted, not a broken scraper, and
the run reports it as exit 5 after one attempt, with a message naming
`ZIMMO_CDP_ENDPOINT`. Check the endpoint before changing code.

Reuse the same `pid` across runs rather than minting a new one each time
— the Scraping Browser API caps live connections per profile, and a
persistent `pid` keeps its cookies (and therefore its trust with
Cloudflare) between runs.

A run holds ONE connection for all its pages, each page in a fresh tab
(Playwright and pyppeteer engines). Reconnecting per page, as versions
before 1.2.1 did, was refused with HTTP 500 roughly every other page —
measured 2026-10-07: five connections and two refusals for a 3-page run
before, one connection and none after. The log's last line says how many
connections the run opened; more than one means an attempt failed and
was retried on a fresh connection.

Without a browser at all, via the Scraper API (one page per call, key
from `TWOCAPTCHA_KEY` in `.env`; it uses a proxy only when you pass
`--proxy`, and does not read `ZIMMO_PROXY`):

```bash
python3 scraper_api_client.py --url "https://www.zimmo.be/nl/gent-9000/te-koop/"
```

### All flags

| Flag | Default | Notes |
|---|---|---|
| `--url` | — (or `ZIMMO_URL`) | A zimmo.be search-results URL, without a `page` parameter (use `--pages`); anything else is refused, exit 2 |
| `--category` | none | Label written into every row |
| `--pages` | 1 | Number of result pages (1 or more) |
| `--delay` | 0.0s | Sleep this long after each successfully-fetched page (rate limiting) |
| `--retries` / `--retry-delay` | 2 / 3.0s | Extra attempts per page (0 = one attempt; negative values are refused) and the wait between them |
| `--concurrency` | 1 | Parallel workers for pages 2+ (page 1 always fetched alone; refused together with `--cdp-endpoint`) |
| `--format` | both | `json`, `csv`, or `both` |
| `--out` | `zimmo_listings` | Output file prefix; its directory must exist and be writable (checked before the first request, exit 2) |
| `--proxy` / `--proxy-file` | none (or `ZIMMO_PROXY`) | A single proxy, or one-per-line file. `scheme://user:pass@host:port`, `scheme://host:port` or `host:port:user:pass` (the 2Captcha dashboard's form). A missing or empty file, an unparseable entry or a SOCKS proxy with a login is refused, exit 2 |
| `--proxy-rotate` / `--no-proxy-rotate` | rotate ON | Rotate to a different exit immediately on any proxy failure (a navigation that times out through the proxy counts as one), or retry the same one |
| `--proxy-shuffle` / `--proxy-block-retries` | off / 3 | Pool behaviour: an exit that failed this many times is never used again in the run, and a page starts on the exit with the fewest failures |
| `--twocaptcha-key` | none (or `TWOCAPTCHA_KEY`) | Needed only if Cloudflare/reCAPTCHA actually appears |
| `--solve-captcha` | `when-blocked` | `always` solves on every detection; `when-blocked` only when the page has no usable content yet; `never` detects but never solves |
| `--min-score` | solver default (0.7) | Minimum reCAPTCHA v3 score to request, 0.1–0.9 |
| `--cdp-endpoint` | none (or `ZIMMO_CDP_ENDPOINT`) | Connect to an existing browser instead of launching one; refused together with `--proxy`/`--proxy-file`, since that browser uses its own exit. Selenium refuses an endpoint with a login (exit 2) |
| `--allow-empty` | off | Write output even if zero products were found |
| `--dump-html` | off | Always save a per-page debug `.html`/`.png`, including on success |
| `--headless` / `--headful` | headless | Toggle a visible browser window |
| `--block-media` | off | Through a local proxy, skip images, video/audio and fonts to save proxy traffic (see [Proxy traffic savers](#proxy-traffic-savers)) |
| `--load-media` | on (or `ZIMMO_LOAD_MEDIA=1`) | Always load them, the default here |
| `--proxy-budget-mb` | 10 (or `ZIMMO_PROXY_BUDGET_MB`) | Stop the run once this many MB (10^6 bytes) went through a local proxy; `0` = no cap (see [Proxy traffic savers](#proxy-traffic-savers)) |

The full set above is identical across all three engines — asserted
automatically by `smoke_test.py`, not just by having copied one file
into the other two.

## Configuration

Secrets belong in `.env`, never on a shared command line or in shell
history — copy `.env.example` to `.env` and fill in real values. An
explicit CLI flag always overrides `.env`; an already-exported shell
variable always overrides `.env` too. An explicitly **empty** value turns
a setting off for one run without editing `.env`: `--cdp-endpoint ""` or
`ZIMMO_PROXY= python3 playwright_scraper.py ...`. A value still containing `{...}`
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
  port and does not forward a `user:pass` embedded in a remote CDP URL,
  so `selenium_scraper.py` refuses such an endpoint up front (exit 2).
  Use `playwright_scraper.py` or `puppeteer_scraper.py` for the
  Scraping Browser API.
- **Selenium's `--proxy-server` cannot authenticate at all.** A
  credential embedded in `--proxy` is stripped, with a warning, rather
  than silently doing nothing useful.
- **pyppeteer on Apple Silicon** downloads an x86_64 Chromium (117), which
  runs under Rosetta and, measured 2026-10-28 on macOS 26.6, crashes at
  once when headless. The run says the local Chromium did not start and
  does not retry; use `--headful` or `playwright_scraper.py` there.

### Proxy traffic savers

A proxy is billed by traffic. With `--proxy` / `--proxy-file` /
`ZIMMO_PROXY`, every engine first makes **one pre-check request** through
it, before any browser starts: a CONNECT to www.zimmo.be and a
`HEAD /robots.txt` inside the tunnel. Chromium reports a proxy's HTTP 407
as a navigation timeout, so a wrong or exhausted credential used to spend
every retry and rotation first. Now:

- **407** (the gateway refused the credential, or the traffic balance is
  exhausted) or **402** (proxy traffic/balance exhausted) ends the run in
  seconds with **exit 5**. The run writes `<out>.latest_attempt.meta.json`
  (`status: failed`, `stop_reason: proxy_auth_failed` or
  `proxy_balance_exhausted`) and leaves any previous output and its
  `.meta.json` alone. The message names `ZIMMO_PROXY` and never prints the
  credential.
- With `--proxy-file`, **every exit** is pre-checked; one that answers 407
  or 402 is dropped from the pool for the run, and the run stops only if
  every exit was refused.
- A proxy that does not answer at all keeps the old behaviour: a warning,
  then the browser's own attempt and the usual retries.
- Nothing is pre-checked without a proxy or with `--cdp-endpoint`.

**Media blocking is opt-in here** (`--block-media`). Through a local proxy,
it aborts image, video/audio and font requests: Playwright by resource
type, pyppeteer through CDP's Fetch domain, and Selenium by file extension
with CDP `Network.setBlockedURLs`. It is off by default on zimmo.be. This
site serves only a residential exit driving a headful browser, and a free
comparison from a datacentre address (2026-10-20, headful, no proxy) got
Cloudflare's HTTP 403. So a media-blocked run could not be checked
against a normal one, and a bot manager this strict may score a browser
that never fetches images or fonts. The parser reads the page's listing
JSON and text, not media. It is never applied over `--cdp-endpoint`.
**On zimmo.be it saves next to nothing:** measured 2026-10-28 through a
German residential exit, headful, 3 pages, Playwright: 8.1 MB with it and
7.8 MB without (another pair, 8.2 MB vs 8.7 MB). The weight of a page here
is its scripts, not its images.

**Every run through a local proxy is metered and capped.**
`--proxy-budget-mb` (default **10**, `0` = no cap, also
`ZIMMO_PROXY_BUDGET_MB`). Once more than that has gone through the proxy,
further requests are refused, no further page attempt starts a browser
(every `--concurrency` worker shares one meter and stops dispatching), the
rows already gathered are kept and the run exits **6** (or **5** with no
rows), `stop_reason: proxy_budget_exceeded`. No new request starts past
the cap, but requests already in flight finish and are counted, so the
total can end somewhat above it: in a manual test (2026-10-28, budget
0.1 MB) Playwright and Puppeteer stopped at 0.2 MB. Selenium used to read
its traffic only after a page had fully loaded and stopped at 4.6 MB; it
now reads it while the page loads (and returns from navigation at
DOMContentLoaded on a metered run), which has not been re-measured live.

**How far 10 MB goes:** one results page cost about **2.6 MB** through a
German residential exit, headful (manual test, 2026-10-28: 3 pages = 7.8 MB
Playwright, 7.6 MB Puppeteer, 9.1 MB Selenium). So the default covers about
three pages; for `--pages 4` or more raise `--proxy-budget-mb`
(roughly 3 MB per page) or the run ends partial, exit 6. `<out>.meta.json`, and `<out>.latest_attempt.meta.json`, which every
metered run writes, rows or not, carry `proxy_bytes`, `proxy_budget_bytes`
(`null` with no cap) and `proxy_budget_exceeded`, and the log ends with
`Proxy traffic: X.X MB of Y MB budget`.

How the bytes are counted, per engine: Playwright sums `request.sizes()`
(request headers + body, response headers + body) of every finished or
failed request; pyppeteer reads CDP `Network.loadingFinished.encodedDataLength`
(bytes received, headers included) plus each request's line, headers and
body from `Network.requestWillBeSent`; Selenium reads the same CDP events
from chromedriver's performance log after each page. Only `http(s)` requests and responses that came over the network are counted: Chrome's own `chrome://`, `data:` and `blob:` loads, cache hits and Chrome's error page count nothing. The figure is an
**estimate of the payload through the tunnel**: TLS, the CONNECT exchange
and connections the browser opens for itself are not visible to any of the
engines, and add a few percent. Measured 2026-10-21 against a local stub
proxy that counted every byte it carried (a 60 kB page, a 30 kB stylesheet,
a 90 kB script): Playwright 1.44 within 0.2%, pyppeteer 0.6%, Selenium
0.7%. Nothing is metered without a proxy or over `--cdp-endpoint`.

## Output

Each run writes `<out>.json` / `<out>.csv` (per `--format`) plus
`<out>.meta.json`, a sidecar recording `status` (`complete` / `partial`
/ `empty`), `stop_reason`, which pages failed by number, the product
count, the search it covered (`selection`: the URL without its `page`
parameter) and whether it read the listing to its end
(`listing_exhausted`, with the pages that made unnecessary in
`exhausted_pages`). **A run that finds zero products writes nothing at
all by default** — pass `--allow-empty` to force writing (an empty CSV
still carries its header row either way).

How each page is judged, measured on zimmo.be 2026-10-07: a served search
page is HTTP 200; Cloudflare's "Even geduld..." challenge is HTTP 403 and
counts as **blocked** unless listings appear after it; a route the site
does not serve is HTTP 404 ("Pagina niet gevonden"); 429 and 5xx are
retried up to `--retries` times and then count as failed.

A served page with no listing is judged by the search's own result count
the page states ("51 resultaten" / "51 résultats", the first one on the
page): a count of 0 is an **empty search**; a later page with a count
above 0 that also says "We vonden helaas geen resultaten" is **past the end
of the listing** (measured 2026-10-28: page 4 of a 51-result search, HTTP
200), and the run stops there, `complete` with `listing_exhausted: true`.
Anything else without a listing — no count, or a count over a grid that
never loaded, and always on page 1 — means the page did not finish
rendering (a load cut off by a dying proxy, say): it is retried and then
counts as failed, never as the end of the listing. The run also stops
without asking for the next page once it holds every result the site
counts and the last page was shorter than page 1, and when a page adds no
new listing (how the site answered past the end before October 2026). The
count and the Dutch "no result" line are read from the page text, quoted
from live pages; the markup around them has not been captured, and the
French "no result" wording is not known, so a French page past the end
ends the run partial (exit 6) rather than complete. If the site changes
that wording, the error is always toward "not obtained", never toward a
false "empty" or "complete".

A page whose listing grid rendered but parsed to zero rows is
a **parser failure** (`parser_failed_pages`), never an empty search. A
failed, blocked or parser-failed page lands in `failed_pages`; if it is
page 1, no later page is requested. A challenge is looked for after the
page had its chance to render (Cloudflare often clears itself), not right
after navigation, so a challenge that stays is named in the log, with what
the solver can and cannot do about it. One page buys at most one paid
captcha solve, across all its attempts and proxy rotations.

Exit codes, identical across all engines: `0` ok · `1` crash ·
`2` bad usage · `3` blocked · `4` zero products (no `--allow-empty`) ·
`5` the content was never obtained (a remote API error, a dead proxy, a
proxy that refused its credential or balance before the run began, a
load timeout, an HTTP error or 404 on page 1, or a rendered page the parser
read nothing from, or the proxy budget spent before any row) · `6` partial
(some pages failed, were blocked or parsed to nothing,
`--proxy-budget-mb` stopped the run: `stop_reason: proxy_budget_exceeded`,
or Ctrl-C stopped it: `stop_reason: interrupted`) · `130` Ctrl-C before
any row was gathered (nothing written except
`<out>.latest_attempt.meta.json`). An interrupted run with rows writes
them, partial, exit 6 (the Puppeteer engine does so on Python 3.11 and
newer). The Scraper API path uses the same page verdicts, except that a
page with no listing is exit 5 there (see [What you need](#what-you-need)).

In the CSV, a text cell starting with `=`, `+`, `-`, `@`, a tab, CR or LF
is prefixed with an apostrophe so a spreadsheet does not run it as a
formula (`csv_cells_escaped` in the sidecar counts them); the JSON keeps
every value as served. Files are replaced atomically, so a failed write
leaves the previous output intact.

Compare two runs: `python3 diff_runs.py --old run1.json --new run2.json`
(either may be the `.csv` output instead)
— refuses to compare a run whose sidecar isn't `status: complete`, and
two runs of different searches (`--allow-selection-mismatch` overrides
that). When the newer run read only a window of the listing (`--pages`
ended before the listing did), a listing missing from it is reported as
**left selection**, not removed: a new listing may simply have pushed it
to the next page. Only a run that read the listing to its end can report
a removal.

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
fixtures ended up in it. The image runs headless by default, and
zimmo.be refuses headless Chromium: use it with `--cdp-endpoint` (a
Scraping Browser API session), or supply a display yourself and pass
`--headful`.

A separate `canary` workflow runs live against one fixed search
(`/nl/gent-9000/te-koop/`, no filter) once a week (Mondays): 3 pages, headful under xvfb, with assertions on the share of
rows carrying a price, living area, bedrooms, energy grade and property
type. It needs a residential exit — measured 2026-09-23 and 2026-10-13,
GitHub's runner is refused like any datacentre address, and a US
residential exit was refused while Belgian and German ones were served —
so it takes one from the `ZIMMO_PROXY` repository secret. Without that
secret (or with an empty one) the canary **fails** with an error naming
it: a run that never reached the site is not reported as a pass, and
neither is a block (exit 3), zero rows, or a parser failure (exit 5).
Besides the share of populated columns it asserts the arithmetic of the
data: every row has a unique `sku`, prices fall in a plausible range, no
`original_price` sits at or below its `price` (vacuous today: that column
is always null, see above), and every URL is on zimmo.be.

## Output schema

`product.schema.json` is a JSON Schema (draft 2020-12) for one row,
generated from the `Product` dataclass by `python3 output_writer.py` and
checked for drift by `smoke_test.py`, so a consumer outside Python can
validate a run. `x-schema-version` goes up whenever a column is added,
renamed or removed, and the CHANGELOG says so.

## Guides

- [How to run the 2scraper scrapers: setup, proxies, exit codes and access modes](https://2captcha.com/h/how-to-run-2scraper-open-source-scrapers) — installation, `.env`, `.meta.json` for the whole family.

## Related scrapers

Same CLI conventions, same output contract (one row per item, JSON or CSV, a `.meta.json` sidecar and shared exit codes):

- [Autotrader scraper](https://github.com/2scraper/autotrader-scraper) — US car listings with VIN, fee-inclusive price, KBB fair…
- [Avito scraper](https://github.com/2scraper/avito-scraper) — Category and search listings, item pages and seller profiles…
- [Craigslist scraper](https://github.com/2scraper/craigslist-scraper) — Craigslist result lists and adverts from all 714 areas
- [dubizzle scraper](https://github.com/2scraper/dubizzle-scraper) — UAE classifieds — motors, property, electronics, jobs…
- [Facebook Marketplace scraper](https://github.com/2scraper/facebook-marketplace-scraper) — Marketplace listings by keyword, category, location…
- [MakeMyTrip scraper](https://github.com/2scraper/makemytrip-scraper) — Hotel prices with taxes and fees, ratings, stars…

All scrapers: [github.com/2scraper](https://github.com/2scraper) · Need another site? [Open a request](https://github.com/2scraper/.github/issues/new?title=%5BScraper%20request%5D%20)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: see
[SECURITY.md](SECURITY.md), not a public issue.

## License

MIT
