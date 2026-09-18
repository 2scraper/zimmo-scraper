#!/usr/bin/env python3
"""
check_no_credentials.py
-----------------------------
ONE implementation of "no committed credential", invoked from BOTH
tests.yml and the offline test suite (CLAUDE.md §16 -- "one
implementation, invoked from both CI and the offline suite, and a check
asserting that the workflow calls the script rather than reimplementing
it"). An earlier version of this exact check existed only as an inline
`git grep` in tests.yml -- a second, independent copy of the same
pattern in a different location is exactly the "two sources of truth"
trap this family found in three sibling repos at once: one copy matched
only `ws://`/`wss://` and let an `http://user:pass@` credential sail
past, while a stricter copy failed on its OWN repo's legitimate
documentation placeholders. This file is the only place the patterns
live now.

Usage:
    python3 check_no_credentials.py            # scans the repo root
    python3 check_no_credentials.py --path DIR # scans a specific tree

Exit 0: nothing found. Exit 1: a possible credential was found (printed
with file:line).
"""

import argparse
import os
import re
import sys
from typing import List, Tuple

_ALLOWLISTED_SUBSTRINGS = ("sample_output",)
# FIXED (external audit, 2026-09-14, codex.md finding #10): the
# previous allowlist exempted ANY path containing "test_" or "/tests/"
# entirely -- confirmed live during audit to make a real secret
# committed as e.g. "test_secret.py" invisible to this scanner. This
# tool's whole job is to catch a credential that should never have been
# committed; a blanket exemption by filename PATTERN (as opposed to a
# specific, named, known-safe fixture file) defeats that job exactly
# where it matters most. Genuine test fixtures use obviously-fake
# values (short synthetic skus, "u2"/"host1" placeholder hosts) that
# already fail _HEX_KEY_RE / _looks_like_real_credential on their own
# merits -- they don't need a path-based exemption to pass cleanly, and
# confirmed clean in this repo's own test suite after this change (see
# smoke_test.py's own invocation of this scanner).

_HEX_KEY_RE = re.compile(r"\b[a-f0-9]{32}\b")

_CREDENTIAL_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")

_MASKED_MARKER = "***:***"

# A documentation EXAMPLE showing the shape of a credentialled URL uses an
# obviously-fake word in place of the real secret ("ws://user:pass@...",
# "PASSWORD@cb.2captcha.com") -- these are exactly what a README/docstring
# SHOULD contain, and are not a real finding. A genuine leaked credential
# is a random-looking string, never one of these literal words, and a
# vendor's own braced placeholder (`{login}`, `{password}`) is caught
# separately by simply containing `{`/`}`.
_PLACEHOLDER_WORDS = {
    "user", "pass", "password", "key", "token", "secret", "changeme",
    "login", "id", "cc", "example", "your_key", "your_password",
}


def _looks_like_real_credential(matched_text: str) -> bool:
    """True only if the user:pass portion doesn't look like an
    obviously-fake documentation placeholder."""
    # matched_text is "scheme://user:pass@" -- pull out the user/pass part.
    after_scheme = matched_text.split("://", 1)[-1]
    creds = after_scheme.rstrip("@")
    if "{" in creds or "}" in creds:
        return False  # a vendor's own braced placeholder, per CLAUDE.md §17
    parts = re.split(r"[:\-]", creds.lower())
    if any(part in _PLACEHOLDER_WORDS for part in parts):
        return False
    return True

_SCAN_EXTENSIONS = (".py", ".md", ".yml", ".yaml", ".toml", ".txt", ".env.example")
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}


def _iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in filenames:
            # FIXED (external audit, 2026-09-14, codex.md finding #10):
            # `.env` has no extension this glob would ever match, and
            # only the LITERAL example file was special-cased -- a real
            # `.env` accidentally committed (a .gitignore miss, a
            # `git add -f`) was invisible to this scanner entirely,
            # confirmed live during audit. `.env` is now checked
            # exactly like every other tracked file.
            if filename.endswith(_SCAN_EXTENSIONS) or filename in (".env.example", ".env"):
                yield os.path.join(dirpath, filename)


def _is_allowlisted(path: str) -> bool:
    return any(marker in path for marker in _ALLOWLISTED_SUBSTRINGS)


def scan(root: str) -> List[Tuple[str, int, str]]:
    """Returns a list of (path, line_number, matched_text) findings."""
    findings = []
    this_file = os.path.abspath(__file__)

    for path in _iter_files(root):
        if os.path.abspath(path) == this_file:
            continue
        if _is_allowlisted(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except (IsADirectoryError, PermissionError):
            continue

        for line_num, line in enumerate(lines, start=1):
            for match in _HEX_KEY_RE.finditer(line):
                findings.append((path, line_num, match.group()))
            for match in _CREDENTIAL_URL_RE.finditer(line):
                if _MASKED_MARKER in line:
                    continue
                if not _looks_like_real_credential(match.group()):
                    continue
                findings.append((path, line_num, match.group()))

    return findings


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--path", default=".", help="Root directory to scan (default: current directory)")
    args = p.parse_args()

    findings = scan(args.path)
    if findings:
        print(f"Possible committed credential(s) found ({len(findings)}):")
        for path, line_num, text in findings:
            print(f"  {path}:{line_num}: {text}")
        print("\nInvestigate before merging. If this is a false positive (a "
              "genuinely fake fixture value), add it to _ALLOWLISTED_SUBSTRINGS "
              "in this file -- the one place that list lives.")
        return 1

    print(f"No committed credentials found (scanned under {args.path!r}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
