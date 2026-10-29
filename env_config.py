"""
env_config.py
-----------------
Hand-rolled .env loader for zimmo-scraper, per the 2scraper family
template. No new dependency -- defers to python-dotenv
only if the project already has it installed; otherwise parses
KEY=VALUE lines itself. ~30 lines of actual logic, as prescribed.

Precedence, highest first:
    explicit CLI flag  ->  exported environment variable  ->  .env file  ->  default

That order matters in both directions: a .env file must never override
something the caller typed on the command line, and an already-exported
variable (a CI secret, direnv, a shell profile) must never be clobbered by
a stray .env file. This module only ever FILLS an unset argparse value; it
never overwrites one the user actually provided.

Run `python3 env_config.py` directly to see what would be picked up,
without printing any secret's actual value -- this is the first thing to
check when a key "isn't working".
"""

import argparse
import os
from pathlib import Path
from typing import Dict, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


# --- Numeric flag types -----------------------------------------------------
#
# argparse turns a ValueError/ArgumentTypeError from these into
# "error: argument --retries: ...", exit 2. --retries counts EXTRA attempts
# (0 = one navigation, no retry), so 0 is legal there; a negative value
# used to run zero navigations and report "content never obtained", exit 5.

def non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return value


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {value}")
    return value


def non_negative_float(text: str) -> float:
    value = float(text)
    if not value >= 0:   # also rejects nan
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {text}")
    return value


def recaptcha_score(text: str) -> float:
    """--min-score: a reCAPTCHA v3 score is 0.1-0.9."""
    value = float(text)
    if not 0.1 <= value <= 0.9:
        raise argparse.ArgumentTypeError(f"must be between 0.1 and 0.9, got {text}")
    return value


# --- Checks made before anything is fetched -------------------------------
#
# Reported 2026-10-28: a bad --out, --url or --proxy-file was found only
# after the pages had been fetched (and a Scraping Browser session paid
# for), and ended as a traceback, exit 1. Each of these is a usage error,
# exit 2, decided before the first request.

SITE_HOST = "zimmo.be"


def check_url(parser: argparse.ArgumentParser, args) -> None:
    parsed = urlparse(args.url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not (
            host == SITE_HOST or host.endswith("." + SITE_HOST)):
        parser.error(f"--url must be a {SITE_HOST} search-results URL "
                     f"(https://www.{SITE_HOST}/nl/...), got {args.url!r}")
    # The run numbers its own pages: a `page` in --url used to be fetched
    # as "page 1" AND again as page 2, while the sidecar recorded the
    # search without it.
    query = parse_qsl(parsed.query, keep_blank_values=True)
    pages = [v for k, v in query if k == "page"]
    if pages and pages != ["1"]:
        parser.error(f"--url carries page={pages[0]}: pass the search without a page "
                     f"parameter and choose how many pages with --pages")
    if pages:
        args.url = urlunparse(parsed._replace(
            query=urlencode([(k, v) for k, v in query if k != "page"])))


def check_out_prefix(parser: argparse.ArgumentParser, out: str,
                     suffixes: Iterable[str]) -> None:
    """Every file a run may write is `<out><suffix>`: its directory must
    exist and be writable, and none of those paths may be a directory."""
    directory = os.path.dirname(os.path.abspath(out))
    if not os.path.isdir(directory):
        parser.error(f"--out {out!r}: directory {directory!r} does not exist")
    if not os.access(directory, os.W_OK | os.X_OK):
        parser.error(f"--out {out!r}: directory {directory!r} is not writable")
    for suffix in suffixes:
        if os.path.isdir(out + suffix):
            parser.error(f"--out {out!r}: {out + suffix!r} is a directory")


def output_suffixes(fmt: str):
    return ([".json"] if fmt in ("json", "both") else []) + \
           ([".csv"] if fmt in ("csv", "both") else []) + \
           [".meta.json", ".latest_attempt.meta.json"]


def validate_run_args(parser: argparse.ArgumentParser, args, *,
                      cdp_credentials_supported: bool = True) -> None:
    """The browser engines' shared checks, after apply_env_defaults()."""
    from proxy_pool import proxy_urls_from_args

    if not args.url:
        parser.error("--url is required (or set ZIMMO_URL in .env)")
    check_url(parser, args)
    refuse_proxy_with_cdp_endpoint(parser, args)
    if args.cdp_endpoint and args.concurrency > 1:
        parser.error("--concurrency > 1 is not supported together with --cdp-endpoint: the "
                     "Scraping Browser API allows one live connection per profile")
    if args.cdp_endpoint and not cdp_credentials_supported and "@" in (
            urlparse(args.cdp_endpoint).netloc):
        parser.error("this engine attaches through Selenium's debuggerAddress, which cannot "
                     "send the login in --cdp-endpoint / ZIMMO_CDP_ENDPOINT; use "
                     "playwright_scraper.py or puppeteer_scraper.py for the Scraping Browser")
    try:
        args.proxy_urls = proxy_urls_from_args(args)
    except ValueError as e:
        parser.error(str(e))
    check_out_prefix(parser, args.out, output_suffixes(args.format))


def refuse_proxy_with_cdp_endpoint(parser: argparse.ArgumentParser, args) -> None:
    """A --cdp-endpoint browser is already running with its own exit: a
    local proxy cannot be applied to it and was silently ignored. Refuse
    the combination (from flags or .env alike) instead of running without
    the proxy the caller asked for."""
    if getattr(args, "cdp_endpoint", None) and (getattr(args, "proxy", None)
                                                 or getattr(args, "proxy_file", None)):
        parser.error("--proxy/--proxy-file cannot be combined with --cdp-endpoint: the remote "
                     "browser uses its own exit (check ZIMMO_PROXY and ZIMMO_CDP_ENDPOINT in "
                     ".env too)")

# Explicit map: env var name -> argparse destination it fills.
# Never map a variable onto a flag that already has a
# non-empty default (e.g. --out defaults to "zimmo_listings") -- the
# loader only fills UNSET values, so such a mapping would be silently
# inert and look configurable when it is not.
ENV_KEYS: Dict[str, str] = {
    "TWOCAPTCHA_KEY": "twocaptcha_key",
    "ZIMMO_CDP_ENDPOINT": "cdp_endpoint",
    "ZIMMO_PROXY": "proxy",
    "ZIMMO_URL": "url",
    # "1" = always load images/video/fonts (--load-media). --load-media
    # defaults to None, not False, so this loader can still fill it.
    "ZIMMO_LOAD_MEDIA": "load_media",
    # Proxy traffic cap per run in MB (--proxy-budget-mb); empty = 10.
    # --proxy-budget-mb defaults to None for the same reason.
    "ZIMMO_PROXY_BUDGET_MB": "proxy_budget_mb",
}

# A copied .env.example placeholder is treated as unset, not as a real
# value -- these are the literal words most likely to appear as a
# leftover example.
_PLACEHOLDER_VALUES = {
    "", "changeme", "your_key_here", "your_2captcha_key_here",
    "ws://user:pass@host:port", "http://user:pass@host:port",
}


def _parse_dotenv_file(path: Path) -> Dict[str, str]:
    """Minimal KEY=VALUE parser: skips blank lines and '#' comments,
    strips one layer of matching quotes. Used only when python-dotenv
    isn't already installed -- see _load_dotenv_values() below."""
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _load_dotenv_values(dotenv_path: Optional[str] = None) -> Dict[str, str]:
    path = Path(dotenv_path) if dotenv_path else Path(".env")
    try:
        from dotenv import dotenv_values  # type: ignore
        return {k: v for k, v in dotenv_values(path).items() if v is not None}
    except ImportError:
        return _parse_dotenv_file(path)


def _is_placeholder(value: Optional[str]) -> bool:
    """Guards against a copied .env.example being read as CONFIGURED: the
    two credentialled URLs the vendor documents (the Scraping Browser
    endpoint and the 2Captcha proxy URL) are written with `{login}`,
    `{password}`-style placeholders IN THE VENDOR'S OWN DOCS, and an
    earlier version of this check only matched a fixed literal set, so
    a value copied straight from the vendor's own example connected
    with the literal string "{login}-zone-..." as a username -- a
    confusing 401 a long way from its real cause. Treat any `{...}`
    left in a value as unset, generically, rather than enumerating only
    the specific braced examples on hand right now."""
    if value is None:
        return True
    stripped = value.strip()
    if stripped.lower() in _PLACEHOLDER_VALUES:
        return True
    if "{" in stripped and "}" in stripped:
        return True
    return False


def apply_env_defaults(args, dotenv_path: Optional[str] = None,
                       skip: Iterable[str] = ()) -> None:
    """Fill any argparse Namespace attribute that is still None/unset,
    using ENV_KEYS -> exported environment variable -> .env file, in that
    order. Never overwrites a value the caller already provided on the
    command line -- that precedence is the whole point of this function.

    An EMPTY value given explicitly -- `--cdp-endpoint ""` or
    `export ZIMMO_PROXY=` -- means "off for this run" and stops the lookup
    there: before 2026-10-28 it fell through to .env, so a .env holding
    both ZIMMO_PROXY and ZIMMO_CDP_ENDPOINT could not be run with either
    one alone without editing the file. Destinations in `skip` are never
    filled (the Scraper API client does not take ZIMMO_PROXY implicitly)."""
    dotenv_values = _load_dotenv_values(dotenv_path)

    for env_var, dest in ENV_KEYS.items():
        if not hasattr(args, dest) or dest in skip:
            continue
        current = getattr(args, dest)
        if current is not None:
            continue  # an explicit CLI flag always wins -- never overwrite it

        exported = os.environ.get(env_var)
        if exported is not None and not exported.strip():
            continue  # exported empty: off for this run
        if not _is_placeholder(exported):
            setattr(args, dest, exported)
            continue

        from_file = dotenv_values.get(env_var)
        if not _is_placeholder(from_file):
            setattr(args, dest, from_file)


def _mask(value: Optional[str]) -> str:
    if value is None:
        return "(not set)"
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}...{value[-2:]} ({len(value)} chars)"


if __name__ == "__main__":
    # Diagnostic entry point -- prints WHERE each value came from, never
    # the value itself. Run this first when a key "isn't working". Names
    # the placeholder reason rather than quoting the value (the value
    # itself can be a credentialled URL).
    dotenv_values = _load_dotenv_values()
    print("env_config.py diagnostic — source of each known variable:\n")
    for env_var in ENV_KEYS:
        exported = os.environ.get(env_var)
        from_file = dotenv_values.get(env_var)
        if exported is not None and not exported.strip():
            source, masked = "exported EMPTY (off; .env not read)", "(not set)"
        elif not _is_placeholder(exported):
            source, masked = "exported environment variable", _mask(exported)
        elif not _is_placeholder(from_file):
            source, masked = ".env file", _mask(from_file)
        elif exported is not None or from_file is not None:
            source, masked = "placeholder only (treated as unset)", "(not set)"
        else:
            source, masked = "not set", "(not set)"
        print(f"  {env_var:22s} -> {source:38s} {masked}")
