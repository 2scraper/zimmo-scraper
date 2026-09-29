"""
tests/test_family_sync.py
-------------------------
Two defects found in this repo by a family-wide probe on 2026-09-29, pinned
so they cannot come back. Neither needs an engine library installed.
"""

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_pyppeteer_never_uses_page_authenticate():
    """pyppeteer's page.authenticate() relies on Network.setRequestInterception,
    which current Chrome removed ("wasn't found", exit 5 before the first
    request — measured 2026-09-29 in a sibling repo). Proxy credentials go
    through the CDP Fetch domain instead (_authenticate_proxy)."""
    tree = ast.parse((REPO / "puppeteer_scraper.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "authenticate"]
    assert not calls, f"page.authenticate() is called at line(s) {[c.lineno for c in calls]}"
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert "_authenticate_proxy" in names


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").is_dir(),
                    reason="not a git checkout")
@pytest.mark.parametrize("path,ignored", [
    (".env.bak", True), (".env.local", True), ("live/dump.html", True),
    ("out.json.page3", True), ("captures/x.html", True),
    (".env.example", False), ("sample_output.json", False),
])
def test_gitignore_by_shape(path, ignored):
    """A renamed .env, a run directory and a paged dump are ignored by SHAPE
    (family template §22-§26); the documented example and the sample are not."""
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO)
    assert (r.returncode == 0) == ignored
