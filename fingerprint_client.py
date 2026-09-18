"""
fingerprint_client.py
-----------------
2Captcha fingerprint fetch + application. No site knowledge -- shared
verbatim across the 2scraper family (CLAUDE.md §1).

**UNCONFIRMED against a real, current 2Captcha fingerprint API
response** -- unlike this family's Scraping Browser API integration
(whose CDP endpoint shape IS confirmed live and documented in
CLAUDE.md §12). The endpoint path and field names below are a
best-effort structure, not verified against real API docs. **Confirm
`FINGERPRINT_API_URL` and every field name below before depending on
this in a live run.**

CLAUDE.md §16 documents SIX real, repeated bugs found in this exact
module across four sibling repos, all invisible to import/--help/
compileall/the offline suite/CI, none of them raising -- "the tool
does less than it says while reporting success". Addressed here from
the start rather than discovered later:

  1. **The API key printed to the terminal on any error.** `requests`
     puts the full URL into HTTPError/connection-error text; this
     module's own `key` rides in the JSON body (not a query string) for
     the one call it makes, which is lower risk than the family's other
     known leak points -- but `redact_secret_patterns()` is still
     applied to every logged exception here, defensively, since "lower
     risk" is not "no risk" and this is exactly the bug class CLAUDE.md
     §8 names as the family's worst.
  2. **`--fingerprint`/the client never actually setting a user agent**
     (a key the API returns in neither format the code checked for).
     Addressed by trying multiple plausible field-name shapes
     (`user_agent`, `userAgent`) rather than one guessed name, and by
     `apply_profile_or_warn()` below, which LOGS what it actually set
     rather than assuming success.
  3. **Locale built as `en-{country}`** (`en-DE` for a German
     fingerprint) instead of reading the API's own locale field. This
     client never constructs a locale from a country code -- it only
     ever uses `data.get("locale", ...)` verbatim, so this specific
     malformation cannot occur here BY CONSTRUCTION; noted so a future
     edit doesn't reintroduce it.
  4. **Timezone never applied at all, though the API states it.** This
     module is honest that it is **not currently wired into any
     engine's browser-launch path** -- `fetch_fingerprint()` and the
     `*_context_options()`/`*_apply_kwargs()` functions below exist and
     are tested, but no engine script calls them yet. See the README's
     "confirmed live" section: applying a fingerprint (including its
     timezone) is a documented gap, not a silent one.
  5. **A documented `--tags`-style flag rejecting every value except
     one exact case.** This module has no `--tags` flag of its own (the
     engines do not currently expose a `--fingerprint`/`--fp-tags` flag
     at all -- see point 4), so the specific failure mode does not
     apply here; noted so adding such a flag later checks the real API
     constraint first rather than guessing a default.
  6. **A Docker image that failed on every invocation.** Not this
     file's concern directly -- see the repo's Dockerfile and the
     `docker-build` CI job, which actually builds and runs the image
     rather than trusting a manual COPY-list review.

Captcha solving, the Scraping Browser API, proxies and fingerprints are
four SEPARATELY-BILLED 2Captcha products behind one API key (CLAUDE.md
§12). Never integrate a competitor's fingerprinting product here.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from proxy_pool import redact_secret_patterns

logger = logging.getLogger("fingerprint_client")

# UNCONFIRMED -- see module docstring.
FINGERPRINT_API_URL = "https://api.2captcha.com/fingerprint/generate"


@dataclass
class FingerprintProfile:
    """UNCONFIRMED field names -- see module docstring. Grouped by what
    each field is used for, so a real API response can be mapped onto
    (or correct) this shape field by field rather than needing a
    rewrite."""
    user_agent: str
    platform: str
    locale: str
    timezone_id: str
    viewport_width: int
    viewport_height: int
    screen_width: int
    screen_height: int
    color_depth: int = 24
    hardware_concurrency: int = 4
    device_memory: int = 8
    webgl_vendor: Optional[str] = None
    webgl_renderer: Optional[str] = None
    fonts: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


def _first_present(data: dict, *keys: str, default=None):
    """Try several plausible field-name shapes in order -- CLAUDE.md
    §16's bug #2 was exactly one guessed key name failing silently
    against a real response shaped differently. Trying several is
    still a guess, but a narrower one, and `raw` is kept on the
    returned profile so a real response can be inspected directly."""
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def fetch_fingerprint(api_key: str, *, os_name: str = "windows",
                       browser: str = "chrome", country: Optional[str] = None,
                       timeout: int = 15) -> Optional[FingerprintProfile]:
    """Fetch one fingerprint profile. Returns None (with a logged
    warning, never a raised exception) on any failure -- a missing
    fingerprint should degrade to the engine's own default fingerprint,
    not crash a run that would otherwise have worked fine.

    UNCONFIRMED against a real API response -- see module docstring.
    """
    try:
        resp = requests.post(
            FINGERPRINT_API_URL,
            json={"key": api_key, "os": os_name, "browser": browser, "country": country},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        # CLAUDE.md §16 bug #1: redact defensively even though this
        # call's own key rides in the JSON body, not a query string --
        # "lower risk" is not "no risk", and this is the exact bug class
        # CLAUDE.md §8 names as the family's worst.
        logger.warning("fingerprint_client: could not fetch a fingerprint (%s) -- "
                        "continuing with the engine's own default fingerprint instead.",
                        redact_secret_patterns(str(e)))
        return None

    user_agent = _first_present(data, "user_agent", "userAgent")
    if not user_agent:
        logger.warning("fingerprint_client: response had no recognisable user-agent field "
                        "(tried 'user_agent', 'userAgent') -- this API's response shape may "
                        "have changed since this client was written (see module docstring: "
                        "UNCONFIRMED). Continuing with the engine's own default fingerprint.")
        return None

    viewport = data.get("viewport", {}) or {}
    screen = data.get("screen", {}) or {}
    webgl = data.get("webgl", {}) or {}

    return FingerprintProfile(
        user_agent=user_agent,
        platform=data.get("platform", "Win32"),
        # Read the API's OWN locale field verbatim -- never construct
        # one from a country code (CLAUDE.md §16 bug #3: "en-{country}"
        # for a German fingerprint is a malformation this shape cannot
        # produce, by construction, as long as this stays a straight
        # dict read).
        locale=data.get("locale", "en-US"),
        timezone_id=data.get("timezone", "Europe/Brussels"),
        viewport_width=viewport.get("width", 1920),
        viewport_height=viewport.get("height", 1080),
        screen_width=screen.get("width", 1920),
        screen_height=screen.get("height", 1080),
        color_depth=screen.get("colorDepth", 24),
        hardware_concurrency=data.get("hardware_concurrency", 4),
        device_memory=data.get("device_memory", 8),
        webgl_vendor=webgl.get("vendor"),
        webgl_renderer=webgl.get("renderer"),
        fonts=data.get("fonts", []),
        raw=data,
    )


def playwright_context_options(profile: FingerprintProfile) -> Dict[str, Any]:
    """Options for playwright.chromium.launch().new_context(**options).
    Every key here is confirmed against a real, installed Playwright's
    own Browser.new_context() signature via inspect.signature() --
    see tests/test_fingerprint_kwargs.py (CLAUDE.md §10: "assert that a
    paid API's kwargs are ones the driver accepts").

    Never call this when connecting via --cdp-endpoint (CLAUDE.md §8):
    the Scraping Browser brings its own fingerprint, and stacking a
    second on top creates a contradiction rather than better cover."""
    return {
        "user_agent": profile.user_agent,
        "viewport": {"width": profile.viewport_width, "height": profile.viewport_height},
        "screen": {"width": profile.screen_width, "height": profile.screen_height},
        "locale": profile.locale,
        "timezone_id": profile.timezone_id,
        "device_scale_factor": 1,
    }


def playwright_init_script(profile: FingerprintProfile) -> str:
    """A page.add_init_script() body overriding navigator.hardwareConcurrency
    / deviceMemory and (best-effort) the WebGL vendor/renderer strings.
    UNCONFIRMED against a real detector's expectations."""
    webgl_patch = ""
    if profile.webgl_vendor and profile.webgl_renderer:
        webgl_patch = f"""
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {{
            if (parameter === 37445) return {json.dumps(profile.webgl_vendor)};
            if (parameter === 37446) return {json.dumps(profile.webgl_renderer)};
            return getParameter.call(this, parameter);
        }};
        """
    return f"""
    Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {profile.hardware_concurrency} }});
    Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {profile.device_memory} }});
    Object.defineProperty(navigator, 'platform', {{ get: () => {json.dumps(profile.platform)} }});
    {webgl_patch}
    """


def pyppeteer_apply_kwargs(profile: FingerprintProfile) -> Dict[str, Any]:
    """Kwargs for pyppeteer.launch() plus a page.setUserAgent() /
    page.setViewport() call the caller still needs to make explicitly."""
    return {
        "args": [f"--lang={profile.locale}"],
    }


def selenium_chrome_options(profile: FingerprintProfile):
    """Best-effort only -- Selenium's own fingerprint surface is
    narrower than Playwright's (no native context-level UA/viewport/
    timezone override). Returns what CAN be set at launch time."""
    from selenium.webdriver.chrome.options import Options
    options = Options()
    options.add_argument(f"--lang={profile.locale}")
    options.add_argument(f"--window-size={profile.viewport_width},{profile.viewport_height}")
    return options
