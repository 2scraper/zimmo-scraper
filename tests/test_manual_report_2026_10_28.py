"""
tests/test_manual_report_2026_10_28.py
-----------------------------------------
Regression checks for the manual test report of 2026-10-27/28 (main at
c4795bb). Each test names the report's item it guards.

The shared modules are tested directly; the engines' REAL fetch loops are
driven against the fake browsers of test_paid_solves_and_verdicts.py (no
network, no paid call) and are skipped, not failed, when an engine's driver
is not installed.
"""

import argparse
import asyncio
import contextlib
import importlib
import io
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

import env_config  # noqa: E402
import output_writer as ow  # noqa: E402
import proxy_check  # noqa: E402
import proxy_pool  # noqa: E402
import smoke_test  # noqa: E402
from product_parser import parse_products, parse_result_count  # noqa: E402
from test_paid_solves_and_verdicts import ENGINES, Site, URL, TILES, _args  # noqa: E402

PAST_END = ("<html><body><h1>Immo te koop in Zwijnaarde (9052) 51 resultaten</h1>"
            "<p>We vonden helaas geen resultaten voor je zoekopdracht</p></body></html>")
WITH_COUNT = TILES.replace("<zimmo-listing", "<h1>Immo te koop in Gent (9000) 51 resultaten</h1>"
                           "<zimmo-listing", 1)


@pytest.fixture(params=sorted(ENGINES))
def engine(request):
    make = ENGINES[request.param]
    if request.param != "selenium":
        return make

    def selenium(monkeypatch, site):
        # The shared fake driver answers no innerText and waits out real
        # WebDriverWait timeouts; give it the page text and short waits.
        mod, run = make(monkeypatch, site)
        build = mod.build_driver

        def build_with_text(args, proxy_server_only=None):
            d = build(args, proxy_server_only)
            plain = d.execute_script
            d.execute_script = (lambda script, *a: d.page_source if "innerText" in script
                                else plain(script, *a))
            return d
        monkeypatch.setattr(mod, "build_driver", build_with_text)
        monkeypatch.setattr(mod._wait_for_listing_markers, "__defaults__", (0.3,))
        return mod, run
    selenium.__name__ = make.__name__
    return selenium


def _meta(tmp_path):
    return json.load(open(tmp_path / "run.meta.json"))


# --- #3: Brussels minus grades -------------------------------------------------

def test_epb_minus_grade_is_read():
    tile = TILES.replace("energy-labels/epb_f.svg", "energy-labels/epb_e_minus.svg")
    rows = {p.sku: p for p in parse_products(tile, URL)}
    assert rows["LRZK9"].epc_label == "E-"
    assert rows["LR7M2"].epc_label == "A+"


# --- #32: the price-filter chip is not a listing -------------------------------

def test_filter_panel_outside_the_tiles_is_not_a_row():
    chip = ('<div class="filters"><a href="/nl"><img src="assets/favicon.svg"/></a>'
            '<span>€ 200.000 – € 400.000</span>'
            '<a href="/nl/uitgebreid-zoeken?search=eyJmaWx0ZXIi">Filters Meer filters</a></div>')
    rows = parse_products(chip + TILES, URL + "filter?price=200000-400000")
    assert rows and all(p.sku for p in rows), [(p.sku, p.url) for p in rows]
    assert len(rows) == len(parse_products(TILES, URL))


# --- #1, #2, #26: the result count decides what an empty page means ------------

def test_result_count_and_page_verdicts():
    assert parse_result_count(PAST_END) == 51
    assert parse_result_count("<p>0 résultats</p>") == 0
    assert parse_result_count("<p>1.234 resultaten</p>") == 1234
    assert parse_result_count("<p>loading</p>") is None
    assert ow.classify_page(0, 200, False, False, 51) == ow.PAGE_PAST_END
    assert ow.classify_page(0, 200, False, False, 0) == ow.PAGE_EMPTY
    assert ow.classify_page(0, 200, False, False, None) == ow.PAGE_INCOMPLETE
    assert ow.page_outcome_is_ok(ow.PAGE_PAST_END)
    assert not ow.page_outcome_is_ok(ow.PAGE_INCOMPLETE)
    assert ow.should_retry_page(ow.PAGE_INCOMPLETE, 200)


def test_last_page_rule():
    rows = [ow.Product(sku=str(i), url=str(i)) for i in range(9)]
    assert ow.is_last_page([], 21, 51, 42)              # past the end
    assert ow.is_last_page(rows, 21, 51, 51)            # short page, count reached
    assert not ow.is_last_page(rows, 21, 51, 50)        # count not reached
    assert not ow.is_last_page(rows * 3, 21, 20, 27)    # full page: a count alone is not enough


def test_listing_end_on_the_last_requested_page_is_exhausted(tmp_path):
    code = ow.finish_run([ow.Product(sku="A", url="u")], str(tmp_path / "run"), "json",
                         pages_requested=2, pages_completed=2, listing_end_seen=True, url=URL)
    meta = _meta(tmp_path)
    assert code == 0 and meta["status"] == "complete" and meta["listing_exhausted"] is True


# --- #8: Ctrl-C ----------------------------------------------------------------

def test_interrupted_run_keeps_its_rows(tmp_path):
    code = ow.finish_run([ow.Product(sku="A", url="u")], str(tmp_path / "run"), "json",
                         pages_requested=3, pages_completed=1, unattempted_pages=[2, 3],
                         interrupted=True, url=URL)
    meta = _meta(tmp_path)
    assert code == ow.EXIT_PARTIAL and meta["stop_reason"] == "interrupted"
    code = ow.finish_run([], str(tmp_path / "none"), "json", pages_requested=3,
                         pages_completed=0, unattempted_pages=[1, 2, 3], interrupted=True)
    assert code == ow.EXIT_INTERRUPTED == 130
    assert not (tmp_path / "none.json").exists()
    assert json.load(open(tmp_path / "none.latest_attempt.meta.json"))["stop_reason"] == "interrupted"


# --- #9, #10, #19, #23: proxy arguments -----------------------------------------

def _proxy_args(**kw):
    return types.SimpleNamespace(**{"proxy": None, "proxy_file": None, **kw})


def test_colon_form_proxy_is_understood():
    for raw in ("eu2.proxy.example:2334:user-zone-x:SECRETPW",
                "http://eu2.proxy.example:2334:user-zone-x:SECRETPW"):
        e = proxy_pool.parse_proxy_url(raw)
        assert (e.scheme, e.host, e.port, e.username, e.password) == (
            "http", "eu2.proxy.example", 2334, "user-zone-x", "SECRETPW")
        assert "SECRETPW" not in e.masked()


def test_proxy_errors_never_print_the_password(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text("eu2.proxy.example:notaport:user:SECRETPW\n")
    with pytest.raises(ValueError) as err:
        proxy_pool.proxy_urls_from_args(_proxy_args(proxy_file=str(bad)))
    assert "SECRETPW" not in str(err.value)
    with pytest.raises(ValueError) as err:
        proxy_pool.parse_proxy_url("http://user:SECRETPW@host-without-port")
    assert "SECRETPW" not in str(err.value)


@pytest.mark.parametrize("make, needle", [
    (lambda p: _proxy_args(proxy_file=str(p / "missing.txt")), "could not read --proxy-file"),
    (lambda p: (p / "e.txt").write_text("# none\n\n") and _proxy_args(proxy_file=str(p / "e.txt")),
     "holds no proxy"),
    (lambda p: _proxy_args(proxy="socks5://user:SECRETPW@127.0.0.1:1080"), "cannot authenticate a SOCKS"),
])
def test_proxy_argument_errors_are_usage_errors(tmp_path, make, needle):
    with pytest.raises(ValueError) as err:
        proxy_pool.proxy_urls_from_args(make(tmp_path))
    assert needle in str(err.value) and "SECRETPW" not in str(err.value)


# --- #26, #27: exit selection -----------------------------------------------------

def test_pool_prefers_exits_that_have_not_failed_and_never_hands_out_a_retired_one():
    pool = proxy_pool.ProxyPool(["http://a:1", "http://b:2", "http://c:3"], block_retries=1)
    a, b, c = pool._exits
    assert pool.after_failure(a, 0, True, "Timeout", "x") is b
    assert pool.after_failure(b, 0, True, "Timeout", "x") is c
    # a and b are retired: every page now starts on c, never on a.
    assert {pool.get_exit_for_worker(i).host for i in range(5)} == {"c"}
    assert pool.after_failure(c, 0, True, "Timeout", "x") is None
    assert pool.get_exit_for_worker(0) is None


def test_no_rotate_logs_one_line(caplog):
    pool = proxy_pool.ProxyPool(["http://a:1", "http://b:2"])
    with caplog.at_level(logging.WARNING, logger="proxy_pool"):
        assert pool.after_failure(pool._exits[0], 0, False, "Proxy error", "x") is pool._exits[0]
    assert len(caplog.records) == 1 and "rotating" not in caplog.text


# --- #6: a refused first exit no longer refuses the pool -------------------------

def _stub_proxy(status):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.recv(4096)
                conn.sendall(f"HTTP/1.1 {status} X\r\nContent-Length: 0\r\n\r\n".encode())
    threading.Thread(target=serve, daemon=True).start()
    return srv, f"http://user:pass@127.0.0.1:{srv.getsockname()[1]}"


def test_precheck_retires_a_refused_exit_and_keeps_the_rest():
    s407, u407 = _stub_proxy(407)
    s200, u200 = _stub_proxy(200)
    try:
        pool = proxy_pool.ProxyPool([u407, u200])
        assert proxy_check.gate(pool, URL, timeout=5) is None
        assert pool.get_exit_for_worker(0).raw_url == u200
        only = proxy_pool.ProxyPool([u407, u407.replace("user:pass", "login:password")])
        assert proxy_check.gate(only, URL, timeout=5).stop_reason == "proxy_auth_failed"
    finally:
        s407.close()
        s200.close()


# --- #17: an explicitly empty value means "off" ----------------------------------

def test_empty_export_or_flag_does_not_fall_through_to_dotenv(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("ZIMMO_PROXY=http://from-dotenv:1\nZIMMO_CDP_ENDPOINT=ws://from-dotenv:2\n")
    monkeypatch.setenv("ZIMMO_PROXY", "")
    monkeypatch.delenv("ZIMMO_CDP_ENDPOINT", raising=False)
    args = argparse.Namespace(proxy=None, cdp_endpoint="")
    env_config.apply_env_defaults(args, dotenv_path=str(dotenv))
    assert args.proxy is None and args.cdp_endpoint == ""
    args = argparse.Namespace(proxy=None, cdp_endpoint=None)
    monkeypatch.delenv("ZIMMO_PROXY")
    env_config.apply_env_defaults(args, dotenv_path=str(dotenv), skip=("proxy",))
    assert args.proxy is None and args.cdp_endpoint == "ws://from-dotenv:2"


# --- #5, #7, #11, #12, #13, #28: checked before anything is fetched -------------

def _validate(tmp_path, **kw):
    args = argparse.Namespace(**{
        "url": URL, "out": str(tmp_path / "run"), "format": "both", "proxy": None,
        "proxy_file": None, "cdp_endpoint": None, "concurrency": 1, **kw})
    parser = argparse.ArgumentParser(prog="probe")
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            env_config.validate_run_args(parser, args)
        except SystemExit as e:
            return e.code, err.getvalue(), args
    return 0, "", args


@pytest.mark.parametrize("kw, needle", [
    ({"url": "x"}, "zimmo.be search-results URL"),
    ({"url": "https://example.com/"}, "zimmo.be search-results URL"),
    ({"url": URL + "?page=2"}, "page=2"),
    ({"out": "/nonexistent_dir_for_test/x"}, "does not exist"),
    ({"cdp_endpoint": "ws://127.0.0.1:1", "concurrency": 2}, "--concurrency"),
])
def test_usage_errors_exit_2(tmp_path, kw, needle):
    code, err, _ = _validate(tmp_path, **kw)
    assert code == 2 and needle in err, err


def test_out_naming_a_directory_is_refused(tmp_path):
    (tmp_path / "adir.json").mkdir()
    code, err, _ = _validate(tmp_path, out=str(tmp_path / "adir"), format="json")
    assert code == 2 and "is a directory" in err


def test_page_1_in_the_url_is_dropped(tmp_path):
    code, _, args = _validate(tmp_path, url=URL + "?page=1&x=y")
    assert code == 0 and args.url == URL + "?x=y"


def test_selenium_refuses_a_credentialled_cdp_endpoint(tmp_path):
    args = argparse.Namespace(url=URL, out=str(tmp_path / "run"), format="json", proxy=None,
                              proxy_file=None, cdp_endpoint="ws://user:pass@cb.example:9222",
                              concurrency=1)
    with contextlib.redirect_stderr(io.StringIO()) as err, pytest.raises(SystemExit) as e:
        env_config.validate_run_args(argparse.ArgumentParser(prog="p"), args,
                                     cdp_credentials_supported=False)
    assert e.value.code == 2 and "debuggerAddress" in err.getvalue()


def test_min_score_range():
    assert env_config.recaptcha_score("0.7") == 0.7
    for bad in ("5", "0", "1"):
        with pytest.raises(argparse.ArgumentTypeError):
            env_config.recaptcha_score(bad)


# --- #16: diff_runs reads a CSV run --------------------------------------------

def test_diff_runs_compares_a_csv_run_with_a_json_run(tmp_path):
    rows = [ow.Product(sku="A", url="u1", title="=1+1", price=250000.0, bedrooms=3,
                       surface_m2=120.5),
            ow.Product(sku="B", url="u2")]
    ow.finish_run(rows, str(tmp_path / "a"), "json", pages_requested=1, pages_completed=1, url=URL)
    ow.finish_run(rows, str(tmp_path / "b"), "csv", pages_requested=1, pages_completed=1, url=URL)
    run = lambda *a: subprocess.run([sys.executable, os.path.join(ROOT, "diff_runs.py"), *a],
                                    capture_output=True, text=True)
    r = run("--old", str(tmp_path / "a.json"), "--new", str(tmp_path / "b.csv"), "--fail-on-change")
    assert r.returncode == 0 and "Changed:         0" in r.stdout, r.stdout
    r = run("--old", str(tmp_path / "a.json"), "--new", str(tmp_path / "nope.json"))
    assert r.returncode == 2 and "no such file" in r.stdout


# --- #20: late asyncio errors do not end the log with a traceback ---------------

def test_late_connection_errors_are_one_debug_line(caplog):
    proxy_pool.quiet_late_connection_errors()
    log = logging.getLogger("asyncio")
    with caplog.at_level(logging.INFO):
        try:
            raise RuntimeError("server rejected WebSocket connection: HTTP 500")
        except RuntimeError:
            log.error("Task exception was never retrieved", exc_info=True)
    assert "Traceback" not in caplog.text and "never retrieved" not in caplog.text


# --- the engines' own loops -----------------------------------------------------

def test_page_past_the_end_ends_the_listing(engine, monkeypatch, tmp_path):
    """#2: the page after the last one is HTTP 200 with the search's count and
    no tile. It ends the run as complete and exhausted, and page 3 is never
    requested."""
    site = Site({1: (200, WITH_COUNT, 7), 2: (200, PAST_END, 0), 3: (200, WITH_COUNT, 7)})
    mod, run = engine(monkeypatch, site)
    assert run(_args(tmp_path, pages=3)) == 0
    meta = _meta(tmp_path)
    assert site.navigations == 2
    assert meta["status"] == "complete" and meta["listing_exhausted"] is True
    assert meta["exhausted_pages"] == [3]


def test_past_the_end_on_the_last_requested_page_is_still_exhausted(engine, monkeypatch, tmp_path):
    site = Site({1: (200, WITH_COUNT, 7), 2: (200, PAST_END, 0)})
    mod, run = engine(monkeypatch, site)
    assert run(_args(tmp_path, pages=2)) == 0
    assert _meta(tmp_path)["listing_exhausted"] is True


def test_cut_off_page_is_failed_not_empty(engine, monkeypatch, tmp_path):
    """#26: a page whose load was cut off (HTTP 200, no tile, no count) used
    to count as a served empty page."""
    site = Site({1: (200, WITH_COUNT, 7), 2: (200, "<html><body></body></html>", 0)})
    mod, run = engine(monkeypatch, site)
    assert run(_args(tmp_path, pages=2, retries=1)) == ow.EXIT_PARTIAL
    meta = _meta(tmp_path)
    assert meta["failed_pages"] == [2] and meta["listing_exhausted"] is False
    assert site.navigations == 3, "the cut-off page is retried once"


def _interrupt_on(site, page, exc):
    answer = site.answer

    def patched(url):
        if ("page=%d" % page) in url or (page == 1 and "page=" not in url):
            raise exc
        return answer(url)
    site.answer = patched


def test_ctrl_c_keeps_the_pages_already_fetched(engine, monkeypatch, tmp_path):
    """#8: Ctrl-C used to exit 1 and lose page 1's rows."""
    name = engine.__name__.strip("_")
    site = Site({1: (200, WITH_COUNT, 7), 2: (200, WITH_COUNT, 7), 3: (200, WITH_COUNT, 7)})
    mod, run = engine(monkeypatch, site)
    _interrupt_on(site, 2, asyncio.CancelledError() if name == "pyppeteer" else KeyboardInterrupt())
    assert run(_args(tmp_path, pages=3)) == ow.EXIT_PARTIAL
    meta = _meta(tmp_path)
    assert meta["stop_reason"] == "interrupted" and meta["product_count"] > 0
    assert meta["unattempted_pages"] == [2, 3]


def test_ctrl_c_on_page_1_exits_130(engine, monkeypatch, tmp_path):
    name = engine.__name__.strip("_")
    site = Site({1: (200, WITH_COUNT, 7)})
    mod, run = engine(monkeypatch, site)
    _interrupt_on(site, 1, asyncio.CancelledError() if name == "pyppeteer" else KeyboardInterrupt())
    assert run(_args(tmp_path, pages=2)) == ow.EXIT_INTERRUPTED
    assert not (tmp_path / "run.json").exists()


# --- #26 in each engine: a timeout through a proxy moves to another exit ---------

def _proxied_args(tmp_path, servers):
    args = _args(tmp_path, pages=2, retries=2)
    args.proxy_urls = list(servers)
    args.proxy_budget_mb = 0
    return args


def _no_precheck(monkeypatch):
    monkeypatch.setattr(proxy_check, "gate", lambda pool, url, timeout=None: None)


def test_playwright_timeout_through_a_proxy_rotates(monkeypatch, tmp_path):
    try:
        mod = importlib.import_module("playwright_scraper")
    except ImportError as e:
        pytest.skip(f"playwright unavailable: {e}")
    _no_precheck(monkeypatch)
    used = []

    class Page:
        url = ""

        def __init__(self, server):
            self.server = server

        def goto(self, url, **_):
            used.append((self.server, url))
            if self.server.endswith(":2"):
                raise mod.PWTimeout("Timeout 60000ms exceeded.")
            self.url, self.html = url, WITH_COUNT
            return types.SimpleNamespace(status=200)

        def content(self):
            return self.html

        def eval_on_selector_all(self, *_):
            return 7

        def wait_for_function(self, *_, **__):
            return None

        def wait_for_timeout(self, *_):
            return None

    class PW:
        def __init__(self):
            self.chromium = types.SimpleNamespace(launch=self._launch)

        def _launch(self, proxy=None, **_):
            server = proxy["server"]
            ctx = types.SimpleNamespace(new_page=lambda: Page(server), route=lambda *a: None,
                                        on=lambda *a: None)
            return types.SimpleNamespace(new_context=lambda **_: ctx, close=lambda: None)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(mod, "sync_playwright", PW)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    args = _proxied_args(tmp_path, ["http://silent:2", "http://a:1", "http://c:3"])
    assert mod.scrape(args) == 0
    page1 = [s for s, u in used if "page=" not in u]
    page2 = [s for s, u in used if "page=2" in u]
    # The silent exit fails once, the retry moves off it, and page 2 does
    # not go back to it (the report saw it chosen again on every page).
    assert page1 == ["http://silent:2", "http://a:1"], page1
    assert page2 and "http://silent:2" not in page2, page2


def test_pyppeteer_refused_handshake_is_reported_at_once():
    """#29: a 401 on the websocket upgrade used to cost 3 x 60 s."""
    try:
        mod = importlib.import_module("puppeteer_scraper")
    except ImportError as e:
        pytest.skip(f"pyppeteer unavailable: {e}")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
    threading.Thread(target=serve, daemon=True).start()

    async def go():
        return await mod._connect_cdp(f"ws://user:pass@127.0.0.1:{srv.getsockname()[1]}", timeout_s=20)
    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        with pytest.raises(mod.CdpRefused) as err:
            loop.run_until_complete(go())
        assert loop.time() - started < 10
        assert "401" in str(err.value) and ":pass@" not in str(err.value)
    finally:
        loop.close()
        srv.close()


def test_selenium_budget_block_enables_the_network_domain():
    """#15: Network.setBlockedURLs without Network.enable on that session."""
    import proxy_meter
    sent = []
    driver = types.SimpleNamespace(execute_cdp_cmd=lambda cmd, params: sent.append(cmd))
    meter = proxy_meter.ProxyMeter(0.000001)
    proxy_meter.install_selenium(driver, meter)
    meter.add(10)
    assert sent == ["Network.enable", "Network.setBlockedURLs"]
