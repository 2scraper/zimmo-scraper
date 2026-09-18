"""
product_parser.py
-------------------
Extracts real-estate listing records from a zimmo.be search-results page.

**UPDATE, confirmed live 2026-09-15**: a real run against the live site
found that zimmo.be has moved to an Angular-rendered listing grid since
this was first confirmed (2026-08-22) -- the `properties` embedded-JSON
array described below is GONE (0 matches on a real, fully-rendered
420KB page), and the page's only JSON-LD block is now a `BreadcrumbList`
(navigation only), not a real-estate schema. **The CSS/price-anchored
fallback is therefore the path actually in use on the live site today**,
not a last-resort fallback -- and a real bug in it (a correctly-widened
DOM scope being found, then discarded and silently recomputed narrower)
was found and fixed the same day; see `_parse_css_fallback`'s own
docstring and `smoke_test.py`'s `check_css_fallback_widened_scope_is_used`
for the full story. The properties-JSON and JSON-LD paths are kept below
in case a future page type (or a reversion) still uses them.

CONFIRMED LIVE (2026-08-22, no longer the active path -- see UPDATE
above): zimmo.be embedded the ENTIRE current page's listings as a clean
JSON array, inline in a <script> tag:

    <script>
        $(function () {
            app.start({
                search: {...filters...},
                properties: [ {...listing...}, {...listing...}, ... ]
            });
        });
    </script>

Each object in `properties` was a complete, well-structured listing
record. This primary path was explicitly confirmed sound by an external
audit (2026-09-14, codex.md): "Основной embedded-JSON путь обычно
избегает этой ошибки" (the primary embedded-JSON path is generally
immune to the price-parsing bug found in the fallback) -- prices here
came from `prijs`/`zprijs` as already-numeric JSON fields, never
regex-parsed from display text at all.

Strategy, in priority order (unchanged; what changed 2026-09-15 is
which path actually fires on a live page, not this precedence)
----------------------------------------------------------------------
  1. **The `properties` JSON array** -- CONFIRMED ABSENT on the live
     site as of 2026-09-15 (was confirmed present 2026-08-22).
  2. schema.org JSON-LD -- CONFIRMED ABSENT for listing data as of
     2026-09-15 (only a BreadcrumbList block is present).
  3. **Price-anchored CSS/regex fallback -- CONFIRMED LIVE AND ACTIVE
     as of 2026-09-15.** Extracts `sku` from a `.zimmo-code` span,
     `title` from a `.title` span (nested zimmo-code excluded), and
     `location` from a plain `<address>` tag -- all confirmed against
     real captured markup.

FIXED (audit finding #8): the CSS fallback's own price parser had a
confirmed, reproducible bug -- "€ 1250" read as 125.0, "1250 €" read as
250.0, "€10000" read as 100.0, and "325.000" (Belgian thousands-grouped)
read as 325 instead of 325000. The root cause: the old regex bounded
its leading digit-group to exactly 1-3 characters assuming a thousands
separator was ALWAYS present, silently truncating any unseparated
digit run instead of raising or falling back. `_parse_price_near_eur()`
below replaces that logic entirely: it captures every digit/separator
character adjacent to the € sign as one group, THEN classifies each
separator using Belgian convention (`,` introduces a decimal remainder;
`.` or a space is always a thousands grouping, never a decimal point)
rather than assuming a fixed group width. `_to_float()` (used for
surface_m2, EPC values, and other single decimal-point measurements) is
kept as a SEPARATE function specifically because that context's meaning
of "." is a genuine decimal point, not a thousands separator --
conflating the two was the mechanism behind the bug, not just its
symptom.
"""

import json
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from output_writer import Product

logger = logging.getLogger("product_parser")

_WIDEN_CAP_DEPTH = 8

SELECTORS = {
    # FIXED (found via a real live run, 2026-09-16): the previous value
    # `a[href*="/"]` was a loose PRE-confirmation guess -- any relative
    # link at all matches it, so it also matches every navigation, cookie-
    # consent, language-switcher, and footer-legal link on the page.
    # Confirmed live: a genuinely blocked Cloudflare/shell-only render of
    # zimmo.be still has 91 such links (Didomi cookie-consent controls,
    # nav menu, "Mijn Zimmo" login, /fr/ language switch, footer legal),
    # none of them a real listing. This selector is used for TWO things
    # across every engine -- `_wait_for_listing_markers()`'s "has the
    # page actually rendered listings yet" wait, and (newer)
    # `--solve-captcha=when-blocked`'s "does this page already have
    # enough real content that solving isn't worth it" check -- and a
    # selector this loose made the second one actively wrong: it counted
    # the blocked shell's own 91 nav links as "already rendered", so
    # --solve-captcha=when-blocked (the DEFAULT) skipped solving
    # entirely on a page that was genuinely blocked, confirmed live via
    # the exact log line "not attempting to solve it" on a page that
    # was, underneath, still "Just a moment..."-equivalent.
    # `.zimmo-code` is the confirmed-live, listing-specific marker
    # (product_parser.py's own CSS-fallback extraction already keys off
    # it) -- present only on a real listing tile, never on nav/cookie/
    # footer chrome.
    "item_link": '.zimmo-code',
}

# Captures every digit/./space/, character run adjacent to a € sign, as
# ONE group -- deliberately NOT bounded to a fixed leading-digit width,
# since that bound is exactly what caused audit finding #8 (a 4+ digit
# run with no separator was silently truncated to its first 1-3 digits).
# FIXED (found via a real live run against zimmo.be, 2026-09-16): the
# previous version allowed a bare SPACE as a thousands-grouping
# character INSIDE the captured digit run (`[\d.\s]*`), in addition to
# `.`. Confirmed live this was actively dangerous, not just unneeded:
# BeautifulSoup's own get_text(" ", strip=True) joins every text node
# in a tile with a single space, so "€ 385.000" followed anywhere later
# in the same widened scope by an unrelated number -- a bedroom count,
# a surface area -- got GREEDILY swallowed as if it were a continuation
# of the price's own thousands grouping. Confirmed: "€ 385.000 2
# slaapkamers 120 m²" (a real tile's flattened text) parsed as
# 3,850,002 instead of 385,000, by consuming the "2" bedroom count
# across the space as if it were another thousands group. Zimmo's own
# confirmed price format uses "." as its thousands separator (e.g.
# "€ 260.000"); no confirmed live example uses a bare space for this,
# so that support is removed rather than only narrowed -- the single
# `\s?` between € and the first digit (matching "€ 1250" / "1250 €")
# is unaffected, since it sits OUTSIDE this capture group entirely.
_PRICE_NEAR_EUR_RE = re.compile(r"€\s?(\d[\d.]*(?:,\d+)?)|(\d[\d.]*(?:,\d+)?)\s?€")
# FIXED (found via this repo's own adversarial re-testing, 2026-09-15,
# not the external audit): the previous pattern required ONLY non-letter
# characters between "EPC" and the grade letter
# (`[^A-Za-z]{0,10}`), so any natural-language phrasing with a word in
# between -- "EPC label C", "EPC label: A+" -- silently failed to match
# at all, leaking that raw text into the CSS fallback's own title field
# instead of being recognized and stripped as EPC data. Widened to allow
# any 20 characters (non-greedy) before the grade letter. The EPC
# keyword itself is matched case-insensitively via an inline `(?i:...)`
# flag, but the CAPTURED GRADE LETTER is deliberately checked
# case-SENSITIVE (uppercase only) -- a real EPC label is always
# displayed as a capital letter, and combining case-insensitivity with
# "any 20 characters" would otherwise match a random lowercase a/b/c/d/
# e/f/g inside an ordinary English word (confirmed: an earlier all-
# case-insensitive version of this exact fix matched "a" out of
# "available" in unrelated marketing text near the word "EPC").
_EPC_LABEL_RE = re.compile(r"(?i:\bEPC\b).{0,20}?\b([A-G][+]{0,2})")
_EPC_VALUE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s?kWh\s?/\s?m²?", re.IGNORECASE)
_SURFACE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s?m(?:²|2)\b")
_BEDROOMS_RE = re.compile(r"(\d+)\s?(?:slaapkamers?|chambres?|bedrooms?)", re.IGNORECASE)
_SALE_WORDS = ("te koop", "à vendre", "a vendre", "for sale")
_RENT_WORDS = ("te huur", "à louer", "a louer", "for rent")

_JUNK_TEXTS = {
    "", "#", "meer info", "plus d'infos", "contacteer", "contactez",
    "bekijk", "voir", "vergelijk", "comparer", "opslaan", "sauvegarder",
    "delen", "partager",
}

_REAL_ESTATE_TYPES = {
    "Product", "Offer", "RealEstateListing", "SingleFamilyResidence",
    "Apartment", "House", "Residence", "Accommodation",
}


def _parse_price_near_eur(m: re.Match) -> Optional[float]:
    """Belgian number convention: `,` introduces a decimal remainder
    (e.g. "325.000,50" = 325000.50); `.` is always a thousands
    grouping, never a decimal point, regardless of how many digits
    follow. A bare space is deliberately NOT treated as a grouping
    character here -- see _PRICE_NEAR_EUR_RE's own comment for why
    that was confirmed live to be actively dangerous (it swallowed an
    unrelated nearby number, like a bedroom count, into the price)."""
    raw = m.group(1) or m.group(2)
    if not raw:
        return None
    if "," in raw:
        integer_part, _, decimal_part = raw.rpartition(",")
    else:
        integer_part, decimal_part = raw, ""
    integer_part = integer_part.replace(".", "")
    if not integer_part.isdigit():
        return None
    value = float(integer_part)
    if decimal_part.isdigit():
        value += float(decimal_part) / (10 ** len(decimal_part))
    return value


def _to_float(text: Optional[str]) -> Optional[float]:
    """For genuine single-decimal-point measurements ONLY (surface_m2,
    EPC numeric value) -- NEVER for a price. A price's own "." is a
    thousands separator in Belgian convention, not a decimal point;
    conflating the two was the root cause of audit finding #8. Use
    _parse_price_near_eur() for any price-shaped text instead."""
    if not text:
        return None
    cleaned = text.replace(" ", "").replace(",", ".")
    m = re.search(r"\d+\.?\d*", cleaned)
    return float(m.group()) if m else None


def _detect_listing_type(text: str, url: str) -> Optional[str]:
    lowered = (text + " " + url).lower()
    if any(w in lowered for w in _SALE_WORDS):
        return "sale"
    if any(w in lowered for w in _RENT_WORDS):
        return "rent"
    return None


def _extract_image_url(image_field) -> Optional[str]:
    if image_field is None:
        return None
    if isinstance(image_field, list):
        image_field = image_field[0] if image_field else None
    if image_field is None:
        return None
    if isinstance(image_field, str):
        return image_field
    if isinstance(image_field, dict):
        return image_field.get("url") or image_field.get("contentUrl")
    return None


def _iter_jsonld_candidates(data):
    blocks = data if isinstance(data, list) else [data]
    for block in blocks:
        if not isinstance(block, dict):
            continue
        items = block.get("itemListElement")
        graph = block.get("@graph")
        if items:
            for entry in items:
                yield entry
        elif graph:
            for entry in graph:
                yield entry
        else:
            yield block


def _parse_jsonld(html: str, base_url: str) -> List[Product]:
    soup = BeautifulSoup(html, "html.parser")
    products: List[Product] = []

    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        for entry in _iter_jsonld_candidates(data):
            node = entry.get("item", entry) if isinstance(entry, dict) else entry
            if not isinstance(node, dict):
                continue
            node_type = node.get("@type")
            node_types = node_type if isinstance(node_type, list) else [node_type]
            if not any(t in _REAL_ESTATE_TYPES for t in node_types):
                continue

            offers = node.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                offers = {}

            url_path = node.get("url") or offers.get("url") or ""
            # A JSON-LD price is already a plain number/numeric string,
            # not display text needing thousands-separator parsing --
            # _to_float() is correct here, unlike in the CSS fallback.
            price = offers.get("price") if offers.get("price") is not None else node.get("price")

            address = node.get("address")
            location = address.get("addressLocality") if isinstance(address, dict) else None

            products.append(Product(
                url=urljoin(base_url, url_path) if url_path else base_url,
                sku=node.get("sku") or node.get("productID"),
                title=node.get("name"),
                price=_to_float(str(price)) if price is not None else None,
                currency=offers.get("priceCurrency", "EUR"),
                image_url=_extract_image_url(node.get("image")),
                location=location,
            ))
    return products


def _strip_known_fields(text: str) -> str:
    cleaned = _PRICE_NEAR_EUR_RE.sub(" ", text)
    cleaned = _EPC_LABEL_RE.sub(" ", cleaned)
    cleaned = _EPC_VALUE_RE.sub(" ", cleaned)
    cleaned = _SURFACE_RE.sub(" ", cleaned)
    cleaned = _BEDROOMS_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\(\s*\)", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" ,.-|")


def _common_ancestor(tags, cap_depth: int = _WIDEN_CAP_DEPTH):
    if not tags:
        return None
    node = tags[0].parent
    depth = 0
    while node is not None and depth < cap_depth:
        if all(t is node or t in node.descendants for t in tags):
            return node
        node = node.parent
        depth += 1
    return node


def _find_image_near(anchors, cap_depth: int = _WIDEN_CAP_DEPTH) -> Optional[str]:
    for a in anchors:
        img = a.find("img")
        if img:
            return img.get("src") or img.get("data-src")
    node = anchors[0].parent
    depth = 0
    while node is not None and depth < cap_depth:
        img = node.find("img")
        if img:
            return img.get("src") or img.get("data-src")
        node = node.parent
        depth += 1
    return None


def _parse_css_fallback(html: str, base_url: str, category: Optional[str]) -> List[Product]:
    """FOUND AND FIXED via live-run diagnosis, 2026-09-15: zimmo.be has
    moved to an Angular-rendered listing grid since the 2026-08-22
    capture -- confirmed live, the `properties` embedded-JSON path and
    a real-estate JSON-LD path are both now ABSENT (a live capture's
    only JSON-LD block was a BreadcrumbList, not listing data), making
    this fallback the one actually exercised for the first time. It
    was broken: the widening loop below correctly walked UP from an
    anchor with no price of its own (Zimmo's real tile: the `<a>` around
    the title/address has no price in its own text; the price lives in
    a SIBLING `<div class="price">` one level up, at the tile's shared
    container) and correctly found the right scope containing the price
    -- but that widened scope was then DISCARDED. A second pass used to
    recompute the scope from scratch via `_common_ancestor(anchors)`,
    which only reaches each anchor's own immediate parent -- the `<a>`'s
    own `<h2>`, not the tile container the price actually lives in --
    so `price` came back None on every real listing and the whole page
    parsed as 0 products. Fixed by keeping the scope the widening loop
    already found, per anchor, instead of recomputing a narrower one."""
    soup = BeautifulSoup(html, "html.parser")

    # base_url -> (anchors sharing it, the widened scope(s) that found a
    # price for them) -- the FIX is that this scope is now KEPT, not
    # thrown away and recomputed narrower afterward.
    groups: dict = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        text = a.get_text(" ", strip=True)
        has_price_here = _PRICE_NEAR_EUR_RE.search(text)
        scope = a
        if not has_price_here:
            candidate = a.parent
            depth = 0
            found = False
            while candidate is not None and depth < _WIDEN_CAP_DEPTH:
                sibling_link_count = len(candidate.find_all("a"))
                if sibling_link_count > 4:
                    break
                if _PRICE_NEAR_EUR_RE.search(candidate.get_text(" ", strip=True)):
                    scope = candidate
                    found = True
                    break
                candidate = candidate.parent
                depth += 1
            if not found:
                continue
        base = urljoin(base_url, href.split("#")[0])
        groups.setdefault(base, []).append((a, scope))

    products: List[Product] = []
    for url, anchor_scope_pairs in groups.items():
        anchors = [a for a, _ in anchor_scope_pairs]
        # Multiple anchors for the same URL (a thumbnail link and a
        # title link both pointing at the same listing, say) usually
        # widen to the same real tile container -- take the shallowest
        # (first-found) widened scope rather than re-deriving one from
        # anchors alone, which is exactly the bug this fix addresses.
        scope = anchor_scope_pairs[0][1]
        if scope is None:
            continue
        full_text = scope.get_text(" ", strip=True)

        price_match = _PRICE_NEAR_EUR_RE.search(full_text)
        price = _parse_price_near_eur(price_match) if price_match else None
        if price is None:
            continue

        texts = [a.get_text(" ", strip=True) for a in anchors]
        stripped_texts = [_strip_known_fields(t) for t in texts]
        clean_texts = [t for t in stripped_texts if t.strip().lower() not in _JUNK_TEXTS]
        title = max(clean_texts, key=len) if clean_texts else None

        # Confirmed live 2026-09-15 (Angular-rendered tile, no longer
        # matching the 2026-08-22 embedded-JSON capture): a listing's
        # own code is in a `.zimmo-code` span nested inside `.title`,
        # and its street/city are in a plain `<address>` tag -- both
        # precise enough to extract directly rather than leaving them
        # jumbled into one free-text title, when present.
        sku = None
        code_el = scope.select_one(".zimmo-code")
        if code_el:
            sku = code_el.get_text(strip=True)

        title_el = scope.select_one(".title")
        if title_el:
            # Exclude the nested .zimmo-code text from the title itself.
            title_clone_text = title_el.get_text(" ", strip=True)
            if sku:
                title_clone_text = title_clone_text.replace(sku, "").strip()
            if title_clone_text:
                title = title_clone_text

        location = None
        address_el = scope.select_one("address")
        if address_el:
            location = address_el.get_text(" ", strip=True)

        epc_label_match = _EPC_LABEL_RE.search(full_text)
        epc_value_match = _EPC_VALUE_RE.search(full_text)
        surface_match = _SURFACE_RE.search(full_text)
        bedrooms_match = _BEDROOMS_RE.search(full_text)

        products.append(Product(
            url=url,
            sku=sku,
            title=title,
            location=location,
            price=price,
            image_url=_find_image_near(anchors),
            category=category,
            listing_type=_detect_listing_type(full_text, url),
            epc_label=epc_label_match.group(1).upper() if epc_label_match else None,
            epc_value=_to_float(epc_value_match.group(1)) if epc_value_match else None,
            surface_m2=_to_float(surface_match.group(1)) if surface_match else None,
            bedrooms=int(bedrooms_match.group(1)) if bedrooms_match else None,
        ))
    return products


def _extract_bracket_matched(html: str, start_idx: int) -> Optional[str]:
    depth = 0
    in_string = False
    escape = False
    for i in range(start_idx, len(html)):
        ch = html[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    return html[start_idx:i + 1]
    return None


def _parse_properties_json(html: str, base_url: str, category: Optional[str]) -> List[Product]:
    markers = ("properties: [", '"properties":[', "properties:[", '"properties": [')
    start = -1
    matched_marker = None
    for marker in markers:
        idx = html.find(marker)
        if idx != -1:
            start = idx + len(marker) - 1
            matched_marker = marker
            break
    if start == -1:
        if "app.start(" in html and "properties" in html:
            idx = html.find("properties")
            logger.info("product_parser: 'app.start(' and 'properties' both present, but no "
                        "known marker spacing matched. Context: %r",
                        html[max(0, idx - 5):idx + 30])
        else:
            logger.info("product_parser: no 'properties' JSON array found on this page -- "
                        "falling back to JSON-LD / CSS-based extraction.")
        return []
    logger.info("product_parser: found properties array via marker %r", matched_marker)

    raw = _extract_bracket_matched(html, start)
    if raw is None:
        logger.warning("product_parser: found the 'properties' marker but bracket-matching "
                        "failed to find a closing bracket -- falling back.")
        return []

    try:
        listings = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("product_parser: found and bracket-matched the properties array "
                        "(%d chars) but json.loads() failed: %s -- falling back.", len(raw), e)
        return []

    products: List[Product] = []
    for item in listings:
        if not isinstance(item, dict):
            continue

        # These are already-numeric JSON fields, not display text --
        # _to_float() (decimal-point semantics) is correct here; the
        # thousands-separator bug that affects display-text parsing in
        # the CSS fallback cannot occur on this path (confirmed by
        # external audit as the reliable path for exactly this reason).
        price = _to_float(item.get("prijs"))
        original_price = _to_float(item.get("zprijs")) if item.get("zprijs") else None
        discount_pct = None
        if price and original_price and original_price > price:
            discount_pct = round((1 - price / original_price) * 100, 1)

        address = item.get("address") or ""
        gemeente = item.get("gemeente") or ""
        subtype = item.get("subtype_naam") or item.get("type") or ""
        title_parts = [p for p in (subtype, address, gemeente) if p]
        title = ", ".join(title_parts) if title_parts else None

        status = (item.get("status") or "").lower()
        if "for_sale" in status or "sale" in status:
            listing_type = "sale"
        elif "for_rent" in status or "rent" in status:
            listing_type = "rent"
        else:
            listing_type = None

        advertiser = item.get("advertiser") or {}
        epc_label = item.get("energyLabel") or item.get("energyLabelCategory")

        url_path = item.get("url") or item.get("pand_url") or ""

        products.append(Product(
            url=urljoin(base_url, url_path) if url_path else base_url,
            sku=item.get("code"),
            title=title,
            brand=advertiser.get("name") if isinstance(advertiser, dict) else None,
            price=price,
            original_price=original_price,
            discount_pct=discount_pct,
            image_url=item.get("hoofdFoto"),
            category=category,
            listing_type=listing_type,
            property_type=item.get("type"),
            location=f"{address}, {item.get('postcode', '')} {gemeente}".strip(", "),
            surface_m2=_to_float(item.get("b_woonopp")),
            bedrooms=int(item["slaapkamers"]) if str(item.get("slaapkamers", "")).isdigit() else None,
            epc_label=epc_label.upper() if epc_label else None,
            epc_value=_to_float(item.get("energyWaarde")),
        ))
    return products


def parse_products(html: str, base_url: str, category: Optional[str] = None) -> List[Product]:
    products = _parse_properties_json(html, base_url, category)
    if products:
        logger.info("product_parser: extracted %d listing(s) via the properties-JSON path.", len(products))
        return products

    products = _parse_jsonld(html, base_url)
    if products:
        logger.info("product_parser: extracted %d listing(s) via JSON-LD.", len(products))
        for p in products:
            p.category = category
        return products

    products = _parse_css_fallback(html, base_url, category)
    logger.info("product_parser: extracted %d listing(s) via the CSS/price-anchored fallback.", len(products))
    return products
