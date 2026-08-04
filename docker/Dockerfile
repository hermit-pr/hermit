FROM python:3.13-slim AS base

# Install the opencode binary into the image for local dev only. The
# production path is the init-container pattern, so this stays off by default.
ARG INSTALL_OPENCODE=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system hermit \
    && useradd --system --gid hermit --home-dir /app hermit \
    && apt-get update \
    && apt-get install -y --no-install-recommends git \
    && if [ "$INSTALL_OPENCODE" = "true" ]; then \
         apt-get install -y --no-install-recommends nodejs npm \
         && npm install -g opencode-ai; \
       fi \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN mkdir -p /workspace && chown hermit:hermit /workspace

COPY docker/entrypoint.sh /usr/local/bin/hermit-entrypoint
RUN chmod +x /usr/local/bin/hermit-entrypoint

USER hermit

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

ENTRYPOINT ["/usr/local/bin/hermit-entrypoint"]