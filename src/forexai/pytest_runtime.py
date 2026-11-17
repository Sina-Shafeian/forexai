from __future__ import annotations

import getpass
import os
from pathlib import Path
import re


def pytest_load_initial_conftests(early_config, parser, args) -> None:
    """Isolate pytest writable paths by the real Windows process identity."""
    identity = _process_identity()
    os.environ["FOREXAI_PYTEST_ID"] = identity
    basetemp = Path(early_config.rootpath) / f".pytest_runtime_{identity}_tmp"
    early_config.known_args_namespace.basetemp = str(basetemp)
    early_config.option.basetemp = str(basetemp)


def _process_identity() -> str:
    try:
        value = os.getlogin()
    except OSError:
        value = getpass.getuser()
    normalized = re.sub(r"[^a-z0-9_.-]+", "_", value.lower()).strip("._")
    return normalized or "unknown"
