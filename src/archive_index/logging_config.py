"""Application logging configuration."""

from __future__ import annotations

import logging


def configure_logging(verbose: bool = False) -> None:
    """Configure the process-wide console logger for the CLI."""

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
