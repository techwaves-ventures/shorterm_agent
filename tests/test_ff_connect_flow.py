"""Tests for the honest FurnishedFinder connect flow + worker-backed scraping.

Standalone (no pytest dependency): run with the project's venv so it exercises
the real modules against a throwaway SQLite database:

    ./.venv/bin/python tests/test_ff_connect_flow.py

Covers the two release blockers from VEN-20:
  1. Saving only the FF email/consent must NOT read as connected — it lands in
     `needs_verification`; `connected` is only reached after a real scrape.
  2. "Check now" on a serverless (no-Playwright) host must enqueue a worker job
     via the shared DB instead of calling Playwright in-process (no raw error).
"""
import os
import sys
import tempfile
from pathlib import Path

# --- Isolate DB + secrets BEFORE importing the app modules -----------------
_TMP = tempfile.mkdtemp(prefix="ff_test_")
os.environ["SQLITE_PATH"] = str(Path(_TMP) / "test.db")
os.environ.pop("DATABASE_URL", None)  # force SQLite
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["OPERATOR_EMAIL"] = "op@test.local"
os.environ["OPERATOR_PASSWORD"] = "op-password-123"

from cryptography.fernet import Fernet  # noqa: E402
os.environ["FF_CRED_KEY"] = Fernet.generate_key().decode()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_leads  # noqa: E402
import ff_account  # noqa: E402
import jobs  # noqa: E402
from sites import furnishedfinder  # noqa: E402

_FAILURES: list[str] = []


def check(cond, msg):
    if cond:
        print(f"  ok  {msg}")
    else:
        print(f" FAIL {msg}")
        _FAILURES.append(msg)


# ---------------------------------------------------------------------------


def test_connect_states():
    print("test_connect_states")
    t = "t-connect"
    check(ff_account.get_state(t) == ff_account.NOT_CONNECTED, "unlinked tenant is not_connected")

    ff_account.connect(t, "host@example.test")
    st = ff_account.status(t)
    check(ff_account.get_state(t) == ff_account.NEEDS_VERIFICATION,
          "saving email lands in needs_verification (NOT connected)")
    check(st["connected"] is False, "status.connected is False after email save")
    check(ff_account.is_verified(t) is False, "is_verified False after email save")
    check(ff_account.has_account(t) is True, "has_account True after email save")
    check(st["masked_email"] == "h***@example.test", "email is masked, not exposed")

    ff_account.mark_state(t, ff_account.VERIFYING)
    check(ff_account.get_state(t) == ff_account.VERIFYING, "mark_state -> verifying")

    ff_account.mark_state(t, ff_account.CONNECTED)
    st = ff_account.status(t)
    check(st["connected"] is True and ff_account.is_verified(t),
          "connected only after a real verification (mark_state connected)")
    check(st["verified_at"] is not None, "verified_at is stamped on connect")

    ff_account.mark_state(t, ff_account.ERROR, error="login failed")
    st = ff_account.status(t)
    check(st["connected"] is False and st["state"] == ff_account.ERROR, "error state is not connected")
    check(st["last_error"] == "login failed", "last_error stored (UI-safe)")

    # Re-saving the email resets to needs_verification and clears prior verify/error.
    ff_account.connect(t, "host2@example.test")
    st = ff_account.status(t)
    check(st["state"] == ff_account.NEEDS_VERIFICATION and st["verified_at"] is None
          and st["last_error"] is None, "reconnect resets to needs_verification")

    # mark_state never creates a row (operator '1' safety).
    ff_account.mark_state("no-such-tenant", ff_account.CONNECTED)
    check(ff_account.get_state("no-such-tenant") == ff_account.NOT_CONNECTED,
          "mark_state on a missing row is a no-op (operator-safe)")


def test_jobs_queue():
    print("test_jobs_queue")
    t = "t-job"
    j1 = jobs.enqueue(t)
    check(j1["status"] == jobs.QUEUED, "enqueue creates a queued job")
    j2 = jobs.enqueue(t)
    check(j2["id"] == j1["id"], "second enqueue coalesces onto the active job")
    check(jobs.get_active(t) is not None, "get_active finds the queued job")

    check(jobs.worker_online() is False, "no heartbeat yet -> worker offline")
    jobs.heartbeat("worker-A")
    check(jobs.worker_online() is True, "worker online after heartbeat")

    claimed = jobs.claim_next("worker-A")
    check(claimed and claimed["id"] == j1["id"] and claimed["status"] == jobs.RUNNING,
          "claim_next takes the queued job and marks it running")
    check(jobs.claim_next("worker-A") is None, "queue empty after claim")

    check(jobs.submit_otp(t, "654321") is True, "submit_otp attaches a code to the active job")
    check(jobs.consume_otp(j1["id"]) == "654321", "worker consumes the decrypted OTP")
    check(jobs.consume_otp(j1["id"]) is None, "OTP is single-use (cleared after consume)")

    import json
    jobs.set_status(j1["id"], jobs.DONE, "Done — leads updated.", counts=json.dumps({"furnishedfinder": {"lead": 2}}))
    check(jobs.latest(t)["status"] == jobs.DONE, "set_status marks the job done")
    check(jobs.get_active(t) is None, "a done job is no longer active")

    t_err = "t-job-error-cooldown"
    err_job = jobs.enqueue(t_err)
    jobs.set_status(err_job["id"], jobs.ERROR, "Couldn't verify your FurnishedFinder login.")
    retry = jobs.enqueue(t_err)
    check(retry["id"] == err_job["id"] and jobs.get_active(t_err) is None,
          "recent error retry is throttled instead of creating another login job")

    # A brand-new tenant with no active job can't submit an OTP.
    check(jobs.submit_otp("t-nobody", "111") is False, "submit_otp fails with no active job")


def test_public_state():
    print("test_public_state")
    t = "t-ui"
    check(jobs.public_state(t)["status"] == "idle", "no job -> idle")

    jobs.enqueue(t)
    ps = jobs.public_state(t)
    check(ps["status"] == "launching" and ps["running"] is True, "queued projects as active/launching")

    j = jobs.claim_next("worker-B")
    check(jobs.public_state(t)["status"] == "checking", "running projects as checking")

    jobs.set_status(j["id"], jobs.WAITING_FOR_OTP, "Enter the code FurnishedFinder emailed you.")
    check(jobs.public_state(t)["status"] == "waiting_for_otp", "waiting_for_otp projects through")

    import json
    jobs.set_status(j["id"], jobs.DONE, "Done.", counts=json.dumps({"furnishedfinder": {"lead": 1}}))
    ps = jobs.public_state(t)
    check(ps["status"] == "done" and ps["counts"].get("furnishedfinder"), "done projects with counts")

    j2 = jobs.enqueue(t)
    jobs.set_status(j2["id"], jobs.ERROR, "Couldn't verify your FurnishedFinder login. Please try Check now again.")
    ps = jobs.public_state(t)
    check(ps["status"] == "error" and "stack" not in ps["message"].lower(),
          "error projects a friendly (non-raw) message")


def test_serverless_refresh_routing():
    print("test_serverless_refresh_routing")
    import models
    import dashboard

    # Simulate a serverless host: Playwright not importable.
    saved = check_leads.sync_playwright
    check_leads.sync_playwright = None
    try:
        check(check_leads.playwright_available() is False, "playwright_available() False on serverless")

        # run_scrape still raises the clear error if ever called directly...
        raised = False
        try:
            check_leads.run_scrape(tenant_id="t-web")
        except RuntimeError:
            raised = True
        check(raised, "run_scrape still guards with a clear error when misused")

        # ...but /refresh must NOT call it — it enqueues a worker job instead.
        email = "webuser@test.local"
        if not models.get_user_by_email(email):
            models.create_user(email, "pw-123456")
        user = models.get_user_by_email(email)
        tid = user.tenant_id

        dashboard.app.config["TESTING"] = True
        client = dashboard.app.test_client()
        r = client.post("/login", data={"email": email, "password": "pw-123456"}, follow_redirects=False)
        check(r.status_code in (302, 303), "login redirects")

        r = client.post("/connect", data={"ff_email": "landlord@ff.test", "consent": "1"})
        check(ff_account.get_state(tid) == ff_account.NEEDS_VERIFICATION,
              "/connect saves email as needs_verification (not connected)")

        r = client.post("/refresh")
        check(r.status_code == 200, "/refresh returns 200 on serverless (no raw error)")
        body = r.get_json()
        check(body and body.get("running") is True, "/refresh reports an active worker-backed run")
        check(jobs.get_active(tid) is not None, "/refresh enqueued a job in the shared DB")

        r = client.get("/api/status")
        status_body = r.get_json()
        check(status_body.get("status") in ("launching", "checking", "waiting_for_otp"),
              "/api/status reflects the queued worker job")
        check(status_body.get("ff_status", {}).get("state") == ff_account.NEEDS_VERIFICATION,
              "/api/status includes current FF account verification state")

        r = client.post("/otp", data={"code": "999888"})
        check(r.get_json().get("ok") is True, "/otp routes the code to the active worker job")
    finally:
        check_leads.sync_playwright = saved


def test_force_worker_queue_routing():
    print("test_force_worker_queue_routing")
    import dashboard
    import models

    email = "force-worker@test.local"
    if not models.get_user_by_email(email):
        models.create_user(email, "pw-123456")
    user = models.get_user_by_email(email)
    tid = user.tenant_id

    os.environ["FORCE_WORKER_QUEUE"] = "1"
    try:
        dashboard.app.config["TESTING"] = True
        client = dashboard.app.test_client()
        r = client.post("/login", data={"email": email, "password": "pw-123456"}, follow_redirects=False)
        check(r.status_code in (302, 303), "force-worker login redirects")

        r = client.post("/connect", data={"ff_email": "force@ff.test", "consent": "1"})
        check(ff_account.get_state(tid) == ff_account.NEEDS_VERIFICATION,
              "force-worker /connect saves email for verification")

        r = client.post("/refresh")
        check(r.status_code == 200, "FORCE_WORKER_QUEUE /refresh returns 200")
        check(jobs.get_active(tid) is not None, "FORCE_WORKER_QUEUE enqueues a DB job")

        r = client.post("/otp", data={"code": "123456"})
        check(r.get_json().get("ok") is True, "FORCE_WORKER_QUEUE /otp routes through jobs")
    finally:
        os.environ.pop("FORCE_WORKER_QUEUE", None)


def test_cloudflare_challenge_is_fatal():
    print("test_cloudflare_challenge_is_fatal")

    class Body:
        def inner_text(self, timeout=0):
            return "Attention Required! | Cloudflare\nCloudflare Ray ID: test"

    class FakePage:
        url = "https://www.furnishedfinder.com/members/tenant-lead"

        def title(self):
            return "Attention Required! | Cloudflare"

        def locator(self, selector):
            return Body()

    err = None
    try:
        furnishedfinder._raise_if_blocked(FakePage(), "test")
    except Exception as e:
        err = e
    check(isinstance(err, furnishedfinder.FurnishedFinderBlocked),
          "Cloudflare challenge raises a FurnishedFinderBlocked error")
    check(getattr(err, "fatal_scrape", False) is True,
          "Cloudflare challenge is fatal for the worker job")
    check("security challenge" in getattr(err, "user_safe_message", ""),
          "Cloudflare error has a UI-safe message")


def test_ff_login_dialog_invalidates_session_probe():
    print("test_ff_login_dialog_invalidates_session_probe")

    class Locator:
        def __init__(self, text="", visible=False):
            self.text = text
            self.visible = visible

        def inner_text(self, timeout=0):
            return self.text

        @property
        def first(self):
            return self

        def is_visible(self, timeout=0):
            return self.visible

    class FakePage:
        url = "https://www.furnishedfinder.com/members/tenant-lead"

        def title(self):
            return "Tenant Leads | Furnished Finder"

        def goto(self, url, wait_until=None):
            self.url = url

        def wait_for_timeout(self, ms):
            pass

        def locator(self, selector):
            if selector == "body":
                return Locator(
                    "Skip to main content\n"
                    "Dialog content\n"
                    "Login to your account\n"
                    "Not a member yet? Sign Up\n"
                    "Forgot Username Or Password?\n"
                    "Login"
                )
            if selector == "input#username":
                return Locator(visible=True)
            return Locator()

    check(furnishedfinder._session_ok(FakePage()) is False,
          "FF login dialog means the persistent browser session is not authenticated")


if __name__ == "__main__":
    test_connect_states()
    test_jobs_queue()
    test_public_state()
    test_serverless_refresh_routing()
    test_force_worker_queue_routing()
    test_cloudflare_challenge_is_fatal()
    test_ff_login_dialog_invalidates_session_probe()
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL TESTS PASSED")
