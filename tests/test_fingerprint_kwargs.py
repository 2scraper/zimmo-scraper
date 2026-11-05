"""
tests/test_fingerprint_kwargs.py
------------------------------------
"Assert that a paid API's kwargs are ones the driver accepts. An
unknown key in new_context(**kwargs) is a TypeError at launch, on the
paid path, at runtime."

Built from fixtures/fingerprint_chromium.json, a real (trimmed) Fingerprint
API response. Skipped, not failed, when Playwright isn't installed, since
this specifically tests whether fingerprint_client.py's output is valid
INPUT to Playwright's own API.
"""

import inspect
import json
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

try:
    from playwright.sync_api import Browser
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

from fingerprint_client import playwright_context_kwargs

pytestmark = pytest.mark.skipif(
    not PLAYWRIGHT_AVAILABLE, reason="playwright not installed -- cannot check its real new_context() signature"
)


def _options():
    with open(os.path.join(HERE, "..", "fixtures", "fingerprint_chromium.json"), encoding="utf-8") as fh:
        fp = json.load(fh)
    # The engine merges these over its own default locale.
    return {"locale": "nl-BE", **playwright_context_kwargs(fp)}


def test_playwright_context_kwargs_keys_are_all_real_kwargs():
    options = _options()
    real_params = set(inspect.signature(Browser.new_context).parameters.keys())
    unknown_keys = set(options.keys()) - real_params

    assert not unknown_keys, (
        f"playwright_context_kwargs() returned key(s) {unknown_keys} that "
        f"Browser.new_context() does not accept -- this would be a TypeError "
        f"at launch, on the paid fingerprint path, at runtime."
    )


def test_playwright_context_kwargs_actually_binds():
    """Beyond just being known parameter names, confirm the whole dict
    binds as a valid call."""
    sig = inspect.signature(Browser.new_context)
    # self is the first parameter; bind against everything else.
    sig.bind_partial(None, **_options())
