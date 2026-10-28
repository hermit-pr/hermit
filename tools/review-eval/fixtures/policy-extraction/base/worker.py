"""Background worker."""

from config import parse_config


def run():
    return parse_config("worker.json")
