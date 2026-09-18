"""
captcha_solver.py
------------------
Shared helper used by all engines (Playwright / Selenium / Puppeteer).

Detection runs after EVERY page navigation, regardless of what URL was
requested. Two real-world reCAPTCHA v3 markup formats have been
confirmed live on different sites in this scraper family, and both are
checked here.

FIXED (external audit, 2026-09-14, codex.md finding #2): the live
zimmo.be page at audit time presented a Cloudflare **Managed
Challenge** -- a hidden `cf-turnstile-response` input exists, but there
is no `<div class="cf-turnstile" data-sitekey="...">` element the old
detector required. A Managed Challenge is Cloudflare's own automatic,
non-widget challenge type; unlike a standalone Turnstile widget, it may
not expose an extractable sitekey in the page's own markup at all, and
even when it does, solving a Managed Challenge purely by submitting a
token is not confirmed to work the way it does for a standalone
Turnstile widget.

Given that, this module now does NOT pretend to solve every Cloudflare
state via a guessed markup pattern for a page this codebase has not
itself inspected. Instead:
  1. Sitekey extraction is broadened to any `data-sitekey` attribute on
     the page (not only one tied to a `cf-turnstile`-classed element),
     since a Managed Challenge's own Turnstile invocation may render
     the sitekey on a different element.
  2. If Cloudflare markers are present (`detect_cloudflare_challenge`)
     but NO sitekey can be extracted, `detect_turnstile_challenge()`
     still returns a `TurnstileChallenge`, with `sitekey=None` --
     distinct from returning `None` outright, so a caller can tell
     "definitely blocked, but this module cannot solve it" apart from
     "no challenge seen at all". `solve_turnstile()` raises a clear,
     actionable `RuntimeError` for that case rather than submitting a
     nonsensical `sitekey=None` to 2captcha's API.
  3. The engines' own `Captcha.setAutoSolve` (over --cdp-endpoint, on a
     genuine 2Captcha Scraping Browser API session) remains the
     recommended path for a Managed Challenge specifically -- that
     mechanism operates on the live browser session itself, not a
     markup-parsed sitekey, and does not depend on this module
     correctly guessing a challenge's HTML shape.

Flow:
  1. If a reCAPTCHA v3 challenge is detected, solve it via 2captcha.com's
     recaptchaV3 method.
  2. The resulting token is injected into the page's `g-recaptcha-response`
     textarea and any bound callback is invoked.

No other captcha vendor is integrated (per spec: no competitors).
"""

from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger("captcha_solver")

TWOCAPTCHA_IN_URL = "https://2captcha.com/in.php"
TWOCAPTCHA_RES_URL = "https://2captcha.com/res.php"


@dataclass
class CaptchaChallenge:
    kind: str
    sitekey: str
    action: str = "verify"
    page_url: str = ""


def detect_recaptcha_v3(html: str, page_url: str) -> Optional[CaptchaChallenge]:
    if "recaptcha" not in html.lower():
        return None

    for widget_match in re.finditer(r"<captcha-widget\b([^>]*)>", html, re.IGNORECASE):
        attrs = widget_match.group(1)
        version_match = re.search(r'data-version=["\']v(\d)["\']', attrs)
        sitekey_match = re.search(r'data-sitekey=["\']([\w-]{20,})["\']', attrs)
        if version_match and version_match.group(1) == "3" and sitekey_match:
            action_match = re.search(r'data-action=["\']([\w_]+)["\']', attrs)
            action = action_match.group(1) if action_match and action_match.group(1) != "null" else "verify"
            return CaptchaChallenge(
                kind="recaptcha_v3",
                sitekey=sitekey_match.group(1),
                action=action,
                page_url=page_url,
            )

    if "grecaptcha" not in html:
        return None

    exec_match = re.search(
        r"grecaptcha\.execute\(\s*['\"]([\w-]{20,})['\"]\s*,\s*\{\s*action:\s*['\"]([\w_]+)['\"]",
        html,
    )
    if exec_match:
        return CaptchaChallenge(
            kind="recaptcha_v3",
            sitekey=exec_match.group(1),
            action=exec_match.group(2),
            page_url=page_url,
        )

    key_match = re.search(r"data-sitekey=['\"]([\w-]{20,})['\"]", html)
    if key_match and "grecaptcha.render" in html:
        return CaptchaChallenge(kind="recaptcha_v3", sitekey=key_match.group(1), page_url=page_url)

    return None


def _solve_with_2captcha(api_key: str, challenge: CaptchaChallenge,
                          min_score: float = 0.7, poll_interval: int = 5,
                          max_wait: int = 120) -> str:
    submit = requests.post(TWOCAPTCHA_IN_URL, data={
        "key": api_key,
        "method": "userrecaptcha",
        "version": "v3",
        "action": challenge.action,
        "min_score": min_score,
        "googlekey": challenge.sitekey,
        "pageurl": challenge.page_url,
        "json": 1,
    }, timeout=30)
    submit.raise_for_status()
    payload = submit.json()
    if payload.get("status") != 1:
        raise RuntimeError(f"2captcha submit error: {payload.get('request')}")

    task_id = payload["request"]
    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        try:
            result = requests.get(TWOCAPTCHA_RES_URL, params={
                "key": api_key, "action": "get", "id": task_id, "json": 1,
            }, timeout=30).json()
        except requests.RequestException as e:
            from proxy_pool import redact_secret_patterns
            raise RuntimeError(f"2captcha polling request failed: {redact_secret_patterns(str(e))}") from None
        if result.get("status") == 1:
            logger.info("2captcha.com solved the reCAPTCHA v3 challenge.")
            return result["request"]
        if result.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha polling error: {result.get('request')}")

    raise TimeoutError("2captcha.com did not return a token in time")


def solve_recaptcha_v3(challenge: CaptchaChallenge, twocaptcha_api_key: Optional[str],
                        min_score: Optional[float] = None) -> str:
    if not twocaptcha_api_key:
        raise RuntimeError(
            "reCAPTCHA v3 detected but no --twocaptcha-key was provided. "
            "Pass --twocaptcha-key or set TWOCAPTCHA_KEY in .env."
        )
    kwargs = {} if min_score is None else {"min_score": min_score}
    return _solve_with_2captcha(twocaptcha_api_key, challenge, **kwargs)


def detect_cloudflare_challenge(html: str) -> bool:
    """Best-effort detection of a Cloudflare interstitial in general --
    including both a standalone Turnstile widget page and a Managed
    Challenge page (confirmed live on zimmo.be, 2026-09-14 audit: a
    Managed Challenge with no visible Turnstile widget markup still
    matches here via the generic Cloudflare interstitial markers and
    the `cf-turnstile-response` hidden-field check)."""
    markers = (
        "checking your browser",
        "just a moment",
        "cf-browser-verification",
        "cf_chl_",
        "turnstile",
        "attention required! | cloudflare",
        "performing security verification",
        "cf-turnstile-response",
    )
    lowered = html.lower()
    return any(m in lowered for m in markers)


@dataclass
class TurnstileChallenge:
    sitekey: Optional[str]
    page_url: str = ""


def detect_turnstile_challenge(html: str, page_url: str) -> Optional[TurnstileChallenge]:
    """Returns None only when there is NO Cloudflare Turnstile-family
    signal at all. When Cloudflare markers ARE present but no sitekey
    could be extracted (a Managed Challenge, confirmed live 2026-09-14
    -- see module docstring), returns a TurnstileChallenge with
    sitekey=None rather than None outright, so a caller can distinguish
    "blocked, but this module cannot solve it" from "nothing detected"."""
    lowered = html.lower()
    if "turnstile" not in lowered and "cf-turnstile-response" not in lowered:
        return None

    # Broadened per audit finding #2: any data-sitekey on the page, not
    # only one tied to a cf-turnstile-classed element -- a Managed
    # Challenge's own invocation may render the sitekey elsewhere.
    m = re.search(r'class=["\'][^"\']*cf-turnstile[^"\']*["\'][^>]*data-sitekey=["\']([\w-]+)["\']', html)
    if not m:
        m = re.search(r'data-sitekey=["\']([\w-]+)["\'][^>]*class=["\'][^"\']*cf-turnstile', html)
    if not m:
        # Broadest fallback: ANY data-sitekey attribute at all, on any
        # element, anywhere near a Cloudflare/Turnstile marker.
        m = re.search(r'data-sitekey=["\']([\w-]{10,})["\']', html)

    if m:
        return TurnstileChallenge(sitekey=m.group(1), page_url=page_url)

    logger.warning("captcha_solver: Cloudflare Turnstile/Managed-Challenge markers are present "
                    "(a 'cf-turnstile-response' field and/or Cloudflare interstitial text), but "
                    "no extractable sitekey was found anywhere on the page. This is consistent "
                    "with a Cloudflare MANAGED CHALLENGE rather than a standalone Turnstile "
                    "widget -- this module cannot solve it via a markup-parsed sitekey. Use "
                    "--cdp-endpoint with a genuine 2Captcha Scraping Browser API session "
                    "instead; its Captcha.setAutoSolve operates on the live browser session "
                    "directly and does not depend on this detection succeeding.")
    return TurnstileChallenge(sitekey=None, page_url=page_url)


def _solve_turnstile_with_2captcha(api_key: str, challenge: TurnstileChallenge,
                                    poll_interval: int = 5, max_wait: int = 120) -> str:
    submit = requests.post(TWOCAPTCHA_IN_URL, data={
        "key": api_key,
        "method": "turnstile",
        "sitekey": challenge.sitekey,
        "pageurl": challenge.page_url,
        "json": 1,
    }, timeout=30)
    submit.raise_for_status()
    payload = submit.json()
    if payload.get("status") != 1:
        raise RuntimeError(f"2captcha submit error: {payload.get('request')}")

    task_id = payload["request"]
    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        try:
            result = requests.get(TWOCAPTCHA_RES_URL, params={
                "key": api_key, "action": "get", "id": task_id, "json": 1,
            }, timeout=30).json()
        except requests.RequestException as e:
            from proxy_pool import redact_secret_patterns
            raise RuntimeError(f"2captcha polling request failed: {redact_secret_patterns(str(e))}") from None
        if result.get("status") == 1:
            logger.info("2captcha.com solved the Turnstile challenge.")
            return result["request"]
        if result.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha polling error: {result.get('request')}")

    raise TimeoutError("2captcha.com did not return a Turnstile token in time")


def solve_turnstile(challenge: TurnstileChallenge, twocaptcha_api_key: Optional[str]) -> str:
    """Raises a clear, actionable error for the sitekey=None case
    (audit finding #2) rather than submitting a nonsensical request to
    2captcha's API and getting back a confusing remote error."""
    if challenge.sitekey is None:
        raise RuntimeError(
            "This Cloudflare challenge has no extractable sitekey (likely a Managed "
            "Challenge, not a standalone Turnstile widget) -- cannot solve it via "
            "2captcha.com's turnstile method. Use --cdp-endpoint with a genuine 2Captcha "
            "Scraping Browser API session instead."
        )
    if not twocaptcha_api_key:
        raise RuntimeError(
            "Turnstile challenge detected but no --twocaptcha-key was provided. "
            "Pass --twocaptcha-key or set TWOCAPTCHA_KEY in .env."
        )
    return _solve_turnstile_with_2captcha(twocaptcha_api_key, challenge)


INJECT_TURNSTILE_TOKEN_JS = """
(token) => {
  let el = document.querySelector('input[name="cf-turnstile-response"]');
  if (!el) {
    el = document.createElement('input');
    el.type = 'hidden';
    el.name = 'cf-turnstile-response';
    document.body.appendChild(el);
  }
  el.value = token;
  try {
    const widget = document.querySelector('.cf-turnstile');
    const cbName = widget && widget.getAttribute('data-callback');
    if (cbName && typeof window[cbName] === 'function') {
      window[cbName](token);
    }
  } catch (e) { /* best effort, non-fatal */ }
  return true;
}
"""


INJECT_TOKEN_JS = """
(token) => {
  let el = document.getElementById('g-recaptcha-response');
  if (!el) {
    el = document.createElement('textarea');
    el.id = 'g-recaptcha-response';
    el.name = 'g-recaptcha-response';
    el.style.display = 'none';
    document.body.appendChild(el);
  }
  el.value = token;
  el.innerHTML = token;
  try {
    if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
      Object.values(window.___grecaptcha_cfg.clients).forEach((client) => {
        Object.values(client).forEach((prop) => {
          if (prop && typeof prop === 'object') {
            Object.values(prop).forEach((cb) => {
              if (typeof cb === 'function') { try { cb(token); } catch (e) {} }
            });
          }
        });
      });
    }
  } catch (e) { /* best effort, non-fatal */ }
  return true;
}
"""
