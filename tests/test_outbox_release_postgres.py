"""VEN-131: the release guards must be a CAS on **Postgres**, not just SQLite.

`tests/test_dashboard_send_state.py` proves the sibling guard refuses a second
message, and its concurrency case genuinely discriminates a check-then-act guard
from an atomic one — but only on SQLite, whose single writer lock serializes
every transaction. That backend cannot see this class of defect at all, and it
is not the deployed one: `DEPLOY.md` marks `DATABASE_URL` required on Vercel.

Measured on PG 16.14 with the guards expressed purely as SQL predicates
(`NOT EXISTS` in the UPDATE's WHERE, `INSERT ... SELECT ... WHERE NOT EXISTS`):

    release_to_send (UPDATE-shaped): 14/15 concurrent pairs -> 2 messages in flight
    add(unless_in_flight) (INSERT):  15/15 concurrent pairs -> 2 messages in flight

Under READ COMMITTED each transaction evaluates the sub-select against its own
snapshot, neither sees the other's uncommitted row, and because the two UPDATEs
touch *different* rows there is no row lock to serialize them. The fix is
`db.lock_key` — an advisory lock on the item, taken inside the same transaction
before the predicate is evaluated. Same probes after it: 0/15 and 0/15.

Skipped when no local Postgres is reachable, so the suite still runs anywhere.
That skip is the honest cost: on a SQLite-only host these guards are NOT covered,
and a green run here does not mean the deployed backend is safe.
"""
import os
import tempfile
import threading
import uuid

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("SECRET_KEY", "test-secret")

PG_HOST = "/var/run/postgresql"
SITE = "furnishedfinder"


def _pg_url():
    """A throwaway database, or None when this host has no Postgres."""
    import shutil
    import subprocess

    if not (os.path.exists(PG_HOST) and shutil.which("createdb")):
        return None
    name = f"ven131_test_{uuid.uuid4().hex[:12]}"
    try:
        subprocess.run(["createdb", "-h", PG_HOST, name],
                       check=True, capture_output=True, timeout=30)
    except Exception:
        return None
    return name, f"postgresql://@/{name}?host={PG_HOST}"


@pytest.fixture()
def pg_outbox(monkeypatch):
    """`outbox` bound to a fresh Postgres, reloaded so `db` re-reads the URL."""
    made = _pg_url()
    if made is None:
        pytest.skip("no local Postgres; the release guards are UNVERIFIED here")
    name, url = made

    import importlib
    import subprocess

    monkeypatch.setenv("DATABASE_URL", url)
    import db
    importlib.reload(db)
    import outbox
    importlib.reload(outbox)

    # Positive control: without this the fixture could hand back a SQLite
    # connection and every assertion below would pass on the wrong backend.
    with outbox._conn() as c:
        assert c.pg, "fixture is not actually talking to Postgres"

    yield outbox

    try:
        subprocess.run(["dropdb", "-h", PG_HOST, "--force", name],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    importlib.reload(db)
    importlib.reload(outbox)


def _seed(outbox, item_id, status):
    msg = outbox.add("t1", SITE, item_id, sequence="presale", step_id="intro",
                     step_label="Intro", body="b", auto=False)
    if msg["status"] != status:
        outbox.set_status(msg["id"], status)
    return outbox.get(msg["id"])


def _in_flight(outbox, item_id):
    rows = outbox.rows_by_item("t1", SITE).get(item_id, [])
    return sum(r["status"] in outbox.IN_FLIGHT for r in rows)


def _race(fn, n=2):
    """Run `fn` in n threads released from one barrier."""
    barrier = threading.Barrier(n)
    errors = []

    def go(i):
        barrier.wait()
        try:
            fn(i)
        except Exception as exc:                    # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return errors


def test_two_approvals_racing_on_postgres_cannot_both_win(pg_outbox):
    """The UPDATE-shaped guard. 14/15 breached before `db.lock_key`."""
    outbox = pg_outbox
    breaches = []
    for trial in range(8):
        item = f"u{trial}"
        rows = [_seed(outbox, item, outbox.PENDING) for _ in range(2)]
        won = {}

        def approve(i):
            mid = rows[i]["id"]
            won[mid] = outbox.release_to_send(
                mid, from_statuses=outbox.APPROVABLE)[0]

        _race(approve)
        if _in_flight(outbox, item) > 1:
            breaches.append((trial, won))
        assert sorted(won.values(), key=str) == [False, True], (
            f"trial {trial}: both racing approvals claimed to have released: {won}"
        )

    assert not breaches, (
        f"two messages in flight for one guest in {len(breaches)}/8 trials — the "
        f"sibling predicate is not atomic on this backend: {breaches}"
    )


def test_two_sends_racing_on_postgres_cannot_both_queue(pg_outbox):
    """The INSERT-shaped guard — the `/responder/send` path. 15/15 breached."""
    outbox = pg_outbox
    for trial in range(8):
        item = f"i{trial}"
        made = {}

        def send(i):
            made[i] = outbox.add("t1", SITE, item, sequence="presale",
                                 step_id="intro", step_label="Reply",
                                 body=f"click-{i}", auto=True,
                                 unless_in_flight=True)

        _race(send)
        assert _in_flight(outbox, item) == 1, (
            f"trial {trial}: {_in_flight(outbox, item)} messages queued for one "
            f"guest by two concurrent clicks: {made}"
        )
        assert sum(v is not None for v in made.values()) == 1, made


def test_the_guard_does_not_strand_delivery_on_postgres(pg_outbox):
    """The dangerous inverse: too strong a guard means a message never sends.

    A row must still be able to claim itself (`queued`->`sending`) with the
    guard armed, or the drainer deadlocks against its own row and nothing is
    ever delivered.
    """
    outbox = pg_outbox
    lone = _seed(pg_outbox, "s1", outbox.QUEUED)

    assert outbox.set_status(lone["id"], outbox.SENDING,
                             unless_sibling_in_flight=True) is True
    assert outbox.get(lone["id"])["status"] == outbox.SENDING

    # And it still reaches a terminal state.
    outbox.set_status(lone["id"], outbox.SENT)
    assert outbox.get(lone["id"])["status"] == outbox.SENT

    # With nothing in flight, a fresh message is released normally.
    nxt = _seed(pg_outbox, "s1", outbox.PENDING)
    released, row = outbox.release_to_send(nxt["id"],
                                           from_statuses=outbox.APPROVABLE)
    assert released is True and row["status"] == outbox.QUEUED
