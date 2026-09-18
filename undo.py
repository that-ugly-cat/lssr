"""Undo for a deletion on the Records step.

A removal there is a *hard* delete by design: removals are tracked from
screening onward, as exclude decisions with a reviewer's name on them, and a
record dropped before any of that happened should leave no trace. That design
is kept — and this is the ten seconds of grace around it, for the mis-click and
the batch selected one row too wide.

Two things make it more than a dictionary in memory.

**It restores the original primary keys.** A record's id is not an internal
detail: it is the filename of its stored PDF on disk (``data/fulltext/{ws}/{id}
.pdf``) and one half of every dedup dismissal key. A record that came back under
a fresh id would come back subtly wrong — its full text orphaned, its "not a
duplicate" rulings pointing at nothing. Where an id has been taken in the
meantime the record is restored under a new one and its children are remapped,
which is the lesser wrong and is reported rather than hidden.

**It is written to disk, not held in the process.** A batch delete can carry
hundreds of records with their abstracts and, if retrieval already ran, their
full texts — tens of megabytes that have no business sitting in the memory of a
container capped at 1 GB. On the volume they also survive a restart, so an undo
offered is an undo that still works a deploy later.
"""
import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import inspect as sa_inspect

from models import Earmark, Extraction, RawReference, Record, ScreenDecision

UNDO_ROOT = Path(os.getenv("UNDO_ROOT", "data/undo"))

#: How long a snapshot is kept. Far longer than the toast that offers it: the
#: cost is a small file, and the alternative is a promise that expires while
#: somebody is reading the sentence that makes it.
MAX_AGE_SECONDS = 24 * 3600

_DT = "__dt__"


def _dump_row(obj) -> dict:
    out = {}
    for col in sa_inspect(obj).mapper.column_attrs:
        v = getattr(obj, col.key)
        out[col.key] = {_DT: v.isoformat()} if isinstance(v, datetime) else v
    return out


def _load_row(model, data: dict, **override):
    kw = {}
    for k, v in data.items():
        if isinstance(v, dict) and _DT in v:
            try:
                v = datetime.fromisoformat(v[_DT])
            except (TypeError, ValueError):
                v = None
        kw[k] = v
    kw.update(override)
    return model(**kw)


def snapshot(db, ws_id: int, recs: list) -> dict:
    """Everything `_delete_records` is about to destroy, as plain data."""
    rids = [r.id for r in recs]
    child = lambda model: (db.query(model).filter(model.record_id.in_(rids)).all()  # noqa: E731
                           if rids else [])
    return {
        "workspace_id": ws_id,
        "records": [_dump_row(r) for r in recs],
        "screen_decisions": [_dump_row(r) for r in child(ScreenDecision)],
        "extractions": [_dump_row(r) for r in child(Extraction)],
        "raw_references": [_dump_row(r) for r in child(RawReference)],
        "earmarks": [_dump_row(r) for r in child(Earmark)],
    }


def _dir(ws_id: int) -> Path:
    d = UNDO_ROOT / str(ws_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def stash(ws_id: int, payload: dict) -> str:
    """Write a snapshot and return the token that buys it back."""
    prune(ws_id)
    token = secrets.token_urlsafe(16)
    (_dir(ws_id) / f"{token}.json").write_text(json.dumps(payload), encoding="utf-8")
    return token


def prune(ws_id: int, max_age: int = MAX_AGE_SECONDS) -> None:
    """Drop snapshots nobody is coming back for. Best effort: a snapshot that
    cannot be deleted is not worth failing a delete over."""
    cutoff = time.time() - max_age
    try:
        for f in _dir(ws_id).glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def _path(ws_id: int, token: str) -> Path | None:
    # The token comes off the wire and is about to be a filename: anything but
    # the alphabet secrets.token_urlsafe produces is refused outright rather
    # than sanitised, because a path that needs sanitising is already an attack.
    if not token or not all(c.isalnum() or c in "-_" for c in token):
        return None
    p = _dir(ws_id) / f"{token}.json"
    return p if p.is_file() else None


def restore(db, ws_id: int, token: str) -> dict:
    """Put a snapshot back. Returns {records, renumbered} or None if the token
    is unknown — which is the ordinary case for an undo clicked twice."""
    p = _path(ws_id, token)
    if p is None:
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload.get("workspace_id") != ws_id:
        return None

    taken = {r[0] for r in db.query(Record.id).all()}
    id_map, renumbered = {}, 0
    for data in payload.get("records", []):
        old = data.get("id")
        if old in taken:
            data = dict(data, id=None)      # let the database pick a free one
            renumbered += 1
        rec = _load_row(Record, data, workspace_id=ws_id)
        db.add(rec)
        db.flush()                          # so the new id is known
        id_map[old] = rec.id

    for model, key in ((ScreenDecision, "screen_decisions"),
                       (Extraction, "extractions"),
                       (RawReference, "raw_references"),
                       (Earmark, "earmarks")):
        for data in payload.get(key, []):
            rid = id_map.get(data.get("record_id"))
            if rid is None:
                continue                    # its record did not come back
            db.add(_load_row(model, dict(data, id=None),
                             workspace_id=ws_id, record_id=rid))
    db.commit()
    p.unlink(missing_ok=True)
    return {"records": len(id_map), "renumbered": renumbered}
