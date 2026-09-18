"""
tests/test_concurrency.py
-----------------------------
Drives playwright_scraper.py's `_fetch_pages_concurrently` directly, with
a stub `fetch_fn` and no browser at all (CLAUDE.md §10) -- a live run
cannot always reach this machinery, since page 1 is fetched alone and
decides whether the rest may be addressed: a blocked page 1 means the
workers here never start.

Guarded behind try/except ImportError so the offline suite still passes
with Playwright not installed (CLAUDE.md §10) -- this file specifically
tests playwright_scraper.py's own dispatcher, so it is skipped, not
failed, when that engine's own dependency is absent.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from playwright_scraper import _fetch_pages_concurrently
    from output_writer import Product
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not PLAYWRIGHT_AVAILABLE, reason="playwright not installed -- this engine's own dispatcher can't be tested"
)


def _make_stub_fetch(page_to_products: dict, page_to_outcome: dict = None,
                      raise_on: set = None, call_log: list = None):
    """A stand-in for `_fetch_page` with the identical return contract:
    (products, success, blocked, remote_api_error). No browser, no
    network, no sleep beyond a token amount to exercise real concurrency
    (so completions genuinely interleave, not just execute in submission
    order on a single thread)."""
    page_to_outcome = page_to_outcome or {}
    raise_on = raise_on or set()

    def fetch_fn(page_num, worker_offset):
        if call_log is not None:
            call_log.append(page_num)
        time.sleep(0.01)
        if page_num in raise_on:
            raise RuntimeError(f"simulated worker crash on page {page_num}")
        products = page_to_products.get(page_num, [])
        ok, blocked, remote_err = page_to_outcome.get(page_num, (True, False, False))
        return products, ok, blocked, remote_err

    return fetch_fn


def test_every_queued_page_fetched_exactly_once():
    call_log = []
    products = {n: [Product(sku=f"S{n}", url=f"u{n}")] for n in range(2, 6)}
    fetch_fn = _make_stub_fetch(products, call_log=call_log)

    page_results, failed, blocked, remote_err, unattempted = _fetch_pages_concurrently(
        fetch_fn, list(range(2, 6)), concurrency=3)

    assert sorted(call_log) == [2, 3, 4, 5], "every queued page must be fetched exactly once"
    assert len(call_log) == 4, "no page fetched more than once"
    assert set(page_results.keys()) == {2, 3, 4, 5}
    assert failed == []
    assert unattempted == []


def test_outcomes_restorable_to_page_order():
    # Deliberately give later pages a shorter sleep so they complete
    # FIRST in wall-clock time -- the dispatcher's own dict is keyed by
    # page number regardless of completion order, so the CALLER (not
    # this function) is what restores page order via sorted(page_results);
    # this test confirms page_results itself is keyed correctly for that.
    products = {2: [Product(sku="S2", url="u2")], 3: [Product(sku="S3", url="u3")],
                4: [Product(sku="S4", url="u4")]}
    fetch_fn = _make_stub_fetch(products)

    page_results, *_ = _fetch_pages_concurrently(fetch_fn, [2, 3, 4], concurrency=3)

    ordered_skus = [p.sku for n in sorted(page_results) for p in page_results[n]]
    assert ordered_skus == ["S2", "S3", "S4"]


def test_end_of_listing_event_stops_dispatch():
    # Page 3 adds a product whose sku was ALREADY seen (via
    # seen_skus_initial, simulating "page 1 already had this sku") --
    # page 3 therefore adds no NEW sku, and dispatch of pages 4+ must
    # never happen at all. concurrency=1 makes this deterministic to
    # assert (no race on which pages happened to already be in flight).
    call_log = []
    products = {
        2: [Product(sku="S2", url="u2")],
        3: [Product(sku="ALREADY_SEEN", url="u3")],  # no NEW sku
        4: [Product(sku="S4", url="u4")],
        5: [Product(sku="S5", url="u5")],
    }
    fetch_fn = _make_stub_fetch(products, call_log=call_log)

    page_results, failed, blocked, remote_err, unattempted = _fetch_pages_concurrently(
        fetch_fn, [2, 3, 4, 5], concurrency=1, seen_skus_initial={"ALREADY_SEEN"})

    assert call_log == [2, 3], "dispatch must stop right after the page that added no new sku"
    assert 4 in unattempted and 5 in unattempted
    assert 4 not in page_results and 5 not in page_results


def test_unattempted_pages_are_not_counted_as_failed():
    products = {2: [Product(sku="ALREADY_SEEN", url="u2")]}
    fetch_fn = _make_stub_fetch(products)

    _, failed, _, _, unattempted = _fetch_pages_concurrently(
        fetch_fn, [2, 3, 4], concurrency=1, seen_skus_initial={"ALREADY_SEEN"})

    assert failed == [], "an unattempted page is not a failure -- it was never tried"
    assert unattempted == [3, 4]


def test_worker_exception_does_not_hang_or_lose_siblings():
    call_log = []
    products = {2: [Product(sku="S2", url="u2")], 4: [Product(sku="S4", url="u4")]}
    fetch_fn = _make_stub_fetch(products, raise_on={3}, call_log=call_log)

    page_results, failed, blocked, remote_err, unattempted = _fetch_pages_concurrently(
        fetch_fn, [2, 3, 4], concurrency=3)

    assert sorted(call_log) == [2, 3, 4], "a sibling's crash must not prevent the others from running"
    assert 3 in failed
    assert 2 in page_results and 4 in page_results
    assert page_results[2][0].sku == "S2"
    assert page_results[4][0].sku == "S4"


def test_concurrency_one_behaves_like_sequential():
    products = {n: [Product(sku=f"S{n}", url=f"u{n}")] for n in range(2, 5)}
    fetch_fn = _make_stub_fetch(products)

    page_results, failed, *_ = _fetch_pages_concurrently(fetch_fn, [2, 3, 4], concurrency=1)

    assert set(page_results.keys()) == {2, 3, 4}
    assert failed == []
