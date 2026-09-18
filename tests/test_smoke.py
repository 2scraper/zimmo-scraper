"""
tests/test_smoke.py
-----------------------
Wraps smoke_test.py as a single pytest test, so `pytest` works as an
entry point without a second copy of the checks (CLAUDE.md §10:
smoke_test.py is one file of plain functions with inline fixtures --
no pytest, no conftest, no fixtures directory of its own; this file is
the only place pytest-specific glue lives).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import smoke_test  # noqa: E402


def test_smoke():
    assert smoke_test.check_banned_wording(), "banned wording found — see output above"
    assert smoke_test.check_env_example_matches_env_keys(), ".env.example / ENV_KEYS mismatch — see output above"
    assert smoke_test.main() == 0, "one or more smoke checks failed — see output above"
