"""Entry point."""

from config import load_config


def main():
    return load_config("app.json")
