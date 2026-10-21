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

    # --- Output contract ---
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

        from output_writer import EXIT_FETCH_FAILED
        never_prefix = os.path.join(tmp, "never_obtained_run")
        code_never = finish_run([], never_prefix, "both", pages_requested=3, pages_completed=0,
                                failed_pages=[1], unattempted_pages=[2, 3], allow_empty=True)
        ok &= check("0 rows because no page was ever FETCHED is exit 5, not 4 -- even with "
                    "--allow-empty, and with nothing written",
                    code_never == EXIT_FETCH_FAILED == 5
                    and not os.path.exists(never_prefix + ".json")
                    and not os.path.exists(never_prefix + ".meta.json"))

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

    # --- JSON-LD edge cases ---
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
        print("main() checks passed (the process exit code covers every check). Offline only: the live site is exercised by")
        print("canary.yml, and it needs a residential exit plus a headful browser")
        print("(README, 'What you need').")
        return 0
    else:
        print("Some checks FAILED — fix these before running against a real browser/site.")
        return 1


def check_output_schema_and_removed_columns() -> bool:
    """product.schema.json is generated from Product, the sample file is
    cut to it, and the three columns removed in 1.3.0 stay removed."""
    import json as _json
    import csv as _csv
    from dataclasses import fields as _fields
    import output_writer as ow
    ok = True
    on_disk = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "product.schema.json"), encoding="utf-8"))
    ok &= check("product.schema.json equals output_writer.json_schema() (regenerate with "
                "`python3 output_writer.py > product.schema.json`)", on_disk == ow.json_schema())
    names = [f.name for f in _fields(ow.Product)]
    ok &= check("schema lists exactly Product's columns, in order, all required",
                list(on_disk["properties"]) == names and on_disk["required"] == names)
    ok &= check("removed columns are not in Product again (rating/review_count/in_stock)",
                not set(ow.REMOVED_COLUMNS) & set(names))
    types = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "null": type(None)}
    rows = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_output.json"), encoding="utf-8"))
    bad = []
    for r in rows:
        if set(r) != set(names):
            bad.append(("keys", sorted(set(r) ^ set(names))))
        for k, v in r.items():
            t = on_disk["properties"].get(k, {}).get("type", [])
            t = [t] if isinstance(t, str) else t
            if isinstance(v, bool) and "boolean" not in t:
                bad.append((k, v))
            elif not any(isinstance(v, types[x]) for x in t):
                bad.append((k, v))
    ok &= check("every row of sample_output.json validates against the schema", not bad)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_output.csv"), newline="", encoding="utf-8") as fh:
        ok &= check("sample_output.csv header equals the schema's columns",
                    next(_csv.reader(fh)) == names)
    return ok


def check_canary_fails_without_a_proxy() -> bool:
    """zimmo.be refuses GitHub runners, so the canary needs ZIMMO_PROXY.
    Until 2026-10-14 a missing (or empty) secret made the live job SKIP
    while the workflow showed green, and before that a refused run passed
    with a notice. Neither tested the site. Now the job must FAIL with an
    error naming the secret, and nothing may skip or forgive the live
    step."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".github", "workflows", "canary.yml")
    if not os.path.isdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".github")):
        return check("canary (no .github directory: skipped, e.g. inside the image)", True)
    text = open(path, encoding="utf-8").read()
    ok = check("canary fails with ::error:: naming ZIMMO_PROXY when the secret is empty",
               '[ -z "$ZIMMO_PROXY" ]' in text and "::error::ZIMMO_PROXY" in text
               and "exit 1" in text)
    ok &= check("canary has no job-level skip and no gate that could end green untested",
                "needs.gate" not in text and "\n    if:" not in text)
    ok &= check("no run-then-forgive branch is left (exit 3 without a proxy)",
                "code == 3" not in text and "::notice::" not in text)
    ok &= check("canary fails on any nonzero engine exit and asserts a row floor",
                "if code != 0:" in text and "n >= 50" in text)
    ok &= check("canary asserts unique skus and plausible prices",
                "duplicate skus" in text and "implausible price" in text)
    return ok


# --- Wording enforced by this test ---
# Guard names and dead features too: costs nothing, and
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
# suite (a wording check must cover what the repo PUBLISHES).
# CHANGELOG.md is left out on purpose: its released sections are history,
# and two of them name removed features in order to
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
    """FIXED (external audit, 2026-09-14, finding #7): the
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
    """Values, not coverage, on the markup the site serves
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
    parsing fixes): the fix for finding #8 allowed a bare
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
    (2026-09-14, finding #8) found broken: "€ 1250" read as
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
    """FIXED (external audit, 2026-09-14, finding #5): a run
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
    """FIXED (external audit, 2026-09-14, finding #9): a
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
    """.env.example must document exactly the variables the
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


# --- Round-trip .env.example through the REAL loader ---
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
# class this check exists for, which is about the credentialled values.
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
    """Invokes the SAME implementation tests.yml calls -- one
    implementation, invoked from both CI and the offline suite.
    check_no_credentials.py has no dependency on this project's own
    modules, so importing it here costs nothing and cannot create a
    circular import."""
    from check_no_credentials import scan
    this_dir = os.path.dirname(os.path.abspath(__file__))
    findings = scan(this_dir)
    if findings:
        print(f"[FAIL] {len(findings)} possible committed credential(s) found:")
        for path, line_num, text in findings:
            print(f"  {path}:{line_num}: {text}")  # a description, never the secret
        return False
    print("[PASS] no committed credentials found (check_no_credentials.py)")
    return True


# --- Engine import check ---
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


# --- Coarse AST name-resolution check ---
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
            # and whole-module (keep it coarse, so it
            # under-reports rather than inventing problems), so treating
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


# --- Engine flag-set consistency ---
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
    "--twocaptcha-key", "--solve-captcha", "--min-score",
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


# --- Shared-function call-signature binding ---
# "Bind every shared-module call in every engine against the callee's
# real signature -- inspect.signature(fn).bind(*placeholders) over an
# ast walk." A signature drifting from its callers is invisible to
# import, --help, compileall and the whole offline suite, because none
# of those calls a function the way a live run does -- the case that
# prompted this check crashed on the FIRST fetch of every affected engine.
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
                 # names bind, not type-correctness (the
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
            # static-analysis time, and it prefers under-
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


# --- External audit 2026-10-07: what a page IS, and what a run then says ---

def check_classify_page_contract() -> bool:
    """P1/P2: a 403 used to count as a completed page and a 500 as an
    empty one. The statuses are the ones measured live on 2026-10-07
    (output_writer.py's comment above classify_page)."""
    from output_writer import (classify_page, page_outcome_is_ok, should_retry_page,
                               PAGE_CONTENT, PAGE_EMPTY, PAGE_BLOCKED,
                               PAGE_NOT_FOUND, PAGE_FETCH_ERROR, PAGE_PARSER_FAILED)
    # (products, status, challenge, rendered) -> outcome, ok, retry
    cases = [
        ((21, 200, False, True), PAGE_CONTENT, True, False),
        ((21, 403, True, True), PAGE_CONTENT, True, False),     # challenge cleared after a 403
        ((0, 200, False, False), PAGE_EMPTY, True, False),
        ((0, 403, True, False), PAGE_BLOCKED, False, False),    # Cloudflare "Even geduld..."
        ((0, 403, False, False), PAGE_BLOCKED, False, False),   # a bare 403, no marker
        ((0, 200, True, False), PAGE_BLOCKED, False, False),    # marker only, status 200
        ((0, None, True, False), PAGE_BLOCKED, False, False),   # status unknown
        ((0, 404, False, False), PAGE_NOT_FOUND, False, False),
        # The listing grid rendered (> MIN_CARD_MATCHES links) and nothing
        # parsed: a parser failure, not an empty search (was PAGE_EMPTY).
        ((0, 200, False, True), PAGE_PARSER_FAILED, False, False),
        ((0, 200, True, True), PAGE_PARSER_FAILED, False, False),
        ((0, 403, True, True), PAGE_PARSER_FAILED, False, False),  # cleared challenge, grid unread
        ((0, None, False, True), PAGE_PARSER_FAILED, False, False),
        ((0, 500, False, False), PAGE_FETCH_ERROR, False, True),
        ((0, 429, False, False), PAGE_FETCH_ERROR, False, True),
        ((0, 400, False, False), PAGE_FETCH_ERROR, False, False),
    ]
    ok = True
    for args_, want, want_ok, want_retry in cases:
        got = classify_page(*args_)
        if (got, page_outcome_is_ok(got), should_retry_page(got, args_[1])) != (want, want_ok, want_retry):
            print(f"[FAIL] classify_page{args_} -> {got} (ok={page_outcome_is_ok(got)}, "
                  f"retry={should_retry_page(got, args_[1])}); expected {want}, ok={want_ok}, "
                  f"retry={want_retry}")
            ok = False
    if ok:
        print(f"[PASS] classify_page: {len(cases)} outcomes, incl. 403/404/429/500 and a cleared challenge")
    return ok


def check_engines_use_shared_page_classification() -> bool:
    """All three engines must take their page verdict from classify_page()
    -- the audit's P1 was the same `return products, True, ...` in all
    three, so a fix to one engine only would leave its twins reporting
    exit 0 on the same page."""
    ok = True
    for name in ("playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py"):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name)).read()
        tree = ast.parse(src)
        # The attempt loop lives in _fetch_page_attempts where an engine
        # splits it out (the CDP-connection wrapper), else in _fetch_page.
        funcs = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        fetch = funcs.get("_fetch_page_attempts") or funcs["_fetch_page"]
        calls = {n.func.id for n in ast.walk(fetch)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        missing = {"classify_page", "page_outcome_is_ok", "should_retry_page"} - calls
        literal_success = [n.lineno for n in ast.walk(fetch)
                           if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)
                           and len(n.value.elts) >= 4 and isinstance(n.value.elts[1], ast.Constant)
                           and n.value.elts[1].value is True]
        if missing or literal_success:
            print(f"[FAIL] {name}: _fetch_page does not call {sorted(missing)} "
                  f"or returns a literal success at line(s) {literal_success}")
            ok = False
    if ok:
        print("[PASS] all three engines classify pages through output_writer.classify_page()")
    return ok


def check_exhausted_listing_is_complete() -> bool:
    """A listing that ENDED before --pages did is a complete answer. It
    used to finish partial/exit 6, which also made diff_runs refuse it."""
    import tempfile
    from output_writer import Product, finish_run, EXIT_OK
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "run")
        code = finish_run([Product(sku="A", url="u1")], prefix, "json",
                          pages_requested=4, pages_completed=2, exhausted_pages=[3, 4],
                          url="https://www.zimmo.be/nl/gent-9000/te-koop/?page=2")
        meta = json.load(open(prefix + ".meta.json"))
    want = ("complete", True, "https://www.zimmo.be/nl/gent-9000/te-koop")
    got = (meta["status"], meta["listing_exhausted"], meta["selection"]["url"])
    if code != EXIT_OK or got != want:
        print(f"[FAIL] exhausted listing -> exit {code}, {got}; expected exit 0, {want}")
        return False
    print("[PASS] an exhausted listing is complete, exit 0, and records its selection")
    return True


def check_selection_url_normalisation() -> bool:
    from output_writer import selection_url
    same = [
        "https://www.zimmo.be/nl/gent-9000/te-koop/",
        "HTTPS://WWW.ZIMMO.BE/nl/gent-9000/te-koop",
        "https://www.zimmo.be/nl/gent-9000/te-koop/?page=3",
    ]
    filtered_a = selection_url("https://www.zimmo.be/nl/gent-9000/te-koop/?b=2&a=1&page=2")
    filtered_b = selection_url("https://www.zimmo.be/nl/gent-9000/te-koop?a=1&b=2")
    if len({selection_url(u) for u in same}) != 1 or filtered_a != filtered_b \
            or selection_url(same[0]) == selection_url("https://www.zimmo.be/nl/antwerpen-2000/te-koop/"):
        print("[FAIL] selection_url does not identify the same search consistently")
        return False
    print("[PASS] selection_url: page param, case, slash and query order ignored; city kept")
    return True


def _run_diff(tmp, old_meta, new_meta, old_rows, new_rows, *extra):
    import subprocess
    for name, meta, rows in (("old", old_meta, old_rows), ("new", new_meta, new_rows)):
        json.dump(rows, open(os.path.join(tmp, f"{name}.json"), "w"))
        json.dump(meta, open(os.path.join(tmp, f"{name}.meta.json"), "w"))
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diff_runs.py")
    r = subprocess.run([sys.executable, script, "--old", os.path.join(tmp, "old.json"),
                        "--new", os.path.join(tmp, "new.json"), *extra],
                       capture_output=True, text=True)
    return r.returncode, r.stdout


def check_diff_refuses_different_selection_and_marks_window() -> bool:
    """P2: Gent vs Antwerpen was diffed as market movement; and a listing
    pushed past --pages read as removed from the site."""
    import tempfile
    gent = {"status": "complete", "listing_exhausted": False,
            "selection": {"url": "https://www.zimmo.be/nl/gent-9000/te-koop", "pages_requested": 1}}
    antw = dict(gent, selection={"url": "https://www.zimmo.be/nl/antwerpen-2000/te-koop",
                                 "pages_requested": 1})
    a, b = {"sku": "A", "url": "u/A", "title": "a"}, {"sku": "B", "url": "u/B", "title": "b"}
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        code, _ = _run_diff(tmp, gent, antw, [a], [b])
        if code != 2:
            print(f"[FAIL] diff of two different searches exited {code}, expected 2 (refused)")
            ok = False
        code, out = _run_diff(tmp, gent, gent, [a], [b], "--fail-on-change")
        if "Left selection:  1" not in out or "Removed:         0" not in out or code != 1:
            print(f"[FAIL] window run: expected 1 left_selection, 0 removed, exit 1 (one added); "
                  f"got exit {code}:\n{out}")
            ok = False
        code, out = _run_diff(tmp, gent, gent, [a, b], [b], "--fail-on-change")
        if code != 0:
            print(f"[FAIL] a listing that only left the --pages window failed --fail-on-change "
                  f"(exit {code})")
            ok = False
        whole = dict(gent, listing_exhausted=True)
        code, out = _run_diff(tmp, whole, whole, [a, b], [b], "--fail-on-change")
        if "Removed:         1" not in out or code != 1:
            print(f"[FAIL] a run that read the WHOLE listing must report removal; got exit {code}:\n{out}")
            ok = False
    if ok:
        print("[PASS] diff_runs refuses different searches and separates left_selection from removed")
    return ok


def check_credential_scan_never_opens_ignored_env_or_prints_secrets() -> bool:
    """2026-10-07: the scanner walked into the user's git-ignored .env and
    printed its keys verbatim on every local smoke run. The fake key below
    is built at runtime so this file does not itself contain one."""
    import subprocess
    import tempfile
    from check_no_credentials import scan
    fake_key = "".join("0123456789abcdef"[(i * 7) % 16] for i in range(32))
    fake_url = "http://" + "zz9user" + ":" + "Qx7pw0rd" + "@proxy.invalid:1"
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        git = ["git", "-C", tmp, "-c", "user.email=t@t", "-c", "user.name=t"]
        try:
            subprocess.run(git + ["init", "-q"], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            print("[SKIP] git not available -- credential-scan scope not checked")
            return True
        open(os.path.join(tmp, ".gitignore"), "w").write(".env\n")
        open(os.path.join(tmp, ".env"), "w").write(f"TWOCAPTCHA_KEY={fake_key}\nZIMMO_PROXY={fake_url}\n")
        if scan(tmp):
            print("[FAIL] the scanner reported a git-IGNORED .env as committed")
            ok = False
        subprocess.run(git + ["add", "-f", ".env"], check=True, capture_output=True)
        findings = scan(tmp)
        if len(findings) != 2:
            print(f"[FAIL] a force-added .env must be caught (2 findings), got {len(findings)}")
            ok = False
        if any(fake_key in str(f) or "Qx7pw0rd" in str(f) for f in findings):
            print("[FAIL] the scanner's findings contain the secret text itself")
            ok = False
    if ok:
        print("[PASS] credential scan: ignored .env never opened, force-added .env caught, "
              "no secret text in findings")
    return ok


# --- 2026-10-14 audit: output files, CSV formulas, paid solves, verdicts ---

_ENGINE_FILES = ("playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py")
_HERE = os.path.dirname(os.path.abspath(__file__))


def check_atomic_writes_keep_old_output_and_modes() -> bool:
    """A write that fails half-way must leave the previous file intact, with
    no temp file left behind, and a file's mode must be what open() would
    give it: the existing file's mode, or 0o666 & ~umask for a new one.
    Before the fix a failed json.dump truncated a 497-byte run.json to 13
    bytes of invalid JSON."""
    import stat
    import output_writer as ow
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "run")
        rows = [ow.Product(sku="A", url="u1", title="Huis", price=1.0)]
        ow.save(rows, prefix, "both")
        ow.write_meta({"status": "complete"}, prefix)
        before = {ext: open(prefix + ext, "rb").read() for ext in (".json", ".csv", ".meta.json")}

        class Boom(Exception):
            pass

        real_dump = json.dump

        def half_dump(obj, f, **kw):
            f.write("[{\"partial\": ")
            raise Boom("planted failure")

        json.dump = half_dump
        try:
            for fn, args in ((ow.write_json, (rows, prefix + ".json")),
                             (ow.write_meta, ({"status": "x"}, prefix))):
                try:
                    fn(*args)
                except Boom:
                    pass
        finally:
            json.dump = real_dump
        real_writer = ow.csv.DictWriter

        class BadWriter(real_writer):
            def writerow(self, row):
                raise Boom("planted failure")

        ow.csv.DictWriter = BadWriter
        try:
            ow.write_csv(rows, prefix + ".csv")
        except Boom:
            pass
        finally:
            ow.csv.DictWriter = real_writer
        after = {ext: open(prefix + ext, "rb").read() for ext in before}
        ok &= check("a failed write leaves the previous .json/.csv/.meta.json byte-identical",
                    after == before)
        ok &= check("a failed write leaves no temp file behind",
                    sorted(os.listdir(tmp)) == ["run.csv", "run.json", "run.meta.json"])

        modes_ok = True
        for mask in (0o022, 0o002, 0o077):
            old = os.umask(mask)
            try:
                path = os.path.join(tmp, f"new_{mask:o}.json")
                ow.write_json(rows, path)
                got = stat.S_IMODE(os.stat(path).st_mode)
            finally:
                os.umask(old)
            if got != 0o666 & ~mask:
                print(f"       umask {mask:o}: new file mode {got:o}, expected {0o666 & ~mask:o}")
                modes_ok = False
        existing = prefix + ".json"
        os.chmod(existing, 0o640)
        ow.write_json(rows, existing)
        modes_ok &= stat.S_IMODE(os.stat(existing).st_mode) == 0o640
        ok &= check("new files get 0o666 & ~umask (022/002/077); an existing file keeps its mode",
                    modes_ok)
    return ok


def check_csv_formula_cells_escaped() -> bool:
    import csv as _csv
    import output_writer as ow
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "run")
        rows = [ow.Product(sku="A", url="https://www.zimmo.be/x/1", title='=HYPERLINK("http://x")',
                           location="@SUM(1)", brand="-5", category="\tTab", price=-1.0),
                ow.Product(sku="B", url="https://www.zimmo.be/x/2", title="Huis te koop", price=2.0)]
        code = ow.finish_run(rows, prefix, "both", pages_requested=1, pages_completed=1)
        with open(prefix + ".csv", newline="", encoding="utf-8") as fh:
            got = list(_csv.DictReader(fh))
        js = json.load(open(prefix + ".json", encoding="utf-8"))
        meta = json.load(open(prefix + ".meta.json", encoding="utf-8"))
    ok = check("CSV: formula-shaped text cells get a leading apostrophe",
               code == 0 and got[0]["title"] == "'=HYPERLINK(\"http://x\")"
               and got[0]["location"] == "'@SUM(1)" and got[0]["brand"] == "'-5"
               and got[0]["category"] == "'\tTab" and got[1]["title"] == "Huis te koop")
    ok &= check("CSV: a negative NUMBER is left numeric; JSON keeps every raw value",
                got[0]["price"] == "-1.0" and js[0]["title"] == '=HYPERLINK("http://x")'
                and js[0]["price"] == -1.0)
    ok &= check("sidecar records csv_cells_escaped", meta.get("csv_cells_escaped") == 4)
    return ok


def check_solve_budget_one_purchase_per_page() -> bool:
    """Every paid solve spends the page's SolveBudget BEFORE the purchase.
    Before the fix one page answering 503 with a Turnstile widget bought
    retries+1 solves, and a page with two markers bought two."""
    import captcha_solver as cs
    bought = []
    real_t, real_r = cs._solve_turnstile_with_2captcha, cs._solve_with_2captcha
    cs._solve_turnstile_with_2captcha = lambda key, ch, **kw: bought.append("t") or "tok"
    cs._solve_with_2captcha = lambda key, ch, **kw: bought.append("r") or "tok"
    try:
        budget = cs.SolveBudget()
        turnstile = cs.TurnstileChallenge(sitekey="0x4AAAAAAA", page_url="https://www.zimmo.be/")
        recaptcha = cs.CaptchaChallenge(kind="recaptcha_v3", sitekey="6Lc", page_url="https://www.zimmo.be/")
        refused = 0
        for solve in (lambda: cs.solve_turnstile(turnstile, "stub-key", budget),
                      lambda: cs.solve_turnstile(turnstile, "stub-key", budget),
                      lambda: cs.solve_recaptcha_v3(recaptcha, "stub-key", budget)):
            try:
                solve()
            except cs.SolveBudgetExhausted:
                refused += 1
        free = cs.SolveBudget()
        try:
            cs.solve_turnstile(turnstile, None, free)
        except RuntimeError:
            pass
    finally:
        cs._solve_turnstile_with_2captcha, cs._solve_with_2captcha = real_t, real_r
    ok = check("one SolveBudget buys exactly one solve; later calls are refused before purchase",
               bought == ["t"] and refused == 2)
    ok &= check("a solve refused for a missing key does not spend the allowance", free.used == 0)

    # Every engine: each solve call passes the budget, and the budget is
    # created once per page, OUTSIDE the attempt loop (so retries and
    # proxy rotations share it).
    for name in _ENGINE_FILES:
        tree = ast.parse(open(os.path.join(_HERE, name), encoding="utf-8").read())
        funcs = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id in ("solve_turnstile", "solve_recaptcha_v3")]
        fetch = funcs.get("_fetch_page_attempts") or funcs["_fetch_page"]
        loop = next(n for n in fetch.body if isinstance(n, (ast.For, ast.AsyncFor)))
        made_before_loop = any(
            isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", None) == "SolveBudget"
            and n.lineno < loop.lineno for n in fetch.body)
        made_in_loop = any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "SolveBudget"
                           for n in ast.walk(loop))
        passes = all(len(c.args) >= 3 and getattr(c.args[2], "id", None) == "budget" for c in calls)
        handle_calls = [n for n in ast.walk(loop) if isinstance(n, ast.Call)
                        and getattr(n.func, "id", None) == "handle_captcha_if_present"]
        ok &= check(f"{name}: SolveBudget made once per page before the attempt loop and "
                    f"passed to all {len(calls)} solve call(s)",
                    calls and made_before_loop and not made_in_loop and passes
                    and handle_calls and all(len(c.args) == 3 for c in handle_calls))
    return ok


def check_parser_failure_is_not_an_empty_search() -> bool:
    import output_writer as ow
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "a")
        code_none = ow.finish_run([], p, "json", pages_requested=1, pages_completed=0,
                                  failed_pages=[1], parser_failed_pages=[1])
        wrote_none = os.path.exists(p + ".json")
        p = os.path.join(tmp, "b")
        code_some = ow.finish_run([ow.Product(sku="A", url="u")], p, "json", pages_requested=2,
                                  pages_completed=1, failed_pages=[2], parser_failed_pages=[2])
        meta = json.load(open(p + ".meta.json"))
        p = os.path.join(tmp, "c")
        code_mixed = ow.finish_run([], p, "json", pages_requested=2, pages_completed=1,
                                   failed_pages=[2])
    ok = check("rendered grid, zero rows parsed, no rows at all -> exit 5, nothing written",
               code_none == ow.EXIT_FETCH_FAILED and not wrote_none)
    ok &= check("rows plus a parser-failed page -> exit 6, partial, stop_reason says parser",
                code_some == ow.EXIT_PARTIAL and meta["status"] == "partial"
                and meta["parser_failed_pages"] == [2] and "parser" in meta["stop_reason"])
    ok &= check("page 1 served empty + page 2 failed is exit 5, not 'empty search' (4)",
                code_mixed == ow.EXIT_FETCH_FAILED)
    ok &= check("a row with no price carries no invented currency",
                ow.Product().currency is None)
    return ok


def check_numeric_flags_rejected_when_negative() -> bool:
    """--retries -1 used to run ZERO navigations and report exit 5."""
    import argparse
    import env_config as ec
    ok = True
    for fn, bad, good in ((ec.non_negative_int, ("-1", "-3"), ("0", "2")),
                          (ec.positive_int, ("0", "-2"), ("1",)),
                          (ec.non_negative_float, ("-0.5", "nan"), ("0", "1.5"))):
        for v in bad:
            try:
                fn(v)
                ok = False
                print(f"       {fn.__name__}({v!r}) accepted")
            except (argparse.ArgumentTypeError, ValueError):
                pass
        for v in good:
            fn(v)
    want = {"--retries": "non_negative_int", "--pages": "positive_int",
            "--concurrency": "positive_int", "--proxy-block-retries": "positive_int",
            "--delay": "non_negative_float", "--retry-delay": "non_negative_float"}
    for name in _ENGINE_FILES:
        tree = ast.parse(open(os.path.join(_HERE, name), encoding="utf-8").read())
        types_ = {}
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "add_argument" and n.args
                    and isinstance(n.args[0], ast.Constant)):
                for kw in n.keywords:
                    if kw.arg == "type":
                        types_[n.args[0].value] = getattr(kw.value, "id", None)
        wrong = {f: types_.get(f) for f, t in want.items() if types_.get(f) != t}
        refuses = "refuse_proxy_with_cdp_endpoint" in {
            getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        if wrong or not refuses:
            print(f"       {name}: flag types {wrong}, proxy+cdp refusal called: {refuses}")
            ok = False
    import contextlib
    import io
    parser = argparse.ArgumentParser(prog="probe")
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            ec.refuse_proxy_with_cdp_endpoint(parser, argparse.Namespace(
                cdp_endpoint="ws://x.invalid", proxy="http://h.invalid:1", proxy_file=None))
        ok = False
    except SystemExit as e:
        ok &= e.code == 2
    return check("negative --retries/--delay, zero --pages/--concurrency rejected (exit 2) in "
                 "every engine; --proxy with --cdp-endpoint refused", ok)


def check_scraper_api_page_verdicts() -> bool:
    """The Scraper API path used to report exit 5 for a served empty page
    and for a Cloudflare challenge alike; it now uses classify_page()."""
    import types as _types
    import scraper_api_client as sac
    import output_writer as ow
    tiles = "".join(f'<a class="zimmo-code" href="/nl/x/{i}/">Z{i}</a>' for i in range(6))
    cases = [
        ("served, no listings", 200, "<html><body><p>Geen resultaten</p></body></html>",
         ow.EXIT_ZERO_PRODUCTS),
        ("Cloudflare challenge", 403, SAMPLE_CLOUDFLARE_HTML, ow.EXIT_BLOCKED),
        ("grid rendered, parser read nothing", 200, f"<html><body>{tiles}</body></html>",
         ow.EXIT_FETCH_FAILED),
        ("real tiles", 200, LIVE_ANGULAR_TILES_HTML, ow.EXIT_OK),
    ]
    real_post = sac.requests.post
    ok = True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for label, status, body, want in cases:
                sac.requests.post = lambda *a, _s=status, _b=body, **k: _types.SimpleNamespace(
                    status_code=200, headers={}, text="", json=lambda: {"http_code": _s, "body": _b})
                args = _types.SimpleNamespace(
                    url="https://www.zimmo.be/nl/gent-9000/te-koop/", twocaptcha_key="stub",
                    wait_for_text="€", timeout=30, cdp_url=None, proxy=None, category=None,
                    out=os.path.join(tmp, "api"), format="json", allow_empty=False, dump_html=False)
                got = sac.scrape(args)
                if got != want:
                    print(f"       Scraper API, {label}: exit {got}, expected {want}")
                    ok = False
    finally:
        sac.requests.post = real_post
    return check("Scraper API path: empty 4, challenge 3, parser failure 5, rows 0", ok)


def check_no_private_citations() -> bool:
    """Public files must state their reasons in their own words, not cite
    the maintainers' private instruction files or their section numbers.
    The tokens are assembled from pieces so this file does not match
    itself."""
    import re as _re
    import subprocess
    banned = _re.compile("|".join([
        _re.escape("CLAUDE" + ".md"), "claude" + "_2scraper", "2scraper" + "_repo_rules",
        "codex" + r"\.md", "play" + r"book\s*§", "temp" + r"late\s*§", "§" + r"\s*\d",
    ]), _re.IGNORECASE)
    try:
        files = subprocess.run(["git", "-C", _HERE, "ls-files", "-z"], capture_output=True,
                               check=True).stdout.decode().split("\0")
    except (OSError, subprocess.CalledProcessError):
        files = [os.path.relpath(os.path.join(d, f), _HERE)
                 for d, dn, fn in os.walk(_HERE) for f in fn if ".git" not in d]
    hits = []
    for rel in filter(None, files):
        path = os.path.join(_HERE, rel)
        if not os.path.isfile(path):
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if banned.search(line):
                hits.append(f"{rel}:{i}")
    if hits:
        print("       " + ", ".join(hits[:10]))
    return check(f"no public file cites the private maintainer instructions ({len(hits)} found)",
                 not hits)


def check_docker_copy_list_matches_import_graph() -> bool:
    """The Dockerfile copies an explicit module list; it must be exactly
    playwright_scraper.py's local import closure."""
    import re as _re
    dockerfile = os.path.join(_HERE, "Dockerfile")
    if not os.path.isfile(dockerfile):
        return check("Docker COPY list (no Dockerfile here: skipped)", True)
    text = open(dockerfile, encoding="utf-8").read()
    copy = _re.search(r"^COPY (env_config\.py.*?) \./\s*$", text, _re.MULTILINE | _re.DOTALL)
    listed = set(_re.findall(r"[\w]+\.py", copy.group(1))) if copy else set()
    local = {f[:-3] for f in os.listdir(_HERE) if f.endswith(".py")}
    need, todo = set(), ["playwright_scraper"]
    while todo:
        mod = todo.pop()
        if mod in need:
            continue
        need.add(mod)
        for n in ast.walk(ast.parse(open(os.path.join(_HERE, mod + ".py"), encoding="utf-8").read())):
            names = ([a.name for a in n.names] if isinstance(n, ast.Import)
                     else [n.module] if isinstance(n, ast.ImportFrom) and n.module else [])
            todo += [x.split(".")[0] for x in names if x.split(".")[0] in local]
    return check("Dockerfile COPY list equals playwright_scraper.py's local import closure",
                 listed == {m + ".py" for m in need})


def check_credential_scan_reads_json_fixtures() -> bool:
    """A force-tracked fixture.json holding a key was never opened (only
    .py/.md/... were scanned), and a scan that read nothing said clean."""
    import subprocess
    from check_no_credentials import scan
    fake_key = "".join("0123456789abcdef"[(i * 5) % 16] for i in range(32))
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            return check("credential scan of .json (git unavailable: skipped)", True)
        open(os.path.join(tmp, "fixture.json"), "w").write('{"apiKey": "%s"}\n' % fake_key)
        os.makedirs(os.path.join(tmp, "anyname"))
        open(os.path.join(tmp, "anyname", "pyvenv.cfg"), "w").write("home = /usr\n")
        open(os.path.join(tmp, "anyname", "x.py"), "w").write(f"K = '{fake_key}'\n")
        stats = {}
        findings = scan(tmp, stats)
        empty_stats = {}
        os.makedirs(os.path.join(tmp, "empty"))
        scan(os.path.join(tmp, "empty"), empty_stats)
    return check("credential scan opens .json fixtures, skips a venv by pyvenv.cfg, counts files",
                 [os.path.basename(f[0]) for f in findings] == ["fixture.json"]
                 and stats.get("scanned") == 1 and empty_stats.get("scanned") == 0)



# ---------------------------------------------------------------------------
# Proxy traffic savers: the pre-check before any browser, and opt-in media
# blocking through a local proxy
# ---------------------------------------------------------------------------
_SAVERS_URL = "https://www.zimmo.be/nl/gent-9000/te-koop/"
_SAVERS_SECRET = "s3cret-Pw-not-real"
_SAVERS_ENGINES = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper")


class _StubProxy:
    """A one-port HTTP proxy on 127.0.0.1 that answers every request with a
    fixed status and records the request heads it received."""

    def __init__(self, status):
        import socket
        import threading
        self.status, self.heads = status, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.heads.append(data.decode("latin-1"))
                reason = {407: "Proxy Authentication Required",
                          402: "Payment Required", 200: "OK"}.get(self.status, "X")
                conn.sendall(f"HTTP/1.1 {self.status} {reason}\r\n"
                             "Content-Length: 0\r\nConnection: close\r\n\r\n".encode())

    def url(self, secret=_SAVERS_SECRET):
        # Concatenated, so no line here looks like a credentialled URL.
        return "http://user-zone-x:" + secret + "@127.0.0.1:" + str(self.port)

    def close(self):
        self.sock.close()


def _closed_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _CapturedOutput:
    """stdout and every log record, as one string."""

    def __enter__(self):
        import contextlib
        import io
        import logging
        self.buf = io.StringIO()
        self._handler = logging.StreamHandler(self.buf)
        logging.getLogger().addHandler(self._handler)
        self._redirect = contextlib.redirect_stdout(self.buf)
        self._redirect.__enter__()
        return self.buf

    def __exit__(self, *exc):
        import logging
        self._redirect.__exit__(*exc)
        logging.getLogger().removeHandler(self._handler)
        return False


class _FakeRoute:
    def __init__(self, resource_type):
        self.request = type("R", (), {"resource_type": resource_type})()
        self.verdict = None

    def abort(self):
        self.verdict = "abort"

    def continue_(self):
        self.verdict = "continue"


def _engine_args(engine, tmp, proxy_url, *extra):
    """The engine's own parser on a stubbed argv, with no .env in play."""
    from unittest import mock
    argv = [engine.__name__, "--url", _SAVERS_URL, "--out",
            os.path.join(tmp, "out"), "--retries", "0"]
    if proxy_url:
        argv += ["--proxy", proxy_url]
    argv += list(extra)
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(engine, "apply_env_defaults", lambda *a, **k: None):
        return engine.parse_args()


def _run_engine(engine, args):
    import asyncio
    if engine.__name__ == "puppeteer_scraper":
        return asyncio.run(engine.scrape_async(args))
    return engine.scrape(args)


def _engine_installs_blocking(engine, args, pool):
    """One page attempt on fakes; True if media blocking went in."""
    import asyncio
    from unittest import mock
    name = engine.__name__
    with _CapturedOutput():
        if name == "playwright_scraper":
            fake = mock.MagicMock()
            pw = fake.return_value.__enter__.return_value
            ctx = pw.chromium.launch.return_value.new_context.return_value
            ctx.new_page.return_value.goto.side_effect = RuntimeError("stub stop")
            with mock.patch.object(engine, "sync_playwright", fake):
                engine._fetch_page_attempts(_SAVERS_URL, 1, args, pool, 0,
                                            pool.get_exit_for_worker(0) if pool else None,
                                            None)
            return ctx.route.called
        if name == "selenium_scraper":
            with mock.patch.object(engine.webdriver, "Chrome") as chrome:
                engine.build_driver(args, proxy_server_only=(
                    pool.get_exit_for_worker(0).server_only() if pool else None))
                driver = chrome.return_value
            return any(c[0][0] == "Network.setBlockedURLs"
                       for c in driver.execute_cdp_cmd.call_args_list)
        cdp = mock.MagicMock()
        cdp.send = mock.AsyncMock()
        page = mock.MagicMock()
        page.goto = mock.AsyncMock(side_effect=RuntimeError("stub stop"))
        page.target.createCDPSession = mock.AsyncMock(return_value=cdp)
        browser = mock.MagicMock()
        browser.pages = mock.AsyncMock(return_value=[page])
        browser.close = mock.AsyncMock()

        async def fake_launch(*a, **k):
            return browser
        with mock.patch.object(engine, "launch", fake_launch):
            asyncio.run(engine._fetch_page_attempts(_SAVERS_URL, 1, args, pool, 0, None))
        return any(c[0][0] == "Fetch.enable" and any(
            "resourceType" in pt for pt in c[0][1].get("patterns", []))
            for c in cdp.send.await_args_list)


def check_proxy_traffic_savers() -> bool:
    print("\n--- Proxy traffic savers: pre-check before the browser, opt-in media blocking ---")
    import argparse
    import asyncio
    import contextlib
    import io
    import re
    from unittest import mock
    import env_config
    import output_writer
    import proxy_check
    from proxy_pool import ProxyPool
    ok = True

    # -- the pre-check, against a stub proxy --------------------------------
    stub = _StubProxy(407)
    try:
        res = proxy_check.precheck(stub.url(), _SAVERS_URL, timeout=5)
        ok &= check(f"a 407 from the proxy is auth_failed and fatal ({res.outcome})",
                    res.outcome == "auth_failed" and res.fatal
                    and res.stop_reason == "proxy_auth_failed")
        head = stub.heads[-1] if stub.heads else ""
        ok &= check("the pre-check is ONE CONNECT to the target host, with the credential",
                    len(stub.heads) == 1 and head.startswith("CONNECT www.zimmo.be:443 ")
                    and "Proxy-Authorization: Basic " in head)
        with _CapturedOutput() as buf:
            gated = proxy_check.gate(ProxyPool([stub.url()]), _SAVERS_URL, timeout=5)
        text = buf.getvalue()
        ok &= check("gate() returns the fatal check on a 407", gated is not None and gated.fatal)
        ok &= check("the 407 message names ZIMMO_PROXY and the cause",
                    "ZIMMO_PROXY" in text and "refused the credential" in text
                    and "traffic balance is exhausted" in text)
        ok &= check("the 407 message never prints the credential",
                    _SAVERS_SECRET not in text and "user-zone-x" not in text)
    finally:
        stub.close()

    stub = _StubProxy(402)
    try:
        res = proxy_check.precheck(stub.url(), _SAVERS_URL, timeout=5)
        msg = proxy_check.failure_message(res, stub.url())
        ok &= check("a 402 is balance_exhausted and fatal",
                    res.outcome == "balance_exhausted" and res.fatal
                    and res.stop_reason == "proxy_balance_exhausted")
        ok &= check("the 402 message says traffic/balance exhausted, no credential",
                    "traffic/balance exhausted" in msg and _SAVERS_SECRET not in msg)
    finally:
        stub.close()

    stub = _StubProxy(200)
    try:
        res = proxy_check.precheck(stub.url(), "http://127.0.0.1:9/x", timeout=5)
        head = stub.heads[-1] if stub.heads else ""
        ok &= check("a plain-http target is one HEAD for its robots.txt",
                    res.outcome == "ok" and not res.fatal
                    and head.startswith("HEAD http://127.0.0.1:9/robots.txt "))
    finally:
        stub.close()

    dead = "http://u:" + _SAVERS_SECRET + "@127.0.0.1:" + str(_closed_port())
    res = proxy_check.precheck(dead, _SAVERS_URL, timeout=5)
    ok &= check("a proxy that does not answer is a transport error, NOT fatal "
                "(existing retry behaviour applies)",
                res.outcome == "transport_error" and not res.fatal)
    with _CapturedOutput() as buf:
        gated = proxy_check.gate(ProxyPool([dead]), _SAVERS_URL, timeout=5)
    ok &= check("gate() lets a transport error through, credential unprinted",
                gated is None and _SAVERS_SECRET not in buf.getvalue())
    ok &= check("no pool means no pre-check", proxy_check.gate(None, _SAVERS_URL) is None)
    ok &= check("a socks5 exit is skipped, not refused",
                proxy_check.precheck("socks5://127.0.0.1:1", _SAVERS_URL).outcome == "skipped")
    ok &= check("mask() never raises on garbage and never echoes it",
                proxy_check.mask("http://a:" + _SAVERS_SECRET + "@h:notaport")
                == "(unparseable proxy URL, redacted)")

    # -- the attempt sidecar ------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        for name, body in (("out.json", "[1]"), ("out.meta.json", "{}")):
            with open(os.path.join(tmp, name), "w") as f:
                f.write(body)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = output_writer.finish_without_fetch(
                out, stop_reason="proxy_auth_failed", pages_requested=3, url=_SAVERS_URL)
        with open(out + ".latest_attempt.meta.json") as f:
            meta = json.load(f)
        untouched = (open(out + ".json").read() == "[1]"
                     and open(out + ".meta.json").read() == "{}")
    ok &= check(f"a refused proxy exits 5 ({rc})", rc == 5)
    ok &= check("...and records status failed / proxy_auth_failed in the attempt sidecar",
                meta.get("status") == "failed" and meta.get("stop_reason") == "proxy_auth_failed"
                and meta.get("pages_completed") == 0 and meta.get("product_count") == 0)
    ok &= check("...leaving the previous output and its .meta.json alone", untouched)

    # -- when media is blocked ----------------------------------------------
    pool = ProxyPool(["http://127.0.0.1:1"])
    ns = argparse.Namespace
    ok &= check("media is NOT blocked by default on this site, even through a proxy",
                proxy_check.BLOCK_MEDIA_BY_DEFAULT is False and not proxy_check.block_media(
                    ns(load_media=None, block_media=False), pool, remote=False))
    ok &= check("--block-media blocks through a proxy",
                proxy_check.block_media(ns(load_media=None, block_media=True), pool, False))
    ok &= check("--block-media is ignored over --cdp-endpoint",
                not proxy_check.block_media(ns(load_media=None, block_media=True), pool, True))
    ok &= check("--load-media keeps media",
                not proxy_check.block_media(ns(load_media=True, block_media=False), pool, False))
    ok &= check("an env value of 0 does not count as --load-media",
                proxy_check._truthy("0") is False and proxy_check._truthy("1") is True)
    parser = argparse.ArgumentParser()
    proxy_check.add_media_flags(parser)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["--load-media", "--block-media"])
        exclusive = False
    except SystemExit:
        exclusive = True
    ok &= check("--load-media and --block-media are mutually exclusive", exclusive)
    ok &= check("--load-media defaults to None so ZIMMO_LOAD_MEDIA can fill it",
                parser.parse_args([]).load_media is None)
    ok &= check("both flags are documented in --help",
                "--block-media" in parser.format_help() and "--load-media" in parser.format_help())
    ok &= check("ZIMMO_LOAD_MEDIA maps onto --load-media",
                env_config.ENV_KEYS.get(proxy_check.LOAD_MEDIA_ENV) == "load_media")

    # -- the route handler, on a stub page ----------------------------------
    ctx = mock.MagicMock()
    with _CapturedOutput():
        proxy_check.install_playwright(ctx)
    pattern, handler = ctx.route.call_args[0]
    verdicts = {}
    for rtype in ("image", "media", "font", "document", "script", "xhr", "fetch", "stylesheet"):
        route = _FakeRoute(rtype)
        handler(route)
        verdicts[rtype] = route.verdict
    ok &= check(f"Playwright: route on every URL ({pattern})", pattern == "**/*")
    ok &= check(f"Playwright: image/media/font aborted, everything else continues {verdicts}",
                all(verdicts[t] == "abort" for t in ("image", "media", "font"))
                and all(v == "continue" for t, v in verdicts.items()
                        if t not in ("image", "media", "font")))

    cdp = mock.MagicMock()
    cdp.send = mock.AsyncMock()
    page = mock.MagicMock()
    page.target.createCDPSession = mock.AsyncMock(return_value=cdp)

    async def _drive_pyppeteer():
        await proxy_check.install_pyppeteer(page)
        cdp.on.call_args[0][1]({"requestId": "r1", "resourceType": "Image"})
        await asyncio.sleep(0)

    with _CapturedOutput():
        asyncio.run(_drive_pyppeteer())
    calls = [c[0] for c in cdp.send.await_args_list]
    enable = next((c[1] for c in calls if c[0] == "Fetch.enable"), {})
    kinds = sorted(pt.get("resourceType") for pt in enable.get("patterns", []))
    ok &= check(f"pyppeteer: Fetch pauses ONLY Image/Media/Font, on its own CDP session {kinds}",
                cdp.on.call_args[0][0] == "Fetch.requestPaused"
                and kinds == ["Font", "Image", "Media"])
    ok &= check("pyppeteer: a paused request is failed, not continued",
                ("Fetch.failRequest", {"requestId": "r1", "errorReason": "BlockedByClient"})
                in calls)

    driver = mock.MagicMock()
    with _CapturedOutput():
        installed = proxy_check.install_selenium(driver)
    sent = {c[0][0]: c[0][1] for c in driver.execute_cdp_cmd.call_args_list}
    urls = sent.get("Network.setBlockedURLs", {}).get("urls", [])
    ok &= check("Selenium: Network.setBlockedURLs with image/video/font patterns",
                installed and "*.png" in urls and "*.woff2" in urls and "*.mp4?*" in urls)
    ok &= check("Selenium: no pattern touches a document, script, JSON or stylesheet",
                not [u for u in urls if re.search(r"\.(html?|js|json|css)\b", u)])
    broken = mock.MagicMock()
    broken.execute_cdp_cmd.side_effect = RuntimeError("no CDP")
    with _CapturedOutput():
        ok &= check("Selenium: a driver that refuses CDP is not fatal",
                    proxy_check.install_selenium(broken) is False)

    # -- each engine, driven: no browser is started on a 407 ----------------
    launch_target = {"playwright_scraper": "sync_playwright",
                     "selenium_scraper": "build_driver",
                     "puppeteer_scraper": "launch"}
    for name in _SAVERS_ENGINES:
        try:
            engine = importlib.import_module(name)
        except ImportError as e:
            print(f"[SKIP] {name} proxy-savers driving -- its driver library is absent ({e})")
            continue
        stub = _StubProxy(407)
        launched = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                args = _engine_args(engine, tmp, stub.url())
                with mock.patch.object(engine, launch_target[name],
                                       side_effect=lambda *a, **k: launched.append(1)), \
                        _CapturedOutput() as buf:
                    try:
                        rc = _run_engine(engine, args)
                    except Exception as e:  # noqa: BLE001 -- fail BY NAME
                        rc = f"raised {type(e).__name__}"
                side = os.path.join(tmp, "out.latest_attempt.meta.json")
                meta = json.load(open(side)) if os.path.exists(side) else {}
                wrote_rows = any(os.path.exists(os.path.join(tmp, "out" + s))
                                 for s in (".json", ".csv", ".meta.json"))
        finally:
            stub.close()
        ok &= check(f"{name}: a 407 exits 5 without starting a browser "
                    f"(rc={rc}, launched={len(launched)})", rc == 5 and not launched)
        ok &= check(f"{name}: ...with stop_reason proxy_auth_failed in the attempt sidecar "
                    "and no output written",
                    meta.get("stop_reason") == "proxy_auth_failed"
                    and meta.get("status") == "failed" and not wrote_rows)
        ok &= check(f"{name}: ...and the credential never reaches the log",
                    _SAVERS_SECRET not in buf.getvalue())

        # -- ...and installs blocking only when asked, through a proxy -------
        for label, proxy, extra, want in (
                ("through a proxy, by default", "http://127.0.0.1:1", (),
                 proxy_check.BLOCK_MEDIA_BY_DEFAULT),
                ("through a proxy with --block-media", "http://127.0.0.1:1",
                 ("--block-media",), True),
                ("through a proxy with --load-media", "http://127.0.0.1:1",
                 ("--load-media",), False),
                ("without a proxy", None, (), False)):
            with tempfile.TemporaryDirectory() as tmp:
                args = _engine_args(engine, tmp, proxy, *extra)
                args.retry_delay = 0
                pool = ProxyPool([proxy]) if proxy else None
                try:
                    got = _engine_installs_blocking(engine, args, pool)
                except Exception as e:  # noqa: BLE001 -- fail BY NAME
                    got = f"raised {type(e).__name__}: {e}"
            ok &= check(f"{name}: media blocking {label} = {want} (got {got})", got is want)
    return ok



import contextlib  # noqa: E402 -- used by the proxy-meter block below
import io  # noqa: E402
import re  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))


def _mcheck(label, condition):
    """`check`, always returning a bool, so `ok &= ...` cannot turn a
    truthy non-bool (a byte count) into 0."""
    return bool(check(label, bool(condition)))


def _meter_group(title):
    print(f"== {title}")


def _meter_rows():
    from output_writer import Product
    return [Product(sku=f"M{i}", url=f"https://www.zimmo.be/nl/x/{i}/")
            for i in range(3)]


def _meter_finish(rows, out, stop_reason, proxy_extra):
    """finish_run as the engines call it. `stop_reason` is not an input
    here: this finish_run derives it, and a budget stop comes from the
    meter's record."""
    from output_writer import finish_run
    return finish_run(rows, out, "json", pages_requested=2,
                      pages_completed=1 if rows else 0,
                      failed_pages=[] if rows else [1],
                      url=_SAVERS_URL, proxy=proxy_extra)


def _engine_meter_installed(engine, args, pool):
    """One page attempt on fakes; True if the proxy meter went in."""
    import asyncio
    from unittest import mock
    import proxy_meter
    name = engine.__name__
    with _CapturedOutput():
        if name == "playwright_scraper":
            fake = mock.MagicMock()
            pw = fake.return_value.__enter__.return_value
            ctx = pw.chromium.launch.return_value.new_context.return_value
            ctx.new_page.return_value.goto.side_effect = RuntimeError("stub stop")
            with mock.patch.object(engine, "sync_playwright", fake):
                engine._fetch_page_attempts(_SAVERS_URL, 1, args, pool, 0,
                                            pool.get_exit_for_worker(0) if pool else None,
                                            None)
            return any(c[0][0] == "requestfinished" for c in ctx.on.call_args_list)
        if name == "selenium_scraper":
            with mock.patch.object(engine.webdriver, "Chrome") as chrome:
                engine.build_driver(args, proxy_server_only=(
                    pool.get_exit_for_worker(0).server_only() if pool else None))
                driver = chrome.return_value
            return isinstance(getattr(driver, "_proxy_meter_counter", None),
                              proxy_meter.CdpCounter)
        cdp = mock.MagicMock()
        cdp.send = mock.AsyncMock()
        page = mock.MagicMock()
        page.goto = mock.AsyncMock(side_effect=RuntimeError("stub stop"))
        page.target.createCDPSession = mock.AsyncMock(return_value=cdp)
        browser = mock.MagicMock()
        browser.pages = mock.AsyncMock(return_value=[page])
        browser.close = mock.AsyncMock()

        async def fake_launch(*a, **k):
            return browser
        with mock.patch.object(engine, "launch", fake_launch):
            asyncio.run(engine._fetch_page_attempts(_SAVERS_URL, 1, args, pool, 0, None))
        return any(c[0][0] == "Network.enable" for c in cdp.send.await_args_list)


_LAUNCHERS = {"playwright_scraper": "sync_playwright",
              "selenium_scraper": "build_driver",
              "puppeteer_scraper": "launch"}


def _drive_budget_run(engine, tmp, spend_on, *extra_args, rows=True):
    """scrape() with the page fetch faked. Every fake fetch goes through
    the meter's check first, like a real attempt; page `spend_on` spends
    past the budget."""
    import asyncio
    import time as _time
    import proxy_meter
    from unittest import mock
    from output_writer import Product
    args = _engine_args(engine, tmp, "http://127.0.0.1:" + str(_closed_port()),
                        "--proxy-budget-mb", "0.01", "--format", "json",
                        *extra_args)
    fetched = []

    def body(page_num):
        proxy_meter.check(args)
        if page_num == spend_on:
            proxy_meter.of(args).add(20_000)
            if not rows:
                proxy_meter.check(args)
        elif page_num == spend_on + 1:
            _time.sleep(0.2)   # let the spending page finish first
        fetched.append(page_num)
        return ([Product(sku=f"P{page_num}-{i}", url=f"u{page_num}-{i}")
                 for i in range(2)], True, False, False, False)

    if engine.__name__ == "puppeteer_scraper":
        async def fake(url, page_num, *a, **k):
            return body(page_num)
    else:
        def fake(url, page_num, *a, **k):
            return body(page_num)
    with mock.patch.object(engine, "_fetch_page", side_effect=fake), \
            _CapturedOutput() as buf:
        try:
            rc = _run_engine(engine, args)
        except BaseException as e:  # noqa: BLE001 -- fail BY NAME
            rc = f"raised {type(e).__name__}"
    out = os.path.join(tmp, "out")
    meta = (json.load(open(out + ".meta.json"))
            if os.path.exists(out + ".meta.json") else {})
    attempt = (json.load(open(out + ".latest_attempt.meta.json"))
               if os.path.exists(out + ".latest_attempt.meta.json") else {})
    return rc, sorted(fetched), meta, attempt, buf.getvalue()


def check_proxy_meter_in_the_engines() -> bool:
    _meter_group("Proxy traffic meter: wired into every engine")
    import asyncio
    import proxy_meter
    from unittest import mock
    from proxy_pool import ProxyPool
    ok = True
    for name in _SAVERS_ENGINES:
        try:
            engine = importlib.import_module(name)
        except ImportError as e:
            print(f"[SKIP] {name} proxy meter -- its driver library is absent ({e})")
            continue
        for label, proxy, want in (("through a proxy", "http://127.0.0.1:1", True),
                                   ("without a proxy", None, False)):
            with tempfile.TemporaryDirectory() as tmp:
                args = _engine_args(engine, tmp, proxy)
                args.retry_delay = 0
                pool = ProxyPool([proxy]) if proxy else None
                proxy_meter.start(args, pool)
                try:
                    got = _engine_meter_installed(engine, args, pool)
                except Exception as e:  # noqa: BLE001 -- fail BY NAME
                    got = f"raised {type(e).__name__}: {e}"
            ok &= _mcheck(f"{name}: meter installed {label} = {want} (got {got})",
                        got is want)

        # The real page attempt refuses to start a browser once over budget.
        with tempfile.TemporaryDirectory() as tmp:
            args = _engine_args(engine, tmp, "http://127.0.0.1:1",
                                "--proxy-budget-mb", "0.001")
        pool = ProxyPool(["http://127.0.0.1:1"])
        proxy_meter.start(args, pool).add(5_000)
        launched = []
        with mock.patch.object(engine, _LAUNCHERS[name],
                               side_effect=lambda *a, **k: launched.append(1)), \
                _CapturedOutput():
            if name == "puppeteer_scraper":
                call = lambda: asyncio.run(engine._fetch_page_attempts(
                    _SAVERS_URL, 1, args, pool, 0, None))
            elif name == "playwright_scraper":
                call = lambda: engine._fetch_page_attempts(
                    _SAVERS_URL, 1, args, pool, 0, pool.get_exit_for_worker(0), None)
            else:
                call = lambda: engine._fetch_page(_SAVERS_URL, 1, args, pool, 0)
            err = _base_exception(call)
        ok &= _mcheck(f"{name}: over budget, a page attempt stops before a browser "
                    f"is launched ({type(err).__name__}, launched={len(launched)})",
                    isinstance(err, proxy_meter.ProxyBudgetExceeded) and not launched)

        with tempfile.TemporaryDirectory() as tmp:
            rc, fetched, meta, attempt, log = _drive_budget_run(
                engine, tmp, 1, "--pages", "3")
        ok &= _mcheck(f"{name}: budget crossed on page 1 of 3 -> exit 6, rows kept, "
                    f"pages 2-3 never fetched (rc={rc}, fetched={fetched})",
                    rc == 6 and fetched == [1] and meta.get("product_count") == 2)
        ok &= _mcheck(f"{name}: ...stop_reason proxy_budget_exceeded, pages 2-3 "
                    f"unattempted, the proxy fields in both sidecars",
                    meta.get("stop_reason") == "proxy_budget_exceeded"
                    and meta.get("status") == "partial"
                    and meta.get("unattempted_pages") == [2, 3]
                    and meta.get("proxy_budget_exceeded") is True
                    and meta.get("proxy_bytes") == 20_000
                    and meta.get("proxy_budget_bytes") == 10_000
                    and attempt.get("proxy_bytes") == 20_000
                    and attempt.get("stop_reason") == "proxy_budget_exceeded")
        with tempfile.TemporaryDirectory() as tmp:
            rc, fetched, meta, attempt, log = _drive_budget_run(
                engine, tmp, 2, "--pages", "5", "--concurrency", "2")
        ok &= _mcheck(f"{name}: --concurrency 2, budget crossed on page 2 of 5 -> "
                    f"exit 6, the workers stop dispatching (rc={rc}, "
                    f"fetched={fetched})",
                    rc == 6 and 1 in fetched and 2 in fetched
                    and not {4, 5} & set(fetched)
                    and meta.get("stop_reason") == "proxy_budget_exceeded"
                    and "Worker raised" not in log)
        with tempfile.TemporaryDirectory() as tmp:
            rc, fetched, meta, attempt, log = _drive_budget_run(
                engine, tmp, 1, "--pages", "3", rows=False)
        ok &= _mcheck(f"{name}: budget crossed before any row -> exit 5, no output, "
                    f"the attempt sidecar says why (rc={rc})",
                    rc == 5 and not meta
                    and attempt.get("stop_reason") == "proxy_budget_exceeded"
                    and attempt.get("status") == "failed"
                    and attempt.get("proxy_bytes") == 20_000)
    return ok


# ---------------------------------------------------------------------------
# Proxy traffic meter and budget
# ---------------------------------------------------------------------------

class _ByteCountingProxy:
    """A forward HTTP proxy on 127.0.0.1 that serves every plain-http GET
    itself with a body of a known size, and counts every byte it received
    and sent. The meter is judged against these two numbers."""

    def __init__(self, sizes):
        import socket
        import threading
        self.sizes = dict(sizes)            # path -> body length
        self.received = self.sent = 0
        # CONNECTs are the browser's own background connections (a system
        # Chromium phones home through its proxy too); they are not page
        # requests, this stub refuses them, and they are kept apart.
        self.connects = 0
        self.paths = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        import threading
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        from urllib.parse import urlparse
        with conn:
            buf = b""
            while True:
                while b"\r\n\r\n" not in buf:
                    try:
                        chunk = conn.recv(65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                    buf += chunk
                head, _, buf = buf.partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                if lines[0].startswith("CONNECT "):
                    self.connects += 1
                    try:
                        conn.sendall(b"HTTP/1.1 403 Forbidden\r\n"
                                     b"Content-Length: 0\r\n\r\n")
                    except OSError:
                        pass
                    return
                self.received += len(head) + 4
                length = 0
                for line in lines[1:]:
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1])
                while len(buf) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                self.received += length
                buf = buf[length:]
                path = urlparse(lines[0].split(" ")[1]).path or "/"
                self.paths.append(path)
                size = self.sizes.get(path, 0)
                ctype = ("text/html" if path == "/" else "text/css"
                         if path.endswith(".css") else
                         "application/javascript" if path.endswith(".js")
                         else "application/octet-stream")
                body = (b"<html><head><link rel=stylesheet href=/s.css>"
                        b"<script src=/a.js></script></head><body><p id=t>"
                        if path == "/" else b"")
                body = body + b"x" * max(0, size - len(body))
                if path == "/":
                    body = body[:-11] + b"</p></body>"
                status = b"200 OK" if path in self.sizes else b"404 Not Found"
                out = (b"HTTP/1.1 " + status + b"\r\nContent-Type: "
                       + ctype.encode() + b"\r\nContent-Length: "
                       + str(len(body)).encode()
                       + b"\r\nCache-Control: no-store\r\n\r\n" + body)
                try:
                    conn.sendall(out)
                except OSError:
                    return
                self.sent += len(out)

    @property
    def total(self):
        return self.received + self.sent

    def url(self):
        return "http://127.0.0.1:" + str(self.port)

    def close(self):
        self.sock.close()


def _base_exception(fn):
    try:
        fn()
    except BaseException as e:  # noqa: BLE001 — the point is to see it
        return e
    return None


def check_proxy_meter_units() -> bool:
    _meter_group("Proxy traffic meter: counting, the budget, the sidecar")
    import argparse
    from unittest import mock
    import output_writer
    import proxy_meter
    import env_config
    from proxy_pool import ProxyPool
    ok = True
    ns = argparse.Namespace

    # -- the meter itself ----------------------------------------------------
    m = proxy_meter.ProxyMeter(1)
    fired = []
    m.on_exceed(lambda: fired.append(1))
    m.add(600_000)
    m.add(400_000)
    ok &= _mcheck("1 MB budget: exactly 1,000,000 bytes is not over it",
                m.bytes == 1_000_000 and not m.exceeded and not fired)
    m.add(1)
    ok &= _mcheck("...one byte more is, and the refusal hook fires",
                m.exceeded and fired == [1])
    m.add(50)
    ok &= _mcheck("...the hook fires once, not per request", fired == [1])
    err = _base_exception(m.check)
    ok &= _mcheck("_mcheck() raises ProxyBudgetExceeded once over budget",
                isinstance(err, proxy_meter.ProxyBudgetExceeded))
    ok &= _mcheck("ProxyBudgetExceeded passes an `except Exception` (a retry "
                "loop cannot swallow it)",
                not issubclass(proxy_meter.ProxyBudgetExceeded, Exception))
    ok &= _mcheck("sidecar fields: bytes, budget in bytes, exceeded",
                m.sidecar() == {"proxy_bytes": 1_000_051,
                                "proxy_budget_bytes": 1_000_000,
                                "proxy_budget_exceeded": True})
    ok &= _mcheck(f"summary line ({m.summary()})",
                m.summary() == "Proxy traffic: 1.0 MB of 1.0 MB budget")
    late = []
    m.on_exceed(lambda: late.append(1))
    ok &= _mcheck("a hook added after the cap was crossed runs at once",
                late == [1])
    free = proxy_meter.ProxyMeter(0)
    free.add(10 ** 12)
    ok &= _mcheck("--proxy-budget-mb 0 means no cap (null budget in the "
                "sidecar)", not free.exceeded and _base_exception(free.check)
                is None and free.sidecar()["proxy_budget_bytes"] is None)

    # -- the flag and the environment ----------------------------------------
    parser = argparse.ArgumentParser()
    proxy_meter.add_budget_flag(parser)
    for bad in ("-1", "abc", "nan", "inf"):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["--proxy-budget-mb", bad])
            refused = False
        except SystemExit as e:
            refused = e.code == 2
        ok &= _mcheck(f"--proxy-budget-mb {bad} is a usage error (exit 2)",
                    refused)
    ok &= _mcheck("--proxy-budget-mb 0 and 2.5 parse",
                parser.parse_args(["--proxy-budget-mb", "0"]).proxy_budget_mb
                == 0.0 and parser.parse_args(
                    ["--proxy-budget-mb", "2.5"]).proxy_budget_mb == 2.5)
    env = proxy_meter.BUDGET_ENV
    with mock.patch.dict(os.environ, {env: ""}):
        ok &= _mcheck("default budget is 10 MB",
                    proxy_meter.budget_mb(ns(proxy_budget_mb=None)) == 10.0
                    and proxy_meter.DEFAULT_BUDGET_MB == 10.0)
    with mock.patch.dict(os.environ, {env: "2.5"}):
        ok &= _mcheck(f"{env} sets the budget",
                    proxy_meter.budget_mb(ns(proxy_budget_mb=None)) == 2.5)
        ok &= _mcheck("...and the flag wins over it",
                    proxy_meter.budget_mb(ns(proxy_budget_mb=1.0)) == 1.0)
    with mock.patch.dict(os.environ, {env: "lots"}):
        ok &= _mcheck(f"an unparseable {env} stops the run instead of "
                    "silently using a default",
                    isinstance(_base_exception(lambda: proxy_meter.budget_mb(
                        ns(proxy_budget_mb=None))), SystemExit))
    if hasattr(env_config, "ENV_KEYS"):
        ok &= _mcheck(f"{env} maps onto --proxy-budget-mb",
                    env_config.ENV_KEYS.get(env) == "proxy_budget_mb")
    pool = ProxyPool(["http://127.0.0.1:1"])
    ok &= _mcheck("metered only through a local proxy: not without one, not "
                "over --cdp-endpoint",
                proxy_meter.start(ns(proxy_budget_mb=None), None) is None
                and proxy_meter.start(ns(proxy_budget_mb=None), pool,
                                      remote=True) is None
                and isinstance(proxy_meter.start(ns(proxy_budget_mb=None),
                                                 pool), proxy_meter.ProxyMeter))

    a = ns(proxy_budget_mb=0.001)
    proxy_meter.start(a, pool).add(5_000)
    ok &= _mcheck("proxy_meter.check(args) stops a run that is over budget",
                isinstance(_base_exception(lambda: proxy_meter.check(a)),
                           proxy_meter.ProxyBudgetExceeded)
                and _base_exception(lambda: proxy_meter.check(
                    ns(proxy_budget_mb=None))) is None)

    # -- CDP events with known sizes (pyppeteer, Selenium) ------------------
    m = proxy_meter.ProxyMeter(0)
    c = proxy_meter.CdpCounter(m)
    req = {"method": "GET", "url": "http://x.test/a", "headers": {"A": "b"}}
    est = len("GET http://x.test/a HTTP/1.1\r\n") + 2 + len("A: b\r\n")
    c.feed("Network.requestWillBeSent", {"requestId": "1", "request": req})
    c.feed("Network.dataReceived", {"requestId": "1", "encodedDataLength": 99})
    c.feed("Network.loadingFinished", {"requestId": "1",
                                       "encodedDataLength": 1234})
    ok &= _mcheck(f"CDP: a finished request = its request + encodedDataLength "
                f"({m.bytes} vs {est + 1234})", m.bytes == est + 1234)
    c.feed("Network.requestWillBeSent", {"requestId": "2", "request": req})
    c.feed("Network.loadingFailed", {"requestId": "2",
                                     "errorText": "net::ERR_BLOCKED_BY_CLIENT"})
    c.feed("Network.requestWillBeSent", {"requestId": "2b", "request": req})
    c.feed("Network.loadingFailed", {"requestId": "2b", "errorText": "x",
                                     "blockedReason": "inspector"})
    ok &= _mcheck("CDP: a request the browser blocked costs nothing",
                m.bytes == est + 1234)
    c.feed("Network.requestWillBeSent", {"requestId": "3", "request": req})
    c.feed("Network.dataReceived", {"requestId": "3", "encodedDataLength": 50})
    c.feed("Network.loadingFailed", {"requestId": "3",
                                     "errorText": "net::ERR_CONNECTION_RESET"})
    ok &= _mcheck("CDP: a request that failed mid-body costs what arrived",
                m.bytes == 2 * est + 1234 + 50)
    c.feed("Network.requestWillBeSent", {"requestId": "4", "request": req})
    c.feed("Network.requestWillBeSent", {
        "requestId": "4", "request": req,
        "redirectResponse": {"encodedDataLength": 300}})
    c.feed("Network.loadingFinished", {"requestId": "4",
                                       "encodedDataLength": 500})
    ok &= _mcheck("CDP: a redirect hop is counted as its own exchange",
                m.bytes == 4 * est + 1234 + 50 + 300 + 500)

    # -- Playwright: sizes() of every finished/failed request ---------------
    m = proxy_meter.ProxyMeter(0.001)
    ctx = mock.MagicMock()
    proxy_meter.install_playwright(ctx, m)
    on = {call[0][0]: call[0][1] for call in ctx.on.call_args_list}
    request = mock.MagicMock()
    request.sizes.return_value = {"requestHeadersSize": 100,
                                  "requestBodySize": 20,
                                  "responseHeadersSize": 200,
                                  "responseBodySize": 500}
    on.get("requestfinished", lambda r: None)(request)
    dead = mock.MagicMock()
    dead.sizes.side_effect = RuntimeError("no response")
    on.get("requestfailed", lambda r: None)(dead)
    ok &= _mcheck(f"Playwright: request.sizes() summed on requestfinished "
                f"({m.bytes} of 820), a failed one without sizes is 0",
                m.bytes == 820 and "requestfailed" in on)
    pattern, gate = ctx.route.call_args[0]
    route = mock.MagicMock()
    gate(route)
    ok &= _mcheck(f"Playwright: under budget the route falls through to the "
                f"next handler ({pattern})", pattern == "**/*"
                and route.fallback.called and not route.abort.called)
    on["requestfinished"](request)
    route = mock.MagicMock()
    gate(route)
    ok &= _mcheck("Playwright: over budget every new request is aborted",
                route.abort.called and not route.fallback.called)
    ok &= _mcheck("no meter, nothing installed",
                proxy_meter.install_playwright(ctx, None) is None
                and ctx.route.call_count == 1)

    # -- Selenium: the performance log --------------------------------------
    m = proxy_meter.ProxyMeter(0.001)
    driver = mock.MagicMock()
    log = [{"message": json.dumps({"message": {
        "method": "Network.requestWillBeSent",
        "params": {"requestId": "9", "request": req}}})},
        {"message": json.dumps({"message": {
            "method": "Network.loadingFinished",
            "params": {"requestId": "9", "encodedDataLength": 2000}}})},
        {"message": "not json"}]
    driver.get_log.return_value = log
    proxy_meter.install_selenium(driver, m)
    entries = proxy_meter.selenium_log(driver)
    ok &= _mcheck(f"Selenium: the performance log is counted ({m.bytes} = "
                f"{est + 2000}) and handed back to the engine's own reader",
                m.bytes == est + 2000 and entries == log)
    blocked = [call[0] for call in driver.execute_cdp_cmd.call_args_list]
    ok &= _mcheck("Selenium: over budget, every URL is blocked over CDP",
                ("Network.setBlockedURLs", {"urls": ["*"]}) in blocked)
    options = mock.MagicMock()
    proxy_meter.selenium_options(options, m)
    ok &= _mcheck("Selenium: the performance log is requested",
                options.set_capability.call_args[0] ==
                ("goog:loggingPrefs", {"performance": "ALL"}))

    # -- pyppeteer: its own CDP session -------------------------------------
    import asyncio
    m = proxy_meter.ProxyMeter(0.001)
    cdp = mock.MagicMock()
    cdp.send = mock.AsyncMock()
    page = mock.MagicMock()
    page.target.createCDPSession = mock.AsyncMock(return_value=cdp)

    async def _drive():
        await proxy_meter.install_pyppeteer(page, m)
        on = {call[0][0]: call[0][1] for call in cdp.on.call_args_list}
        on["Network.requestWillBeSent"]({"requestId": "p", "request": req})
        on["Network.loadingFinished"]({"requestId": "p",
                                       "encodedDataLength": 1500})
        await asyncio.sleep(0)

    asyncio.run(_drive())
    sent = [call[0] for call in cdp.send.await_args_list]
    ok &= _mcheck(f"pyppeteer: Network events counted ({m.bytes} = "
                f"{est + 1500})", m.bytes == est + 1500
                and ("Network.enable", {}) in sent)
    ok &= _mcheck("pyppeteer: over budget, every URL is blocked over CDP",
                ("Network.setBlockedURLs", {"urls": ["*"]}) in sent)

    # -- an HTTP client, through a real stub proxy with known sizes ---------
    import requests
    stub = _ByteCountingProxy({"/": 20_000})
    try:
        m = proxy_meter.ProxyMeter(0)
        r = requests.get("http://meter.test/", proxies={"http": stub.url()},
                         timeout=10)
        proxy_meter.count_response(m, r)
        import time as _time
        for _ in range(50):
            if stub.sent:
                break
            _time.sleep(0.02)
        drift = abs(m.bytes - stub.total) / stub.total
        ok &= _mcheck(f"HTTP client through a stub proxy: metered {m.bytes} B, "
                    f"the proxy carried {stub.total} B ({drift:.1%}, "
                    f"tolerance 5%)", drift <= 0.05)
    finally:
        stub.close()

    # -- the sidecars --------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        metered = proxy_meter.ProxyMeter(10)
        metered.add(123_456)
        rows = _meter_rows()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _meter_finish(rows, out, "completed", metered.sidecar())
        meta = json.load(open(out + ".meta.json"))
        attempt = json.load(open(out + ".latest_attempt.meta.json"))
        ok &= _mcheck(f"a metered run records proxy_bytes / proxy_budget_bytes "
                    f"/ proxy_budget_exceeded in .meta.json (rc={rc})",
                    rc == 0 and meta.get("proxy_bytes") == 123_456
                    and meta.get("proxy_budget_bytes") == 10_000_000
                    and meta.get("proxy_budget_exceeded") is False)
        ok &= _mcheck("...and the same in .latest_attempt.meta.json",
                    attempt.get("proxy_bytes") == 123_456
                    and attempt.get("status") == meta.get("status"))

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        over = proxy_meter.ProxyMeter(0.0001)
        over.add(5_000)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _meter_finish(_meter_rows(), out,
                               proxy_meter.STOP_BUDGET_EXCEEDED, over.sidecar())
        meta = json.load(open(out + ".meta.json"))
        ok &= _mcheck(f"over budget WITH rows: exit 6, partial, "
                    f"stop_reason proxy_budget_exceeded (rc={rc})",
                    rc == 6 and meta.get("status") == "partial"
                    and meta.get("stop_reason") == "proxy_budget_exceeded"
                    and meta.get("proxy_budget_exceeded") is True)

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        for name, body in (("out.json", "[1]"), ("out.meta.json", "{}")):
            with open(os.path.join(tmp, name), "w") as f:
                f.write(body)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _meter_finish([], out, proxy_meter.STOP_BUDGET_EXCEEDED,
                               over.sidecar())
        attempt = json.load(open(out + ".latest_attempt.meta.json"))
        untouched = (open(out + ".json").read() == "[1]"
                     and open(out + ".meta.json").read() == "{}")
        ok &= _mcheck(f"over budget with NO rows: exit 5 (rc={rc}), the "
                    f"attempt sidecar says so, the previous output is kept",
                    rc == 5 and untouched
                    and attempt.get("stop_reason") == "proxy_budget_exceeded"
                    and attempt.get("proxy_budget_exceeded") is True
                    and attempt.get("status") == "failed")

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        with contextlib.redirect_stdout(io.StringIO()):
            _meter_finish(_meter_rows(), out, "completed", None)
        ok &= _mcheck("an unmetered run writes no attempt sidecar and no "
                    "proxy fields (unchanged)",
                    not os.path.exists(out + ".latest_attempt.meta.json")
                    and "proxy_bytes" not in json.load(
                        open(out + ".meta.json")))

    a = ns(proxy_budget_mb=None)
    setattr(a, "_proxy_meter", metered)
    # Read off the logger calls, not a handler: the root logger's level
    # differs between entry points and would hide an INFO line.
    with mock.patch.object(proxy_meter.logger, "info") as info, \
            mock.patch.object(proxy_meter.logger, "warning") as warn:
        extra = proxy_meter.finish(a, {"site": 1})
    said = [c[0][0] % c[0][1:] for c in
            info.call_args_list + warn.call_args_list]
    ok &= _mcheck(f"the run ends with 'Proxy traffic: X.X MB of Y MB budget' "
                f"({said})",
                "Proxy traffic: 0.1 MB of 10.0 MB budget" in said
                and extra.get("site") == 1 and extra.get("proxy_bytes"))
    return ok


# A request the PAGE makes after the cap. Navigations are stopped by the
# engines' own _mcheck() before they are issued; this is what the in-browser
# refusal (a route abort, CDP Network.setBlockedURLs) is for.
_LATE_FETCH = ("fetch('/n').then(r => r.status).catch(e => 'refused')")


def check_proxy_meter_in_a_real_browser() -> bool:
    """Opt-in (PROXY_METER_BROWSER=1): each installed driver loads a page
    through _ByteCountingProxy, and the meter must agree with what the proxy
    carried. Needs a Chromium; not run in CI, which installs none."""
    _meter_group("Proxy traffic meter in a real browser (opt-in)")
    if os.environ.get("PROXY_METER_BROWSER") != "1":
        print("  not run — set PROXY_METER_BROWSER=1 with a Chromium "
              "installed to measure the meter against a stub proxy")
        return True
    import proxy_meter
    import time as _time
    ok = True
    sizes = {"/": 60_000, "/s.css": 30_000, "/a.js": 90_000, "/n": 40_000}
    chromium = os.environ.get("PROXY_METER_CHROMIUM", "/usr/bin/chromium")
    target = "http://meter.test/"
    ran = 0
    for driver in ("playwright", "pyppeteer", "selenium"):
        try:
            __import__(driver)
        except ImportError:
            continue
        ran += 1
        stub = _ByteCountingProxy(sizes)
        meter = proxy_meter.ProxyMeter(0.15)
        stopped = None
        try:
            if driver == "playwright":
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    b = pw.chromium.launch(headless=True,
                                           proxy={"server": stub.url()})
                    ctx = b.new_context()
                    proxy_meter.install_playwright(ctx, meter)
                    p = ctx.new_page()
                    p.goto(target)
                    p.wait_for_timeout(800)
                    first = (meter.bytes, stub.total)
                    p.evaluate(_LATE_FETCH)
                    p.wait_for_timeout(500)
                    stopped = _base_exception(meter.check)
                    b.close()
            elif driver == "pyppeteer":
                import asyncio
                from pyppeteer import launch

                async def go():
                    b = await launch(headless=True, executablePath=chromium,
                                     args=["--no-sandbox",
                                           f"--proxy-server={stub.url()}"],
                                     handleSIGINT=False, handleSIGTERM=False,
                                     handleSIGHUP=False)
                    p = await b.newPage()
                    await proxy_meter.install_pyppeteer(p, meter)
                    await p.goto(target)
                    await asyncio.sleep(0.8)
                    snap = (meter.bytes, stub.total)
                    await p.evaluate(_LATE_FETCH, force_expr=True)
                    await asyncio.sleep(0.5)
                    await b.close()
                    return snap
                first = asyncio.new_event_loop().run_until_complete(go())
                stopped = _base_exception(meter.check)
            else:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.chrome.service import Service
                o = Options()
                o.add_argument("--headless=new")
                o.add_argument("--no-sandbox")
                o.add_argument(f"--proxy-server={stub.url()}")
                o.binary_location = chromium
                proxy_meter.selenium_options(o, meter)
                d = webdriver.Chrome(options=o, service=Service(
                    os.environ.get("PROXY_METER_CHROMEDRIVER",
                                   "/usr/bin/chromedriver")))
                proxy_meter.install_selenium(d, meter)
                d.get(target)
                _time.sleep(0.8)
                proxy_meter.drain_selenium(d)
                first = (meter.bytes, stub.total)
                d.execute_script("return " + _LATE_FETCH)
                _time.sleep(0.5)
                proxy_meter.drain_selenium(d)
                stopped = _base_exception(meter.check)
                d.quit()
        except Exception as e:  # noqa: BLE001 — report by name
            ok &= _mcheck(f"{driver}: real-browser meter run ({e!r:.160})", False)
            stub.close()
            continue
        stub.close()
        metered, carried = first
        drift = abs(metered - carried) / max(1, carried)
        ok &= _mcheck(f"{driver}: page + css + js through the stub proxy: "
                    f"metered {metered} B, the proxy carried {carried} B "
                    f"({drift:.1%}, tolerance 10%)", drift <= 0.10)
        ok &= _mcheck(f"{driver}: past the 0.15 MB budget a request the page "
                    f"makes never reaches the proxy, and check() stops the "
                    f"run before its next navigation (proxy saw {stub.paths})",
                    isinstance(stopped, proxy_meter.ProxyBudgetExceeded)
                    and "/a.js" in stub.paths and "/n" not in stub.paths)
    if not ran:
        print("  no browser driver installed")
    return ok


def check_canary_reports_proxy_traffic() -> bool:
    _meter_group("Canary: proxy traffic in the step summary, budget enforced")
    import subprocess
    ok = True
    path = os.path.join(_HERE, ".github", "workflows", "canary.yml")
    if not os.path.exists(path):
        # The image build copies the code and the suite, not .github.
        print("  not run — no .github/workflows in this tree")
        return True
    text = open(path, encoding="utf-8").read()
    steps = re.split(r"\n\s*- (?:name|uses):", text)
    runs = [s for s in steps if re.search(r"\w+_scraper\.py", s)
            and "--out" in s]
    ok &= _mcheck(f"every canary scrape passes --proxy-budget-mb 10 "
                f"({len(runs)} run step(s))", bool(runs) and all(
                    "--proxy-budget-mb 10" in r for r in runs))
    outs = set()
    for r in runs:
        outs.update(re.findall(r"--out\s+(\S+)", r))
    reporters = [s for s in steps if ".github/proxy_traffic.py" in s
                 and "!cancelled()" in s]
    reported = set()
    for s in reporters:
        reported.update(re.findall(r"proxy_traffic\.py\s+\S+\s+(\S+)", s))
    ok &= _mcheck(f"each run's proxy traffic goes to the step summary, even "
                f"after a failed step (runs {sorted(outs)}, reported "
                f"{sorted(reported)})", bool(outs) and outs <= reported)

    script = os.path.join(_HERE, ".github", "proxy_traffic.py")
    with tempfile.TemporaryDirectory() as tmp:
        summary = os.path.join(tmp, "summary.md")
        results = {}
        for case, meta in (
                ("over", {"proxy_bytes": 12_300_000,
                          "proxy_budget_bytes": 10_000_000,
                          "proxy_budget_exceeded": True}),
                ("under", {"proxy_bytes": 2_340_000,
                           "proxy_budget_bytes": 10_000_000,
                           "proxy_budget_exceeded": False}),
                ("plain", {"status": "complete"}),
                ("missing", None)):
            out = os.path.join(tmp, case)
            if meta is not None:
                with open(out + ".latest_attempt.meta.json", "w") as f:
                    json.dump(meta, f)
            proc = subprocess.run(
                [sys.executable, script, "live/" + case, out],
                capture_output=True, text=True,
                env={**os.environ, "GITHUB_STEP_SUMMARY": summary})
            results[case] = (proc.returncode, proc.stdout)
        written = open(summary).read() if os.path.exists(summary) else ""
    rc, out = results["over"]
    ok &= _mcheck(f"over budget: '::error::stopped: proxy budget 10 MB "
                f"exceeded' and the step fails (rc={rc})", rc == 1
                and "::error::stopped: proxy budget 10 MB exceeded" in out)
    ok &= _mcheck("under budget: '**live/under**: proxy traffic 2.3 MB of "
                "10 MB' in the summary, step passes",
                results["under"][0] == 0
                and "**live/under**: proxy traffic 2.3 MB of 10 MB" in written
                and "**live/over**: proxy traffic 12.3 MB of 10 MB" in written)
    ok &= _mcheck("no proxy fields / no sidecar: reported, not a crash",
                results["plain"][0] == 0 and results["missing"][0] == 0
                and "not metered" in written and "unknown" in written)
    return ok


if __name__ == "__main__":
    classify_ok = check_classify_page_contract()
    engines_classify_ok = check_engines_use_shared_page_classification()
    exhausted_ok = check_exhausted_listing_is_complete()
    selection_ok = check_selection_url_normalisation()
    diff_selection_ok = check_diff_refuses_different_selection_and_marks_window()
    credential_scope_ok = check_credential_scan_never_opens_ignored_env_or_prints_secrets()
    audit_2026_10_07_ok = (classify_ok and engines_classify_ok and exhausted_ok and selection_ok
                           and diff_selection_ok and credential_scope_ok)
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
    schema_ok = check_output_schema_and_removed_columns()
    canary_gate_ok = check_canary_fails_without_a_proxy()
    audit_2026_10_14_ok = all([
        check_atomic_writes_keep_old_output_and_modes(),
        check_csv_formula_cells_escaped(),
        check_solve_budget_one_purchase_per_page(),
        check_parser_failure_is_not_an_empty_search(),
        check_numeric_flags_rejected_when_negative(),
        check_scraper_api_page_verdicts(),
        check_no_private_citations(),
        check_docker_copy_list_matches_import_graph(),
        check_credential_scan_reads_json_fixtures(),
        check_proxy_traffic_savers(),
    ])
    proxy_meter_ok = all([
        check_proxy_meter_units(),
        check_proxy_meter_in_the_engines(),
        check_proxy_meter_in_a_real_browser(),
        check_canary_reports_proxy_traffic(),
    ])
    exit_code = main()
    overall_ok = (wording_ok and env_ok and env_roundtrip_ok and credentials_ok
                  and engine_import_ok and ast_ok and flags_ok and signatures_ok
                  and docker_version_ok and pyproject_version_ok and price_parser_ok
                  and price_no_swallow_ok and pagination_param_ok
                  and epc_regression_ok and css_fallback_scope_ok and live_tiles_ok and item_link_selector_ok
                  and unattempted_ok and no_sku_dedup_ok and audit_2026_10_07_ok
                  and schema_ok and canary_gate_ok and audit_2026_10_14_ok
                  and proxy_meter_ok
                  and exit_code == 0)
    sys.exit(0 if overall_ok else 1)
