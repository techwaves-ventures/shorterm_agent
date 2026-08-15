"""Ensure the project root is resolved from the worktree, not the CWD.

`python -m pytest` adds the CWD to sys.path. When tests run from a separate
working directory (e.g. a CI scratch checkout), Python may import project
modules from that directory rather than from the worktree the test files live
in. Individual test files that manipulate sys.path fix this for themselves, but
only if they are the first file imported — in a multi-file run, a file imported
earlier may have already cached a stale module in sys.modules.

conftest.py runs before any test module is imported, so inserting the project
root here is the one place that is always early enough.
"""
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
