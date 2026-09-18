"""
tests/test_concurrency_async.py
-----------------------------------
Same coverage as tests/test_concurrency.py, but for puppeteer_scraper.py's
asyncio-based `_fetch_pages_concurrently` -- see that file's own module
docstring for the full reasoning. Uses plain `asyncio.run()` inside
ordinary sync test functions rather than adding a pytest-asyncio
dependency for one test file.

Guarded behind try/except ImportError (CLAUDE.md §10) -- skipped, not
failed, when pyppeteer is not installed.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from puppeteer_scraper import _fetch_pages_concurrently
    from output_writer import Product
    PYPPETEER_AVAILABLE = True
except ImportError:
    PYPPETEER_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not PYPPETEER_AVAILABLE, reason="pyppeteer not installed -- this engine's own dispatcher can't be tested"
)


def _make_stub_fetch(page_to_products: dict, raise_on: set = None, call_log: list = None):
    raise_on = raise_on or set()

    async def fetch_fn(page_num, worker_offset):
        if call_log is not None:
            call_log.append(page_num)
        await asyncio.sleep(0.01)
        if page_num in raise_on:
            raise RuntimeError(f"simulated worker crash on page {page_num}")
        products = page_to_products.get(page_num, [])
        return products, True, False, False

    return fetch_fn


def test_every_queued_page_fetched_exactly_once():
    call_log = []
    products = {n: [Product(sku=f"S{n}", url=f"u{n}")] for n in range(2, 6)}
    fetch_fn = _make_stub_fetch(products, call_log=call_log)

    page_results, failed, blocked, remote_err, unattempted = asyncio.run(
        _fetch_pages_concurrently(fetch_fn, list(range(2, 6)), concurrency=3))

    assert sorted(call_log) == [2, 3, 4, 5]
    assert len(call_log) == 4
    assert set(page_results.keys()) == {2, 3, 4, 5}
    assert failed == []
    assert unattempted == []


def test_end_of_listing_event_stops_dispatch():
    call_log = []
    products = {
        2: [Product(sku="S2", url="u2")],
        3: [Product(sku="ALREADY_SEEN", url="u3")],
        4: [Product(sku="S4", url="u4")],
        5: [Product(sku="S5", url="u5")],
    }
    fetch_fn = _make_stub_fetch(products, call_log=call_log)

    page_results, failed, blocked, remote_err, unattempted = asyncio.run(
        _fetch_pages_concurrently(fetch_fn, [2, 3, 4, 5], concurrency=1,
                                   seen_skus_initial={"ALREADY_SEEN"}))

    assert call_log == [2, 3], "dispatch must stop right after the page that added no new sku"
    assert 4 in unattempted and 5 in unattempted


def test_worker_exception_does_not_hang_or_lose_siblings():
    call_log = []
    products = {2: [Product(sku="S2", url="u2")], 4: [Product(sku="S4", url="u4")]}
    fetch_fn = _make_stub_fetch(products, raise_on={3}, call_log=call_log)

    page_results, failed, blocked, remote_err, unattempted = asyncio.run(
        _fetch_pages_concurrently(fetch_fn, [2, 3, 4], concurrency=3))

    assert sorted(call_log) == [2, 3, 4]
    assert 3 in failed
    assert 2 in page_results and 4 in page_results


def test_unattempted_pages_are_not_counted_as_failed():
    products = {2: [Product(sku="ALREADY_SEEN", url="u2")]}
    fetch_fn = _make_stub_fetch(products)

    _, failed, _, _, unattempted = asyncio.run(
        _fetch_pages_concurrently(fetch_fn, [2, 3, 4], concurrency=1,
                                   seen_skus_initial={"ALREADY_SEEN"}))

    assert failed == []
    assert unattempted == [3, 4]
