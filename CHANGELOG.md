# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning
follows [SemVer](https://semver.org/) as closely as a CLI toolkit can
manage. A patch release means "fixes" — not that every flag is frozen,
so a behaviour-changing default landing in a patch is stated plainly
here rather than treated as a violation of the format.

## [1.1.0] — Measured against today's site (2026-09-23)

> **Output changes for existing users.** Listings whose price reads
> "Prijs op aanvraag" / "Prix sur demande" are now ROWS, with `price`
> and `currency` both null — before, they were silently dropped. And
> `bedrooms`, `epc_label` and `property_type`, which were null on every
> row of every run since zimmo.be's Angular redesign, are now filled.
> No flag and no column changed.

> **Correction to 1.0.0's "no account needed" promise.** It is still
> true that no 2Captcha key is required. It is not true that nothing is:
> measured 2026-09-23, zimmo.be serves listings to a **residential exit
> with a headful browser**, and refuses headless Chromium (even from a
> Belgian home address) and any datacentre address. The README now says
> so, with the measurements. The 2Captcha Scraping Browser API also
> works (Belgian exit, 3 of 3 pages).

> **The "daily canary" 1.0.0 cited never ran.** It was gated on a
> `TWOCAPTCHA_KEY` secret that was never set, so every run took the
> skip branch and the badge read green without touching the site.

### Fixed
- **Parser, on the markup the site serves today** (NL and FR, sale and
  rent; checked field by field against the tiles' own labels on 105
  tiles over 7 live pages, 0 mismatches):
  - each listing is scoped by its own `<zimmo-listing>` element instead
    of by walking up until a price appears, which is what dropped
    price-on-request listings;
  - `bedrooms`, living area and the energy grade are read from the
    tile's feature ICONS, which are the same in both languages — EPC
    (Flanders, Wallonia) and Brussels' EPB alike; `epc_x.svg` (no grade)
    stays null rather than becoming a letter;
  - areas are read the Belgian way: "2.578 vierkante meter" is 2,578 m²,
    not 2.578;
  - `surface_m2` is the LIVING area only — a shop's commercial surface
    or a plot area is no longer written into it;
  - `property_type` and `listing_type` come from the URL segment
    (`/te-koop/huis/…`, `/a-louer/…`), so a new-build project whose
    title carries no sale word is no longer `listing_type: null`.
- **Selenium never started on a machine whose Chromium was not the newest
  Chrome.** `webdriver-manager` downloaded the latest chromedriver for
  Google Chrome (154) against an installed Chromium 152, and every
  session died with "session not created" — reported as exit 5. Replaced
  by Selenium Manager, built into `selenium>=4.6`, which matches the
  installed browser; the `webdriver-manager` dependency is gone.
- **The Scraper API path failed on every call, and was billed for it.**
  The live API now requires `waitFor` as an object and answered the
  JSON-encoded string this client sent with HTTP 422 "params.waitFor must
  be an object" ($0.0005 each). It also reports the target's status in
  `http_code`, while `status` is its own "success" — which the client
  compared with 400 and would have raised `TypeError` on. Both fixed;
  measured after the fix: 21 rows, exit 0.
- **Exit 5 for a run that never obtained a page.** Zero rows with every
  attempted page failed (a dead proxy, a load timeout) reported exit 4,
  "the catalogue is empty". It is now 5, "the content was never
  obtained", as across the family since 2026-09-21.
- **The banned-wording check read only `.py` files**, so the README
  tagline and the package description both used a phrase it bans. It
  now scans the published text files too (README, pyproject, workflows,
  `.env.example`, Dockerfile, CONTRIBUTING, SECURITY).

### Changed
- `canary.yml` runs headful under xvfb, takes its proxy from a
  `ZIMMO_PROXY` secret when one is set, and asserts column shares
  (living area, bedrooms, EPC, property type), not only a row count.
  Dispatched once on this branch with no secret: GitHub's runner was
  refused (exit 3) and the job ended in its `::notice::`, as designed —
  so until a `ZIMMO_PROXY` secret exists, green means "refused as
  documented", not "tested".
- The README's "What you need" section replaces the old "Honesty"
  section, which contradicted itself on the Scraper API and still
  described an embedded-JSON path the site removed on 2026-09-15.
- The Scraping Browser example keeps its endpoint in `.env`; it used to
  put a password on the command line.

## [1.0.0] — Stable public interface (2026-09-18)

No change to what the scraper does. This release is a commitment about
what will NOT change: from here on the CLI flags and the output schema
are a public interface, and anything that breaks them is a 2.0.0, not
a patch. 0.4.0 earned that -- it was the release where a series of
real runs against the live site replaced guesses with confirmed
behaviour, and nothing has had to move since.

What is now covered by that promise:

- **The CLI flags of all three engines.** Including the 0.4.0 fix that
  made `--no-proxy-rotate` a real, working opt-out. Renaming or
  removing a flag now requires a major bump.
- **The output schema.** The `Product` dataclass field order, which is
  also the JSON key order and the CSV header, plus the `.meta.json`
  run-metadata keys. CI already asserts `sample_output.{json,csv}`
  match the dataclass exactly, so drift fails the build rather than
  reaching a consumer.
- **The "no account needed" promise.** The scraper keeps running with
  no proxy and no captcha key; those stay opt-in.

What is explicitly NOT covered, because it is not ours to promise:
zimmo.be's own markup. The parsing paths behind these flags will keep
moving as the site does -- that is the whole point of the daily canary
-- and such a change is a patch, not a break, as long as the flags and
the columns above stay put.

### Added
- `release.yml`: pushing a `v*` tag cuts a GitHub Release with notes
  taken from this file's matching section, and refuses the tag unless
  the tag, `pyproject.toml` and this CHANGELOG agree on the version.
  A release tagged v1.0.0 against a pyproject still saying 0.4.0 is
  the kind of mismatch nobody notices until a bug is filed against a
  version that never existed. It runs on the workflow's own
  `GITHUB_TOKEN`, so publishing needs no personal token anywhere.
- The canary now also runs when `canary.yml` itself is edited. It is
  the one workflow that can sit broken for a full day unnoticed --
  a typo in it would otherwise surface at 06:17 UTC the next day, to
  whoever happened to read the log.

### Fixed
- **The offline CI job could never have been green.** It installed
  `requirements.txt` alone and then ran `--help` for all three engine
  scrapers, each of which imports its driver at module level, so every
  one died with `ModuleNotFoundError`. The module-level import is
  deliberate -- `engine-smoke` asserts it by `ast` walk precisely so a
  real `ImportError` cannot decay into a silent skip -- and the three
  engine requirement files are mutually unsatisfiable in one
  environment, so the engine CLIs simply do not belong in that job.
  Their `--help` is covered per engine in `engine-smoke`'s own venv.
- **`playwright install --with-deps` failed on `ubuntu-latest`.** The
  pinned `playwright==1.44.0` predates Ubuntu 24.04 and cannot resolve
  its system packages; support for it landed in playwright 1.45. The
  Playwright jobs now pin `ubuntu-22.04`, matching the Dockerfile's
  own `v1.44.0-jammy` base image, so the two pins stay in sync.
- **A red engine hid the others.** `fail-fast: false` on the engine
  matrix -- selenium was once cancelled mid-flight by playwright's
  failure, so its real status was never reported at all.

## [0.4.0] — Live-run fixes against the real site (2026-09-15/16)

The 0.3.0 audit (below) was static/external and found real bugs; this
release is what a series of ACTUAL runs against the live site, the
same week, found on top of it -- six further confirmed defects, none
of them visible from code review or the offline test suite alone.
Every item was independently reproduced against zimmo.be before being
fixed, and each has its own regression test in `smoke_test.py`.

### Fixed
- **Pagination silently did nothing.** `page_url()` built `?p=N`,
  confirmed live in an earlier, unrelated MCP-tooling session against
  the site at an earlier point in time. A live run against the CURRENT
  site found `?p=N` is ignored outright -- page 2 returned page 1's
  own listings verbatim, which this project's own data-based
  termination correctly (but wrongly, given the stale parameter) read
  as "the listing is exhausted" after just one page, on every
  multi-page run. The real parameter, found by inspecting the site's
  own pagination link directly, is `page` (`?page=2`). Every multi-page
  run before this fix silently returned only page 1's own listings,
  repeated, regardless of `--pages`.
- **The CSS fallback returned 0 products on the live site.** zimmo.be
  has moved to an Angular-rendered listing grid since the properties-
  JSON path was confirmed (2026-08-22) -- that path and a real-estate
  JSON-LD path are both now absent entirely (a live capture's only
  JSON-LD block is a `BreadcrumbList`, navigation only). The CSS
  fallback is therefore the path actually in use today, and it had its
  own bug: a widening search correctly found the tile's price-
  containing ancestor (the price lives in a sibling `<div class="price">`,
  not inside the `<a>` around the title), but that widened scope was
  discarded and silently recomputed narrower in a second pass, missing
  the price on every real listing.
- **Prices came back as billions.** The price parser (already
  rewritten once for the 0.3.0 audit's own thousands-separator bug)
  allowed a bare SPACE as an additional grouping character inside the
  price digits. Real tile text is flattened to one string with single
  spaces between every element (`get_text(" ", strip=True)`), so a
  real price such as "€ 385.000" followed anywhere later in the same
  text by an unrelated number -- a bedroom count, a surface area --
  was greedily swallowed as a continuation of the price's own
  thousands grouping: "€ 385.000 2 slaapkamers 120 m²" parsed as
  3,850,002 instead of 385,000, live, on a real page. A bare space is
  no longer treated as a grouping character; `.` (the site's own
  confirmed separator) still is.
- **`--solve-captcha=when-blocked` (the default) skipped solving on a
  genuinely blocked page.** `SELECTORS["item_link"]` was a loose
  pre-confirmation guess (`a[href*="/"]`) matching any relative link at
  all -- including a blocked/shell page's own 91 navigation, cookie-
  consent, language-switcher and footer-legal links, none of them a
  real listing. `when-blocked`'s own "does this page already have
  real content" check counted those 91 links as "already rendered" and
  skipped solving on a page that was still genuinely gated. Replaced
  with `.zimmo-code`, a marker confirmed present only on a real listing
  tile, never on nav/cookie/footer chrome -- this selector feeds both
  this check and the pre-existing render-detection wait, across all
  three engines.
- **An EPC label followed by a word failed to parse at all.**
  `"EPC label C"`, `"EPC label: A+"` and similar natural-language
  phrasing did not match the EPC regex, which required ONLY non-letter
  characters between "EPC" and the grade letter -- leaking the raw EPC
  text into the CSS fallback's own title field instead of being
  recognised and stripped. Widened to allow up to 20 characters
  (non-greedy) before the grade letter; the grade letter itself is
  matched case-SENSITIVE (uppercase only) specifically to avoid a
  case-insensitive match snagging a random lowercase a/b/c/d/e/f/g out
  of an ordinary nearby English word -- confirmed as a real risk when
  an earlier, all-case-insensitive version of this same fix matched
  "a" out of "available" in unrelated text near the word "EPC".
- **`scraper_api_client.py`'s wait condition could never succeed.**
  `DEFAULT_WAIT_FOR_TEXT` was `"properties: ["`, confirmed live
  2026-08-22 but removed from the site entirely by the same Angular
  rewrite noted above -- every call using the default timed out
  unconditionally, regardless of whether Cloudflare would have let the
  request through. Changed to `"€"`, confirmed live to appear as
  genuine rendered price text on every real listing tile.

### Repo-readiness fixes (found during final pre-push review, not live testing)
- **`canary.yml` required every page to complete and `status ==
  "complete"` on a single attempt** -- given the run-to-run
  inconsistency documented above (found the SAME day this canary was
  written), this made the canary likely to fail on the site's own
  normal instability rather than a real scraper regression, exactly
  the "always red, so everyone ignores it" failure mode this repo's
  own CI philosophy warns against. Now retries the whole run up to 3
  times and accepts `partial` (not just `complete`) as long as a
  reasonable number of priced listings came through.
- **`.gitignore` did not cover ad-hoc, one-off diagnostic scripts**
  written during a live-testing session -- confirmed as a real gap:
  none of `reliability_test.sh`, `check_quality.py`, `check_context.py`
  or `analyze_reliability.py` were excluded automatically, and all had
  to be removed by hand before this repo's own first commit. Added a
  `scratch_*` naming convention (and the specific names above) so a
  future one-off script is excluded automatically.
- The default branch was `master` (this environment's `git init`
  default); renamed to `main` to match the push instructions actually
  given alongside this repo.

### Also documented (not code changes, but confirmed live findings)
- Whether a plain Scraper API call, a `--cdp-url`-routed call, or the
  Playwright engine's own `Captcha.setAutoSolve` gets through zimmo.be's
  Managed Challenge is **inconsistent run to run** -- the identical
  configuration succeeded and failed on different attempts across
  every one of these three paths during live testing. See
  `scraper_api_client.py`'s own module docstring and
  `playwright_scraper.py`'s reliability-testing notes for the full,
  dated history; budget for retries in production regardless of which
  path is used.
- The 2Captcha proxy field on `scraper_api_client.py`'s `--proxy` flag
  was added this release (previously unsupported) and independently
  confirmed live via the proxy provider's own usage dashboard, not
  just this project's own output.

## [0.3.0] — External audit fixes (codex.md, 2026-09-14)

An independent audit ran this project against the live site and its own
test suite and found ten confirmed defects, several severe. Every
finding below was independently REPRODUCED before being fixed, not
patched on the audit's word alone — see each item for how.

> **Behaviour change:** `--delay` now actually sleeps between
> successful page fetches (previously "reserved" but silently unused).
> Its default changed from 2.0 to **0.0** specifically because it now
> does something — keeping the old default would have added a real,
> new 2-second-per-page slowdown nobody asked for. Set `--delay 2` back
> explicitly if you want the old de facto pacing.

> **Behaviour change:** `--proxy-rotate` alone could never actually be
> turned off (`action="store_true", default=True` has no CLI-reachable
> False state) — this was a decorative flag. `--no-proxy-rotate` is the
> new, real opt-out.

### Fixed — critical
- **Playwright broke on every `--pages > 1` run, including
  `--concurrency 1`.** A single `sync_playwright()` context created in
  the main thread was shared into worker threads via
  `ThreadPoolExecutor`; Playwright's sync API is explicitly documented
  as unsafe across threads this way. Reproduced directly:
  `greenlet.error: Cannot switch to a different thread` from a
  two-line repro, at any concurrency. Fixed by having `_fetch_page`
  open and tear down its OWN `sync_playwright()` context every call,
  in the main thread and in every worker alike. Re-verified end-to-end
  with the real dispatcher (`_fetch_pages_concurrently`) and 3
  concurrent real browser launches — no failures.

### Fixed — data correctness
- **CSS-fallback price parsing was wrong for any price without a
  thousands separator.** `"€ 1250"` parsed as 125.0, `"1250 €"` as
  250.0, `"€10000"` as 100.0; Belgian-grouped `"325.000"` (meaning
  325,000) parsed as 325. Root cause: the old regex bounded its
  leading digit group to a fixed 1–3 characters, silently truncating
  anything longer with no separator present. Replaced with a parser
  that captures the full digit run and classifies `,` as the Belgian
  decimal separator (not `.` or a bare space, which are always
  thousands grouping) — locked in place by a regression test covering
  all of the audit's own failing inputs plus the previously-passing
  ones. The primary embedded-JSON path was independently confirmed
  unaffected (already-numeric JSON fields, never regex-parsed as
  display text).
- **A run's own `.meta.json` could report `status=complete` while
  having deliberately never attempted several requested pages** (e.g.
  after page 1 came back blocked). `unattempted_pages` was computed by
  every engine but never actually passed into `finish_run()`.
  `finish_run()` now accepts and acts on it; all three engines now
  pass it through.
- **A `Product` with no `sku`** (common from the JSON-LD/CSS fallback
  paths) previously bypassed deduplication and diff entirely — two
  identical rows for the same URL both survived. Both `dedupe_by_sku()`
  and `diff_runs.py`'s own indexing now fall back to the listing's URL
  when `sku` is absent.
- **`diff_runs --fail-on-change`** only failed on changed EXISTING
  rows, contradicting its own help text ("if anything actually
  changed"); a run where every listing was replaced by different ones
  passed silently. Now fails on additions and removals too.

### Fixed — reliability
- **A dead/rejected proxy exit stayed a retry candidate until its
  failure count crossed `--proxy-block-retries`** (default 3). With
  the default 2 retries, a good next exit could only be selected on
  the LAST attempt of a page that had already burned its other
  retries on the bad one — with no attempts left to use it.
  `rotate_away_from()` is now called on every single failure;
  `--proxy-block-retries` now only controls permanent retirement from
  the pool, a separate, slower-moving concern.
- **`--solve-captcha=never` did not prevent the solver from being
  called.** Detection and solving were unconditional regardless of
  this flag. Now: `never` detects but never solves; `always` solves on
  every detection; `when-blocked` (default) solves only when the page
  doesn't already show usable listing content.
- **The live Cloudflare challenge on zimmo.be is a Managed Challenge**
  (a hidden `cf-turnstile-response` field, no `.cf-turnstile[data-
  sitekey]` widget element) — the previous detector required that
  specific widget markup and missed it. Detection is broadened to any
  `data-sitekey` attribute on the page; when Cloudflare markers are
  present but genuinely no sitekey is extractable, the code now
  returns an explicit "blocked, unsolvable via this path" signal
  (rather than either a false negative or a fabricated sitekey) and
  points at `--cdp-endpoint` with a real Scraping Browser API session
  as the correct path for this specific challenge type.

### Fixed — security
- `parse_proxy_url()`'s own `ValueError` on a malformed proxy URL
  previously embedded the raw URL VERBATIM, including any real
  `username:password` — an exception message is a log, and this one
  printed a credential the first time a proxy URL was malformed.
  Redacted before raising, like every other exception path in this
  family.
- `check_no_credentials.py` had two confirmed blind spots: `.env`
  itself was never scanned (only the literal `.env.example` filename
  was special-cased), and ANY path containing `test_` was blanket-
  exempted — meaning a real secret committed as e.g. `test_secret.py`
  was invisible to the tool whose entire job is to catch exactly that.
  Both fixed; re-verified against the audit's own two reproduction
  cases, and against this repo's own real fixtures to confirm no new
  false positives.

### Fixed — delivery / packaging
- `requirements-playwright.txt` allowed `playwright>=1.44` while the
  Dockerfile pins a `v1.44.0`-tagged base image with a matching
  browser revision baked in — an unpinned range could install a newer
  client library on top of an older browser. Both now pin `1.44.0`
  exactly, with an automated check (`smoke_test.py`) asserting the two
  never drift apart silently again.
- `pyproject.toml` said version `0.1.0` while this CHANGELOG already
  claimed `0.2.0` — one had been bumped without the other. Now `0.3.0`
  in both, with an automated check preventing the same drift again.
- Added the previously-missing `LICENSE` file (README already claimed
  MIT).

## [0.2.0] — Rewrite to the 2scraper family template

> **Behaviour change:** secrets now belong in `.env` (see `.env.example`
> and `env_config.py`), not on the command line. An explicit flag
> always wins over `.env`.

> **Behaviour change:** a run that finds zero products now writes
> **nothing** by default, instead of overwriting previous output with
> an empty file. Pass `--allow-empty` to restore the old behaviour.

### Added
- `env_config.py`, `proxy_pool.py`, `fingerprint_client.py`,
  `diff_runs.py`, `scraper_api_client.py`, `check_no_credentials.py` —
  new shared, site-knowledge-free modules per the family template.
- Full output contract in `output_writer.py`: exit codes 0–6,
  `finish_run()`, a `.meta.json` sidecar, an empty CSV that still
  carries its header, `dedupe_by_sku()` preserving page order.
- Real pagination: `page_url()` reconstructs zimmo.be's own confirmed
  `?p=N` parameter, plus data-based termination — a page that adds no
  new `sku` stops further dispatch — implemented identically across
  all three engines and tested with a stubbed browser
  (`tests/test_concurrency.py`, `test_concurrency_async.py`).
- Concurrency (`--concurrency`), with page 1 always fetched alone and
  each worker owning one proxy exit for its lifetime.
- Full flag set per the family standard, verified identical across all
  three engines by an automated test rather than by inspection.
- `.github/workflows/tests.yml` (offline matrix, per-engine venvs, and
  a `docker-build` job that actually builds and runs the image — not
  just a manual review of its COPY list) and `canary.yml` (daily live
  run, with a skip guard so a missing secret goes green with a notice
  instead of red).
- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue templates.
- `smoke_test.py` gained: a banned-wording scan, an `.env.example`
  round-trip through the real loader, a check that every engine's
  driver import is at module level, a coarse AST name-resolution walk,
  an engine flag-set consistency check (against the contract and
  against each other), and a shared-function call-signature binding
  check across all engines (`inspect.signature(fn).bind()` over an AST
  walk of every call site) — this last one caught nothing wrong on this
  repo, which is itself the point of having it before it does.

### Changed
- `product_parser.py`: tile-widening cap raised from 3 to 8 levels to
  match the family default. JSON-LD parsing now also handles `@graph`
  and an `image` field shaped as an `ImageObject` dict. `SELECTORS`'
  key renamed from an ad-hoc `item_link_css_approx` to the family's own
  `item_link` convention (CLAUDE.md §5).
- `env_config.py`: a value containing `{...}` (a vendor's own braced
  placeholder, e.g. `ws://{login}-zone-...`) is now treated as unset,
  not just a fixed literal list — a copied `.env.example` following the
  vendor's own documented format previously read as configured.
- `fingerprint_client.py`: hardened against six specific, previously-
  documented bugs found repeatedly across sibling repos (an API key
  leaking via an unredacted exception, a single guessed field name for
  the user agent, a malformed `en-{country}` locale, an unapplied
  timezone) — see the module's own docstring for which of the six
  apply here and how each is addressed. Honestly notes it is not yet
  wired into any engine's browser-launch path.
- `captcha_solver.py`: `min_score` is now reachable from the public
  `solve_recaptcha_v3()` entry point. Both 2captcha polling calls now
  redact the API key from any exception they raise — `requests` puts
  the full URL, including the `key=` query parameter, into that text
  otherwise.
- Puppeteer and Selenium: a proxy's credential no longer reaches the
  browser's own launch argv (visible to `ps`) — Puppeteer authenticates
  via `page.authenticate()`; Selenium (which cannot authenticate a
  proxy at all) strips the credential and warns instead of silently
  connecting unauthenticated.
- The credential-detection check that used to live only as an inline
  `git grep` in `tests.yml` is now `check_no_credentials.py`, one
  implementation invoked from both CI and the offline suite.

### Removed
- The local "auto-solve" placeholder integration (`--antidetect` flag
  and its local endpoint) — never backed by a real product. The
  engines' own `Captcha.setAutoSolve` (over `--cdp-endpoint`, on a
  genuine Scraping Browser API session) is the real, confirmed path.
- Every reference to `gate.2prx.com` as if it were a distinct proxy
  host — 2prx.com is 2captcha.com's own proxy product.

## [0.1.0] — Initial release

Three engines (Playwright primary, Selenium and Puppeteer for parity),
single-page scraping via zimmo.be's embedded `properties` JSON array,
JSON-LD and price-anchored CSS fallbacks, Cloudflare Turnstile and
reCAPTCHA v3 detection/solving via 2captcha.com.
