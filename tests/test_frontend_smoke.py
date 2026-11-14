"""Runs the Node smoke test for the drive-explorer card's pure JS helpers.

Skipped entirely when `node` isn't on PATH (it isn't a project dependency,
just a nicety for contributors who have it). `node --test <directory>` is
unreliable on at least one Node 24 build on Windows (it tries to `require()`
the directory itself and fails with MODULE_NOT_FOUND), so this passes an
explicit list of test files instead of the bare directory.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_TEST_DIR = REPO_ROOT / "tests" / "frontend"


def _node_available() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _node_available(), reason="node is not on PATH")
def test_frontend_smoke() -> None:
    """Run every *.test.mjs under tests/frontend/ with Node's built-in test runner."""
    test_files = sorted(str(p) for p in FRONTEND_TEST_DIR.glob("*.test.mjs"))
    assert test_files, f"no *.test.mjs files found under {FRONTEND_TEST_DIR}"

    result = subprocess.run(
        ["node", "--test", *test_files],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"node --test failed (exit {result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
