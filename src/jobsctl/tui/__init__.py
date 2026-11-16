"""The `browse` TUI (interface.md, TUI), an optional extra.

This package imports nothing from Textual at import time: `model.py`, `actions.py` and
`result.py` are plain Python and are tested without the extra; only `app.py` imports Textual, and
only `commands/browse.py` imports `app.py`, inside the handler (so `tui_missing` can be reported).
"""
