# zimmo-scraper — Playwright edition, containerised.
#
# Explicit COPY list, not `COPY . .` (CLAUDE.md §11): the image should
# carry no test suite, no fixtures and no stray .env. The list below is
# checked against playwright_scraper.py's own import graph by this
# repo's docker-build CI job (via a real `ast` walk, not a manual
# review) -- keep the two in sync by hand.

FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt

# Exactly what playwright_scraper.py's own import graph needs, and
# nothing else -- no tests/, no .env, no sample_output.*, no README.
COPY env_config.py output_writer.py product_parser.py captcha_solver.py \
     proxy_pool.py playwright_scraper.py ./

# The exact invocation a missing module breaks (CLAUDE.md §11) -- CI's
# docker-build job actually runs this, rather than trusting that the
# image "should" work.
ENTRYPOINT ["python3", "playwright_scraper.py"]
CMD ["--help"]
