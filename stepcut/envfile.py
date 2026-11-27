"""Minimal .env reader. Values are returned to the caller and never printed."""
from __future__ import annotations

import os
from pathlib import Path


def load_env(root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    path = root / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    # Real environment variables win over the file.
    for key in ("CURSOR_API_KEY", "STEPCUT_AGENT_MODEL"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values
