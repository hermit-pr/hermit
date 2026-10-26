"""Configuration loading."""

import json


def load_config(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
