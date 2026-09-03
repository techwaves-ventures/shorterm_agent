"""Ensure the project root is resolved from the worktree, not the CWD.

`python -m pytest` adds the CWD to sys.path. When tests run from a separate
working directory (e.g. a CI scratch checkout), Python may import project
modules from that directory rather than from the worktree the test files live
in. Individual test files that manipulate sys.path fix this for themselves, but
only if they are the first file imported — in a multi-file run, a file imported
earlier may have already cached a stale module in sys.modules.

conftest.py runs before any test module is imported, so inserting the project
root here is the one place that is always early enough.

The background-agent opt-out below lives here for the same reason: importing
`dashboard` starts the autopilot scheduler as an *import side effect*, so the
flag has to be set before the first test module is imported — a fixture would
already be too late.
"""
import os
import sys
from pathlib import Path

# VEN-162: keep long-lived background threads out of the test process.
# `automation.start_scheduler` / `start_drainer` spawn daemon threads that
# outlive the test that spawned them and keep opening connections to whichever
# temp database is current, so an unrelated test later in the run could see a
# drained row or a half-applied migration ("duplicate column name: ...").
# `setdefault`, not assignment, so `DISABLE_BACKGROUND_AGENTS=0 pytest` still
# reproduces the old behaviour on demand.
os.environ.setdefault("DISABLE_BACKGROUND_AGENTS", "1")

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
