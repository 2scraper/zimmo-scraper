"""
tests/test_paid_solves_and_verdicts.py
-----------------------------------------
Each engine's REAL fetch loop, against a fake browser (no network, no
paid call: the two 2captcha.com submit functions are stubbed and only
counted).

- One page gets at most one paid solve, however many attempts it takes
  and however many challenge markers it carries. Before 2026-10-14 a page
  answering 503 with a Turnstile widget bought retries+1 solves, and a
  page with Turnstile and reCAPTCHA markers bought two.
- A page whose listing grid rendered but parsed to zero rows is a parser
  failure (exit 5 with no rows, exit 6 with rows), never "the search is
  empty" (exit 4).
- --retries below 0 is an argparse error (exit 2); it used to run zero
  navigations and report exit 5.

Skipped, not failed, when an engine's driver is not installed; the
engine-smoke CI job runs each engine in its own venv.
"""

import asyncio
import importlib
import json
import os
import sys
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import captcha_solver  # noqa: E402
import smoke_test  # noqa: E402

URL = "https://www.zimmo.be/nl/gent-9000/te-koop/"
TURNSTILE = smoke_test.SAMPLE_TURNSTILE_HTML
BOTH_MARKERS = smoke_test.SAMPLE_TURNSTILE_HTML + smoke_test.SAMPLE_RECAPTCHA_HTML
TILES = smoke_test.LIVE_ANGULAR_TILES_HTML


class Site:
    """What each page URL answers: (status, html, listing-link count)."""

    def __init__(self, pages):
        self.pages = pages
        self.navigations = 0

    def answer(self, url):
        self.navigations += 1
        page = int(url.split("page=")[1]) if "page=" in url else 1
        return self.pages[page]


@pytest.fixture
def purchases(monkeypatch):
    bought = []
    monkeypatch.setattr(captcha_solver, "_solve_turnstile_with_2captcha",
                        lambda key, ch, **kw: bought.append("turnstile") or "tok")
    monkeypatch.setattr(captcha_solver, "_solve_with_2captcha",
                        lambda key, ch, **kw: bought.append("recaptcha") or "tok")
    return bought


def _args(tmp_path, pages=1, retries=2):
    return types.SimpleNamespace(
        url=URL, pages=pages, concurrency=1, proxy=None, proxy_file=None,
        proxy_shuffle=False, proxy_block_retries=3, proxy_rotate=True,
        cdp_endpoint=None, out=str(tmp_path / "run"), format="json",
        allow_empty=False, retries=retries, retry_delay=0, delay=0, category=None,
        solve_captcha="when-blocked", twocaptcha_key="stub-key-never-sent", min_score=None,
        dump_html=False, headless=True,
    )


# --- Playwright ---------------------------------------------------------------

def _playwright(monkeypatch, site):
    try:
        mod = importlib.import_module("playwright_scraper")
    except ImportError as e:
        pytest.skip(f"playwright unavailable: {e}")

    class Page:
        url = ""

        def goto(self, url, **_):
            self.url = url
            self.status, self.html, self.count = site.answer(url)
            return types.SimpleNamespace(status=self.status)

        def content(self):
            return self.html

        def eval_on_selector_all(self, *_):
            return self.count

        def wait_for_function(self, *_, **__):
            if self.count <= mod.MIN_CARD_MATCHES:
                raise TimeoutError("no listing markers")

        def wait_for_timeout(self, *_):
            return None

        def evaluate(self, *_):
            return None

        def reload(self, **_):
            return None

        def screenshot(self, **_):
            return None

    class Browser:
        def new_context(self, **_):
            return types.SimpleNamespace(new_page=Page)

        def close(self):
            return None

    class PW:
        chromium = types.SimpleNamespace(launch=lambda **_: Browser())

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(mod, "sync_playwright", PW)
    return mod, (lambda args: mod.scrape(args))


# --- pyppeteer ----------------------------------------------------------------

def _pyppeteer(monkeypatch, site):
    try:
        mod = importlib.import_module("puppeteer_scraper")
    except ImportError as e:
        pytest.skip(f"pyppeteer unavailable: {e}")

    class Page:
        url = ""

        async def goto(self, url, *_):
            self.url = url
            self.status, self.html, self.count = site.answer(url)
            return types.SimpleNamespace(status=self.status)

        async def content(self):
            return self.html

        async def querySelectorAllEval(self, *_):
            return self.count

        async def waitForFunction(self, *_, **__):
            if self.count <= mod.MIN_CARD_MATCHES:
                raise TimeoutError("no listing markers")

        async def evaluate(self, *_):
            return None

        async def reload(self, *_):
            return None

        async def screenshot(self, *_):
            return None

    class Browser:
        async def pages(self):
            return [Page()]

        async def close(self):
            return None

    async def launch(**_):
        return Browser()

    async def no_sleep(*_):
        return None

    monkeypatch.setattr(mod, "launch", launch)
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    return mod, (lambda args: asyncio.run(mod.scrape_async(args)))


# --- Selenium -----------------------------------------------------------------

def _selenium(monkeypatch, site):
    try:
        mod = importlib.import_module("selenium_scraper")
    except ImportError as e:
        pytest.skip(f"selenium unavailable: {e}")

    class Driver:
        current_url = ""

        def get(self, url):
            self.current_url = url
            self.status, self.page_source, self.count = site.answer(url)

        def execute_script(self, script, *_):
            return "complete" if "readyState" in script else None

        def find_elements(self, *_):
            return [object()] * self.count

        def refresh(self):
            return None

        def save_screenshot(self, *_):
            return None

        def quit(self):
            return None

        close = quit

    monkeypatch.setattr(mod, "build_driver", lambda args, proxy_server_only=None: Driver())
    monkeypatch.setattr(mod, "_navigation_status", lambda driver: driver.status)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    return mod, (lambda args: mod.scrape(args))


ENGINES = {"playwright": _playwright, "pyppeteer": _pyppeteer, "selenium": _selenium}


@pytest.fixture(params=sorted(ENGINES))
def engine(request):
    return ENGINES[request.param]


def test_one_paid_solve_per_page_across_retries(engine, monkeypatch, tmp_path, purchases):
    site = Site({1: (503, TURNSTILE, 0)})
    mod, run = engine(monkeypatch, site)
    code = run(_args(tmp_path, retries=5))
    assert site.navigations == 6, "503 is retried: six attempts"
    assert purchases == ["turnstile"], f"bought {purchases} for one page"
    assert code == 5


def test_one_paid_solve_for_a_page_with_two_markers(engine, monkeypatch, tmp_path, purchases):
    site = Site({1: (503, BOTH_MARKERS, 0)})
    mod, run = engine(monkeypatch, site)
    run(_args(tmp_path, retries=0))
    assert len(purchases) == 1, f"bought {purchases} for one page"


def test_each_page_has_its_own_allowance(engine, monkeypatch, tmp_path, purchases):
    rows = TILES
    site = Site({1: (200, rows + TURNSTILE, 6), 2: (503, TURNSTILE, 0)})
    mod, run = engine(monkeypatch, site)
    args = _args(tmp_path, pages=2, retries=1)
    args.solve_captcha = "always"
    run(args)
    assert len(purchases) == 2, "one solve on page 1, one on page 2"


def test_rendered_grid_that_parses_to_nothing_is_a_parser_failure(engine, monkeypatch, tmp_path):
    site = Site({1: (200, TILES, 6)})
    mod, run = engine(monkeypatch, site)
    monkeypatch.setattr(mod, "parse_products", lambda html, url, category=None: [])
    code = run(_args(tmp_path))
    assert code == 5, "a parser failure is not an empty search (4)"
    assert site.navigations == 1, "a parser failure is not retried"
    assert not (tmp_path / "run.json").exists()


def test_parser_failure_after_good_page_is_partial(engine, monkeypatch, tmp_path):
    from product_parser import parse_products as real_parse
    site = Site({1: (200, TILES, 6), 2: (200, TILES.replace("LR", "QQ"), 6)})
    mod, run = engine(monkeypatch, site)
    monkeypatch.setattr(mod, "parse_products",
                        lambda html, url, category=None: [] if "page=2" in url
                        else real_parse(html, url, category=category))
    code = run(_args(tmp_path, pages=2))
    meta = json.load(open(tmp_path / "run.meta.json"))
    assert code == 6
    assert meta["status"] == "partial" and meta["parser_failed_pages"] == [2]
    assert "parser" in meta["stop_reason"]


@pytest.mark.parametrize("module_name", ["playwright_scraper", "puppeteer_scraper",
                                         "selenium_scraper"])
@pytest.mark.parametrize("argv", [["--retries", "-1"], ["--pages", "0"], ["--concurrency", "0"],
                                  ["--delay", "-1"],
                                  ["--cdp-endpoint", "ws://x.invalid:1", "--proxy",
                                   "http://h.invalid:1"]])
def test_bad_flag_values_are_usage_errors(module_name, argv, monkeypatch, tmp_path):
    try:
        mod = importlib.import_module(module_name)
    except ImportError as e:
        pytest.skip(f"{module_name} unavailable: {e}")
    monkeypatch.chdir(tmp_path)           # no stray .env
    for var in ("ZIMMO_PROXY", "ZIMMO_CDP_ENDPOINT", "ZIMMO_URL", "TWOCAPTCHA_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "argv", [module_name, "--url", URL] + argv)
    with pytest.raises(SystemExit) as e:
        mod.parse_args()
    assert e.value.code == 2


@pytest.mark.parametrize("module_name", ["playwright_scraper", "puppeteer_scraper",
                                         "selenium_scraper"])
def test_retries_zero_is_one_attempt(module_name, monkeypatch, tmp_path):
    try:
        mod = importlib.import_module(module_name)
    except ImportError as e:
        pytest.skip(f"{module_name} unavailable: {e}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [module_name, "--url", URL, "--retries", "0"])
    assert mod.parse_args().retries == 0
