# zimmo-scraper

![release](https://img.shields.io/github/v/release/2scraper/zimmo-scraper?include_prereleases)
![tests](https://img.shields.io/github/actions/workflow/status/2scraper/zimmo-scraper/tests.yml?branch=main&label=tests)
![canary](https://img.shields.io/github/actions/workflow/status/2scraper/zimmo-scraper/canary.yml?branch=main&label=canary)
![python](https://img.shields.io/badge/python-3.9%E2%80%933.12-blue)
![license](https://img.shields.io/github/license/2scraper/zimmo-scraper?cacheSeconds=3600)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![what you need](https://img.shields.io/badge/what%20you%20need-a%20residential%20exit%20%2B%20headful%20browser-orange)

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
| 2Captcha Scraper API | its own | served — 21 rows, exit 0 |

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
pip install -r requirements.txt -r requirements-playwright.txt --break-system-packages
playwright install chromium

cp .env.example .env   # on a server: set ZIMMO_PROXY to a residential proxy

python3 playwright_scraper.py --url "https://www.zimmo.be/nl/gent-9000/te-koop/" --headful
# on a server with no display:
xvfb-run -a python3 playwright_scraper.py --url "https://www.zimmo.be/nl/gent-9000/te-koop/" --headful
```

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

A profile's credentials expire (about a day in this family's
experience): an HTTP 401 `deny_no_user` on connect means a stale profile,
not a broken scraper, and the run reports it as exit 5.

Reuse the same `pid` across runs rather than minting a new one each time
— the Scraping Browser API caps live connections per profile, and a
persistent `pid` keeps its cookies (and therefore its trust with
Cloudflare) between runs.

Without a browser at all, via the Scraper API (one page per call, key
from `TWOCAPTCHA_KEY` in `.env`):

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
`5` the content was never obtained (a remote API error, a dead proxy, a
load timeout — no page could be fetched) · `6` partial (some pages failed).

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
a real filtered category once a day: 3 pages, headful under xvfb, with
assertions on the share of rows carrying a price, living area, bedrooms,
energy grade and property type. It needs a residential exit to pass —
measured 2026-09-23, GitHub's runner is refused like any datacentre
address — so it takes one from a `ZIMMO_PROXY` repository secret, and
without that secret a refusal ends in a `::notice::` saying so rather
than a red badge. Read the badge with that in mind: green without the
secret means "tried, refused as documented", not "tested".

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
