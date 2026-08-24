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
| `PUBLIC_URL` | for `/mcp` | _(none)_ | the app's public origin, e.g. `https://lssr.yourdomain.example`. The MCP transport checks the `Host` header against DNS rebinding, so behind a proxy the public host must be listed here or **every** MCP request is refused with *Invalid Host header*. It is also what the profile page prints as the endpoint to connect to |
| `AUTH_MODE` | no | `local` | `local` = own login. `gateway` = trust an SSO gate in front (see the last section) |
| `BORANT_TRUSTED_PROXY` | in `gateway` | `127.0.0.1` | the address the proxy connects from; headers from anywhere else are ignored |
| `BORANT_LOGOUT_URL` | no | `https://id.borant.eu/logout` | where "log out" goes in `gateway` mode |

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
cp .env.example .env                              # edit JWT_SECRET / FERNET_KEY / PAPER2MD_URL
cp docker-compose.yml.example docker-compose.yml  # edit only if paper2md is a sibling container
docker compose up -d --build
docker compose exec app python seed_admin.py you@example.com "Your Name" "a-password"
```

**`docker-compose.yml` is not in the repo** — only `docker-compose.yml.example` is.
The compose file carries host-specific wiring (whether paper2md happens to run as
a sibling container, and therefore whether a shared docker network exists), which
would break a standalone deploy if it were committed, and would be lost on the
next `git pull` if it were edited in place. Same split as `.env`.

The example maps the app to `127.0.0.1:8013` and mounts `./data` for the SQLite DB
and fetched PDFs (`data/fulltext/`). `mem_limit: 1000m` caps memory on a small box.

### SQLite in WAL mode

Worth doing once, before the first long job:

```bash
docker compose down
docker run --rm -v "$PWD/data:/app/data" lssr-app python -c "import sqlite3; c=sqlite3.connect('/app/data/lssr.db'); print(c.execute('PRAGMA journal_mode=WAL').fetchone())"
docker compose up -d
```

The default (`delete`) makes readers and writers block each other, so browsing the
UI while a harvest or a screening run is writing can fail with *database is
locked*. WAL lets them coexist. It needs exclusive access for an instant, hence
the stop.

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

`PAPER2MD_URL` must point at a paper2md the app can actually reach. Add
`PAPER2MD_API_KEY` (issued from paper2md's admin page) to lift the upload cap to
50MB.

**If paper2md runs as a container on the same host, do not use its public URL.**
Every conversion would leave the machine, cross the proxy and come back, and an
edge timeout — Cloudflare's is about 100 seconds — kills precisely the large PDFs
worth converting, with a 524 the app can do nothing about. Conversion time tracks
the document's structure, not its size: a 0.4 MB paper took 168s here and a 2.6 MB
one took 32s, so there is no threshold to stay under.

`http://localhost:8008` is not the answer either: inside a container localhost is
the container itself. Nor is `host.docker.internal`, if paper2md is bound to
`127.0.0.1` on the host — traffic from a container arrives on the bridge address,
which a loopback-bound service refuses.

Share a docker network instead:

```bash
docker network create paper2md-shared
```

then on **both** services, in each compose file:

```yaml
    networks: [default, paper2md-shared]

networks:
  paper2md-shared:
    external: true
```

and set `PAPER2MD_URL=http://paper2md:8000` (paper2md's *internal* port, not the
published one). Its loopback binding stays, so the proxy and anything on the host
keep working. Verify from inside the app container: the response should carry
paper2md's own `server: uvicorn` and no proxy headers.

## 5. Verify

- `https://lssr.yourdomain.example/login` — auth
- `https://lssr.yourdomain.example/` — reviews list
- each user sets their Anthropic API key under **Profile** before any LLM step

The MCP surface, with a key minted under **Profile → MCP keys**:

```bash
curl -s -X POST https://lssr.yourdomain.example/mcp \
  -H "X-API-Key: lssr_…" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 400
```

A `401 missing or invalid API key` means the key is wrong or revoked; *Invalid
Host header* means `PUBLIC_URL` does not match the host the request arrived on.
Without a key the endpoint answers 401 and nothing else — it is not browsable.

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

## Behind an SSO gate (`AUTH_MODE=gateway`)

Optional, and off unless you switch it on. In `gateway` LSSR stops checking
passwords and reads the identity headers set by a `forward_auth` gate in front
of it. `/login` redirects home, and "log out" goes to `BORANT_LOGOUT_URL` so the
central session dies with the local cookie.

**The public share pages are untouched.** `/r/{token}` is how someone outside
the project reads a review, and it needs no account in either mode. That is also
what makes this app comfortable to gate: the people who would be annoyed by a
sign-in wall never meet it.

**`local` stays the default.** An app that believes `X-Borant-Sub` with nothing
in front of it lets in anyone who sends that header.

**And `/mcp` stays outside the gate**, with its own per-user key. A model client
has no browser and no cookie, so putting it behind a domain session would mean
switching it off. `/mcp/*` covers the `/mcp/k/{key}` variant too. Nothing is
loosened by this: with no valid key that path answers 401 and stops, and a valid
key reaches only what its owner reaches.

```
lssr.borant.eu {
    @pubbliche path / /r/* /health /static/* /mcp /mcp/* /login /logout
    handle @pubbliche {
        import noforge
        import nocookie
        reverse_proxy localhost:8013
    }
    handle {
        import borantid
        reverse_proxy localhost:8013
    }
}
```

`/login` and `/logout` stay outside because the app already handles them itself
in this mode; gating them would answer a sign-in attempt with a redirect to a
different sign-in.

**Link the existing reviewers before switching on, and read the report.**

```bash
docker exec lssr python map_borant.py --map you@example.org=01ABC…
docker exec lssr python map_borant.py --report
```

This matters more here than in most apps. A reviewer's id is the `reviewer_id`
on every screening decision and extraction they have made, so an unlinked person
does not merely land on an empty screen — they land on a *different id*, their
decisions stay attached to a row nobody reaches, and blind double-screening
starts treating the two rows as two reviewers. The report prints the work
attached to each account for exactly that reason.

**Long forms and session expiry.** This is the app with the most page-level form
POSTs in the estate, so it is the one where a session expiring mid-form costs
the most: a browser turns a `302` on a `POST` into a `GET` and drops the body.
The gate's sliding session (renewed on every pass, so a session in active use
does not expire) is the mitigation that matters; a heartbeat from the longest
forms is the cheap next one if it ever bites in practice. Worth knowing that
this is not a problem the gate introduces — a 7-day JWT with a fixed expiry, as
used in `local` mode, fails exactly the same way and cannot be renewed at all.

`BORANT_TRUSTED_PROXY` is the second lock and the setting people get wrong.
Under Docker the proxy runs on the host, so the container sees a bridge gateway
and not `127.0.0.1`. Read it off reality:

```bash
curl -s -o /dev/null http://127.0.0.1:8013/health && docker logs lssr 2>&1 | tail -1
```

Rollback, two lines and no data migration:

```bash
sed -i 's/^AUTH_MODE=gateway/AUTH_MODE=local/' .env
docker compose up -d
```

## The landing, the home, and the role hint

Same shape in every app of the perimeter, so there is nothing to remember per
tool.

**`/` is a public showcase and never asks who is reading it.** Not laziness: on
the public branch of the reverse proxy the `X-Borant-*` headers are stripped by
construction, so a branch on the user is always false behind the gate and
sometimes true without one — the same page with two behaviours. By not asking,
the page is identical in both modes and one button covers all four cases:
gated or standalone, already signed in or not. It also shows no internal
counts: anyone can read it.

**The app lives at `/app`**, which is gated, and the showcase's button
points there — not at `/login`, which on a page that can never recognise anyone
would close a loop with no way in, and not at the gate's own URL, which would
work and would wire Borant ID into an app that must keep running without it.

**The role hint is honoured, and its vocabulary is one word: `admin`.** That
flag opens user management and not the product, which anyone holding a grant
already has. A profile created as an admin this way is logged loudly. An
unrecognised hint grants nothing.

**A page that needs an identity fails closed.** In `gateway` an unauthenticated
request does *not* redirect to `/login` — the app switches that route off in
this mode and sends it back, so the two would bounce forever. Production never
shows it because the gate intercepts first, but a wrong proxy matcher would
produce a spin instead of an error, and a loop is far harder to diagnose than a
status code. The answer is a 503 naming what the operator should check, because
a request arriving with no identity means the gate did not run.
