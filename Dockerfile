# zimmo-scraper — Playwright edition, containerised.
#
# Explicit COPY list, not `COPY . .`: the image should
# carry no test suite, no fixtures and no stray .env. smoke_test.py
# checks the list below against playwright_scraper.py's own local import
# graph (an `ast` walk), so a missing module fails the offline suite.
#
# The default run is headless, and zimmo.be refuses headless Chromium:
# use the image with --cdp-endpoint, or give it a display and --headful.

FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt

# Exactly what playwright_scraper.py's own import graph needs, and
# nothing else -- no tests/, no .env, no sample_output.*, no README.
COPY env_config.py output_writer.py product_parser.py captcha_solver.py \
     proxy_pool.py proxy_check.py playwright_scraper.py ./

# The exact invocation a missing module breaks -- CI's
# docker-build job actually runs this, rather than trusting that the
# image "should" work.
ENTRYPOINT ["python3", "playwright_scraper.py"]
CMD ["--help"]
