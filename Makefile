.PHONY: verify live-macos test lint fmt fmt-check typecheck mdlint

verify: lint fmt-check typecheck test mdlint

test:
	uv run pytest

# Run by a human on a Mac (docs/macos-checklist.md); skipped with a reason elsewhere.
live-macos:
	JOBSCTL_LIVE_MACOS=1 uv run pytest -m live tests/test_macos_live.py -v -rA -p no:cacheprovider

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

mdlint:
	uv run pymarkdown --config .pymarkdown.json scan -r --respect-gitignore .
