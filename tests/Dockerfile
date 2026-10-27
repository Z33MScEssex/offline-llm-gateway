# Test runner image: Python plus pytest and nothing else, so the host needs only Docker.
# Pattern from the Dockerfile reference (FROM python, RUN pip install, WORKDIR).
FROM python:3.12-slim
COPY requirements-dev.txt /tmp/requirements-dev.txt
# Installed at build time, so running the tests later needs no internet (stage 5 relies on this)
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt
WORKDIR /tests
