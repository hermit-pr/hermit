FROM python:3.14-alpine AS base

# Install the opencode binary into the image for local dev only. The
# production path is the init-container pattern, so this stays off by default.
ARG INSTALL_OPENCODE=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

LABEL org.opencontainers.image.description="H.E.R.M.I.T — airgapped code-review bot for on-premise GitLab & GitHub, powered by opencode + vLLM"

RUN addgroup -S hermit \
    && adduser -S -G hermit -h /app hermit \
    && apk add --no-cache git libstdc++ libgcc \
    && if [ "$INSTALL_OPENCODE" = "true" ]; then \
         apk add --no-cache nodejs npm \
         && npm install -g opencode-ai; \
       fi

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