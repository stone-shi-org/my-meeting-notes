#!/usr/bin/env python3
"""Entrypoint: `python run.py`."""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,  # app.logging_config owns this
    )


if __name__ == "__main__":
    main()
