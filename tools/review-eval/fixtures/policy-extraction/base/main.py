"""Entry point."""

from config import parse_config


def main():
    return parse_config("app.json")
