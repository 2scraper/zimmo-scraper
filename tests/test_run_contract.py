"""
tests/test_run_contract.py
-----------------------------
The route the external audit of 2026-10-07 asked for: page outcome ->
run status -> files on disk -> diff_runs.py, driven through each engine's
real `scrape()` with only `_fetch_page` stubbed (no browser, no network).

The stub returns what the engine's own `_fetch_page` returns for each kind
of page, computed through the same shared `classify_page()` -- so these
tests pin the run-level contract, and smoke_test.py's
check_engines_use_shared_page_classification pins that every engine
really calls it.

Each engine is skipped, not failed, when its driver is not installed
(CLAUDE.md §10); the engine-smoke CI job runs each in its own venv.
"""

import asyncio
import importlib
import json
import os
import subprocess
import sys
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from output_writer import (  # noqa: E402
    Product, classify_page, page_outcome_is_ok, PAGE_BLOCKED,
    EXIT_OK, EXIT_BLOCKED, EXIT_FETCH_FAILED, EXIT_PARTIAL,
)

URL = "https://www.zimmo.be/nl/gent-9000/te-koop/"


def _rows(page):
    return [Product(sku=f"P{page}-{i}", url=f"https://www.zimmo.be/x/{page}/{i}", price=1.0)
            for i in range(6)]


def _page(products, status, challenge=False):
    """What _fetch_page returns for a page with this content and status."""
    outcome = classify_page(len(products), status, challenge, bool(products))
    return products, page_outcome_is_ok(outcome), outcome == PAGE_BLOCKED, False


ENGINES = [
    ("playwright_scraper", "scrape", False),
    ("puppeteer_scraper", "scrape_async", True),
    ("selenium_scraper", "scrape", False),
]


def _load(module_name):
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        pytest.skip(f"{module_name} unavailable: {e}")


def _run(engine, plan, pages, out, monkeypatch, url=URL):
    module_name, entry, is_async = engine
    mod = _load(module_name)
    fetched = []

    if is_async:
        async def fake(u, page_num, args, pool, worker_offset, cdp=None):
            fetched.append(page_num)
            return plan[page_num]
    else:
        def fake(u, page_num, args, pool, worker_offset, cdp=None):
            fetched.append(page_num)
            return plan[page_num]
    monkeypatch.setattr(mod, "_fetch_page", fake)

    args = types.SimpleNamespace(
        url=url, pages=pages, concurrency=1, proxy=None, proxy_file=None,
        proxy_shuffle=False, proxy_block_retries=3, cdp_endpoint=None,
        out=str(out), format="json", allow_empty=False,
    )
    result = getattr(mod, entry)(args)
    code = asyncio.run(result) if is_async else result
    meta_path = f"{out}.meta.json"
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
    return code, meta, fetched


@pytest.fixture(params=ENGINES, ids=[e[0] for e in ENGINES])
def engine(request):
    return request.param


def test_blocked_second_page_is_partial_not_complete(engine, tmp_path, monkeypatch):
    plan = {1: _page(_rows(1), 200), 2: _page([], 403, challenge=True)}
    code, meta, _ = _run(engine, plan, 2, tmp_path / "run", monkeypatch)
    assert code == EXIT_PARTIAL
    assert meta["status"] == "partial" and meta["failed_pages"] == [2]
    assert meta["pages_completed"] == 1


def test_blocked_first_page_stops_the_run(engine, tmp_path, monkeypatch):
    plan = {1: _page([], 403, challenge=True), 2: _page(_rows(2), 200)}
    code, meta, fetched = _run(engine, plan, 2, tmp_path / "run", monkeypatch)
    assert code == EXIT_BLOCKED
    assert fetched == [1], "page 1 blocked: no later page may be requested"
    assert meta is None, "a run with no data writes no sidecar"


def test_server_error_everywhere_is_fetch_failed_not_empty(engine, tmp_path, monkeypatch):
    plan = {1: _page([], 500), 2: _page([], 500)}
    code, meta, fetched = _run(engine, plan, 2, tmp_path / "run", monkeypatch)
    assert code == EXIT_FETCH_FAILED
    assert fetched == [1]


def test_server_error_on_second_page_is_partial(engine, tmp_path, monkeypatch):
    plan = {1: _page(_rows(1), 200), 2: _page([], 500)}
    code, meta, _ = _run(engine, plan, 2, tmp_path / "run", monkeypatch)
    assert code == EXIT_PARTIAL and meta["failed_pages"] == [2]


def test_exhausted_listing_is_complete_and_diffable(engine, tmp_path, monkeypatch):
    # Measured live 2026-10-07: past the last page, zimmo.be serves page 1 again.
    plan = {1: _page(_rows(1), 200), 2: _page(_rows(1), 200),
            3: _page(_rows(3), 200), 4: _page(_rows(4), 200)}
    code, meta, fetched = _run(engine, plan, 4, tmp_path / "old", monkeypatch)
    assert code == EXIT_OK and meta["status"] == "complete"
    assert meta["exhausted_pages"] == [3, 4] and meta["listing_exhausted"] is True
    assert fetched == [1, 2]

    # Second run: one listing gone from a listing read to its end -> removed.
    plan[1] = _page(_rows(1)[1:], 200)
    plan[2] = _page(_rows(1)[1:], 200)
    _run(engine, plan, 4, tmp_path / "new", monkeypatch)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "diff_runs.py"),
                        "--old", str(tmp_path / "old.json"), "--new", str(tmp_path / "new.json"),
                        "--fail-on-change"], capture_output=True, text=True)
    assert "Removed:         1" in r.stdout and r.returncode == 1, r.stdout


def test_diff_refuses_two_different_cities(engine, tmp_path, monkeypatch):
    plan = {1: _page(_rows(1), 200)}
    _run(engine, plan, 1, tmp_path / "gent", monkeypatch)
    _run(engine, plan, 1, tmp_path / "antw", monkeypatch,
         url="https://www.zimmo.be/nl/antwerpen-2000/te-koop/")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "diff_runs.py"),
                        "--old", str(tmp_path / "gent.json"), "--new", str(tmp_path / "antw.json")],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "different searches" in r.stdout


def test_pyppeteer_cdp_calls_are_bounded():
    """A Scraping Browser profile still busy from the previous page answers
    the websocket upgrade with HTTP 500; pyppeteer swallows that in a
    background task and its connect() waits forever (measured live
    2026-10-07: ten minutes on page 2). The bound turns it into a retry."""
    mod = _load("puppeteer_scraper")
    with pytest.raises(TimeoutError, match="CDP connect did not answer"):
        asyncio.run(mod._bounded(asyncio.sleep(5), 0.05, "CDP connect"))
