"""Shared pytest fixtures and helpers for ezpi tests."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

import ezpi


@pytest.fixture(params=["pygit2", "subprocess"], autouse=True)
def git_backend(request: pytest.FixtureRequest) -> Generator[str, None, None]:
    """Run each test with both git backends."""
    backend = request.param
    if backend == "subprocess":
        os.environ["EZPI_USE_GIT_SUBPROCESS"] = "1"
    elif "EZPI_USE_GIT_SUBPROCESS" in os.environ:
        del os.environ["EZPI_USE_GIT_SUBPROCESS"]

    # Skip pygit2 tests if pygit2 is not available
    if backend == "pygit2" and not ezpi.HAS_PYGIT2:
        pytest.skip("pygit2 not available")

    yield backend

    # Clean up
    if "EZPI_USE_GIT_SUBPROCESS" in os.environ:
        del os.environ["EZPI_USE_GIT_SUBPROCESS"]


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """Create a bare git repository for testing."""
    repo_path = tmp_path / "test.git"
    subprocess.run(["git", "init", "--bare", str(repo_path)], check=True, capture_output=True)
    return repo_path


def get_commit_message(repo: Path) -> str:
    """Get the commit message from the latest commit."""
    result = subprocess.run(
        ["git", "--git-dir", str(repo), "log", "-1", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def get_commit_body(repo: Path) -> bytes:
    """Get the blob content from the latest commit."""
    # Get the tree from the latest commit
    tree_result = subprocess.run(
        ["git", "--git-dir", str(repo), "log", "-1", "--format=%T"],
        check=True,
        capture_output=True,
        text=True,
    )
    tree = tree_result.stdout.strip()

    # Get the blob hash from the tree
    ls_result = subprocess.run(
        ["git", "--git-dir", str(repo), "ls-tree", tree],
        check=True,
        capture_output=True,
        text=True,
    )
    # Format: "100644 blob <hash>\t<filename>"
    blob_hash = ls_result.stdout.split()[2]

    # Get the blob content
    blob_result = subprocess.run(
        ["git", "--git-dir", str(repo), "cat-file", "blob", blob_hash],
        check=True,
        capture_output=True,
    )
    return blob_result.stdout
