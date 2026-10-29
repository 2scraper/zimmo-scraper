"""
proxy_pool.py
-----------------
Proxy pool, rotation policy, credential masking, and argv safety. No
site knowledge.

Two fixes per external audit (2026-09-14, finding #4):
  1. A failed proxy exit now rotates away IMMEDIATELY on the next
     attempt, rather than staying a candidate until its failure count
     reaches --proxy-block-retries. Previously, with the default of 2
     retries, a good next exit could only be selected on the LAST
     attempt of a page that had already burned its other retries on
     the bad one. `rotate_away_from()` is now called on every single
     failure; `--proxy-block-retries` still controls how many TOTAL
     failures permanently retire an exit from the pool, a different,
     slower-moving concern than "don't retry the same dead exit".
  2. `parse_proxy_url()`'s own ValueError previously embedded the raw
     URL VERBATIM, including any username:password -- an exception
     message is a log, and this one printed a real credential the
     first time a proxy URL was malformed.
"""

import logging
import random
import re
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("proxy_pool")

_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "net::ERR_PROXY_CONNECTION_FAILED",
    "net::ERR_TUNNEL_CONNECTION_FAILED",
    "407",  # Proxy Authentication Required -- confirmed live against a
            # real 2Captcha proxy exit during audit; per 2Captcha's own
            # proxy-error guide this means a bad login/password/zone/
            # account-state issue, and is exactly as much "this exit is
            # no good" as a connection failure, not a timeout to retry.
)

_CREDENTIAL_URL_RE = re.compile(r"(://)[^\s/:@]+:[^\s/@]+(@)")
_KEY_PARAM_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s\"')]+)", re.IGNORECASE
)


def redact_secret_patterns(text: str) -> str:
    text = _CREDENTIAL_URL_RE.sub(r"\1***:***\2", text)
    text = _KEY_PARAM_RE.sub(r"\1=***", text)
    return text


@dataclass
class ProxyExit:
    raw_url: str
    scheme: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    failure_count: int = 0

    def masked(self) -> str:
        if self.username:
            return f"{self.scheme}://***:***@{self.host}:{self.port}"
        return f"{self.scheme}://{self.host}:{self.port}"

    def server_only(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    def auth_tuple(self) -> Optional[Tuple[str, str]]:
        if self.username is None:
            return None
        return (self.username, self.password or "")


# `host:port:user:pass`, optionally with a scheme in front: the form the
# 2Captcha proxy dashboard hands out. Reported 2026-10-28: passed as
# --proxy it raised a ValueError whose text held the password verbatim,
# because the URL redaction above only knows `user:pass@`.
_COLON_FORM_RE = re.compile(
    r"^(?:(?P<scheme>[a-z][a-z0-9+.-]*)://)?(?P<host>[^:/@\s]+):(?P<port>\d{1,5})"
    r":(?P<user>[^:@\s]+):(?P<password>[^@\s]+)$", re.IGNORECASE)


def _bare_host(url: str) -> str:
    """The host part of a proxy string, for an error message that must not
    repeat the string itself (it can hold a password in any position)."""
    rest = url.split("://", 1)[-1]
    host = rest.rsplit("@", 1)[-1].split(":", 1)[0].split("/", 1)[0]
    return host or "?"


def normalize_proxy_url(url: str) -> str:
    """`host:port:user:pass` -> `http://user:pass@host:port`; anything else
    is returned unchanged."""
    url = url.strip()
    m = _COLON_FORM_RE.match(url)
    if not m or "@" in url:
        return url
    return (f"{(m.group('scheme') or 'http').lower()}://{m.group('user')}:"
            f"{m.group('password')}@{m.group('host')}:{m.group('port')}")


def parse_proxy_url(url: str) -> ProxyExit:
    url = normalize_proxy_url(url)
    try:
        parsed = urlparse(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        host = port = None
    if not host or not port:
        # Never quote the value: it can carry a password in a shape the
        # redaction does not recognise. The host is enough to find it.
        raise ValueError(
            f"Not a valid proxy (host {_bare_host(url)!r}): expected "
            f"scheme://user:pass@host:port, scheme://host:port or host:port:user:pass"
        )
    return ProxyExit(
        raw_url=url,
        scheme=(parsed.scheme or "http").lower(),
        host=host,
        port=port,
        username=parsed.username,
        password=parsed.password,
    )


def load_proxy_file(path: str) -> List[str]:
    urls: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(normalize_proxy_url(line))
    return urls


def proxy_urls_from_args(args) -> Optional[List[str]]:
    """The run's proxy list from --proxy-file or --proxy, validated; None
    without a proxy. Raises ValueError with a message safe to print (no
    credential) for an unreadable or empty file, an unparseable entry, or a
    SOCKS proxy with a login, which no engine here can use: Chromium does
    not authenticate SOCKS (Playwright says so at launch, after the run has
    already started)."""
    if getattr(args, "proxy_file", None):
        try:
            urls = load_proxy_file(args.proxy_file)
        except OSError as e:
            raise ValueError(f"could not read --proxy-file {args.proxy_file!r}: "
                             f"{e.strerror or type(e).__name__}") from None
        except UnicodeDecodeError:
            raise ValueError(f"--proxy-file {args.proxy_file!r} is not a UTF-8 text file") from None
        if not urls:
            raise ValueError(f"--proxy-file {args.proxy_file!r} holds no proxy "
                             f"(only blank lines and # comments)")
        where = "--proxy-file"
    elif getattr(args, "proxy", None):
        urls = [normalize_proxy_url(args.proxy)]
        where = "--proxy / ZIMMO_PROXY"
    else:
        return None
    for n, url in enumerate(urls, 1):
        try:
            exit_ = parse_proxy_url(url)
        except ValueError as e:
            raise ValueError(f"{where}, entry {n}: {e}") from None
        if exit_.scheme.startswith("socks") and exit_.username:
            raise ValueError(f"{where}, entry {n} ({exit_.masked()}): Chromium cannot "
                             f"authenticate a SOCKS proxy; use its http:// endpoint")
    return urls


class ProxyPool:
    """Exit selection, measured against a three-exit pool on 2026-10-28
    (good, silent, good): page 2 tried the silent exit twice and page 3
    went back to the exit that had just failed, while the third, working
    exit was never used. Now:

    * an exit that reached --proxy-block-retries failures in a row is not
      handed out while another exit is usable; a page that reached the site
      through an exit (record_success) clears its count, so slow pages
      spread over a long run do not retire a working exit;
    * among the rest, the exit with the FEWEST failures is chosen, so a page
      starts on an exit that has not failed this run whenever one exists;
    * when every exit has reached the limit, the next page still starts on
      the least-failed one (as before this change) -- only an exit the
      pre-check refused (retire()) is never used again;
    * rotate_away_from() never returns the exit that just failed while
      another usable one exists.
    """

    def __init__(self, urls: List[str], shuffle: bool = False, block_retries: int = 3):
        if not urls:
            raise ValueError("ProxyPool needs at least one proxy URL")
        exits = [parse_proxy_url(u) for u in urls]
        if shuffle:
            random.shuffle(exits)
        self._exits = exits
        self._block_retries = block_retries
        self._refused: set = set()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._exits)

    def _usable(self) -> List[ProxyExit]:
        return [e for e in self._exits if e.failure_count < self._block_retries]

    def _pick(self, candidates: List[ProxyExit], worker_offset: int) -> ProxyExit:
        fewest = min(e.failure_count for e in candidates)
        best = [e for e in candidates if e.failure_count == fewest]
        return best[worker_offset % len(best)]

    def get_exit_for_worker(self, worker_offset: int) -> Optional[ProxyExit]:
        """None only when every exit was refused by the pre-check: the
        caller must then fail the page, never fall back to a direct
        connection."""
        with self._lock:
            usable = self._usable() or [e for e in self._exits if id(e) not in self._refused]
            return self._pick(usable, worker_offset) if usable else None

    def record_success(self, good: ProxyExit) -> None:
        """A page reached the site through `good`: its failures were not in a row."""
        with self._lock:
            if id(good) not in self._refused:
                good.failure_count = 0

    def retire(self, dead: ProxyExit) -> None:
        """Take an exit out of the run for good (the pre-check's 407/402)."""
        with self._lock:
            self._refused.add(id(dead))
            dead.failure_count = max(dead.failure_count, self._block_retries)

    def rotate_away_from(self, dead: ProxyExit, worker_offset: int) -> Optional[ProxyExit]:
        """Called on EVERY failure of `dead` -- not just once its
        failure_count crosses --proxy-block-retries. That threshold now
        only controls when an exit is excluded from the pool ENTIRELY,
        not whether THIS retry gets a fresh exit."""
        with self._lock:
            dead.failure_count += 1
            if dead.failure_count >= self._block_retries:
                logger.warning("proxy_pool: %s failed %d time(s) -- treating as blocked, "
                                "will not be re-selected for this run.",
                                dead.masked(), dead.failure_count)

            candidates = [e for e in self._usable() if e is not dead]
            if not candidates:
                if dead.failure_count < self._block_retries:
                    return dead
                logger.error("proxy_pool: every exit in the pool has failed --block-retries "
                              "times -- no exit left to rotate to.")
                return None
            return self._pick(candidates, worker_offset)

    def after_failure(self, failed: ProxyExit, worker_offset: int, rotate: bool,
                      what: str, detail: str) -> Optional[ProxyExit]:
        """Count one failure of `failed` (a proxy error, or a navigation that
        timed out through it) and return the exit for the next attempt;
        None when no usable exit is left. Logs ONE line saying what happens
        next -- it used to say "rotating" and then, with --no-proxy-rotate,
        "retrying the SAME exit" right after it."""
        if not rotate:
            with self._lock:
                failed.failure_count += 1
            logger.warning("%s on exit %s (%s) -- --no-proxy-rotate: retrying the same exit.",
                           what, failed.masked(), detail)
            return failed
        nxt = self.rotate_away_from(failed, worker_offset)
        if nxt is None:
            logger.warning("%s on exit %s (%s) -- no usable exit left.", what, failed.masked(), detail)
        elif nxt is failed:
            logger.warning("%s on exit %s (%s) -- no other usable exit, retrying this one.",
                           what, failed.masked(), detail)
        else:
            logger.warning("%s on exit %s (%s) -- rotating to %s.",
                           what, failed.masked(), detail, nxt.masked())
        return nxt


class _LateConnectionErrorFilter(logging.Filter):
    """asyncio reports a future nobody awaited -- a CDP connect abandoned
    after its timeout, a browser connection closed under a callback -- with
    a 20-line traceback when the object is collected, often after the run
    has written its result (report of 2026-10-28: "Task exception was never
    retrieved ... InvalidStatusCode" after a successful 63-row run). The
    attempt it belonged to was already handled; keep one line, at DEBUG."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.msg)
        if not ("exception was never retrieved" in msg or msg.startswith("Exception in callback")):
            return True
        exc = record.exc_info[1] if record.exc_info else None
        record.msg = "Late error from an already-closed browser connection, ignored: %s"
        record.args = (redact_secret_patterns(
            f"{type(exc).__name__}: {exc}" if exc else msg.splitlines()[0])[:300],)
        record.exc_info = None
        record.exc_text = None
        record.levelno, record.levelname = logging.DEBUG, "DEBUG"
        return logging.getLogger().isEnabledFor(logging.DEBUG)


def quiet_late_connection_errors() -> None:
    log = logging.getLogger("asyncio")
    if not any(isinstance(f, _LateConnectionErrorFilter) for f in log.filters):
        log.addFilter(_LateConnectionErrorFilter())


def is_proxy_error(error_message: str) -> bool:
    return any(marker in error_message for marker in _PROXY_ERROR_MARKERS)


def warn_if_concurrency_without_pool(concurrency: int, pool: Optional[ProxyPool]) -> None:
    if concurrency > 1 and pool is None:
        logger.warning(
            "--concurrency %d was requested with no --proxy/--proxy-file configured -- "
            "all %d workers will share the SAME exit address, sending %dx the traffic "
            "from one IP. This is faster to get scored than to gather data; consider "
            "a proxy pool before raising concurrency.", concurrency, concurrency, concurrency)


def refuse_concurrency_with_cdp_endpoint(concurrency: int, cdp_endpoint: Optional[str]) -> None:
    if concurrency > 1 and cdp_endpoint:
        raise SystemExit(
            "--concurrency > 1 is not supported together with --cdp-endpoint: the "
            "Scraping Browser API allows one live connection per profile, so workers "
            "would collide with a profile_locked error. Use a different --cdp-endpoint "
            "pid per run instead, one run each."
        )
