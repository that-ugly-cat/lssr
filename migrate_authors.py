"""One-off migration: rewrite Record.authors into the canonical '; ' format.

Re-parses legacy comma-joined author strings heuristically (see authors.py).
Respects DATABASE_URL (default sqlite:///./data/lssr.db) — run from the app
root, inside the container on the VPS.

    python migrate_authors.py --dry-run   # preview the rewrites
    python migrate_authors.py             # apply
"""
import sys

from authors import canonicalize
from models import Record, SessionLocal


def main(dry: bool):
    db = SessionLocal()
    recs = db.query(Record).filter(Record.authors.isnot(None)).all()
    changed = 0
    for r in recs:
        new = canonicalize(r.authors)
        if new and new != r.authors:
            changed += 1
            if dry:
                print(f"#{r.id}: {r.authors!r}\n     -> {new!r}")
            else:
                r.authors = new
    if not dry:
        db.commit()
    print(f"{changed} of {len(recs)} records {'to update (dry run)' if dry else 'updated'}")


if __name__ == "__main__":
    main(dry="--dry-run" in sys.argv)
