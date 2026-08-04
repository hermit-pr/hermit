"""Centralised logging configuration for master and slave processes."""

import logging
import sys

from hermit import __version__

FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)-8s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def configure_logging(level: str = "INFO") -> None:
    """Set up structured logging with ISO-8601 timestamps and version info.

    Call once at process startup so every log line carries an ISO timestamp
    and the running version.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stderr,
        format=FORMAT,
        datefmt=DATE_FORMAT,
    )
    logging.Formatter.default_msec_format = "%s.%03d"
    logging.Formatter.default_time_format = DATE_FORMAT
    logger = logging.getLogger(__name__)
    logger.info("H.E.R.M.I.T v%s starting", __version__)
