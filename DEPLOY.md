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
| `PAPER2MD_URL` | **in practice yes** | `http://localhost:8008` | paper2md service used at step 7. The default only works when paper2md runs on the same host; point it at the deployed instance (e.g. `https://paper2md.yourdomain.example`) or every conversion fails with "connection refused" |
| `PAPER2MD_API_KEY` | no, but recommended | _(none)_ | an issued paper2md key, sent as `X-API-Key`. Without it uploads are capped at 10MB; with it, 50MB — papers routinely exceed the anonymous cap |
| `UNPAYWALL_EMAIL` | no | workspace owner's email | contact email sent to the Unpaywall API |
| `ELSEVIER_API_KEY` | no | _(none)_ | ScienceDirect TDM. Free from [dev.elsevier.com](https://dev.elsevier.com). On its own it only works from the institution's IP range — on a server it is refused (403) unless the token below is set too |
| `ELSEVIER_INSTTOKEN` | no | _(none)_ | institutional token the library obtains from Elsevier. Required for *any* Elsevier full text off the institution's network, including from the server |
| `SPRINGER_API_KEY` | no | _(none)_ | Springer Nature **Open Access** API key, free from [dev.springernature.com](https://dev.springernature.com). Not the Meta API key — that returns metadata only |
| `WILEY_TDM_TOKEN` | no | _(none)_ | Wiley TDM client token, issued from a Wiley Online Library account with the institution's entitlement |

The publisher credentials are normally set **per user**, in Profile → *Publisher
full-text access*: the entitlement follows the person and their institution, not
the server. The env vars above are only a fallback default for users who haven't
set their own — on a shared box you can leave them empty.

Either way they are the last layer of step 6: they run only after the open-access
ladder (Europe PMC → Unpaywall/OpenAlex → landing pages) has failed, and only for
DOIs carrying that publisher's prefix. Leave one unset and its publisher is
simply skipped.

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
lssr.yourdomain.example {
    reverse_proxy 127.0.0.1:8013
}
```

Reload after editing: `sudo systemctl reload caddy`.

`PAPER2MD_URL` must point at a paper2md the app can actually reach. The simplest
and most reliable choice is its public URL (`https://paper2md.yourdomain.example`). A
`http://localhost:8008` only works if paper2md listens on the same host *and*
network namespace — from inside a container localhost is the container itself, so
conversions fail with `Connection refused`. Add `PAPER2MD_API_KEY` (issued from
paper2md's admin page) to lift the upload cap to 50MB.

## 5. Verify

- `https://lssr.yourdomain.example/login` — auth
- `https://lssr.yourdomain.example/` — reviews list
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
