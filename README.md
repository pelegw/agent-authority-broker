# Agent Authority Broker

**Status: under construction (v0.2.0, phase 0 of 8).** Only the skeleton exists:
configuration, database schema, origin/Cloudflare Access security, health
endpoint, compose files, and the secrets bootstrap. Nothing agent-facing works yet.

AI agents shouldn't hold your credentials. The broker holds them instead
(WhatsApp, GitHub, Gmail, Calendar, Drive) and decides, on every call, what
each agent may do: its effective permission is the owner's connected systems
∩ the agent's grant chain ∩ its role. Grants can only narrow, agents can
delegate narrower child keys, anything outside a grant becomes a draft for a
human to approve (console or Telegram), and every decision is recorded in a
hash-chained log with the authority chain that allowed it. Agents use REST
(primary) or MCP.

It is the successor of WA_GW, a WhatsApp-only gateway.

## Layout

- `broker/`: the Python service (FastAPI), package `broker`, CLI `aab`, tests
- `sidecars/whatsapp/`: Go WhatsApp sidecar (whatsmeow)
- `scripts/init_secrets.py`: generates every broker-owned secret into `.env`
- `deploy/`, `edge/`, `docker-compose*.yml`: local and EC2 + Cloudflare deploys

## Run the tests

```bash
cd broker
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # .venv/bin/pip on Linux/macOS
.venv/Scripts/python -m pytest

cd ../sidecars/whatsapp
go build ./... && go test ./...
```

## Run locally

```bash
python scripts/init_secrets.py            # writes .env (mode 0600), prints a checklist
docker compose up -d --build
curl http://127.0.0.1:8080/v1/health      # {"status":"ok","version":"0.2.0"}
```

Public deployment behind Cloudflare: see [deploy/DEPLOY.md](deploy/DEPLOY.md).
