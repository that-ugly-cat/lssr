# Deploying LSSR

LSSR is a FastAPI app backed by one SQLite file, with background threads for the
long-running steps (PubMed download, screening, full-text fetch, assessment,
synthesis). It calls the Claude API (per-user key) and the
[paper2md](https://github.com/that-ugly-cat/paper2md) service for step 7.

## 1. Configuration (environment variables)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `JWT_SECRET` | **yes, in production** | `change-me-in-production` | signs JWTs — set a long random value |
| `FERNET_KEY` | **yes, in production** | `change-me-in-production` | encrypts per-user Anthropic API keys at rest |
| `DATABASE_URL` | no | `sqlite:////app/data/lssr.db` | SQLite path |
| `PAPER2MD_URL` | no | `http://localhost:8008` | paper2md service used at step 7 |
| `UNPAYWALL_EMAIL` | no | workspace owner's email | contact email sent to the Unpaywall API |

Generate the keys:

```bash
python -c "import secrets; print(secrets.token_hex(32))"                                    # JWT_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # FERNET_KEY
```

## 2. Docker (recommended)

```bash
cp .env.example .env      # edit JWT_SECRET / FERNET_KEY / PAPER2MD_URL
docker compose up -d --build
docker compose exec app python seed_admin.py you@example.com "Your Name" "a-password"
```

`docker-compose.yml` maps the app to `127.0.0.1:8013` and mounts `./data` for the
SQLite DB and fetched PDFs (`data/fulltext/`). `mem_limit: 1000m` caps memory on a
small box.

## 3. Local / bare-metal

```bash
pip install -r requirements.txt
cp .env.example .env       # edit JWT_SECRET / FERNET_KEY
python seed_admin.py you@example.com "Your Name" "a-password"
uvicorn main:app --host 0.0.0.0 --port 8013
```

## 4. Reverse proxy (HTTPS)

Example **Caddy** (add a DNS A record first, Cloudflare "DNS only"):

```
lssr.borant.eu {
    reverse_proxy 127.0.0.1:8013
}
```

Reload after editing: `sudo systemctl reload caddy`.

`PAPER2MD_URL` should point at the paper2md container. On the shared box that is
another localhost port (e.g. `http://localhost:8008`); reachability depends on
Docker networking — put both on the same Docker network or use the host gateway.

## 5. Verify

- `https://lssr.borant.eu/login` — auth
- `https://lssr.borant.eu/` — reviews list
- each user sets their Anthropic API key under **Profile** before any LLM step

## 6. Updating

```bash
cd /opt/apps/lssr
git pull
docker compose up -d --build
```

`data/` (SQLite + PDFs) and `.env` are gitignored — `git pull` never touches them.

## 7. Backups

```bash
cp data/lssr.db backup-$(date +%F).db
tar czf backup-fulltext-$(date +%F).tar.gz data/fulltext
```
