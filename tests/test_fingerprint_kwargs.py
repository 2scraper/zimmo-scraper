"""
tests/test_fingerprint_kwargs.py
------------------------------------
"Assert that a paid API's kwargs are ones the driver accepts. An
unknown key in new_context(**kwargs) is a TypeError at launch, on the
paid path, at runtime." (CLAUDE.md §10)

Guarded behind try/except ImportError -- skipped, not failed, when
Playwright isn't installed, since this specifically tests whether
fingerprint_client.py's output is valid INPUT to Playwright's own API.
"""

import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from playwright.sync_api import Browser
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

from fingerprint_client import FingerprintProfile, playwright_context_options

pytestmark = pytest.mark.skipif(
    not PLAYWRIGHT_AVAILABLE, reason="playwright not installed -- cannot check its real new_context() signature"
)


def test_playwright_context_options_keys_are_all_real_kwargs():
    profile = FingerprintProfile(
        user_agent="Mozilla/5.0 Test", platform="Win32", locale="nl-BE",
        timezone_id="Europe/Brussels", viewport_width=1920, viewport_height=1080,
        screen_width=1920, screen_height=1080,
    )
    options = playwright_context_options(profile)

    real_params = set(inspect.signature(Browser.new_context).parameters.keys())
    unknown_keys = set(options.keys()) - real_params

    assert not unknown_keys, (
        f"playwright_context_options() returned key(s) {unknown_keys} that "
        f"Browser.new_context() does not accept -- this would be a TypeError "
        f"at launch, on the paid fingerprint path, at runtime."
    )


def test_playwright_context_options_actually_binds():
    """Beyond just being known parameter names, confirm the whole dict
    binds as a valid call -- catches a case where two options are
    individually valid but mutually exclusive (e.g. viewport vs
    no_viewport), which the key-membership check above wouldn't catch."""
    profile = FingerprintProfile(
        user_agent="Mozilla/5.0 Test", platform="Win32", locale="nl-BE",
        timezone_id="Europe/Brussels", viewport_width=1920, viewport_height=1080,
        screen_width=1920, screen_height=1080,
    )
    options = playwright_context_options(profile)
    sig = inspect.signature(Browser.new_context)
    # self is the first parameter; bind against everything else.
    sig.bind_partial(None, **options)
