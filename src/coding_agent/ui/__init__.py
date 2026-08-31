"""Native desktop interface for the coding agent."""

from __future__ import annotations

from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Import Tkinter only when the desktop interface is actually started."""

    from coding_agent.ui.app import main as desktop_main

    return desktop_main(argv)

__all__ = ["main"]
