"""An oversized body must not turn `/inbound/email` into a 413.

The route's docstring promises one thing above all: *always* 202. Mail
providers retry on non-2xx, so any other status turns a single over-cap
notification into a retry loop, and a flat response is also what stops a prober
using this endpoint to tell configured addresses from unconfigured ones.

That promise was kept only from the `try:` downwards. Everything above it —
`request.content_length`, `request.get_data`, and the `request.form` access in
the secret lookup — runs unguarded, and `MAX_CONTENT_LENGTH` makes werkzeug
raise `RequestEntityTooLarge` from exactly there. Flask renders that as **413**,
before the view body runs, so:

  * a genuine FurnishedFinder notification carrying photos over the cap is
    retried by the provider forever and the lead never lands, and
  * nothing is written to `inbound_rejects`, because the 413 short-circuits the
    view entirely — the operator sees no trace of the lost lead.

Two harness rules this file exists to obey, both learned the hard way:

1. **Drive the WSGI app, not the test client.** Flask's test client re-derives
   the environ and drops `wsgi.input_terminated`, so `input_stream=` plus a
   `Transfer-Encoding: chunked` header delivers *zero bytes* and returns 202 —
   a pass for entirely the wrong reason. `data=` cannot express a chunked body
   at all. Every request here is a hand-built environ handed straight to
   `dashboard.app`, and every assertion is paired with the byte count actually
   consumed, read off the input stream's own cursor.

2. **Never swap `app.view_functions["inbound_email"]`.** `@csrf.exempt` is keyed
   on the function object, so replacing it silently re-arms CSRF and every
   request 400s — which looks like a failure of the code under test.
"""
import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

# --- Isolate DB + secrets BEFORE importing the app modules -----------------
_TMP = tempfile.mkdtemp(prefix="ven213_")
os.environ["SQLITE_PATH"] = str(Path(_TMP) / "test.db")
os.environ.pop("DATABASE_URL", None)  # force SQLite
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["INBOUND_EMAIL_DOMAIN"] = "inbound.example.com"
os.environ["INBOUND_WEBHOOK_SECRET"] = "provider-secret"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard  # noqa: E402
import inbound  # noqa: E402
import models  # noqa: E402

PASSWORD = "test-passphrase-123"
SECRET = "provider-secret"

# Over `MAX_CONTENT_LENGTH` (2 MB) on purpose: that is the limit whose breach
# raises out of the pre-auth reads. 3 MB clears it on every content type.
OVERSIZED = 3 * 1024 * 1024

_seq = iter(range(1, 10_000))


def _tenant():
    email = f"host{next(_seq)}@test.local"
    return models.create_user(email, PASSWORD).tenant_id


# --- The WSGI harness ------------------------------------------------------


def _environ(body: bytes, content_type: str, *, chunked: bool, headers=None,
             path="/inbound/email"):
    """A request environ a real WSGI server would produce.

    `chunked=True` is the genuine article: `wsgi.input_terminated` set and *no*
    `CONTENT_LENGTH`, which is precisely what the test client cannot express.
    """
    stream = io.BytesIO(body)
    environ = {
        "REQUEST_METHOD": "POST",
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "CONTENT_TYPE": content_type,
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": stream,
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if chunked:
        # What gunicorn sets once it has framed a chunked body: the stream is
        # self-terminating, so there is no length to declare.
        environ["wsgi.input_terminated"] = True
        environ["HTTP_TRANSFER_ENCODING"] = "chunked"
    else:
        environ["CONTENT_LENGTH"] = str(len(body))
    for name, value in (headers or {}).items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    return environ, stream


def _call(body: bytes, content_type: str, *, chunked: bool, headers=None,
          path="/inbound/email"):
    """Returns (status_code, response_body, bytes_consumed).

    `bytes_consumed` is the input stream's own cursor after the response, so a
    request that never delivered its body cannot masquerade as one that did.
    """
    environ, stream = _environ(body, content_type, chunked=chunked,
                               headers=headers, path=path)
    captured = {}

    def start_response(status, response_headers, exc_info=None):
        captured["status"] = status
        return lambda data: None

    chunks = dashboard.app(environ, start_response)
    try:
        out = b"".join(chunks)
    finally:
        if hasattr(chunks, "close"):
            chunks.close()
    return int(captured["status"].split()[0]), out, stream.tell()


def _multipart(fields: dict, attachment: bytes = None, field_blob: bytes = None,
               boundary="ven213boundary"):
    """A multipart post: text fields, optionally a file part and a bulk field.

    Both bulk shapes are needed, because they are governed by *different*
    limits and they behaved differently on the unfixed code. A part with a
    filename spools to disk and is bounded only by `MAX_CONTENT_LENGTH`; the
    same bytes in a plain field are form data and hit the much smaller
    `MAX_FORM_MEMORY_SIZE` first. Testing only `b"a" * n` in a field quotes a
    threshold no provider would ever hit; testing only the file part misses the
    cell that was actually raising on the chunked transport.
    """
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f"\r\n\r\n{value}\r\n".encode()
        )
    if field_blob is not None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="blob"'
            f"\r\n\r\n".encode()
            + field_blob
            + b"\r\n"
        )
    if attachment is not None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="attachment-1"; '
            f'filename="photo.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode()
            + attachment
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _body_of(shape: str, size: int) -> tuple[bytes, str]:
    """An oversized body of `size` bytes in each shape a provider might post."""
    if shape == "application/json":
        return b'{"text":"' + b"a" * size + b'"}', "application/json"
    if shape == "application/x-www-form-urlencoded":
        return b"text=" + b"a" * size, "application/x-www-form-urlencoded"
    if shape == "multipart-attachment":
        return _multipart({"from": "no-reply@furnishedfinder.com"},
                          attachment=b"\xff" * size)
    if shape == "multipart-field":
        return _multipart({"from": "no-reply@furnishedfinder.com"},
                          field_blob=b"a" * size)
    if shape == "text/plain":
        return b"a" * size, "text/plain"
    raise AssertionError(f"unhandled body shape {shape!r}")


BODY_SHAPES = [
    "application/json",
    "application/x-www-form-urlencoded",
    "multipart-attachment",
    "multipart-field",
    "text/plain",
]
TRANSPORTS = [
    pytest.param(False, id="content-length"),
    pytest.param(True, id="chunked"),
]


# --- 1. The harness itself -------------------------------------------------


def test_harness_actually_delivers_a_chunked_body():
    """Without this, every other test in the file could pass on zero bytes.

    This is the exact false pass the test client produces: `wsgi.input_terminated`
    missing, nothing read, 202 returned. A control that must be *read* to be
    answered is the only thing that tells the two apart.
    """
    tid = _tenant()
    body = (
        b'{"recipient":"' + inbound.address_for(tid).encode() + b'",'
        b'"from":"no-reply@furnishedfinder.com","subject":"hi","text":"hello"}'
    )
    status, _, consumed = _call(body, "application/json", chunked=True,
                                headers={"X-Inbound-Secret": SECRET})

    assert status == 202
    assert consumed == len(body), (
        "the chunked environ delivered no body — every oversized assertion in "
        "this file would then be passing without the app ever reading anything"
    )


# --- 2. The matrix: oversized and unauthenticated is still 202 -------------


@pytest.mark.parametrize("shape", BODY_SHAPES)
@pytest.mark.parametrize("chunked", TRANSPORTS)
def test_oversized_unauthenticated_is_a_flat_202(shape, chunked):
    """Six of these ten cells answered 413 before the guard.

    All five body shapes on the `Content-Length` transport, where werkzeug
    refuses on the declared length, plus the bulk-*field* multipart on the
    chunked transport, where the form-memory limit refuses mid-parse. The other
    four were already 202 — they are here so a future change cannot quietly
    move one of them into the 413 set.
    """
    body, ctype = _body_of(shape, OVERSIZED)
    status, out, _ = _call(body, ctype, chunked=chunked)

    assert status == 202, (
        f"{shape} over {'chunked' if chunked else 'Content-Length'} "
        "answered non-2xx: a provider would retry this forever"
    )
    assert out == b"", "the flat 202 carries no body — that is what makes it flat"


# --- 3. A real lead, correctly authenticated, with photos over the cap -----


@pytest.mark.parametrize("chunked", TRANSPORTS)
def test_authenticated_provider_multipart_over_the_cap_is_202(chunked):
    """The lost-lead case: a genuine notification whose attachment is too big.

    It is still dropped — `MAX_PAYLOAD_BYTES` is unchanged and out of scope
    here — but it is dropped *once*, with a 202, instead of being retried
    forever behind a 413.
    """
    tid = _tenant()
    body, ctype = _multipart(
        {
            "recipient": inbound.address_for(tid),
            "from": "no-reply@furnishedfinder.com",
            "subject": "You have a new tenant lead",
            "text": "Traveler: Emma M.",
            "secret": SECRET,
        },
        attachment=b"\xff" * OVERSIZED,
    )
    status, out, _ = _call(body, ctype, chunked=chunked,
                           headers={"X-Inbound-Secret": SECRET})

    assert status == 202
    assert out == b""


# --- 4. The pre-auth read stays bounded by a limit the app owns ------------


@pytest.mark.parametrize("shape", BODY_SHAPES)
def test_pre_auth_read_is_bounded_on_the_chunked_transport(shape):
    """The filed premise, pinned as a regression rather than as a fix.

    A chunked body has no `Content-Length`, and the ticket assumed that left the
    pre-auth read unbounded. It does not: werkzeug's `LimitedStream` enforces
    `MAX_CONTENT_LENGTH` against a terminated stream too. This asserts that
    directly, so a change that removed the cap — or read the body some way that
    bypassed it — would fail here instead of shipping.
    """
    cap = dashboard.app.config["MAX_CONTENT_LENGTH"]
    body, ctype = _body_of(shape, 8 * 1024 * 1024)
    status, _, consumed = _call(body, ctype, chunked=True)

    assert status == 202
    assert consumed <= cap, (
        f"{consumed} bytes read from an unauthenticated {shape} body "
        f"against a {cap}-byte cap"
    )


@pytest.mark.parametrize("shape", BODY_SHAPES)
def test_declared_oversize_is_refused_without_reading_a_byte(shape):
    """When the length is declared, nothing should be read at all.

    Werkzeug refuses on the header, so the guard costs no memory on this
    transport. Asserting zero — not merely "bounded" — is what would catch a
    change that started buffering the body before consulting the declaration.
    """
    body, ctype = _body_of(shape, OVERSIZED)
    status, _, consumed = _call(body, ctype, chunked=False)

    assert status == 202
    assert consumed == 0, (
        f"read {consumed} bytes of a body whose declared length already "
        "exceeded the cap"
    )


# --- 5. The form-memory threshold is the app's decision, not a default -----


def test_form_memory_size_is_set_explicitly_and_covers_the_payload_cap():
    """`MAX_FORM_MEMORY_SIZE` is a framework default this app never set.

    It moved from `None` to `500_000` in a transitive upgrade — under the app's
    own `MAX_PAYLOAD_BYTES` (524288) — which silently made a framework default
    the binding limit on a public endpoint instead of `inbound.py`'s documented
    Check 4. Pinning it here means the app owns the number; this assertion fails
    if someone deletes the config line and lets the default govern again.
    """
    configured = dashboard.app.config["MAX_FORM_MEMORY_SIZE"]

    assert configured is not None, "left to the framework default again"
    assert configured >= inbound.MAX_PAYLOAD_BYTES, (
        f"form-memory limit {configured} is below the app's own payload cap "
        f"{inbound.MAX_PAYLOAD_BYTES}, so the framework rejects before "
        "inbound.py's own check can, with a reason the app did not choose"
    )


# --- 6. Nothing else changed -----------------------------------------------


def test_other_routes_still_reject_oversized_bodies():
    """The guard is scoped to this one route.

    A global `errorhandler(RequestEntityTooLarge)` would have been a smaller
    diff and would have silently turned every oversized upload anywhere in the
    dashboard into a success. Only `/inbound/email` has a reason to answer 202.
    """
    body = b"email=a%40b.c&password=" + b"a" * OVERSIZED
    status, _, _ = _call(body, "application/x-www-form-urlencoded",
                         chunked=False, path="/login")

    assert status == 413, (
        "an ordinary dashboard route stopped rejecting oversized input — the "
        "413 handling was made global instead of local to /inbound/email"
    )
