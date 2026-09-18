"""
env_config.py
-----------------
Hand-rolled .env loader for zimmo-scraper, per the 2scraper family
template (CLAUDE.md §3). No new dependency -- defers to python-dotenv
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

import os
from pathlib import Path
from typing import Dict, Optional

# Explicit map: env var name -> argparse destination it fills.
# CLAUDE.md §3: never map a variable onto a flag that already has a
# non-empty default (e.g. --out defaults to "zimmo_listings") -- the
# loader only fills UNSET values, so such a mapping would be silently
# inert and look configurable when it is not.
ENV_KEYS: Dict[str, str] = {
    "TWOCAPTCHA_KEY": "twocaptcha_key",
    "ZIMMO_CDP_ENDPOINT": "cdp_endpoint",
    "ZIMMO_PROXY": "proxy",
    "ZIMMO_URL": "url",
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
    """CLAUDE.md §17: "a copied .env.example read as CONFIGURED" — the
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


def apply_env_defaults(args, dotenv_path: Optional[str] = None) -> None:
    """Fill any argparse Namespace attribute that is still None/unset,
    using ENV_KEYS -> exported environment variable -> .env file, in that
    order. Never overwrites a value the caller already provided on the
    command line -- that precedence is the whole point of this function."""
    dotenv_values = _load_dotenv_values(dotenv_path)

    for env_var, dest in ENV_KEYS.items():
        if not hasattr(args, dest):
            continue
        current = getattr(args, dest)
        if current not in (None, ""):
            continue  # an explicit CLI flag always wins -- never overwrite it

        exported = os.environ.get(env_var)
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
    # the placeholder reason rather than quoting the value (CLAUDE.md
    # §17: the value itself can be a credentialled URL).
    dotenv_values = _load_dotenv_values()
    print("env_config.py diagnostic — source of each known variable:\n")
    for env_var in ENV_KEYS:
        exported = os.environ.get(env_var)
        from_file = dotenv_values.get(env_var)
        if not _is_placeholder(exported):
            source, masked = "exported environment variable", _mask(exported)
        elif not _is_placeholder(from_file):
            source, masked = ".env file", _mask(from_file)
        elif exported is not None or from_file is not None:
            source, masked = "placeholder only (treated as unset)", "(not set)"
        else:
            source, masked = "not set", "(not set)"
        print(f"  {env_var:22s} -> {source:38s} {masked}")
