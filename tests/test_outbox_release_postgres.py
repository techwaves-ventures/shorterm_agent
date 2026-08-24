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


def _seed(outbox, item_id, status, body="b"):
    msg = outbox.add("t1", SITE, item_id, sequence="presale", step_id="intro",
                     step_label="Intro", body=body, auto=False)
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


def test_replays_of_a_delivered_body_race_and_none_get_through(pg_outbox):
    """VEN-170's guard on the backend that runs it — both shapes, 4 racing each.

    The stale-tab replay is not itself a race: the send it duplicates has long
    since settled, so a single-threaded check would find the guard. Racing it
    anyway is what proves the *predicate* survives concurrency on Postgres,
    where under READ COMMITTED each statement gets its own snapshot and a
    `NOT EXISTS` evaluated outside `db.lock_key` is not a compare-and-set at
    all — the failure mode that breached the sibling guard 15/15.

    SQLite cannot answer this question; its writer lock serialises these anyway,
    so a green run there says nothing about the deployed backend.
    """
    outbox = pg_outbox
    delivered = "the words the guest already has"

    # --- insert-shaped: four tabs replaying the same posted text -------------
    sent = _seed(pg_outbox, "r1", outbox.SENT, body=delivered)
    assert outbox.get(sent["id"])["status"] == outbox.SENT
    assert outbox.in_flight_for_item("t1", SITE, "r1") is None, (
        "precondition: the earlier send has settled, so nothing is in flight "
        "and only the body guard can refuse these replays")
    made = {}

    def replay(i):
        made[i] = outbox.add("t1", SITE, "r1", sequence="presale",
                             step_id="intro", step_label="Reply",
                             body=delivered, auto=True,
                             unless_in_flight=True, unless_body_sent=True)

    _race(replay, n=4)
    got_through = [i for i, v in made.items() if v is not None]
    assert not got_through, (
        f"{len(got_through)}/4 racing replays queued a message this guest had "
        f"already received: {made}")

    # --- update-shaped: four retries of failed rows carrying that body ------
    sent2 = _seed(pg_outbox, "r2", outbox.SENT, body=delivered)
    assert outbox.get(sent2["id"])["status"] == outbox.SENT
    failed = [_seed(pg_outbox, "r2", outbox.FAILED, body=delivered)
              for _ in range(4)]
    assert outbox.in_flight_for_item("t1", SITE, "r2") is None, (
        "precondition: failed rows are not in flight")
    released = {}

    def retry(i):
        released[i] = outbox.release_to_send(
            failed[i]["id"], from_statuses=(outbox.FAILED,))[0]

    _race(retry, n=4)
    assert not any(released.values()), (
        f"{sum(bool(v) for v in released.values())}/4 racing retries re-queued a "
        f"body already delivered to this guest: {released}")
    assert _in_flight(outbox, "r2") == 0, (
        f"{_in_flight(outbox, 'r2')} rows in flight after four refused retries")


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

    # With nothing in flight, a fresh message is released normally. "Fresh" now
    # has to mean different *words*, not merely a different row: VEN-170 added a
    # body guard, and this fixture seeded every row with the same body, so the
    # message this line calls fresh was byte-for-byte the one already delivered.
    # Left at "b" the assertion would have gone on passing only while the guard
    # was absent — and the stranding it is here to catch would look identical.
    nxt = _seed(pg_outbox, "s1", outbox.PENDING, body="b2")
    released, row = outbox.release_to_send(nxt["id"],
                                           from_statuses=outbox.APPROVABLE)
    assert released is True and row["status"] == outbox.QUEUED

    # ...and the other half of that distinction, so narrowing the guard back to
    # "nothing in flight" cannot pass this file: same words as a delivered row
    # is refused, on the backend where the predicate is a real CAS.
    #
    # Settle that release first. Left queued it would block the next one all by
    # itself through `unless_sibling_in_flight`, and the assertion below would
    # hold with no body guard in the code at all — a green test measuring the
    # wrong predicate. State the precondition rather than trusting it.
    outbox.set_status(nxt["id"], outbox.SENT)
    assert outbox.in_flight_for_item("t1", SITE, "s1") is None, (
        "precondition: nothing in flight, so only the body guard can refuse")
    dup = _seed(pg_outbox, "s1", outbox.PENDING, body="b")
    released, _ = outbox.release_to_send(dup["id"],
                                         from_statuses=outbox.APPROVABLE)
    assert released is False, (
        "a body already delivered to this guest was released a second time")


# --------------------------------------------------------------------------
# Round 6: the due gate, on the backend that actually runs it
# --------------------------------------------------------------------------
#
# Round 6 added `COALESCE(sib.scheduled_at,'') <= ?` to the sibling predicate so
# a deferred row would stop blocking. That was reverted: a deferred row is not
# cancelled, it is scheduled, and releasing a second message beside it delivers
# both. What survives is the assertion that the *broad* predicate holds on the
# deployed backend — cheap cases, and SQLite is the wrong place to sign them off
# for the same reason the rest of this file exists.

def _seed_at(outbox, item_id, scheduled_at, *, auto=True):
    msg = outbox.add("t1", SITE, item_id, sequence="presale", step_id="intro",
                     step_label="Intro", body="b", auto=auto,
                     scheduled_at=scheduled_at)
    return outbox.get(msg["id"])


def _shift_pg(hours):
    from datetime import datetime, timedelta
    import timeframe
    return (datetime.fromisoformat(timeframe.now())
            + timedelta(hours=hours)).isoformat(timespec="seconds")


def test_a_deferred_sibling_still_blocks_on_postgres(pg_outbox):
    """A deferred row blocks on the deployed backend too.

    The stamp is TEXT and the predicate is now status-only, so there is no
    lexicographic comparison left to differ between backends — which is the
    point: this fails loudly if anyone reintroduces one on PG alone.
    """
    outbox = pg_outbox
    deferred = _seed_at(outbox, "d1", _shift_pg(8))
    assert deferred["status"] == outbox.QUEUED
    assert outbox.next_queued("t1") is None, (
        "precondition: the deferred row is already due")

    pending = _seed(outbox, "d1", outbox.PENDING)
    released, _ = outbox.release_to_send(pending["id"],
                                         from_statuses=outbox.APPROVABLE)
    assert released is False, (
        "Postgres released a second message beside a row scheduled 8h out; "
        "both come due and the guest receives both")


def test_a_due_sibling_still_blocks_on_postgres(pg_outbox):
    """The inverse, on Postgres: the gate must not have disarmed the guard."""
    outbox = pg_outbox
    _seed_at(outbox, "d2", _shift_pg(-1))
    pending = _seed(outbox, "d2", outbox.PENDING)

    released, _ = outbox.release_to_send(pending["id"],
                                         from_statuses=outbox.APPROVABLE)
    assert released is False, (
        "the due gate disarmed the sibling guard on Postgres — a due queued row "
        "stopped blocking a second release")


def test_racing_approvals_beside_a_deferred_row_both_lose(pg_outbox):
    """Concurrency must not find a way past a blocker a single caller respects.

    Two approvals racing beside a deferred row: the advisory lock and the
    sibling predicate together have to refuse *both*, leaving the deferred row
    as the only thing going out. Under the reverted round-6 gate this same
    shape let one through, and then two messages reached the guest — so this
    asserts the delivery count, not just the return values.
    """
    outbox = pg_outbox
    for trial in range(6):
        item = f"dr{trial}"
        _seed_at(outbox, item, _shift_pg(8))          # a real blocker
        rows = [_seed(outbox, item, outbox.PENDING) for _ in range(2)]
        won = {}

        def approve(i):
            mid = rows[i]["id"]
            won[mid] = outbox.release_to_send(
                mid, from_statuses=outbox.APPROVABLE)[0]

        _race(approve)
        assert sorted(won.values(), key=str) == [False, False], (
            f"trial {trial}: an approval got past a deferred blocker under a "
            f"race: {won}")
        assert _in_flight(outbox, item) == 1, (
            f"trial {trial}: expected only the deferred row to be in flight, "
            f"got {_in_flight(outbox, item)}")


def test_add_guard_does_not_escape_its_tenant_item_scope(pg_outbox):
    """VEN-170: the OR-joined insert guard, on the deployed backend.

    A missing parenthesis is a static parse question, so this is not a race and
    does not need the concurrency harness above — but it is asserted here
    anyway because the predicate it checks is the one `DEPLOY.md` says runs on
    Postgres, and "identical on both backends" is worth measuring rather than
    reasoning about. The three axes match the SQLite test of the same name in
    `tests/test_dashboard_send_state.py`.
    """
    ob = pg_outbox
    with ob._conn() as c:
        assert c.pg, "positive control: this fixture is not actually on Postgres"

    body = "Hi! Yes, the unit is available for those dates."

    def add(tid, item):
        return ob.add(tid, SITE, item, sequence="presale", step_id="intro",
                      step_label="Reply", body=body, reason="r", auto=True,
                      unless_in_flight=True, unless_body_sent=True)

    first = add("t1", "A")
    assert first is not None
    ob.set_status(first["id"], ob.SENDING)
    ob.set_status(first["id"], ob.SENT)
    assert ob.in_flight_for_item("t1", SITE, "B") is None, (
        "precondition: nothing in flight for the other guest")
    assert ob.in_flight_for_item("t2", SITE, "C") is None

    assert add("t1", "A") is None, "precondition: the duplicate guard still holds"
    assert add("t1", "B") is not None, "another guest was locked out on PG"
    assert add("t2", "C") is not None, "ANOTHER TENANT was locked out on PG"
