"""
tests/test_cdp_connection.py
--------------------------------
--cdp-endpoint opens ONE connection per run, not one per page.

Measured live on 2026-10-07: reconnecting for every page made the 2Captcha
Scraping Browser answer HTTP 500 on the next page's websocket upgrade (one
live connection per profile), costing a retry on pages 2 and 3 of a 3-page
run -- and with pyppeteer, a ten-minute hang before the connect timeout
existed. These tests drive each engine's REAL scrape() and _fetch_page()
against a fake remote browser that counts connections, so the property is
pinned without a network or a paid session.

Skipped, not failed, when an engine's driver is not installed (CLAUDE.md §10).
"""

import asyncio
import importlib
import json
import os
import sys
import threading
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import smoke_test  # noqa: E402  (LIVE_ANGULAR_TILES_HTML: real tiles, six listings)

SKUS = ("LRZLA", "LR7M2", "KLQRM", "LRZ2M", "LRZK9", "KRBP7")
URL = "https://www.zimmo.be/nl/gent-9000/te-koop/"
ENDPOINT = "ws://user:pass@cb.example.invalid:9222"


def _html_for(url):
    """Six real tiles, with skus made unique per page so no page reads as
    the end of the listing."""
    page = int(url.split("page=")[1]) if "page=" in url else 1
    html = smoke_test.LIVE_ANGULAR_TILES_HTML
    for sku in SKUS:
        html = html.replace(sku, f"{sku}{page}")
    return html


def _args(tmp_path, pages):
    return types.SimpleNamespace(
        url=URL, pages=pages, concurrency=1, proxy=None, proxy_file=None,
        proxy_shuffle=False, proxy_block_retries=3, proxy_rotate=True,
        cdp_endpoint=ENDPOINT, out=str(tmp_path / "run"), format="json",
        allow_empty=False, retries=2, retry_delay=0, delay=0, category=None,
        solve_captcha="never", twocaptcha_key=None, min_score=None,
        dump_html=False, headless=True,
    )


class Remote:
    """What the fake remote browser saw."""

    def __init__(self, fail_connects=0, fail_goto=()):
        self.connects = 0
        self.fail_connects = fail_connects
        self.fail_goto = set(fail_goto)     # urls whose FIRST goto raises
        self.pages_opened = 0
        self.pages_closed = 0
        self.goto_threads = set()


# --- Playwright (sync) ------------------------------------------------------

class _PWPage:
    def __init__(self, remote):
        self.remote, self.url = remote, ""

    def goto(self, url, **_):
        self.remote.goto_threads.add(threading.get_ident())
        if url in self.remote.fail_goto:
            self.remote.fail_goto.discard(url)
            raise RuntimeError("Target page, context or browser has been closed")
        self.url = url
        return types.SimpleNamespace(status=200)

    def content(self):
        return _html_for(self.url)

    def eval_on_selector_all(self, *_):
        return 6

    def wait_for_function(self, *_, **__):
        return None

    def wait_for_timeout(self, *_):
        return None

    def close(self):
        self.remote.pages_closed += 1


class _PWContext:
    def __init__(self, remote):
        self.remote = remote

    def new_page(self):
        self.remote.pages_opened += 1
        return _PWPage(self.remote)

    def new_cdp_session(self, _page):
        return types.SimpleNamespace(send=lambda *a, **k: None, on=lambda *a, **k: None)


class _PWBrowser:
    def __init__(self, remote):
        self.contexts = [_PWContext(remote)]
        self.connected = True

    def is_connected(self):
        return self.connected


def _fake_sync_playwright(remote):
    class Driver:
        def __init__(self):
            self.browsers = []
            self.chromium = types.SimpleNamespace(connect_over_cdp=self._connect)

        def _connect(self, endpoint):
            assert endpoint == ENDPOINT
            if remote.fail_connects:
                remote.fail_connects -= 1
                raise RuntimeError("WebSocket error: 500 Internal Server Error")
            remote.connects += 1
            browser = _PWBrowser(remote)
            self.browsers.append(browser)
            return browser

        def stop(self):
            for b in self.browsers:
                b.connected = False

    return lambda: types.SimpleNamespace(start=Driver)


def _playwright(monkeypatch, remote):
    try:
        mod = importlib.import_module("playwright_scraper")
    except ImportError as e:
        pytest.skip(f"playwright unavailable: {e}")
    monkeypatch.setattr(mod, "sync_playwright", _fake_sync_playwright(remote))
    return mod


def test_playwright_one_connection_for_three_pages(monkeypatch, tmp_path):
    remote = Remote()
    mod = _playwright(monkeypatch, remote)
    assert mod.scrape(_args(tmp_path, 3)) == 0
    meta = json.load(open(tmp_path / "run.meta.json"))
    assert meta["status"] == "complete" and meta["product_count"] == 18
    assert remote.connects == 1, "one CDP connection for the whole run"
    assert remote.pages_opened == remote.pages_closed == 3, "a fresh tab per page, each closed"
    assert remote.goto_threads == {threading.get_ident()}, \
        "Playwright's sync objects must stay on the thread that created them"


def test_playwright_refused_connect_is_retried(monkeypatch, tmp_path):
    remote = Remote(fail_connects=1)
    mod = _playwright(monkeypatch, remote)
    assert mod.scrape(_args(tmp_path, 2)) == 0
    assert remote.connects == 1


def test_playwright_failed_attempt_drops_the_connection(monkeypatch, tmp_path):
    remote = Remote(fail_goto={URL + "?page=2"})
    mod = _playwright(monkeypatch, remote)
    assert mod.scrape(_args(tmp_path, 3)) == 0
    assert remote.connects == 2, "the retry reconnects; the pages after it reuse that"
    assert remote.pages_opened == remote.pages_closed == 4


# --- pyppeteer (async) ------------------------------------------------------

class _PypPage:
    def __init__(self, remote):
        self.remote, self.url = remote, ""
        self.target = types.SimpleNamespace(createCDPSession=self._session)

    async def _session(self):
        async def send(*_a, **_k):
            return None
        return types.SimpleNamespace(send=send, on=lambda *a, **k: None)

    async def goto(self, url, *_):
        if url in self.remote.fail_goto:
            self.remote.fail_goto.discard(url)
            raise RuntimeError("Connection closed")
        self.url = url
        return types.SimpleNamespace(status=200)

    async def content(self):
        return _html_for(self.url)

    async def querySelectorAllEval(self, *_):
        return 6

    async def waitForFunction(self, *_, **__):
        return None

    async def close(self):
        self.remote.pages_closed += 1


def _pyppeteer(monkeypatch, remote):
    try:
        mod = importlib.import_module("puppeteer_scraper")
    except ImportError as e:
        pytest.skip(f"pyppeteer unavailable: {e}")

    async def connect(browserWSEndpoint):
        assert browserWSEndpoint == ENDPOINT
        if remote.fail_connects:
            remote.fail_connects -= 1
            raise RuntimeError("server rejected WebSocket connection: HTTP 500")
        remote.connects += 1

        async def new_page():
            remote.pages_opened += 1
            return _PypPage(remote)

        async def disconnect():
            return None
        return types.SimpleNamespace(newPage=new_page, disconnect=disconnect)

    monkeypatch.setattr(mod, "connect", connect)
    return mod


def test_pyppeteer_one_connection_for_three_pages(monkeypatch, tmp_path):
    remote = Remote()
    mod = _pyppeteer(monkeypatch, remote)
    assert asyncio.run(mod.scrape_async(_args(tmp_path, 3))) == 0
    assert json.load(open(tmp_path / "run.meta.json"))["product_count"] == 18
    assert remote.connects == 1
    assert remote.pages_opened == remote.pages_closed == 3


def test_pyppeteer_failed_attempt_drops_the_connection(monkeypatch, tmp_path):
    remote = Remote(fail_connects=1, fail_goto={URL + "?page=2"})
    mod = _pyppeteer(monkeypatch, remote)
    assert asyncio.run(mod.scrape_async(_args(tmp_path, 3))) == 0
    assert remote.connects == 2
    assert remote.pages_opened == remote.pages_closed == 4
