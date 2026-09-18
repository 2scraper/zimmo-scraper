"""
proxy_pool.py
-----------------
Proxy pool, rotation policy, credential masking, and argv safety. No
site knowledge.

Two fixes per external audit (2026-09-14, codex.md finding #4):
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


def parse_proxy_url(url: str) -> ProxyExit:
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.port:
        # Redact BEFORE raising -- the raw url can carry a real
        # username:password, and this exception's text is a log the
        # moment anything catches and prints it.
        raise ValueError(
            f"Not a valid proxy URL (need scheme://host:port): {redact_secret_patterns(url)!r}"
        )
    return ProxyExit(
        raw_url=url,
        scheme=parsed.scheme or "http",
        host=parsed.hostname,
        port=parsed.port,
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
            urls.append(line)
    return urls


class ProxyPool:
    def __init__(self, urls: List[str], shuffle: bool = False, block_retries: int = 3):
        if not urls:
            raise ValueError("ProxyPool needs at least one proxy URL")
        exits = [parse_proxy_url(u) for u in urls]
        if shuffle:
            random.shuffle(exits)
        self._exits = exits
        self._block_retries = block_retries

    def __len__(self) -> int:
        return len(self._exits)

    def get_exit_for_worker(self, worker_offset: int) -> ProxyExit:
        return self._exits[worker_offset % len(self._exits)]

    def rotate_away_from(self, dead: ProxyExit, worker_offset: int) -> Optional[ProxyExit]:
        """Called on EVERY failure of `dead` -- not just once its
        failure_count crosses --proxy-block-retries. That threshold now
        only controls when an exit is excluded from the pool ENTIRELY,
        not whether THIS retry gets a fresh exit."""
        dead.failure_count += 1
        if dead.failure_count >= self._block_retries:
            logger.warning("proxy_pool: %s failed %d time(s) -- treating as blocked, "
                            "will not be re-selected for this run.",
                            dead.masked(), dead.failure_count)

        candidates = [e for e in self._exits if e.failure_count < self._block_retries and e is not dead]
        if not candidates:
            if dead.failure_count < self._block_retries:
                return dead
            logger.error("proxy_pool: every exit in the pool has failed --block-retries "
                          "times -- no exit left to rotate to.")
            return None
        return candidates[worker_offset % len(candidates)]


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
