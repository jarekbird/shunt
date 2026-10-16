---
name: run-claude-gateway-ref
description: Capture the reference Claude gateway's device-login, managed-settings and telemetry protocol for comparison with Shunt, using isolated local Dex and PostgreSQL fixtures.
---

# Run the reference Claude apps gateway (protocol probe)

Spins up the **real** `claude gateway` (built into the `claude` binary) with a
local Dex IdP and throwaway Postgres, then drives the full device-flow login
**non-interactively with Python HTTP requests** — no browser needed. Use it to (re)capture the
wire contract shunt mirrors: exact token JSON, `/managed/settings` body, env
push, OTLP relay behavior.

Everything is driven by `.claude/skills/run-claude-gateway-ref/driver.sh`
(paths relative to repo root). The `/device` browser leg uses same-origin CSRF
headers and separate in-memory gateway and Dex cookie jars.

## Prerequisites

- Docker running; the fixture uses `postgres:16-alpine` and `ghcr.io/dexidp/dex:v2.44.0`.
- `claude`, Python 3.9+, and `lsof` on macOS/Linux.
- The reference was exercised with Claude gateway 2.1.211 in July 2026. Confirm
  the installed CLI still provides `claude gateway`; these historical captures
  do not establish compatibility with newer versions.

## Run

```bash
# On this server, put artifacts on the experiment volume.
.claude/skills/run-claude-gateway-ref/driver.sh run \
  --output-root /data/spark-bid/experiments/20261016-gateway-reference
```

On another host, use its private artifact directory. `GWREF_WORK` may supply the
output root instead. Each invocation creates a fresh owner-only `gwref-*` run
folder beneath it; it never reuses a prior capture directory. The default
whole-run deadline is 180 seconds; `--timeout` accepts 30–1800 seconds, including
image pulls and startup. Missing dependencies fail before resources are created.

One foreground command starts unique, labelled PostgreSQL and Dex containers,
a loopback telemetry sink, and the reference gateway; logs in as the synthetic
Dex user; captures protocol/settings/models, refresh and bootstrap responses;
and checks byte-identical telemetry delivery. It selects ephemeral loopback
ports and renders the templates into the private run directory. The templates
in this skill are not standalone configs.

Cleanup runs on success, errors, timeout, Ctrl-C and SIGTERM: it reaps its own
child and removes only containers whose ownership label matches this invocation.
A competing gateway listener cannot satisfy readiness. Cleanup failure returns
nonzero and names the resource needing inspection. No saved PID is later used
as authority to kill a process. SIGKILL or a host crash can still leave a labelled
container; inspect that run's ownership before removing it.

This replaces the old detached `up`, `sink-start`, `login`, `probe`, `status` and
`down` commands. Captures remain for inspection after all runtime resources close.
Do not run an old `down` against unrelated processes or containers.

The printed private run directory contains `capture/`, `gateway.log`, rendered
configs, `reference-version.txt`, `run.json` and command diagnostics. Directories are mode 700; generated files are
mode 600. Token responses and cookies are private data: do not print or commit
them. Cookies remain in memory and cookie headers are omitted from captures;
passwords, bearer/refresh tokens and HTTP request
bodies are not passed as process arguments. The gateway gets an isolated
`CLAUDE_CONFIG_DIR`; this probe does not write the user's CLI login state.

Login identity: `dev@example.com` / `password` (synthetic Dex user, no groups).
PostgreSQL and the gateway signing secret are generated per run. No inference
request is made; the upstream API key is deliberately fake.

## Offline regression checks

```bash
python3 -B .claude/skills/run-claude-gateway-ref/test_driver.py
```

These use local fake services and subprocesses; they do not start Docker,
Claude, or a real provider session. Live reference compatibility must be checked
separately when a protocol comparison is needed.

## Verified wire facts (2026-07-16, gateway 2.1.211)

Captured live; these fill the gaps `docs/gateway-protocol.md` marks unspecified:

- Token response (device grant AND refresh grant, same shape):
  `{"access_token":"<HS256 JWT>","refresh_token":"<opaque>","token_type":"Bearer","expires_in":3600}`.
  JWT claims: `sub` (IdP subject), `email`, `name`, `aud`=oidc client_id,
  `iss`=public_url. Refresh **rotates** the refresh token. Gateway refresh =
  refresh against the IdP (3500ms budget) + re-mint; IdP failure → `401 {"error":"invalid_grant"}`.
- `/managed/settings` body: `{"uuid":"sha256:…","checksum":"sha256:…","settings":{<managed-settings.json>}}`,
  `ETag` = checksum, `If-None-Match` → 304. With `telemetry.forward_to` set it
  pushes **six** env vars (docs say five): `CLAUDE_CODE_ENABLE_TELEMETRY=1`,
  `OTEL_{METRICS,LOGS,TRACES}_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_ENDPOINT=<public_url>`,
  `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, merged with policy `env`.
- Discovery includes undocumented `gateway_protocol_version: 1`,
  `token_endpoint_auth_methods_supported: ["none"]`, `scopes_supported`.
- IdP callback is `{public_url}/oauth/callback` (register in the IdP client).
- Telemetry relay is **verbatim** — body forwarded byte-identical, no identity
  attributes stamped by the gateway; `POST /v1/{metrics,logs,traces}` returns
  200 even when the destination is down. Boot log says `signals enabled: metrics`
  for a bare `forward_to` entry.
- `/v1/models` is filtered by the caller's policy `availableModels`; full
  Anthropic shape (`type`, `has_more`, `first_id`, `last_id`).
- Error envelope adds top-level `request_id` alongside `{"type":"error","error":{…}}`.
- `GET /user/bootstrap` (new, Claude Desktop): 404 `not_found_error` unless a
  policy carries a `desktop:` block.

## Gotchas

- **`POST /device` is CSRF-guarded**: without `Origin` + `Referer` +
  `Sec-Fetch-Site: same-origin` it returns 200 with "request came from another
  site and was blocked" instead of the 302 to the IdP. The audit log shows
  `device.verify result=csrf_rejected`.
- **The `gw_dev` cookie from the `/device` POST must be replayed on
  `/oauth/callback`** or the device grant is never approved.
- **The July 2026 Dex `latest` image hung on refresh** (memory AND sqlite3
  storage) → gateway `session.refresh` fails with "timed out after 3500ms" and
  the client gets `invalid_grant`. Pin `v2.44.0`.
- Loopback OIDC issuer is rejected by the SSRF guard unless
  `CLAUDE_GATEWAY_ALLOW_LOOPBACK=1` (driver sets it). `http://` public_url is
  accepted on loopback.
- Don't let `curl -w` write into a JSON capture file — it corrupts the JSON and
  the next `python3 json.load` fails with "Extra data".
- The dummy `upstreams` API key boots fine; only `/v1/messages` inference would
  fail. Postgres is required at boot (gateway runs 6 migrations).

## Troubleshooting

- A refresh failure with "timed out after 3500ms" matched an unpinned Dex image
  in the July investigation. Keep the recorded version for reproduction; inspect
  the private log before attributing a new failure to the same cause.
- A failed device redirect can mean missing CSRF headers or a changed reference
  flow. Inspect the version and private captures; the driver does not follow
  redirects outside this run's gateway and Dex origins.
- Boot fails on config: inspect `gateway.log` in the printed run directory.
  Keep historical findings dated when updating templates for a newer reference.
