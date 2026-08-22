"""Config + state file locations, and the JSON read/write pair.

Named `store` rather than `io` so it never reads as the stdlib module.
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
STATE_DIR = os.path.join(ROOT, "state")


def config_path(name):
    """config_path("edgar") -> <repo>/config/edgar.json"""
    return os.path.join(CONFIG_DIR, f"{name}.json")


def state_path(name):
    """state_path("edgar") -> <repo>/state/edgar.json"""
    return os.path.join(STATE_DIR, f"{name}.json")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
