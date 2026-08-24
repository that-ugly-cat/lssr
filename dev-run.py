"""Avvio locale di LSSR, per guardarlo in un browser.

DB usa-e-getta in `.devdata/`, segreti generati lì la prima volta: non tocca
niente di produzione e non ha modo di farlo — `DATABASE_URL` punta altrove, e
`data/` (dove vivono il DB vero e i PDF scaricati) resta fuori dai piedi.

`PUBLIC_URL` va impostata anche in locale: la superficie `/mcp` controlla
l'header `Host` contro il DNS rebinding, e senza quella variabile ogni richiesta
MCP tornerebbe *Invalid Host header* — un errore che in locale sembra un bug del
codice e non è.

È uno script Python e non uno shell script di proposito: l'anteprima lancia
`bash` sotto WSL, che vede percorsi `/mnt/c/...` mentre l'interprete è un binario
Windows, e i due non si mettono d'accordo su cosa sia una directory. Qui
l'interprete è già quello giusto e non c'è nessuna traduzione da fare.
"""
import os
import pathlib
import secrets

BASE = pathlib.Path(__file__).resolve().parent
DEV = BASE / ".devdata"
DEV.mkdir(exist_ok=True)

chiave = DEV / "fernet.key"
if not chiave.exists():
    from cryptography.fernet import Fernet
    chiave.write_text(Fernet.generate_key().decode(), encoding="utf-8")

jwt = DEV / "jwt.key"
if not jwt.exists():
    jwt.write_text(secrets.token_hex(32), encoding="utf-8")

os.environ.setdefault("JWT_SECRET", jwt.read_text(encoding="utf-8").strip())
os.environ.setdefault("FERNET_KEY", chiave.read_text(encoding="utf-8").strip())
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(DEV / 'dev.db').as_posix()}")
os.environ.setdefault("PUBLIC_URL", "http://localhost:8013")

os.chdir(BASE)

if __name__ == "__main__":
    import models
    from auth import hash_password

    models.init_db()
    db = models.SessionLocal()
    try:
        if db.query(models.User).count() == 0:
            db.add(models.User(email="dev@local", name="Dev",
                               hashed_password=hash_password("dev"),
                               is_admin=True, is_active=True))
            db.commit()
            print("utente di sviluppo: dev@local / dev")
    finally:
        db.close()

    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8013)
