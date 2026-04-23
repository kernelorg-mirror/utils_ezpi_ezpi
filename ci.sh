#!/usr/bin/env sh

set -eu

# Install every extra and dependency group so the type checkers below can
# resolve every import path (including the optional libgit2/pygit2 backend).
uv sync --all-extras --all-groups

uv run ruff format --check
uv run ruff check
uv run ty check
uv run mypy .
uv run pyright
uv run pytest --durations=20
