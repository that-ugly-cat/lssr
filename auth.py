"""
Authentication for LSSR.

Strategy (borant house pattern): JWT stored in an httpOnly cookie named 'session'.
- Token lifetime: 7 days (renewed on login only).
- Secret key via JWT_SECRET env var; startup crashes if missing.
- is_admin flag on User for admin-only routes.

There is deliberately no second factor here. One was sketched in the schema for
a long time and never wired to a route, which is worse than not having it: the
tool advertised a protection it did not apply. Where a second factor is wanted,
it belongs to the SSO gate in front (`AUTH_MODE=gateway`), which has one that
works and can be turned on per app without touching this code.
"""
import ipaddress
import logging
import os
import secrets
from datetime import datetime, timedelta

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from models import User, get_db

log = logging.getLogger("lssr.auth")

SECRET_KEY  = os.environ["JWT_SECRET"]
ALGORITHM   = "HS256"
EXPIRE_DAYS = 7

# Two ways of recognising a reviewer, and `local` is the default on purpose: an
# app that believes an identity header with nothing in front of it lets in
# anyone who sends that header. The gateway path stays dead code until someone
# turns it on deliberately.
#
#   local     email + password against the users table, as it has always worked
#   gateway   an upstream SSO gate vouches for the caller via X-Borant-*
#
# The public share pages (/r/{token}) are outside all of this in both modes: an
# external reader has no account here and is not supposed to get one.
AUTH_MODE = os.environ.get("AUTH_MODE", "local").strip().lower()

# In gateway mode identity headers are believed only from here — the reverse
# proxy, never the internet. Under Docker this is a bridge gateway and NOT
# 127.0.0.1; DEPLOY.md shows how to read the real value off a running container.
TRUSTED_PROXY = os.environ.get("BORANT_TRUSTED_PROXY", "127.0.0.1")


def _parse_trusted(raw: str) -> list:
    nets = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            log.warning("BORANT_TRUSTED_PROXY: ignoring %r, not an address or CIDR", chunk)
    return nets


TRUSTED_PROXIES = _parse_trusted(TRUSTED_PROXY)


def gateway_mode() -> bool:
    return AUTH_MODE == "gateway"


def _from_trusted_proxy(request: Request) -> bool:
    peer = request.client.host if request.client else None
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXIES)


def user_from_gateway(request: Request, db: Session) -> User | None:
    """The reviewer the gate vouched for, or None.

    Lookup is by `borant_sub` and never by email. That is not fussiness here: a
    reviewer's id is the `reviewer_id` on thousands of screening decisions and
    extractions, so landing someone in the wrong row — or in a fresh one — does
    not just inconvenience them, it detaches them from their own work and breaks
    the blind double-screening attribution. An unknown subject therefore gets a
    new profile rather than being matched to an existing one by address;
    map_borant.py does the linking once, by hand, and prints what it did.
    """
    if not gateway_mode():
        return None
    sub = request.headers.get("x-borant-sub")
    if not sub:
        return None
    if not _from_trusted_proxy(request):
        log.warning("X-Borant-Sub from %s, outside BORANT_TRUSTED_PROXY (%s): ignored",
                    request.client.host if request.client else "?", TRUSTED_PROXY)
        return None

    user = db.query(User).filter(User.borant_sub == sub).first()
    if user is not None:
        return user if user.is_active else None

    email = (request.headers.get("x-borant-email", "") or f"{sub}@borant.invalid").strip().lower()
    # A local password nobody knows, rather than none: `AUTH_MODE=local` has to
    # stay a working way back, and a row with no password is not a way back.
    # L'hint del gate puo' proporre `admin`, e da oggi viene onorato.
    #
    # Qui `is_admin` apre la gestione degli utenti — disattivare, resettare
    # password e secondo fattore, promuovere — e non le funzioni del prodotto,
    # che sono aperte a chiunque abbia un grant. La deroga alla regola «mai
    # provisionare privilegio da un header» regge sul solito presupposto: la
    # registrazione aperta su Borant ID e' spenta, e anche una richiesta
    # d'accesso fa scegliere il ruolo all'amministratore approvando, quindi
    # `admin` in quell'header c'e' solo perche' un umano l'ha digitato.
    #
    # Quello che il codice deve comunque e' **rumore**. Un hint non conosciuto
    # e' un refuso, non un ruolo, e non concede niente.
    hint = (request.headers.get("x-borant-hint", "") or "").strip().lower()
    fa_admin = hint == "admin"
    if hint and not fa_admin:
        log.warning("gateway: hint %r non e' un ruolo di questa app, ignorato", hint)
    if fa_admin:
        log.warning("gateway: %s (%s) creato come ADMIN su suggerimento del gate. "
                    "Quel ruolo gestisce gli utenti di questa app: disattivarli, "
                    "resettarne password e secondo fattore. Revocare da /admin se "
                    "non era voluto.", email, sub)
    user = User(email=email, name=request.headers.get("x-borant-name", "") or email,
                hashed_password=hash_password(secrets.token_urlsafe(32)),
                borant_sub=sub, is_active=True, is_admin=fa_admin)
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info("gateway: new profile for %s (%s), admin=%s", email, sub, fa_admin)
    return user


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(days=EXPIRE_DAYS)
    return jwt.encode({"sub": str(user_id), "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def _decode_token(token: str) -> int:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")


def get_current_user(
    request: Request,
    session: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    if gateway_mode():
        # The header wins over the local cookie, always: a leftover cookie must
        # not outlive a session the gate has revoked.
        user = user_from_gateway(request, db)
        if user is not None:
            return user
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user_id = _decode_token(session)
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()  # noqa: E712
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def get_user_or_none(session: str | None, db: Session,
                     request: Request | None = None) -> User | None:
    """Plain function (not a Depends) for pages that render logged-out too."""
    if gateway_mode():
        return user_from_gateway(request, db) if request is not None else None
    if not session:
        return None
    try:
        user_id = _decode_token(session)
    except HTTPException:
        return None
    return db.query(User).filter(User.id == user_id, User.is_active == True).first()  # noqa: E712


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user
