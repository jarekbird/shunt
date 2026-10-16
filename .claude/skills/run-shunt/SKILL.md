---
name: run-shunt
description: Build, launch, and drive shunt — the Claude Code LLM gateway (a Rust/axum Anthropic-Messages proxy). Use to run, start, smoke-test, or curl-drive the gateway, exercise /v1/models discovery and /v1/messages proxying, or connect Claude Code to a local shunt instance.
---

# Run shunt

`shunt` is a Rust ([axum](https://github.com/tokio-rs/axum)) HTTP server — a Claude Code LLM gateway. It has **no GUI**: you drive it with `curl`. It listens on `127.0.0.1:3001` by default and serves an Anthropic-Messages surface (`GET /v1/models`, `POST /v1/messages`, `POST /v1/messages/count_tokens`, `HEAD /`). For each mapped `model` id it diverts inference to another provider (OpenAI / Codex / ChatGPT via the OpenAI Responses API); everything else passes through to Anthropic.

**Primary agent path:** run the committed driver [`smoke.sh`](smoke.sh). It builds the binary, starts a local mock upstream without real provider credentials, and checks the core routes. Provider-specific changes also need the checks below.

All paths below are relative to the repo root (the `shunt/` directory).

## Prerequisites

Everything the driver needs is already standard on macOS/Linux dev boxes:

```bash
cargo --version     # Rust stable (built with 1.94); toolchain via rust-toolchain.toml if present
python3 --version   # smoke.sh uses http.server as a stand-in upstream
curl --version
jq --version        # smoke.sh asserts JSON responses with jq
lsof -v
```

On a bare Ubuntu container: `apt-get install -y curl jq lsof python3` and install Rust via `rustup` if `cargo` is missing.

## Build

```bash
BIN="$(cargo build --locked --message-format=json-render-diagnostics | jq -sr '[.[] | select(.reason == "compiler-artifact" and .target.name == "shunt" and (.target.kind | index("bin"))) | .executable | select(. != null)] | last // empty')"
test -x "$BIN"
```

`BIN` resolves Cargo's actual executable path, including `CARGO_TARGET_DIR` and a configured target triple.

## Run (agent path) — the smoke driver

The HTTP checks use loopback and dummy credentials; Cargo may need network access to fetch dependencies. The driver exits non-zero on the first failed assertion and cleans up its own processes.

```bash
.claude/skills/run-shunt/smoke.sh
```

Expected tail:

```
  PASS shunt check -> config ok
  PASS HEAD / -> 200 (server live)
  PASS GET /v1/models returns configured model
  PASS POST /v1/messages proxied to upstream and returned its body
  PASS malformed request -> 400 invalid_request_error
  PASS GET /health reports ok
  PASS GET /protocol describes the anthropic-messages contract
  PASS GET /routes resolves the configured route
  PASS POST /v1/messages/count_tokens proxied to upstream on its own path
All smoke checks passed.
```

What it covers: every route the server registers unconditionally. Config validation (`shunt check`), liveness (`HEAD /`), health (`GET /health`), the gateway contract (`GET /protocol`), model discovery (`GET /v1/models`), the resolved route table (`GET /routes`), both proxy forward paths (`POST /v1/messages` and `POST /v1/messages/count_tokens`, routed to a local mock that stands in for `api.anthropic.com` and asserted by the path it forwarded to), and the routing error path (a body with no `model` field → `400 invalid_request_error`). Routes that only an optional config section registers (the Codex endpoint, the usage surfaces) are out of scope, since the smoke config does not enable them.

`SHUNT_PORT` and `MOCK_PORT` default to `31711` and `31712`. Set either to `0` to bind an ephemeral port instead, which is the conflict-free way to run the driver beside a live gateway.

The driver has its own regression, [`test_smoke.sh`](test_smoke.sh). Run it after editing `smoke.sh`. It proves the driver refuses a port outside `0..65535` by name; refuses a port another listener already holds, where the real process dies at bind and the driver says which one died; and refuses a shunt listening on a port it never requested, which is the case the pid-derived readiness check catches. It then runs the normal path. The stand-in listener answers every assertion the driver makes, so a driver that trusted `$SHUNT_PORT` over the process it started would go green against it and get caught.

### Drive it by hand

Write a config, launch, and curl it yourself:

```bash
BIN="$(cargo build --locked --message-format=json-render-diagnostics | jq -sr '[.[] | select(.reason == "compiler-artifact" and .target.name == "shunt" and (.target.kind | index("bin"))) | .executable | select(. != null)] | last // empty')"
test -x "$BIN"
"$BIN" run --config ./shunt.toml    # or copy shunt.toml.example first
```

Then, against the running server:

```bash
curl -s "http://127.0.0.1:3001/v1/models?limit=1000" | jq .
# => {"data":[{"id":"claude-opus-via-codex","display_name":"Opus (via Codex)"}]}
```

Validate a config without starting the server:

```bash
BIN="$(cargo build --locked --message-format=json-render-diagnostics | jq -sr '[.[] | select(.reason == "compiler-artifact" and .target.name == "shunt" and (.target.kind | index("bin"))) | .executable | select(. != null)] | last // empty')"
test -x "$BIN"
"$BIN" check --config ./shunt.toml   # prints "config ok" or a precise error
```

CLI shape: `shunt run|check [--config <path>]` (also `shunt --check`). Default config path is `./shunt.toml`; `SHUNT_`-prefixed env vars override (with `__` for nesting).

## Verify provider changes

For a protocol adapter change, have the local mock emit that provider's wire format. Point a temporary provider config at it and drive `/v1/messages` in both JSON and `stream: true` modes. Capture the received path, relevant headers and body shape; also probe malformed model/config errors. Keep credentials synthetic, choose isolated ports, and stop the gateway and mock after capture. Redact authorization and cookies before sharing captures.

The standard smoke driver checks proxy forwarding; it does not establish provider-specific streaming or translation correctness. Pair it with the relevant tests under `tests/`, for example:

```bash
cargo test --test responses_translate               # the translation integration suite
```

Use the current test map and gates in `AGENTS.md` and `.github/workflows/ci.yml`; apply the host's test scheduling policy to whole suites. Keep generated captures and large build artifacts on the host's designated data volume.

## Run (human path) — connect Claude Code to a local shunt

For an isolated gateway configured without client authentication, point Claude Code at it using a synthetic client token. For a deployment with client authentication enabled, use its authorized credential flow instead. See the project's current gateway-connect documentation.

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:3001
export ANTHROPIC_AUTH_TOKEN=local-dummy        # isolated fixture without client auth
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1   # opt-in: pull /v1/models into the picker
export ANTHROPIC_CUSTOM_MODEL_OPTION=gpt-5.2-codex    # add a non-claude id to /model (see Gotchas)
claude    # started from the same shell; /status shows the base URL
```

Verify the wiring without opening Claude Code (this is the docs' own check):

```bash
curl -X POST "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d '{"model":"claude-opus-via-codex","max_tokens":1,"messages":[{"role":"user","content":"."}]}'
```

With a default (anthropic-routed) config this forwards to `api.anthropic.com` and needs a real Anthropic key in the header to get a `200`; a mapped model routed to `openai`/`codex` needs that provider's credential instead. For a credential-free run, use `smoke.sh` (mock upstream) rather than this path.

## Gotchas

- **No GUI, no `npm start`.** It's a Rust HTTP server. "Running it" means launch + `curl`. The driver is the smoke script.
- **Model discovery drops non-`claude`/`anthropic` ids.** Claude Code's `/v1/models` importer ignores any `id` not starting with `claude` or `anthropic`, so to route to e.g. `gpt-5.2-codex` you either alias it under a `claude…` discovery id **or** add it via `ANTHROPIC_CUSTOM_MODEL_OPTION` (the primary way). This is a Claude Code constraint, not a shunt bug.
- **Client and upstream authentication are separate.** The smoke fixture uses a local mock and dummy credentials. Check the active auth configuration before applying those assumptions to a running deployment.
- **`shunt check` is strict about provider shape.** `providers.openai.adapter` and `providers.codex.adapter` must be `"responses"`; `openai.auth` must be `api_key`, `codex.auth` must be `chatgpt_oauth` — otherwise `check` fails with a specific error (`src/config.rs`). Partial TOML is fine: figment merges your file over built-in defaults, so you only need to specify what differs.
- **A discovery `[[models]]` entry with no matching `[[routes]]`** logs a `WARN` at startup/`check` but is not fatal.
- **`GET /protocol` is the machine-readable gateway contract.** It is unauthenticated and reports shunt's package version, Anthropic-Messages format, supported endpoints, header handling, attribution behavior, and model-discovery constraints.
- **zsh quoting:** quote URLs containing `?` (globbing) and mind `noclobber` on `>` redirects when driving by hand in this repo's shell.

## Troubleshooting

- **`smoke.sh` reports `shunt exited during startup` or `mock upstream exited during startup`**: if the log says `Address already in use`, inspect the listener with `lsof -nP -iTCP:${MOCK_PORT:-31712} -sTCP:LISTEN` (or the shunt port). Stop it only if it belongs to your run. Prefer `SHUNT_PORT=0 MOCK_PORT=0` to use ephemeral ports beside other work.
- **`502 Bad Gateway` / `api_error: error sending request for url (...)`** — shunt reached routing but the upstream `base_url` was unreachable (wrong host/port, or the mock/provider isn't up). This is the correct error mapping (`src/error.rs`), not a crash. Check the target `base_url`.
- **`400 invalid_request_error: request body must include a JSON model field`** — the request body isn't JSON with a `model` key. Routing happens before forwarding (`src/routing.rs`).
- **`cargo clippy` fails the build** — CI sets `RUSTFLAGS=-D warnings`; warnings are errors. Fix them before a PR (`cargo clippy --all-targets --all-features -- -D warnings`).
