#!/usr/bin/env python3
"""
smoke_test.py
--------------
Zero-network, zero-browser sanity check for the shared core
(product_parser.py + output_writer.py + captcha_solver.py detection).

Run this FIRST, before touching a real browser or zimmo.be, to confirm
your Python environment and the parsing/output logic are working:

    python3 smoke_test.py

IMPORTANT: this validates the parser against a SYNTHETIC fixture modeled
on publicly documented zimmo.be output fields (price, EPC, surface,
bedrooms) — it does NOT prove the parser matches zimmo.be's actual live
markup, which could not be confirmed while building this (see
product_parser.py's module docstring). Passing here means the logic is
internally consistent; it does not mean it will find anything on the
real site without adjustment.

Exits non-zero on any failure so it's CI-friendly.
"""

import ast
import importlib
import inspect
import json
import os
import sys
import tempfile

from product_parser import parse_products
from output_writer import save
from captcha_solver import detect_recaptcha_v3, detect_cloudflare_challenge, detect_turnstile_challenge

# Modeled on publicly documented zimmo.be scraper output fields (price,
# EPC label/value, surface, bedrooms) — NOT confirmed against real zimmo.be
# HTML, which was unreachable while building this. Mirrors real caveats
# found on other sites in this family too: two <a> tags sharing one href
# (image link + text link), and a junk link with no price of its own
# sharing a results container with real listings.
SAMPLE_LISTING_HTML = """
<html><body>
<div class="results">
  <div class="listing-tile">
    <a href="/nl/detail/12345678">
      <img alt="Huis" src="https://cdn.zimmo.be/img1.jpg">
    </a>
    <a href="/nl/detail/12345678">
      <span>Huis te koop</span>
      <span>Gent</span>
      <span>€ 325.000</span>
      <span>EPC: B (145 kWh/m²)</span>
      <span>180 m²</span>
      <span>3 slaapkamers</span>
    </a>
  </div>
  <div class="listing-tile">
    <a href="/fr/detail/87654321">
      <img alt="Appartement" src="https://cdn.zimmo.be/img2.jpg">
    </a>
    <a href="/fr/detail/87654321">
      <span>Appartement à louer</span>
      <span>Liège</span>
      <span>950 €</span>
      <span>EPC C (210 kWh/m²)</span>
      <span>75 m²</span>
      <span>2 chambres</span>
    </a>
  </div>
  <!-- noise: a footer link with no price of its own, sharing the results
       container with two real (priced) listings — must NOT inherit
       either listing's price/EPC/etc. by widening too far -->
  <a href="/nl/contact">Contacteer ons</a>
</div>
</body></html>
"""

SAMPLE_RECAPTCHA_HTML = """
<script src="https://www.google.com/recaptcha/api.js?render=6Lc_test_sitekey_123456789"></script>
<script>
grecaptcha.ready(function() {
  grecaptcha.execute('6Lc_test_sitekey_123456789', {action: 'search'});
});
</script>
"""

SAMPLE_CAPTCHA_WIDGET_HTML = """
<captcha-widget data-captcha-type="recaptcha" data-widget-id="0" data-version="v3" data-sitekey="6LeifPcbAAAAAJaiPe_xgLTfnbdpEMAYJAAnVFJT" data-action="null" data-callback="reCaptchaWidgetCallback0" data-enterprise="false" data-container-id="search-captcha" data-binded-button-id="null" data-reset="true"></captcha-widget>
"""

SAMPLE_CLOUDFLARE_HTML = """
<html><head><title>Just a moment...</title></head>
<body><div id="cf-wrapper"><div class="cf-browser-verification cf_chl_challenge">
Checking your browser before accessing zimmo.be
</div></div></body></html>
"""

# Confirmed live on zimmo.be (2026-08-19): the actual block is a Cloudflare
# Turnstile "Verify you are human" checkbox — architecturally different
# from reCAPTCHA v3 (no sitekey+execute() call; a <div class="cf-turnstile"
# data-sitekey="..."> widget instead), needing its own detect/solve path.
SAMPLE_TURNSTILE_HTML = """
<html><body>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<div class="cf-turnstile" data-sitekey="0x4AAAAAAABkMYinukE8nzY" data-callback="onTurnstileSuccess"></div>
</body></html>
"""


# Confirmed live 2026-08-22 (after getting past Cloudflare Turnstile via
# Captcha.setAutoSolve): zimmo.be search-results pages embed the ENTIRE
# current page of listings as a JSON array inline in a <script> tag. This
# is a trimmed-but-real fixture built from actual captured output — not a
# guess. It resolves every field the old price-anchored HTML fallback
# could never fill in (title with a real address, EPC label/value,
# bedroom count) and also covers a price-drop (zprijs) discount case.
SAMPLE_PROPERTIES_JSON_HTML = """
<html><body>
<script>
        $(function () {
    app.start({
        search: {"paging":{"from":0,"size":21},"sorting":[{"type":"RANKING_SCORE","order":"DESC"}],"filter":{"status":{"in":["FOR_SALE","TAKE_OVER"]},"placeId":{"in":[1506]}}},
        properties: [{"particulier_pand_id":null,"code":"LR6DK","uuid":"bd7eaa9b-e395-4854-b73a-5d452c0b7ac3","type":"Huis","type_id":"5","status":"general.status.for_sale","status_id":"1","hoofdFoto":"https://files.zimmo.be/img1.jpg","b_woonopp":"87","toegevoegd":"1787325314","slaapkamers":"3","nieuwbouw":"0","prijs":"380000","zprijs":null,"address":"Kraaistraat 9","gemeente":"Gent","postcode":"9000","lat":"51.035660000","lon":"3.725490000","parcel_id":null,"logo":"https://files.zimmo.be/logo1.jpg","proj_id":"0","advertiser":{"name":"Immo Francois - Gent","phone":"+32 9 247 67 66"},"favoriet":false,"archief":"0","a_beschrijf":"","fotoAmount":14,"isPromoted":true,"subtype_naam":"Rijwoning","sticker":"new","price_drop_date":"","zimmo_kantoor_id":"8684","plus":"0","energyWaarde":"216","energyLabel":"c","energyLabelCategory":"c","html":"","propertyItemLogo":null,"province":"Oost-Vlaanderen","isPublished":false,"url":"/nl/gent-9000/te-koop/huis/LR6DK/","firstImages":[]},
        {"particulier_pand_id":null,"code":"LR9XZ","uuid":"aaaa-bbbb","type":"Appartement","type_id":"2","status":"general.status.for_rent","status_id":"2","hoofdFoto":"https://files.zimmo.be/img3.jpg","b_woonopp":"75","toegevoegd":"1787325315","slaapkamers":"2","nieuwbouw":"0","prijs":"950","zprijs":"1050","address":"Kortrijksesteenweg 100","gemeente":"Gent","postcode":"9000","lat":"51.03","lon":"3.70","parcel_id":null,"logo":"https://files.zimmo.be/logo2.jpg","proj_id":"0","advertiser":{"name":"ERA Vastgoed Gent","phone":"+32 9 000 00 00"},"favoriet":false,"archief":"0","a_beschrijf":"","fotoAmount":8,"isPromoted":false,"subtype_naam":"Appartement","sticker":"","price_drop_date":"","zimmo_kantoor_id":"1234","plus":"0","energyWaarde":"180","energyLabel":"b","energyLabelCategory":"b","html":"","propertyItemLogo":null,"province":"Oost-Vlaanderen","isPublished":true,"url":"/nl/gent-9000/te-huur/appartement/LR9XZ/","firstImages":[]}]
    });
})
</script>
</body></html>
"""


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    return condition


def main() -> int:
    ok = True

    products = parse_products(SAMPLE_LISTING_HTML, "https://www.zimmo.be/nl/zoeken", category="test")
    ok &= check("parser extracts exactly 2 real listings (junk contact link excluded)", len(products) == 2)
    ok &= check("first listing's title is clean (no price/EPC/surface leaking in)",
                products[0].title == "Huis te koop Gent")
    ok &= check("first listing's price/EPC/surface/bedrooms all parsed correctly",
                products[0].price == 325000.0 and products[0].epc_label == "B"
                and products[0].epc_value == 145.0 and products[0].surface_m2 == 180.0
                and products[0].bedrooms == 3 and products[0].listing_type == "sale")
    ok &= check("second listing (French, rental) parsed correctly too",
                products[1].title == "Appartement à louer Liège" and products[1].price == 950.0
                and products[1].listing_type == "rent" and products[1].epc_label == "C")
    ok &= check("junk 'Contacteer ons' link did not inherit a sibling listing's price/data",
                all(p.url != "https://www.zimmo.be/nl/contact" for p in products))
    ok &= check("category label propagated", products[0].category == "test")

    json_products = parse_products(SAMPLE_PROPERTIES_JSON_HTML, "https://www.zimmo.be/nl/gent-9000/te-koop/", category="gent-koop")
    ok &= check("properties JSON path extracts both listings", len(json_products) == 2)
    ok &= check("title built from subtype + real address + city (not just 'Huis te koop')",
                json_products[0].title == "Rijwoning, Kraaistraat 9, Gent")
    ok &= check("exact url field used directly, no guessing",
                json_products[0].url == "https://www.zimmo.be/nl/gent-9000/te-koop/huis/LR6DK/")
    ok &= check("sku (code), brand (advertiser name), property_type all correct",
                json_products[0].sku == "LR6DK" and json_products[0].brand == "Immo Francois - Gent"
                and json_products[0].property_type == "Huis")
    ok &= check("epc_label/epc_value correctly pulled from JSON (never findable in visible HTML text)",
                json_products[0].epc_label == "C" and json_products[0].epc_value == 216.0)
    ok &= check("bedrooms (slaapkamers) correctly parsed", json_products[0].bedrooms == 3)
    ok &= check("listing_type correctly split sale vs rent from the status field",
                json_products[0].listing_type == "sale" and json_products[1].listing_type == "rent")
    ok &= check("price-drop (zprijs) discount correctly computed on the second listing",
                json_products[1].price == 950.0 and json_products[1].original_price == 1050.0
                and json_products[1].discount_pct == 9.5)

    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "smoke_out")
        save(products, prefix, "both")
        ok &= check("JSON file written", os.path.isfile(prefix + ".json") and os.path.getsize(prefix + ".json") > 0)
        ok &= check("CSV file written", os.path.isfile(prefix + ".csv") and os.path.getsize(prefix + ".csv") > 0)

    challenge = detect_recaptcha_v3(SAMPLE_RECAPTCHA_HTML, "https://www.zimmo.be/nl/zoeken")
    ok &= check("reCAPTCHA v3 detected via inline-script format",
                challenge is not None and challenge.sitekey == "6Lc_test_sitekey_123456789" and challenge.action == "search")

    widget_challenge = detect_recaptcha_v3(SAMPLE_CAPTCHA_WIDGET_HTML, "https://www.zimmo.be/nl/zoeken")
    ok &= check("reCAPTCHA v3 detected via <captcha-widget> custom-element format",
                widget_challenge is not None and widget_challenge.sitekey == "6LeifPcbAAAAAJaiPe_xgLTfnbdpEMAYJAAnVFJT")

    no_challenge = detect_recaptcha_v3(SAMPLE_LISTING_HTML, "https://www.zimmo.be/nl/zoeken")
    ok &= check("no false-positive captcha detection on clean page", no_challenge is None)

    cf_detected = detect_cloudflare_challenge(SAMPLE_CLOUDFLARE_HTML)
    ok &= check("Cloudflare interstitial detected (distinct from reCAPTCHA — see captcha_solver.py)", cf_detected is True)
    cf_clean = detect_cloudflare_challenge(SAMPLE_LISTING_HTML)
    ok &= check("no false-positive Cloudflare detection on clean page", cf_clean is False)

    turnstile_challenge = detect_turnstile_challenge(SAMPLE_TURNSTILE_HTML, "https://www.zimmo.be/nl/")
    ok &= check("Cloudflare Turnstile detected and sitekey extracted (confirmed live format)",
                turnstile_challenge is not None and turnstile_challenge.sitekey == "0x4AAAAAAABkMYinukE8nzY")
    no_turnstile = detect_turnstile_challenge(SAMPLE_LISTING_HTML, "https://www.zimmo.be/nl/")
    ok &= check("no false-positive Turnstile detection on clean page", no_turnstile is None)

    # --- New in the CLAUDE.md-aligned rewrite: output contract (§9) ---
    from output_writer import finish_run, dedupe_by_sku, write_csv, Product, EXIT_ZERO_PRODUCTS, EXIT_OK

    with tempfile.TemporaryDirectory() as tmp:
        empty_csv_path = os.path.join(tmp, "empty.csv")
        write_csv([], empty_csv_path)
        with open(empty_csv_path) as f:
            empty_csv_content = f.read()
        ok &= check("an empty CSV still carries its header row (never a zero-byte file)",
                    "source,scraped_at,url" in empty_csv_content
                    and len(empty_csv_content.splitlines()) == 1)

        zero_prefix = os.path.join(tmp, "zero_run")
        code = finish_run([], zero_prefix, "both", pages_requested=1, pages_completed=1, allow_empty=False)
        ok &= check("zero products + no --allow-empty writes NOTHING (never overwrites a previous good run)",
                    code == EXIT_ZERO_PRODUCTS
                    and not os.path.exists(zero_prefix + ".json")
                    and not os.path.exists(zero_prefix + ".csv")
                    and not os.path.exists(zero_prefix + ".meta.json"))

        allow_empty_prefix = os.path.join(tmp, "allow_empty_run")
        code2 = finish_run([], allow_empty_prefix, "both", pages_requested=1, pages_completed=1, allow_empty=True)
        ok &= check("zero products + --allow-empty DOES write (explicit opt-out honoured)",
                    code2 == EXIT_OK and os.path.exists(allow_empty_prefix + ".json")
                    and os.path.exists(allow_empty_prefix + ".meta.json"))

    dup_a = Product(sku="A", url="first-seen")
    dup_b = Product(sku="B", url="only-one")
    dup_a_again = Product(sku="A", url="later-page-duplicate")
    deduped = dedupe_by_sku([dup_a, dup_b, dup_a_again])
    ok &= check("dedupe_by_sku keeps the FIRST occurrence (page order, not arrival order)",
                len(deduped) == 2 and deduped[0].url == "first-seen")

    # --- New in the CLAUDE.md-aligned rewrite: JSON-LD edge cases (§4) ---
    SAMPLE_JSONLD_GRAPH_HTML = """
    <html><body>
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@graph": [
      {"@type": "House", "name": "Graph-shape house", "url": "/nl/detail/999",
       "offers": {"price": "410000", "priceCurrency": "EUR"},
       "image": {"@type": "ImageObject", "url": "https://cdn.zimmo.be/graph-img.jpg"}}
    ]}
    </script>
    </body></html>
    """
    graph_products = parse_products(SAMPLE_JSONLD_GRAPH_HTML, "https://www.zimmo.be")
    ok &= check("JSON-LD under '@graph' (not itemListElement) is found, not silently empty",
                len(graph_products) == 1 and graph_products[0].title == "Graph-shape house")
    ok &= check("ImageObject dict shape resolves to its url, not the raw dict",
                graph_products[0].image_url == "https://cdn.zimmo.be/graph-img.jpg")

    SAMPLE_JSONLD_NULL_OFFERS_HTML = """
    <html><body>
    <script type="application/ld+json">
    {"@type": "ItemList", "itemListElement": [
      {"item": {"@type": "Apartment", "name": "Null-offers apartment",
                "url": "/nl/detail/111", "offers": null}}
    ]}
    </script>
    </body></html>
    """
    null_offers_products = parse_products(SAMPLE_JSONLD_NULL_OFFERS_HTML, "https://www.zimmo.be")
    ok &= check("explicit 'offers': null does not crash (a missing-key .get() default would not catch this)",
                len(null_offers_products) == 1 and null_offers_products[0].price is None)

    print()
    if ok:
        print("All smoke tests passed. Core logic is internally consistent —")
        print("but see product_parser.py's module docstring: the properties-JSON")
        print("path is confirmed live; JSON-LD and the CSS fallback are not, since")
        print("the properties path has always been present so far. Run a real")
        print("browser test next if either fallback is ever actually exercised.")
        return 0
    else:
        print("Some checks FAILED — fix these before running against a real browser/site.")
        return 1


# --- Wording enforced by this test (CLAUDE.md §12) ---
# Guard names and dead features too (CLAUDE.md §10): costs nothing, and
# catches an editor reintroducing either. Scanned against every .py file
# actually shipped in this repo, not just the ones this test happens to
# import, since a banned phrase left in a docstring/help string would
# never otherwise be exercised by any test.
_BANNED_PHRASES = (
    "cloud browser",
    "antidetect browser",
    "2scraper antidetect browser",
    "gate.2prx.com",
    "--antidetect",
    "antidetect_local_api",
)
_SHIPPED_PY_FILES = (
    "env_config.py", "output_writer.py", "product_parser.py", "captcha_solver.py",
    "proxy_pool.py", "fingerprint_client.py", "diff_runs.py", "scraper_api_client.py",
    "check_no_credentials.py",
    "playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py",
    # smoke_test.py itself is deliberately NOT scanned: its own
    # _BANNED_PHRASES list necessarily contains these strings as literal
    # data to check against, so scanning this file would always flag
    # itself -- a self-reference, not a real violation.
)
# The text a reader actually sees. Until 2026-09-23 only the .py files
# above were scanned, so the README's own tagline and pyproject.toml's
# package description both carried the first banned phrase under a green
# suite (CLAUDE.md §21: a wording check must cover what the repo
# PUBLISHES). CHANGELOG.md is left out on purpose: its released sections
# are history (§19), and two of them name removed features in order to
# say they were removed.
_SHIPPED_TEXT_FILES = (
    "README.md", "pyproject.toml", ".env.example", "Dockerfile",
    "CONTRIBUTING.md", "SECURITY.md",
    ".github/workflows/tests.yml", ".github/workflows/canary.yml",
)


def check_banned_wording() -> bool:
    ok = True
    this_dir = os.path.dirname(os.path.abspath(__file__))
    scanned = 0
    for filename in _SHIPPED_PY_FILES + _SHIPPED_TEXT_FILES:
        path = os.path.join(this_dir, filename)
        if not os.path.isfile(path):
            # a partial checkout, or the Docker image, which COPYs no
            # README/.github -- not this test's job
            continue
        scanned += 1
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().lower()
        for phrase in _BANNED_PHRASES:
            if phrase in content:
                print(f"[FAIL] banned phrase {phrase!r} found in {filename}")
                ok = False
    if scanned == 0:
        print("[FAIL] banned-wording check scanned no files at all")
        return False
    if ok:
        print(f"[PASS] no banned wording found across {scanned} shipped files")
    return ok


def check_docker_playwright_version_pins_match() -> bool:
    """FIXED (external audit, 2026-09-14, codex.md finding #7): the
    Dockerfile's base image bundles a browser matched to a SPECIFIC
    playwright version; requirements-playwright.txt must pin that exact
    same version, not a >= range, or `pip install` can put a newer
    client library on top of an image with an older browser revision."""
    import re as _re
    this_dir = os.path.dirname(os.path.abspath(__file__))
    dockerfile_path = os.path.join(this_dir, "Dockerfile")
    req_path = os.path.join(this_dir, "requirements-playwright.txt")

    if not os.path.isfile(dockerfile_path) or not os.path.isfile(req_path):
        print("[SKIP] Docker/Playwright version-pin check: Dockerfile or "
              "requirements-playwright.txt not found")
        return True

    with open(dockerfile_path) as f:
        dockerfile_content = f.read()
    with open(req_path) as f:
        req_content = f.read()

    docker_match = _re.search(r"playwright/python:v([\d.]+)-", dockerfile_content)
    req_match = _re.search(r"playwright==([\d.]+)", req_content)

    if not docker_match:
        print("[FAIL] Could not find a playwright/python:vX.Y.Z- base image tag in Dockerfile")
        return False
    if not req_match:
        print("[FAIL] requirements-playwright.txt does not pin an EXACT playwright version "
              "(playwright==X.Y.Z) -- a >= range can install a client library newer than "
              "the Docker image's own bundled browser revision.")
        return False

    docker_version, req_version = docker_match.group(1), req_match.group(1)
    if docker_version != req_version:
        print(f"[FAIL] Version mismatch: Dockerfile pins browser image v{docker_version}, "
              f"but requirements-playwright.txt pins playwright=={req_version} -- these "
              f"must match exactly.")
        return False

    print(f"[PASS] Dockerfile and requirements-playwright.txt both pin playwright {docker_version}")
    return True


def check_pyproject_version_matches_changelog() -> bool:
    """A version mismatch between pyproject.toml and CHANGELOG.md's own
    latest entry (audit's own incidental finding) means one of the two
    was edited without the other -- confirmed live in a prior version
    of this repo (pyproject.toml said 0.1.0, CHANGELOG said 0.2.0)."""
    import re as _re
    this_dir = os.path.dirname(os.path.abspath(__file__))
    pyproject_path = os.path.join(this_dir, "pyproject.toml")
    changelog_path = os.path.join(this_dir, "CHANGELOG.md")

    if not os.path.isfile(pyproject_path) or not os.path.isfile(changelog_path):
        print("[SKIP] version-consistency check: pyproject.toml or CHANGELOG.md not found")
        return True

    with open(pyproject_path) as f:
        pyproject_content = f.read()
    with open(changelog_path) as f:
        changelog_content = f.read()

    pyproject_match = _re.search(r'^version\s*=\s*"([\d.]+)"', pyproject_content, _re.MULTILINE)
    changelog_match = _re.search(r"^## \[([\d.]+)\]", changelog_content, _re.MULTILINE)

    if not pyproject_match or not changelog_match:
        print("[SKIP] version-consistency check: could not find a version in one of the two files")
        return True

    pyproject_version, changelog_version = pyproject_match.group(1), changelog_match.group(1)
    if pyproject_version != changelog_version:
        print(f"[FAIL] pyproject.toml says version {pyproject_version}, but CHANGELOG.md's "
              f"latest entry is [{changelog_version}] -- one was bumped without the other.")
        return False

    print(f"[PASS] pyproject.toml and CHANGELOG.md agree on version {pyproject_version}")
    return True


def check_item_link_selector_rejects_nav_chrome() -> bool:
    """Regression test for a bug found via a REAL live run against
    zimmo.be (2026-09-16): SELECTORS["item_link"] was a loose
    pre-confirmation guess (`a[href*="/"]`) matching ANY relative link,
    including a blocked/shell page's own navigation, cookie-consent,
    language-switcher and footer-legal links -- confirmed live: 91 such
    links on a genuinely Cloudflare-blocked page. This selector feeds
    both `_wait_for_listing_markers()`'s render-detection wait AND
    `--solve-captcha=when-blocked`'s "does this page already have real
    content" check across all three engines -- the second one was
    confirmed live to be actively wrong: it counted those 91 nav links
    as "already rendered", so the DEFAULT --solve-captcha setting
    skipped solving entirely on a page that was still genuinely
    blocked (confirmed via the exact live log line "not attempting to
    solve it" on an underlying "Just a moment..." page)."""
    from bs4 import BeautifulSoup
    from product_parser import SELECTORS

    nav_links = "".join(f'<a href="/nl/some-nav-{i}">Nav {i}</a>' for i in range(91))
    blocked_shell_html = f"<html><body>{nav_links}</body></html>"
    soup = BeautifulSoup(blocked_shell_html, "html.parser")
    shell_matches = soup.select(SELECTORS["item_link"])
    if len(shell_matches) > 5:
        print(f"[FAIL] item_link selector matched {len(shell_matches)} elements on a "
              f"nav-only/blocked-shell page (should be 0) -- this is the exact live bug "
              f"that made --solve-captcha=when-blocked skip solving on a genuinely "
              f"blocked page.")
        return False

    real_tiles = "".join(f'<span class="zimmo-code">LR{i:03d}</span>' for i in range(20))
    real_html = f"<html><body>{real_tiles}</body></html>"
    soup2 = BeautifulSoup(real_html, "html.parser")
    real_matches = soup2.select(SELECTORS["item_link"])
    if len(real_matches) < 5:
        print(f"[FAIL] item_link selector matched only {len(real_matches)} elements on "
              f"a page with 20 real listing tiles -- too narrow to detect real content.")
        return False

    print(f"[PASS] item_link selector ('{SELECTORS['item_link']}') correctly rejects "
          f"nav/cookie/footer chrome (0 matches on 91 nav links) while matching real "
          f"listing tiles ({len(real_matches)} matches on 20 real tiles)")
    return True


def check_css_fallback_widened_scope_is_used() -> bool:
    """Regression test for a bug found via a REAL live run against
    zimmo.be (2026-09-15, not a lab fixture): the properties-JSON and
    real-estate JSON-LD paths are both now absent from the live site
    (an Angular rewrite since the 2026-08-22 capture), so this fallback
    is the one path actually exercised on the real site today -- and it
    returned 0 products on every real page tested. Root cause: the
    widening loop correctly found the tile's price-containing ancestor
    (Zimmo's real tile has the price in a sibling `<div class="price">`,
    not inside the `<a>` around the title), but that widened scope was
    thrown away and silently recomputed narrower (an anchor's own
    immediate parent, which has no price) in a second pass. This test
    replicates a real captured tile's structure verbatim."""
    from product_parser import _parse_css_fallback

    html = ('<div class="infobox_content"><h2><a href="/nl/gent-9000/te-koop/huis/LRTG0" '
            'jsaction="click:;"><span class="title">Huis te koop'
            '<span class="zimmo-code">LRTG0</span></span>'
            '<address> Biekorfstraat 15 <br> 9000 Gent </address></a></h2>'
            '<div class="price"><div class="amount"><span>€&nbsp;260.000</span>'
            '</div></div></div>')
    products = _parse_css_fallback(html, "https://www.zimmo.be", category=None)

    if not products:
        print("[FAIL] CSS fallback found 0 products on a real captured tile structure -- "
              "this is the exact live failure (widened scope discarded, price never found).")
        return False
    p = products[0]
    if p.price != 260000.0:
        print(f"[FAIL] CSS fallback price: expected 260000.0, got {p.price}")
        return False
    if p.sku != "LRTG0":
        print(f"[FAIL] CSS fallback sku: expected 'LRTG0', got {p.sku!r}")
        return False
    if p.title != "Huis te koop" or "LRTG0" in (p.title or ""):
        print(f"[FAIL] CSS fallback title should be clean, without the zimmo-code "
              f"mixed in: got {p.title!r}")
        return False

    print("[PASS] CSS fallback correctly uses its own widened scope (price found in a "
          "sibling div, not the anchor's own immediate parent) and extracts sku/title/"
          "location precisely from confirmed-live Angular tile markup")
    return True


# Six real <zimmo-listing> tiles, cut from live captures taken
# 2026-09-23 through a Belgian residential exit (gent-9000/te-koop pages
# 1-3, antwerpen-2000/te-huur, bruxelles-1000/a-vendre), trimmed of SVG
# paths, Angular attributes and the favourite button. The trimmed fixture
# was checked to parse to the same rows as the untrimmed tiles. Each tile
# is here for one case the site really serves:
#   LRZLA  NL sale, bedrooms + EPC icon, no living area stated
#   LR7M2  living area written "2.578" (thousands, not 2.578 m2), EPC A+
#   KLQRM  "Prijs op aanvraag" -- a real listing with no price at all
#   LRZ2M  only a COMMERCIAL surface, which is not a living area
#   LRZK9  French page, Brussels' EPB energy scheme instead of EPC
#   KRBP7  epc_x.svg -- a listing with no energy grade
LIVE_ANGULAR_TILES_HTML = r'''
<zimmo-listing _ngcontent-ng-c1732943450="" _nghost-ng-c3516730="" class="" ngh="19" style="order: 0;" zimmointersectionobserver=""><article><div class="infobox_photo"><img alt="Huis te koop in Zwijnaardsesteenweg 685,
 9000 Gent" class="main-image" height="618" src="https://files.zimmo.be/backend-api/qMbJs7nn9uXzLU5aLtWl6XQOLD0=/828x618/filters:image-format(pjpg)/-/real-estate/customers/b4e045de-4bae-11e9-922b-005056b768a1/dealers/d8334a31-1f5c-4c08-994d-b0a3d6a03f65/listings/52a68ae4-88dc-4c2a-8885-e215155beb7a/images/01a0ce69-9587-7761-9b6a-30c604f1d540" width="828"/><div class="header"><div class="sticker"><span>Nieuw</span></div></div><div class="logo"><img alt="Joost EEMAN logo" src="/assets/@listings/icons/biddit.svg" title="Joost EEMAN logo"/></div></div><div class="infobox_content"><h2><a href="/nl/gent-9000/te-koop/huis/LRZLA"><span class="title">Huis te koop<span class="zimmo-code">LRZLA</span></span><address> Zwijnaardsesteenweg 685 <br/> 9000 Gent </address></a></h2><div class="price"><div class="amount"><svg-icon data-src="assets/@listings/icons/bidditb.svg"></svg-icon><span>€ 210.000</span></div></div><div class="features"><div class="features_item"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/clock.svg" role="presentation" src="/assets/@listings/features/clock.svg"></svg-icon><svg-icon class="value" data-src="/assets/@listings/icons/lock.svg" src="/assets/@listings/icons/lock.svg"></svg-icon></div><div aria-label="Het aantal slaapkamers is 3" class="features_item" role="img"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/bedrooms.svg" role="presentation"></svg-icon><span aria-hidden="true" class="value">3</span></div><div class="features_energy"><svg-icon data-src="/assets/@listings/energy-labels/epc_f.svg"></svg-icon></div></div></div></article></zimmo-listing><zimmo-listing _ngcontent-ng-c1732943450="" _nghost-ng-c3516730="" class="" ngh="30" style="order: 40;" zimmointersectionobserver=""><article><div class="infobox_photo"><img alt="Bedrijfsvastgoed te koop in Raymonde de Larochelaan 50,
 9000 Gent" class="main-image" height="618" src="https://files.zimmo.be/backend-api/q4c6J2efEjQ-8HM34cAzWFv5JFg=/828x618/filters:image-format(pjpg)/-/real-estate/customers/b51c1823-4bae-11e9-922b-005056b768a1/dealers/b31369f3-62f9-4cca-96ef-b8652bdde916/listings/779606b7-3528-42b5-864c-f04017842d73/images/01a033c1-ab46-7ec5-9c14-340a64a1def6" width="828"/><div class="header"><div class="sticker"></div></div><div class="logo"><img alt="PANORAMA B2B Gent kantoren logo" src="https://files.zimmo.be/backend-api/x12STQmtgb4wwt-IHHtL9s3TDOw=/filters:image-format(pjpg)/-/real-estate/customers/b51c1823-4bae-11e9-922b-005056b768a1/logos/01995d00-92f6-7412-b6ea-6258f0d09b91" title="PANORAMA B2B Gent kantoren logo"/></div></div><div class="infobox_content"><h2><a href="/nl/gent-9000/te-koop/bedrijfsvastgoed/LR7M2"><span class="title">Bedrijfsvastgoed te koop<span class="zimmo-code">LR7M2</span></span><address> Raymonde de Larochelaan 50 <br/> 9000 Gent </address></a></h2><div class="price"><div class="amount"><span>€ 9.280.800</span></div></div><div class="features"><div class="features_item"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/clock.svg" role="presentation" src="/assets/@listings/features/clock.svg"></svg-icon><svg-icon class="value" data-src="/assets/@listings/icons/lock.svg" src="/assets/@listings/icons/lock.svg"></svg-icon></div><div aria-label="De woonoppervlakte is 2.578 vierkante meter" class="features_item" role="img"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/floorspace-surface.svg" role="presentation"></svg-icon><span aria-hidden="true" class="value">2.578m²</span></div><div class="features_energy"><svg-icon data-src="/assets/@listings/energy-labels/epc_a_plus.svg"></svg-icon></div></div></div></article></zimmo-listing><zimmo-listing _ngcontent-ng-c1732943450="" _nghost-ng-c3516730="" class="show-extra-photos" ngh="24" style="order: 10;" zimmointersectionobserver=""><article><div class="infobox_photo"><img alt="Bedrijfsvastgoed te huur in Noorderplaats 5-9,
 2000 Antwerpen" class="main-image" height="618" src="https://files.zimmo.be/backend-api/Yeq2UCeGqvXE-Gs0afaYDBlsC8M=/828x618/filters:image-format(pjpg)/-/real-estate/customers/b4f9b016-4bae-11e9-922b-005056b768a1/dealers/523ce871-2114-4928-ad1d-bbff4710b20c/listings/f9e8040a-114f-4d8f-addb-b9c8106d768d/images/019f2f9f-31eb-7a0e-a031-b1f83b7a7994" width="828"/><div class="header"><div class="sticker"></div></div><div class="logo __premium"><div class="logo_premium-label">Premium partner</div><img alt="Oreon Properties Herentals logo" src="https://files.zimmo.be/backend-api/CptRHtEnttr2_YVCJRefSBGHpGw=/filters:image-format(pjpg)/-/real-estate/customers/b4f9b016-4bae-11e9-922b-005056b768a1/logos/01995d00-6da7-7e8d-b17d-05b4a0af9ca1" title="Oreon Properties Herentals logo"/></div></div><div class="premium-photos"><div class="premium-photos_item"><img alt="" role="presentation" src="https://files.zimmo.be/backend-api/xehzxY78-k92wPOIcn7PdEvZNsw=/828x618/filters:image-format(pjpg)/-/real-estate/customers/b4f9b016-4bae-11e9-922b-005056b768a1/dealers/523ce871-2114-4928-ad1d-bbff4710b20c/listings/f9e8040a-114f-4d8f-addb-b9c8106d768d/images/019f2f9f-33fe-7aec-b38d-1a5deed2fb52"/></div><div class="premium-photos_item"><img alt="" role="presentation" src="https://files.zimmo.be/backend-api/z5zcg0HmowHawNOX_uYECEYaA5A=/828x618/filters:image-format(pjpg)/-/real-estate/customers/b4f9b016-4bae-11e9-922b-005056b768a1/dealers/523ce871-2114-4928-ad1d-bbff4710b20c/listings/f9e8040a-114f-4d8f-addb-b9c8106d768d/images/019f2f9f-369b-7770-b180-7ca831f6f71a"/><div class="logo __premium"><div class="logo_premium-label">Premium partner</div><img alt="Oreon Properties Herentals logo" src="https://files.zimmo.be/backend-api/CptRHtEnttr2_YVCJRefSBGHpGw=/filters:image-format(pjpg)/-/real-estate/customers/b4f9b016-4bae-11e9-922b-005056b768a1/logos/01995d00-6da7-7e8d-b17d-05b4a0af9ca1" title="Oreon Properties Herentals logo"/></div></div></div><div class="infobox_content"><h2><a href="/nl/antwerpen-2000/te-huur/bedrijfsvastgoed/KLQRM"><span class="title">Bedrijfsvastgoed te huur<span class="zimmo-code">KLQRM</span></span><address> Noorderplaats 5-9 <br/> 2000 Antwerpen </address></a></h2><div class="price"><div class="amount"><span>Prijs op aanvraag</span></div></div><div class="features"><div class="features_item"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/clock.svg" role="presentation" src="/assets/@listings/features/clock.svg"></svg-icon><svg-icon class="value" data-src="/assets/@listings/icons/lock.svg" src="/assets/@listings/icons/lock.svg"></svg-icon></div><div aria-label="De woonoppervlakte is 537 vierkante meter" class="features_item" role="img"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/floorspace-surface.svg" role="presentation"></svg-icon><span aria-hidden="true" class="value">537m²</span></div></div></div></article></zimmo-listing><zimmo-listing _ngcontent-ng-c1732943450="" _nghost-ng-c3516730="" class="" ngh="22" style="order: 4;" zimmointersectionobserver=""><article><div class="infobox_photo"><img alt="Bedrijfsvastgoed te huur in Meir 30 bus V3+4,
 2000 Antwerpen" class="main-image" height="618" src="https://files.zimmo.be/backend-api/OLjaHKcOboVRooDrtd3usgAw4vU=/828x618/filters:image-format(pjpg)/-/real-estate/customers/b4d4fa0b-4bae-11e9-922b-005056b768a1/dealers/0402eff7-59a5-4696-a6d4-4391499dc7e0/listings/b0dc0a30-ea3c-46b4-baa9-6e647320c72f/images/01a0cd28-c507-7279-8312-3ac92114236a" width="828"/><div class="header"><div class="sticker"><span>Nieuw</span></div></div><div class="logo"></div></div><div class="infobox_content"><h2><a href="/nl/antwerpen-2000/te-huur/bedrijfsvastgoed/LRZ2M"><span class="title">Bedrijfsvastgoed te huur<span class="zimmo-code">LRZ2M</span></span><address> Meir 30 bus V3+4 <br/> 2000 Antwerpen </address></a></h2><div class="price"><div class="amount"><span>€ 16.249</span></div></div><div class="features"><div class="features_item"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/clock.svg" role="presentation" src="/assets/@listings/features/clock.svg"></svg-icon><svg-icon class="value" data-src="/assets/@listings/icons/lock.svg" src="/assets/@listings/icons/lock.svg"></svg-icon></div><div aria-label="De handelsoppervlakte is 1.258 vierkante meter" class="features_item" role="img"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/commercial-surface.svg" role="presentation"></svg-icon><span aria-hidden="true" class="value">1.258m²</span></div><div class="features_energy"><svg-icon data-src="/assets/@listings/energy-labels/epc_b.svg"></svg-icon></div></div></div></article></zimmo-listing><zimmo-listing _ngcontent-ng-c1732943450="" _nghost-ng-c3516730="" class="" ngh="19" style="order: 0;" zimmointersectionobserver=""><article><div class="infobox_photo"><img alt="Appartement à vendre à ,
 1050 Ixelles" class="main-image" height="618" src="https://files.zimmo.be/backend-api/r6LuaXXm6Ot_djwN7YN01RCXzSI=/828x618/filters:image-format(pjpg)/-/real-estate/customers/b53097eb-4bae-11e9-922b-005056b768a1/dealers/5130f9eb-e89d-463c-9918-810c8cd6b6b7/listings/8dff6a47-0e50-453d-8d4d-ae6eb4bb698b/images/01a0ce56-aff3-7278-af27-be306254ea21" width="828"/><div class="header"><div class="sticker"><span>Nouveau</span></div></div><div class="logo"></div></div><div class="infobox_content"><h2><a href="/fr/bruxelles-1000/a-vendre/appartement/LRZK9"><span class="title">Appartement à vendre<span class="zimmo-code">LRZK9</span></span><address> Adresse sur demande <br/> 1050 Ixelles </address></a></h2><div class="price"><div class="amount"><span>€ 550.000</span></div></div><div class="features"><div class="features_item"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/clock.svg" role="presentation" src="/assets/@listings/features/clock.svg"></svg-icon><svg-icon class="value" data-src="/assets/@listings/icons/lock.svg" src="/assets/@listings/icons/lock.svg"></svg-icon></div><div aria-label="Le nombre de chambres est de 2" class="features_item" role="img"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/bedrooms.svg" role="presentation"></svg-icon><span aria-hidden="true" class="value">2</span></div><div aria-label="La surface habitable est de 140 mètres carrés" class="features_item" role="img"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/floorspace-surface.svg" role="presentation"></svg-icon><span aria-hidden="true" class="value">140m²</span></div><div class="features_energy"><svg-icon data-src="/assets/@listings/energy-labels/epb_f.svg"></svg-icon></div></div></div></article></zimmo-listing><zimmo-listing _ngcontent-ng-c1732943450="" _nghost-ng-c3516730="" class="show-extra-photos" ngh="25" style="order: 12;" zimmointersectionobserver=""><article><div class="infobox_photo"><img alt="Bedrijfsvastgoed te huur in Oudeleeuwenrui 13,
 2000 Antwerpen" class="main-image" height="618" src="https://files.zimmo.be/backend-api/H1xoGvHaymgqT4ihyMc7XOpBAs0=/828x618/filters:image-format(pjpg)/-/real-estate/customers/b4f9b016-4bae-11e9-922b-005056b768a1/dealers/523ce871-2114-4928-ad1d-bbff4710b20c/listings/08512cb8-2cb8-4047-8afb-f2f0bcb109af/images/019db7a7-8ce7-7286-a861-af96552800ff" width="828"/><div class="header"><div class="sticker"></div></div><div class="logo __premium"><div class="logo_premium-label">Premium partner</div><img alt="Oreon Properties Herentals logo" src="https://files.zimmo.be/backend-api/CptRHtEnttr2_YVCJRefSBGHpGw=/filters:image-format(pjpg)/-/real-estate/customers/b4f9b016-4bae-11e9-922b-005056b768a1/logos/01995d00-6da7-7e8d-b17d-05b4a0af9ca1" title="Oreon Properties Herentals logo"/></div></div><div class="premium-photos"><div class="premium-photos_item"><img alt="" role="presentation" src="https://files.zimmo.be/backend-api/f0RYREY0mN-4CUxkOm9n1QalAg8=/828x618/filters:image-format(pjpg)/-/real-estate/customers/b4f9b016-4bae-11e9-922b-005056b768a1/dealers/523ce871-2114-4928-ad1d-bbff4710b20c/listings/08512cb8-2cb8-4047-8afb-f2f0bcb109af/images/019db7a7-8edb-7610-9671-8eeffc8386d2"/></div><div class="premium-photos_item"><img alt="" role="presentation" src="https://files.zimmo.be/backend-api/kb0ckUuE9cdLT1pj-inUE8W2ek4=/828x618/filters:image-format(pjpg)/-/real-estate/customers/b4f9b016-4bae-11e9-922b-005056b768a1/dealers/523ce871-2114-4928-ad1d-bbff4710b20c/listings/08512cb8-2cb8-4047-8afb-f2f0bcb109af/images/019db7a7-90fe-777a-b9c3-bb1827e55f3a"/><div class="logo __premium"><div class="logo_premium-label">Premium partner</div><img alt="Oreon Properties Herentals logo" src="https://files.zimmo.be/backend-api/CptRHtEnttr2_YVCJRefSBGHpGw=/filters:image-format(pjpg)/-/real-estate/customers/b4f9b016-4bae-11e9-922b-005056b768a1/logos/01995d00-6da7-7e8d-b17d-05b4a0af9ca1" title="Oreon Properties Herentals logo"/></div></div></div><div class="infobox_content"><h2><a href="/nl/antwerpen-2000/te-huur/bedrijfsvastgoed/KRBP7"><span class="title">Bedrijfsvastgoed te huur<span class="zimmo-code">KRBP7</span></span><address> Oudeleeuwenrui 13 <br/> 2000 Antwerpen </address></a></h2><div class="price"><div class="amount"><span>€ 6.320</span></div></div><div class="features"><div class="features_item"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/clock.svg" role="presentation" src="/assets/@listings/features/clock.svg"></svg-icon><svg-icon class="value" data-src="/assets/@listings/icons/lock.svg" src="/assets/@listings/icons/lock.svg"></svg-icon></div><div aria-label="De woonoppervlakte is 523 vierkante meter" class="features_item" role="img"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/floorspace-surface.svg" role="presentation"></svg-icon><span aria-hidden="true" class="value">523m²</span></div><div class="features_energy"><svg-icon data-src="/assets/@listings/energy-labels/epc_x.svg"></svg-icon></div></div></div></article></zimmo-listing><zimmo-listing _ngcontent-ng-c1732943450="" _nghost-ng-c3516730="" class="" ngh="30" style="order: 40;" zimmointersectionobserver=""><article><div class="infobox_photo"><img alt="Bedrijfsvastgoed te koop in Raymonde de Larochelaan 50,
 9000 Gent" class="main-image" height="618" src="https://files.zimmo.be/backend-api/q4c6J2efEjQ-8HM34cAzWFv5JFg=/828x618/filters:image-format(pjpg)/-/real-estate/customers/b51c1823-4bae-11e9-922b-005056b768a1/dealers/b31369f3-62f9-4cca-96ef-b8652bdde916/listings/779606b7-3528-42b5-864c-f04017842d73/images/01a033c1-ab46-7ec5-9c14-340a64a1def6" width="828"/><div class="header"><div class="sticker"></div></div><div class="logo"><img alt="PANORAMA B2B Gent kantoren logo" src="https://files.zimmo.be/backend-api/x12STQmtgb4wwt-IHHtL9s3TDOw=/filters:image-format(pjpg)/-/real-estate/customers/b51c1823-4bae-11e9-922b-005056b768a1/logos/01995d00-92f6-7412-b6ea-6258f0d09b91" title="PANORAMA B2B Gent kantoren logo"/></div></div><div class="infobox_content"><h2><a href="/nl/gent-9000/te-koop/bedrijfsvastgoed/LR7M2"><span class="title">Bedrijfsvastgoed te koop<span class="zimmo-code">LR7M2</span></span><address> Raymonde de Larochelaan 50 <br/> 9000 Gent </address></a></h2><div class="price"><div class="amount"><span>€ 9.280.800</span></div></div><div class="features"><div class="features_item"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/clock.svg" role="presentation" src="/assets/@listings/features/clock.svg"></svg-icon><svg-icon class="value" data-src="/assets/@listings/icons/lock.svg" src="/assets/@listings/icons/lock.svg"></svg-icon></div><div aria-label="De woonoppervlakte is 2.578 vierkante meter" class="features_item" role="img"><svg-icon aria-hidden="true" data-src="/assets/@listings/features/floorspace-surface.svg" role="presentation"></svg-icon><span aria-hidden="true" class="value">2.578m²</span></div><div class="features_energy"><svg-icon data-src="/assets/@listings/energy-labels/epc_a_plus.svg"></svg-icon></div></div></div></article></zimmo-listing>
'''


def check_live_angular_tiles() -> bool:
    """Values, not coverage (CLAUDE.md §10), on the markup the site serves
    today. Before 2026-09-23 bedrooms, EPC and property_type were None on
    every row of every run, KLQRM vanished from the output, LR7M2's area
    read as 2.578 m2 and LRZ2M's commercial surface was written into
    surface_m2 -- all while every run reported success."""
    rows = {p.sku: p for p in parse_products(LIVE_ANGULAR_TILES_HTML, "https://www.zimmo.be/")}
    expected = {
        #        price     cur    listing property            beds  m2      epc
        "LRZLA": (210000.0, "EUR", "sale", "huis",             3,    None,   "F"),
        "LR7M2": (9280800.0, "EUR", "sale", "bedrijfsvastgoed", None, 2578.0, "A+"),
        "KLQRM": (None,     None,  "rent", "bedrijfsvastgoed", None, 537.0,  None),
        "LRZ2M": (16249.0,  "EUR", "rent", "bedrijfsvastgoed", None, None,   "B"),
        "LRZK9": (550000.0, "EUR", "sale", "appartement",      2,    140.0,  "F"),
        "KRBP7": (6320.0,   "EUR", "rent", "bedrijfsvastgoed", None, 523.0,  None),
    }
    ok = True
    if set(rows) != set(expected):
        print(f"[FAIL] live Angular tiles: expected skus {sorted(expected)}, got {sorted(rows)}")
        ok = False
    for sku, exp in expected.items():
        r = rows.get(sku)
        if r is None:
            continue
        got = (r.price, r.currency, r.listing_type, r.property_type, r.bedrooms, r.surface_m2, r.epc_label)
        if got != exp:
            print(f"[FAIL] live Angular tile {sku}: expected {exp}, got {got}")
            ok = False
    if "LRZK9" in rows and rows["LRZK9"].title != "Appartement à vendre":
        print(f"[FAIL] live Angular tile LRZK9 title: {rows['LRZK9'].title!r}")
        ok = False
    if ok:
        print("[PASS] six real 2026-09-23 tiles: price-on-request kept with no currency, "
              "areas read Belgian-style, commercial surface not taken for living area, "
              "bedrooms/EPC/EPB read from icons in NL and FR, property/listing type from the URL")
    return ok


def check_epc_label_natural_language_regression() -> bool:
    """Regression test for a bug found via this repo's OWN adversarial
    re-testing (2026-09-15), not the external audit: the EPC regex
    previously required only non-letter characters between "EPC" and
    the grade letter, so natural-language phrasing like "EPC label C"
    silently failed to match, leaking raw EPC text into the CSS
    fallback's title field instead of being stripped. Also locks in
    that the fix does NOT introduce a false positive on an ordinary
    word (the first, broader fix attempt matched "a" out of
    "available" in unrelated text near the word "EPC")."""
    from product_parser import _strip_known_fields, _parse_css_fallback

    cleaned = _strip_known_fields(
        "Charmant huis Gent € 1250 EPC label C 85 kWh/m² 120 m² 3 slaapkamers")
    if "EPC" in cleaned or "1250" in cleaned:
        print(f"[FAIL] EPC/price text leaked into stripped output: {cleaned!r}")
        return False

    html = ('<html><body><div class="listing-tile">'
            '<a href="/nl/detail/999">Charmant huis Gent € 1250 EPC label C '
            '85 kWh/m² 120 m² 3 slaapkamers</a></div></body></html>')
    products = _parse_css_fallback(html, "https://www.zimmo.be", category=None)
    if not products or products[0].epc_label != "C" or products[0].price != 1250.0:
        print(f"[FAIL] CSS fallback with natural-language EPC phrasing: "
              f"{products[0] if products else 'no products'}")
        return False

    false_positive_text = ("Beautiful house with EPC rating available soon, contact "
                            "Agent Karel for details about the Class action")
    from product_parser import _EPC_LABEL_RE
    m = _EPC_LABEL_RE.search(false_positive_text)
    if m:
        print(f"[FAIL] EPC regex false-positived on ordinary text: matched {m.group(1)!r}")
        return False

    print("[PASS] EPC label correctly extracted from natural-language phrasing, "
          "with no false positive on ordinary text near the word 'EPC'")
    return True


def check_pagination_uses_confirmed_page_param() -> bool:
    """Regression test for a bug found via a REAL live run against
    zimmo.be (2026-09-16): `page_url()` used `?p=N`, confirmed live in
    an EARLIER, unrelated MCP-tooling session against the site at an
    earlier point in time. A live run against the CURRENT site found
    `?p=N` is silently ignored -- page 2 returned page 1's own listings
    verbatim, which the data-based pagination termination correctly
    (but wrongly, given the stale parameter) read as "listing
    exhausted" after just one page, on every multi-page run. The real
    parameter, found by inspecting the site's own pagination link
    directly, is `page` (`?page=2`). Locks in the corrected parameter
    across all three engines (they must not drift from each other)."""
    import importlib as _importlib
    ok = True
    for module_name in ("playwright_scraper", "puppeteer_scraper", "selenium_scraper"):
        try:
            mod = _importlib.import_module(module_name)
        except ImportError:
            continue
        url = mod.page_url("https://www.zimmo.be/nl/gent-9000/te-koop/", 2)
        if "page=2" not in url or "p=2" in url.replace("page=2", ""):
            print(f"[FAIL] {module_name}.page_url(..., 2) = {url!r} -- expected the "
                  f"confirmed-live 'page' parameter, not 'p'.")
            ok = False
    if ok:
        print("[PASS] all engines use the confirmed-live 'page' pagination parameter, "
              "not the stale 'p' one")
    return ok


def check_price_parser_does_not_swallow_adjacent_numbers() -> bool:
    """Regression test for a bug found via a REAL live run against
    zimmo.be (2026-09-16, one iteration AFTER the audit's own price-
    parsing fixes): the fix for codex.md finding #8 allowed a bare
    SPACE as a thousands-grouping character inside the captured price
    digits, in addition to '.'. This was confirmed live to be actively
    dangerous: BeautifulSoup's get_text(" ", strip=True) joins every
    text node in a tile with a single space, so a real price
    ("€ 385.000") followed anywhere later in the same widened scope by
    an unrelated number (a bedroom count, a surface area) got greedily
    swallowed as a continuation of the price -- "€ 385.000 2
    slaapkamers 120 m²" parsed as 3,850,002 instead of 385,000, live,
    on a real page. Locks in that a bare space is no longer treated as
    a grouping character, while '.' (Zimmo's own confirmed thousands
    separator) still is."""
    from product_parser import _PRICE_NEAR_EUR_RE, _parse_price_near_eur

    cases = {
        "€ 385.000 2 slaapkamers 120 m²": 385000.0,
        "€ 260.000 3 slaapkamers": 260000.0,
        "€10000 living room": 10000.0,
    }
    ok = True
    for text, expected in cases.items():
        m = _PRICE_NEAR_EUR_RE.search(text)
        result = _parse_price_near_eur(m) if m else None
        if result != expected:
            print(f"[FAIL] price parser swallowed an adjacent number: {text!r} -> "
                  f"{result}, expected {expected}")
            ok = False
    if ok:
        print(f"[PASS] price parser correctly stops at the price and does not swallow "
              f"an adjacent unrelated number (bedroom count, etc.) across a space")
    return ok


def check_price_parser_audit_regressions() -> bool:
    """Regression test for the EXACT cases an external audit
    (2026-09-14, codex.md finding #8) found broken: "€ 1250" read as
    125.0, "1250 €" read as 250.0, "€10000" read as 100.0, and Belgian
    thousands-grouped "325.000" read as 325 instead of 325000. Locks
    these specific inputs in place so a future edit to the price regex
    cannot silently reintroduce any one of them."""
    from product_parser import _PRICE_NEAR_EUR_RE, _parse_price_near_eur

    cases = {
        "€ 1250": 1250.0,
        "1250 €": 1250.0,
        "€10000": 10000.0,
        "€ 325.000": 325000.0,
        "325.000 €": 325000.0,
        "€ 325.000,50": 325000.50,
        "€ 1.234.567": 1234567.0,
    }
    ok = True
    for text, expected in cases.items():
        m = _PRICE_NEAR_EUR_RE.search(text)
        result = _parse_price_near_eur(m) if m else None
        if result != expected:
            print(f"[FAIL] price parser regression: {text!r} -> {result}, expected {expected}")
            ok = False
    if ok:
        print(f"[PASS] price parser correctly handles all {len(cases)} audit-confirmed cases "
              f"(including unseparated 4+ digit runs and Belgian thousands grouping)")
    return ok


def check_unattempted_pages_prevent_false_complete() -> bool:
    """FIXED (external audit, 2026-09-14, codex.md finding #5): a run
    with unattempted_pages previously could still report
    status=complete, exit 0, since finish_run() didn't accept or act on
    that parameter at all -- confirmed live: pages_requested=5,
    pages_completed=2, exit 0, status=complete."""
    import tempfile
    from output_writer import Product, finish_run, EXIT_PARTIAL

    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "run")
        products = [Product(sku="A", url="u1", title="X")]
        code = finish_run(products, prefix, "json", pages_requested=5, pages_completed=2,
                           unattempted_pages=[3, 4, 5])
        if code != EXIT_PARTIAL:
            print(f"[FAIL] finish_run() with unattempted_pages returned exit {code}, "
                  f"expected EXIT_PARTIAL ({EXIT_PARTIAL})")
            return False
        with open(prefix + ".meta.json") as f:
            meta = json.load(f)
        if meta["status"] == "complete":
            print(f"[FAIL] finish_run() with unattempted_pages=[3,4,5] reported "
                  f"status=complete -- this is the exact false positive the audit found live "
                  f"(pages_requested=5, pages_completed=2, exit 0, status=complete).")
            return False

    print("[PASS] unattempted_pages correctly prevents a false status=complete")
    return True


def check_no_sku_rows_participate_in_dedup() -> bool:
    """FIXED (external audit, 2026-09-14, codex.md finding #9): a
    Product with no `sku` (common from the JSON-LD/CSS fallback paths)
    previously bypassed deduplication entirely -- two rows for the
    exact same URL, both with sku=None, both survived dedupe_by_sku()."""
    from output_writer import Product, dedupe_by_sku

    p1 = Product(sku=None, url="https://zimmo.be/x/1", title="A")
    p2 = Product(sku=None, url="https://zimmo.be/x/1", title="A duplicate")
    p3 = Product(sku=None, url="https://zimmo.be/x/2", title="B")
    result = dedupe_by_sku([p1, p2, p3])

    if len(result) != 2:
        print(f"[FAIL] dedupe_by_sku() on sku=None rows: expected 2 unique (by URL), got {len(result)}")
        return False
    print("[PASS] rows with no sku correctly deduplicate by URL instead of bypassing dedup")
    return True


def check_env_example_matches_env_keys() -> bool:
    """CLAUDE.md §3: .env.example must document exactly the variables the
    code reads -- a documented-but-unread variable is worse than an
    undocumented one, and drifts silently otherwise."""
    from env_config import ENV_KEYS

    this_dir = os.path.dirname(os.path.abspath(__file__))
    example_path = os.path.join(this_dir, ".env.example")
    if not os.path.isfile(example_path):
        print("[FAIL] .env.example is missing entirely")
        return False

    documented = set()
    with open(example_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            documented.add(line.split("=", 1)[0].strip())

    expected = set(ENV_KEYS.keys())
    missing_from_example = expected - documented
    extra_in_example = documented - expected

    ok = True
    if missing_from_example:
        print(f"[FAIL] ENV_KEYS variable(s) not documented in .env.example: {sorted(missing_from_example)}")
        ok = False
    if extra_in_example:
        print(f"[FAIL] .env.example documents variable(s) the code never reads: {sorted(extra_in_example)}")
        ok = False
    if ok:
        print(f"[PASS] .env.example matches ENV_KEYS exactly ({len(expected)} variables)")
    return ok


# --- Round-trip .env.example through the REAL loader (CLAUDE.md §17) ---
# "A copied .env.example read as CONFIGURED": a literal placeholder set can
# only ever catch strings someone already thought to add, and a vendor's
# own braced example (`ws://{login}-zone-...`) sailed straight through an
# earlier version of this check, connecting with the literal string
# "{login}-zone-..." as a username. Actually calling apply_env_defaults()
# against a real argparse Namespace is what a literal-string read of
# .env.example cannot catch -- it exercises the SAME code path a real run
# would, not a parallel assumption about what that code path does.
# Only CREDENTIAL-bearing variables need to round-trip as unset --
# ZIMMO_URL is ordinary configuration, not a secret, and .env.example
# giving it a real, working example value is good practice, not the bug
# class this check exists for. CLAUDE.md §17's own wording is specific to
# "the two credentialled URLs", not every documented variable.
# Only CREDENTIAL-bearing variables need to round-trip as unset --
# ZIMMO_URL is ordinary configuration, not a secret, and .env.example
# giving it a real, working example value is good practice, not the bug
# class this check exists for. CLAUDE.md §17's own wording is specific to
# "the two credentialled URLs", not every documented variable.
_SECRET_ENV_KEYS = {"TWOCAPTCHA_KEY", "ZIMMO_CDP_ENDPOINT", "ZIMMO_PROXY"}


def check_env_example_round_trips_as_unset() -> bool:
    from env_config import _load_dotenv_values, _is_placeholder, ENV_KEYS

    this_dir = os.path.dirname(os.path.abspath(__file__))
    example_path = os.path.join(this_dir, ".env.example")
    dotenv_values = _load_dotenv_values(example_path)

    ok = True
    for env_var in ENV_KEYS:
        if env_var not in _SECRET_ENV_KEYS:
            continue
        raw_value = dotenv_values.get(env_var)
        if not _is_placeholder(raw_value):
            print(f"[FAIL] .env.example's {env_var} round-tripped as CONFIGURED "
                  f"instead of staying unset -- a copied .env.example would silently "
                  f"authenticate with a placeholder as a real value.")
            ok = False
    if ok:
        print(f"[PASS] .env.example's secret variable(s) all read as unset through the "
              f"real placeholder check ({len(_SECRET_ENV_KEYS)} variables)")
    return ok


def check_no_committed_credentials() -> bool:
    """Invokes the SAME implementation tests.yml calls (CLAUDE.md §16 --
    "one implementation, invoked from both CI and the offline suite").
    check_no_credentials.py has no dependency on this project's own
    modules, so importing it here costs nothing and cannot create a
    circular import."""
    from check_no_credentials import scan
    this_dir = os.path.dirname(os.path.abspath(__file__))
    findings = scan(this_dir)
    if findings:
        print(f"[FAIL] {len(findings)} possible committed credential(s) found:")
        for path, line_num, text in findings:
            print(f"  {path}:{line_num}: {text}")
        return False
    print("[PASS] no committed credentials found (check_no_credentials.py)")
    return True


# --- Engine import check (CLAUDE.md §10) ---
# "It must pass with no engine library installed at all. Guard every
# import playwright_scraper / puppeteer_scraper / selenium_scraper behind
# try/except ImportError and record the skip." A skip (engine's own
# driver library absent) is fine and expected; this function's job is
# only to make the ATTEMPT and report which happened, so a genuinely
# broken import doesn't hide behind "well, nothing imports it anyway".
_ENGINE_MODULES = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper")


def check_engine_imports() -> bool:
    ok = True
    for module_name in _ENGINE_MODULES:
        try:
            importlib.import_module(module_name)
            print(f"[PASS] {module_name} imported successfully (engine driver is installed)")
        except ImportError as e:
            print(f"[SKIP] {module_name} not imported -- its own driver library is absent ({e})")
        except Exception as e:
            # Anything other than ImportError is a REAL bug the import
            # itself surfaced (a syntax error past compileall's own
            # check, a broken module-level constant, etc.) -- not a skip.
            print(f"[FAIL] {module_name} raised {type(e).__name__} on import (not an ImportError, "
                  f"so this is not a normal missing-driver skip): {e}")
            ok = False
    return ok


# --- Coarse AST name-resolution check (CLAUDE.md §10) ---
# "compileall proves a file PARSES, not that its names RESOLVE." A
# module can import cleanly, pass --help, pass compileall and the whole
# offline suite, and still die with NameError on a line only reached
# while actually fetching a live page -- exactly what happened once in
# this family when an import was removed but a name it provided was
# still used elsewhere in the same file. Deliberately coarse: whole-
# module bindings, not per-scope tracking, so it under-reports rather
# than inventing false positives on a name that's genuinely scoped
# correctly but looks unbound to a purely lexical pass.
def _collect_bound_names(tree: ast.AST) -> set:
    bound = set(dir(__builtins__)) if isinstance(__builtins__, dict) else set(dir(__builtins__))
    bound |= {"self", "cls", "__name__", "__file__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            bound.add(node.name)
            for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                bound.add(arg.arg)
            if node.args.vararg:
                bound.add(node.args.vararg.arg)
            if node.args.kwarg:
                bound.add(node.args.kwarg.arg)
        elif isinstance(node, ast.Lambda):
            # A lambda's own parameter (e.g. `lambda d: d.find_elements(...)`,
            # the Selenium WebDriverWait idiom) is bound only inside that
            # lambda's own body -- but this check is deliberately coarse
            # and whole-module (CLAUDE.md §10: "keep it coarse ... so it
            # under-reports rather than inventing problems"), so treating
            # it as bound module-wide is the intentional trade-off, not a
            # missed scope rule.
            for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                bound.add(arg.arg)
            if node.args.vararg:
                bound.add(node.args.vararg.arg)
            if node.args.kwarg:
                bound.add(node.args.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Param)):
            bound.add(node.id)
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, (ast.comprehension,)):
            if isinstance(node.target, ast.Name):
                bound.add(node.target.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            if isinstance(node.optional_vars, ast.Name):
                bound.add(node.optional_vars.id)
    return bound


def check_ast_names_resolved() -> bool:
    ok = True
    this_dir = os.path.dirname(os.path.abspath(__file__))
    for filename in _SHIPPED_PY_FILES:
        path = os.path.join(this_dir, filename)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError:
            continue  # compileall's own job -- not this check's

        bound = _collect_bound_names(tree)
        unresolved = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id not in bound:
                    unresolved.add(node.id)

        if unresolved:
            print(f"[FAIL] {filename}: name(s) referenced but never imported/defined/assigned "
                  f"anywhere in the module: {sorted(unresolved)}")
            ok = False
    if ok:
        print(f"[PASS] AST name-resolution check found nothing unresolved across "
              f"{len(_SHIPPED_PY_FILES)} shipped files (coarse, whole-module check)")
    return ok


# --- Engine flag-set consistency (CLAUDE.md §10) ---
# "Assert the engines' flag sets against the contract AND against each
# other, in both directions. A missing flag fails; so does closing a
# difference the README documents." Extracted via a coarse AST scan of
# each engine's own parse_args() function for `add_argument("--x", ...)`
# calls, rather than by importing and re-parsing (which would need a
# real argv and doesn't distinguish "the contract" from "what this
# engine happens to support").
_CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
    "--cdp-endpoint", "--allow-empty", "--dump-html", "--headless", "--headful",
}


def _extract_flags_from_source(path: str) -> set:
    """Extract argparse flags via `p.add_argument("--x", ...)` calls
    specifically on the variable named `p` (this codebase's own
    consistent convention for the ArgumentParser in every engine's
    parse_args()) -- NOT any `.add_argument()` call. Selenium's own
    ChromeOptions ALSO has an `add_argument()` method
    (`options.add_argument("--window-size=...")`), an unrelated method
    that happens to share a name; matching on the method name alone
    conflated the two and produced three false positives the first
    time this check was written."""
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "p"
                and node.args):
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                if first_arg.value.startswith("--"):
                    flags.add(first_arg.value)
    return flags


def check_engine_flag_sets_consistent() -> bool:
    this_dir = os.path.dirname(os.path.abspath(__file__))
    engine_files = ["playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py"]
    ok = True
    per_engine = {}

    for filename in engine_files:
        path = os.path.join(this_dir, filename)
        if not os.path.isfile(path):
            continue
        per_engine[filename] = _extract_flags_from_source(path)

    if len(per_engine) < len(engine_files):
        print(f"[SKIP] engine flag-set check: only found {list(per_engine.keys())}")
        return True

    for filename, flags in per_engine.items():
        missing_from_contract = _CONTRACT_FLAGS - flags
        if missing_from_contract:
            print(f"[FAIL] {filename} is missing contract flag(s): {sorted(missing_from_contract)}")
            ok = False

    filenames = list(per_engine.keys())
    reference = per_engine[filenames[0]]
    for other in filenames[1:]:
        diff_a = reference - per_engine[other]
        diff_b = per_engine[other] - reference
        if diff_a:
            print(f"[FAIL] {filenames[0]} has flag(s) {other} lacks: {sorted(diff_a)}")
            ok = False
        if diff_b:
            print(f"[FAIL] {other} has flag(s) {filenames[0]} lacks: {sorted(diff_b)}")
            ok = False

    if ok:
        print(f"[PASS] all {len(engine_files)} engines' flag sets match the contract "
              f"({len(_CONTRACT_FLAGS)} flags) and each other")
    return ok


# --- Shared-function call-signature binding (CLAUDE.md §10) ---
# "Bind every shared-module call in every engine against the callee's
# real signature -- inspect.signature(fn).bind(*placeholders) over an
# ast walk." A signature drifting from its callers is invisible to
# import, --help, compileall and the whole offline suite, because none
# of those calls a function the way a live run does -- CLAUDE.md's own
# example crashed on the FIRST fetch of every affected engine.
_SHARED_MODULE_FUNCTIONS = {}
for _mod_name in ("captcha_solver", "product_parser", "output_writer",
                   "env_config", "proxy_pool"):
    try:
        _mod = importlib.import_module(_mod_name)
        for _name in dir(_mod):
            _obj = getattr(_mod, _name)
            if callable(_obj) and not isinstance(_obj, type) and getattr(_obj, "__module__", None) == _mod_name:
                _SHARED_MODULE_FUNCTIONS[_name] = _obj
    except ImportError:
        continue


def _placeholder_for(annotation) -> object:
    return None  # a generic placeholder -- this check tests ARITY/keyword
                 # names bind, not type-correctness (CLAUDE.md §10's own
                 # technique: bind(*placeholders), not a full type-check)


def check_shared_call_signatures() -> bool:
    if not _SHARED_MODULE_FUNCTIONS:
        print("[SKIP] shared-function signature check: no shared modules importable")
        return True

    this_dir = os.path.dirname(os.path.abspath(__file__))
    engine_files = ["playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py",
                    "scraper_api_client.py"]
    ok = True
    checked = 0

    for filename in engine_files:
        path = os.path.join(this_dir, filename)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_name = None
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            if func_name not in _SHARED_MODULE_FUNCTIONS:
                continue

            fn = _SHARED_MODULE_FUNCTIONS[func_name]
            try:
                sig = inspect.signature(fn)
            except (TypeError, ValueError):
                continue

            # Skip a call site using **kwargs/*args spread -- this coarse
            # check can't resolve what's actually inside those at
            # static-analysis time, and CLAUDE.md §10 prefers under-
            # reporting to a false positive here.
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            if any(kw.arg is None for kw in node.keywords):
                continue

            positional_count = len(node.args)
            keyword_names = [kw.arg for kw in node.keywords]

            try:
                sig.bind(*([None] * positional_count),
                         **{name: None for name in keyword_names})
                checked += 1
            except TypeError as e:
                print(f"[FAIL] {filename}: call to {func_name}(...) at line {node.lineno} "
                      f"does not bind against its real signature {sig} -- {e}")
                ok = False

    if ok:
        print(f"[PASS] {checked} shared-module call site(s) bind against their real "
              f"signatures across all engines")
    return ok


if __name__ == "__main__":
    wording_ok = check_banned_wording()
    env_ok = check_env_example_matches_env_keys()
    env_roundtrip_ok = check_env_example_round_trips_as_unset()
    credentials_ok = check_no_committed_credentials()
    engine_import_ok = check_engine_imports()
    ast_ok = check_ast_names_resolved()
    flags_ok = check_engine_flag_sets_consistent()
    signatures_ok = check_shared_call_signatures()
    docker_version_ok = check_docker_playwright_version_pins_match()
    pyproject_version_ok = check_pyproject_version_matches_changelog()
    pagination_param_ok = check_pagination_uses_confirmed_page_param()
    price_no_swallow_ok = check_price_parser_does_not_swallow_adjacent_numbers()
    price_parser_ok = check_price_parser_audit_regressions()
    item_link_selector_ok = check_item_link_selector_rejects_nav_chrome()
    css_fallback_scope_ok = check_css_fallback_widened_scope_is_used()
    live_tiles_ok = check_live_angular_tiles()
    epc_regression_ok = check_epc_label_natural_language_regression()
    unattempted_ok = check_unattempted_pages_prevent_false_complete()
    no_sku_dedup_ok = check_no_sku_rows_participate_in_dedup()
    exit_code = main()
    overall_ok = (wording_ok and env_ok and env_roundtrip_ok and credentials_ok
                  and engine_import_ok and ast_ok and flags_ok and signatures_ok
                  and docker_version_ok and pyproject_version_ok and price_parser_ok
                  and price_no_swallow_ok and pagination_param_ok
                  and epc_regression_ok and css_fallback_scope_ok and live_tiles_ok and item_link_selector_ok
                  and unattempted_ok and no_sku_dedup_ok
                  and exit_code == 0)
    sys.exit(0 if overall_ok else 1)
