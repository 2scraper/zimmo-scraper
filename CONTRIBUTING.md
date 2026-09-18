# Contributing to zimmo-scraper

Thanks for considering a contribution. This repo is part of the
[2scraper](https://github.com/2scraper) family — several single-site
scrapers sharing one architecture (`~/2scraper/CLAUDE.md`, not committed
to this repo, describes it in full for maintainers).

## Before you start

- **Read the README's "Honesty about what's confirmed live" section
  first.** Several code paths in this repo are explicitly marked as
  unconfirmed against the live site — check there before assuming a
  path works or is broken.
- **Site-specific logic belongs in `product_parser.py`**, plus a
  handful of named constants in the three engine scripts. If you find
  yourself adding zimmo.be-specific knowledge anywhere else
  (`output_writer.py`, `proxy_pool.py`, `env_config.py`, etc.), that is
  very likely the wrong file — those are shared, site-knowledge-free
  modules across the whole family.

## Workflow

1. One branch per logical batch of changes.
2. Open a PR. The description should list each fix with its own
   evidence (a failing case reproduced, a real run's output, a specific
   line number) — "improved reliability" is not evidence.
3. Say explicitly what you deliberately did **not** change, and why, if
   an obvious-looking further change was considered and rejected.
4. CI must be green: `tests.yml` (offline) and, if your change touches
   live behaviour, a manually-dispatched `canary.yml` run.
5. Squash-merge, delete the branch.
6. A version bump and `CHANGELOG.md` entry go in their own commit,
   separate from the fix itself, followed by the tag and release.

## Testing

```bash
python3 smoke_test.py     # or: pytest tests/
```

This must pass **with no browser engine installed at all** — if your
change requires importing Playwright/Selenium/pyppeteer to test, guard
that import behind `try/except ImportError` the way the existing engine
scripts do.

Before submitting:

- Run `python3 check_no_credentials.py` — the same check CI runs.
- If you touched `.env.example` or `env_config.py`'s `ENV_KEYS`, confirm
  the two are still in sync (`smoke_test.py` asserts this automatically).
- If you touched any of the three engines, **run it live**, not just the
  primary one. "Should behave identically to Playwright" is a design
  goal, not a substitute for actually running Puppeteer/Selenium once.

## Reporting a bug

Open an issue with: the exact command you ran, the full output
(redact any real API key/proxy credential first), and — if the bug is
about parsed data being wrong — the specific listing URL and what field
was wrong, so it can be reproduced against a real page rather than
guessed at.

## Security

Do not open a public issue for a security concern — see
[SECURITY.md](SECURITY.md).
