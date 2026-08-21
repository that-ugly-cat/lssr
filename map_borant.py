"""
Link existing reviewers to the subjects an SSO gate knows them by.

Run once, by hand, BEFORE switching AUTH_MODE to `gateway`, and read the report
before believing it:

    docker exec lssr python map_borant.py --map you@example.org=01ABC...
    docker exec lssr python map_borant.py --report

Why a script rather than an automatic match at request time: linking by email is
defensible in principle, because the address arrives from the gate and not from
the client — but doing it live means one typo in the gate's admin panel silently
merges two people, and nobody finds out.

Why it matters more here than elsewhere. A reviewer's id is the `reviewer_id` on
every screening decision and every extraction they have made. Someone who
arrives unlinked does not merely get an inconvenient empty screen: they get a
*different id*, so their thousands of decisions stay attached to a row nobody
reaches any more, and blind double-screening starts counting them as a second
reviewer who happens to agree with themselves. The report below therefore prints
the work attached to each account, because that is the number that says how bad
getting this wrong would be.

Nothing here is destructive: an existing link is reported, never overwritten,
and --unlink undoes one.
"""
import argparse
import sys

from sqlalchemy import text

from models import SessionLocal, User


def _work(db, uid: int) -> tuple[int, int]:
    dec = db.execute(
        text("SELECT COUNT(*) FROM screen_decisions "
             "WHERE reviewer_kind IN ('user','adjudicator') AND reviewer_id = :i"),
        {"i": uid}).scalar() or 0
    ext = db.execute(
        text("SELECT COUNT(*) FROM extractions "
             "WHERE reviewer_kind = 'user' AND reviewer_id = :i"),
        {"i": uid}).scalar() or 0
    return dec, ext


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", action="append", default=[], metavar="EMAIL=SUBJECT",
                    help="link one reviewer to one gate subject; repeatable")
    ap.add_argument("--unlink", action="append", default=[], metavar="EMAIL",
                    help="drop the link for one reviewer; repeatable")
    ap.add_argument("--report", action="store_true",
                    help="print who is linked and who is not, and change nothing")
    args = ap.parse_args()

    db = SessionLocal()
    changed = 0

    for pair in args.map:
        email, sep, subject = pair.partition("=")
        email, subject = email.strip().lower(), subject.strip()
        if not sep or not email or not subject:
            print(f"  SALTO     {pair!r}: serve la forma email=subject")
            continue
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            print(f"  ASSENTE   {email}: nessun reviewer con questo indirizzo")
            continue
        if user.borant_sub == subject:
            print(f"  GIA-OK    {email} -> {subject}")
            continue
        if user.borant_sub:
            print(f"  CONFLITTO {email}: gia' legato a {user.borant_sub}, non sovrascrivo. "
                  f"Usa --unlink prima, se e' voluto.")
            continue
        clash = db.query(User).filter(User.borant_sub == subject).first()
        if clash is not None:
            print(f"  CONFLITTO {email}: il subject {subject} e' gia' di {clash.email}")
            continue
        user.borant_sub = subject
        changed += 1
        dec, ext = _work(db, user.id)
        print(f"  LEGATO    {email} -> {subject}  ({dec} decisioni, {ext} estrazioni)")

    for email in args.unlink:
        email = email.strip().lower()
        user = db.query(User).filter(User.email == email).first()
        if user is None or not user.borant_sub:
            print(f"  NIENTE    {email}: non era legato")
            continue
        print(f"  SLEGATO   {email} (era {user.borant_sub})")
        user.borant_sub = None
        changed += 1

    if changed:
        db.commit()

    print("\n-- stato dei reviewer --")
    scoperti = []
    for u in db.query(User).order_by(User.id).all():
        dec, ext = _work(db, u.id)
        stato = u.borant_sub or "(nessun legame)"
        flag = " ADMIN" if u.is_admin else ""
        print(f"  {u.email:<36} dec={dec:<6} est={ext:<5} {stato}{flag}")
        if not u.borant_sub and u.is_active:
            scoperti.append((u, dec, ext))

    print(f"\n  {len(scoperti)} reviewer attivi senza legame.")
    if scoperti:
        print("  In `gateway` arrivano come profilo NUOVO, quindi con un id diverso da")
        print("  quello a cui e' attaccato il loro lavoro. Se non e' quello che vuoi,")
        print("  legali prima di accendere.")
        persi = sum(d + e for _, d, e in scoperti)
        if persi:
            print(f"  ATTENZIONE: fra loro ci sono {persi} fra decisioni ed estrazioni")
            print("  che resterebbero attaccate a profili irraggiungibili dal gate.")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
