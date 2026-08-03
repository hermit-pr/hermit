"""Command line entrypoint that runs the H.E.R.M.I.T master webhook server."""

import uvicorn

from hermit.config import get_settings
from hermit.jobs import JobStore
from hermit.k8s import build_spawner
from hermit.providers import build_git_client
from hermit.server import create_app


def run() -> None:
    """Start the master webhook server using the configured settings."""
    settings = get_settings()
    client = build_git_client(settings)
    spawner = build_spawner(settings)
    store = JobStore()
    app = create_app(settings, client, spawner, store)
    uvicorn.run(
        app, host=settings.host, port=settings.port, log_level=settings.log_level
    )


if __name__ == "__main__":
    run()
