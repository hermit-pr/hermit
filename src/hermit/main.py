"""Command line entrypoint that runs the H.E.R.M.I.T master webhook server."""

import logging

import uvicorn

from hermit import __version__
from hermit.config import Settings, get_settings
from hermit.jobs import JobStore
from hermit.k8s import build_spawner
from hermit.providers import build_git_client
from hermit.server import create_app

logger = logging.getLogger(__name__)


def _resolve_signing_key(settings: Settings) -> str:
    """Pick the key used to derive deterministic, cross-replica job ids."""
    if settings.report_signing_key:
        return settings.report_signing_key.get_secret_value()
    return settings.webhook_secret.get_secret_value()


def run() -> None:
    """Start the master webhook server using the configured settings."""
    settings: Settings = get_settings()
    logger.info(
        "starting H.E.R.M.I.T master v%s (%s provider) on %s:%d",
        __version__,
        settings.git_provider,
        settings.host,
        settings.port,
    )
    client = build_git_client(settings)
    spawner = build_spawner(settings)
    store = JobStore(_resolve_signing_key(settings))
    app = create_app(settings, client, spawner, store)
    uvicorn.run(
        app, host=settings.host, port=settings.port, log_level=settings.log_level
    )


if __name__ == "__main__":
    run()
