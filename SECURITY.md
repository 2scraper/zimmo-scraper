# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for a security
vulnerability. Instead, use GitHub's private
[Security Advisories](https://github.com/2scraper/zimmo-scraper/security/advisories/new)
feature for this repository, or contact the maintainers privately
through the 2scraper organization.

Include:
- What you found and where (file + line, or a reproduction command).
- What it exposes (a credential, an injection point, something else).
- A minimal reproduction if possible.

## Scope

This is a web scraper, not a service with a network-facing attack
surface of its own — most realistic findings here fall into one of:

- **A credential leak**: a real API key, proxy password, or CDP
  endpoint credential appearing in logs, exception text, a committed
  fixture, or a Docker image. This is the category this family has
  gotten wrong before (see `CHANGELOG.md`) and takes it seriously.
- **A code-injection point**: anywhere user-controlled input (a
  `--url`, a category label) reaches `eval`, a shell command, or an
  unparameterized query. This project does not build SQL and does not
  shell out to a URL, but a regression that introduces one is exactly
  what this policy exists to catch.
- **A supply-chain concern**: a dependency pin, a Docker base image, or
  a GitHub Action version with a known CVE.

## What is explicitly out of scope

- Reports about zimmo.be's own security posture — this repo scrapes a
  third-party site and has no relationship with it beyond that; report
  such findings to zimmo.be directly.
- "The scraper can be blocked by a WAF/rate-limit" — that is expected
  behaviour of a scraper against a site's own anti-bot measures, not a
  vulnerability in this code.

## Supported versions

Only the latest tagged release receives security fixes. See
`CHANGELOG.md` for the current version.
