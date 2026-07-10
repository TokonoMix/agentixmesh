"""Transport-agnostic message record (design §10) + serialization/parsing + validation.

Pure data layer: no I/O. The JSON form literally uses the keys ``from`` and ``to`` so a
later broker (phase 3) can stamp the same record without a redesign.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

#: Maximum body size in utf-8 bytes (bounds fd-/disk-DoS and keeps messages small).
MAX_BODY_BYTES = 64 * 1024

#: Maximum length (in characters) of the optional, sender-claimed ``subject`` field (§2A). A
#: longer subject is hard-truncated on creation — NOT a validate error, just shortened.
SUBJECT_MAX_LEN = 120

#: Allowed message kinds.
KINDS = ("request", "reply", "ack", "done", "notify")

#: Current envelope schema version. Added before the same-user dogfood so a later broker
#: (phase 3) or schema evolution can distinguish a new record without a breaking change. A
#: message without ``v`` (legacy/pre-version) is interpreted as ``1``; an unknown version is rejected.
SCHEMA_VERSION = 1

#: Address = ``"<uid>:<project>"`` — uid is numeric (kernel uid), project a routing label.
#: NO ``$`` anchor: that would let a trailing ``\n`` through. We match with ``fullmatch`` so the
#: whole address (incl. any newline) must fit.
_ADDRESS_RE = re.compile(r"\d+:[A-Za-z0-9._-]+")

_REQUIRED = ("id", "thread", "from_", "to", "kind", "ts_utc", "body")


@dataclass
class Message:
    id: str
    thread: str
    from_: str
    to: str
    kind: str
    ts_utc: str
    body: str
    subject: str = ""
    v: int = SCHEMA_VERSION


def _utc_now_iso() -> str:
    """UTC ISO-8601 with ``Z`` suffix (second precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate_subject(subject) -> str:
    """Normalize an optional ``subject``: empty/absent -> ``""``, longer than
    ``SUBJECT_MAX_LEN`` -> hard-truncated (no error, just shortened).

    A hand-crafted dropbox file can set ``subject`` to any JSON type. Coerce to ``str`` before
    slicing so a non-string value neither crashes the parse path (TypeError) nor bypasses the
    length cap via a long repr (e.g. a list). String subjects are unchanged."""
    if not subject:
        return ""
    return str(subject)[:SUBJECT_MAX_LEN]


def new_message(to, body, kind="request", thread=None, from_=None, subject=None):
    """Build a new message; generates ``id`` and ``ts_utc``. ``thread`` default = ``id``.

    ``subject`` is an optional, sender-claimed subject line (§2A) — DATA, never a
    decision-path field; too long is hard-truncated to ``SUBJECT_MAX_LEN`` characters.
    """
    mid = str(uuid.uuid4())
    return Message(
        id=mid,
        thread=thread if thread is not None else mid,
        from_=from_ if from_ is not None else "",
        to=to,
        kind=kind,
        ts_utc=_utc_now_iso(),
        body=body,
        subject=_truncate_subject(subject),
    )


def to_json(msg: Message) -> str:
    """Serialize to JSON with literal keys ``from``/``to``."""
    return json.dumps(
        {
            "v": msg.v,
            "id": msg.id,
            "thread": msg.thread,
            "from": msg.from_,
            "to": msg.to,
            "kind": msg.kind,
            "ts_utc": msg.ts_utc,
            "body": msg.body,
            "subject": msg.subject,
        },
        ensure_ascii=False,
    )


def from_json(s: str) -> Message:
    """Deserialize; maps JSON key ``from`` -> attribute ``from_``."""
    d = json.loads(s)
    try:
        return Message(
            id=d["id"],
            thread=d["thread"],
            from_=d["from"],
            to=d["to"],
            kind=d["kind"],
            ts_utc=d["ts_utc"],
            body=d["body"],
            # legacy record without a subject field -> no subject; and (untrusted-receive path, a
            # sender can drop JSON directly into the dropbox bypassing new_message()) hard-truncated
            # to SUBJECT_MAX_LEN just like on creation — the cap must apply on receipt too.
            subject=_truncate_subject(d.get("subject", "")),
            v=d.get("v", 1),  # legacy/pre-version record -> schema version 1
        )
    except KeyError as exc:
        raise ValueError(f"message is missing a required field: {exc}") from exc


def validate(msg: Message) -> None:
    """Raise ``ValueError`` on an invalid message (missing field, unknown kind,
    body too large, or ``from_``/``to`` not in ``uid:project`` form)."""
    for field in _REQUIRED:
        val = getattr(msg, field, None)
        if val is None or val == "":
            raise ValueError(f"missing required field: {field}")
    if msg.v != SCHEMA_VERSION:
        raise ValueError(f"unknown envelope version: {msg.v!r} (expected {SCHEMA_VERSION})")
    if msg.kind not in KINDS:
        raise ValueError(f"unknown kind: {msg.kind!r} (allowed: {', '.join(KINDS)})")
    if len(msg.body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError(f"body larger than MAX_BODY_BYTES ({MAX_BODY_BYTES} bytes)")
    for field in ("from_", "to"):
        addr = getattr(msg, field)
        if not _ADDRESS_RE.fullmatch(addr):
            raise ValueError(f"{field} not in uid:project form: {addr!r}")
