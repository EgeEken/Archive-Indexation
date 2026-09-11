"""Isolated native folder picker helper for Windows and desktop hosts."""

from __future__ import annotations

import tkinter
from tkinter import filedialog


def main() -> int:
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(title="Choose archive workspace folder")
        print(path or "", flush=True)
    finally:
        root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
