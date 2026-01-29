# gateway

FastAPI “Local AI Gateway” exposing OpenAI-ish endpoints and an internal tool bus.

## Security notes

- All API routes are bearer-protected; treat `GATEWAY_BEARER_TOKEN` like a password.
- The example env file in `app/.env.example` binds to loopback by default. If you bind to `0.0.0.0` for LAN access, use IP allowlisting/firewall rules.
- Operational deployment and configuration guidance lives in `ai-infra/services/gateway/README.md`.

### TLS / HTTPS

- The gateway can be configured to serve HTTPS directly by setting `GATEWAY_TLS_CERT_PATH`
	and `GATEWAY_TLS_KEY_PATH` to point at a PEM cert and key. This is primarily useful
	for simple local testing; production deployments should prefer a reverse proxy
	(nginx, Caddy) which provides richer TLS management.
- Outbound connections to model backends honor TLS settings:
	- `BACKEND_VERIFY_TLS` (default true) controls verification.
	- `BACKEND_CA_BUNDLE` can point at a custom CA bundle file for upstreams.
	- `BACKEND_CLIENT_CERT` can be a single PEM path or two paths separated by a comma
		(`cert.pem,key.pem`) to enable client cert auth to upstreams.

## Images (text-to-image)

The gateway exposes an OpenAI-ish images endpoint:

- `POST /v1/images/generations` (bearer-protected)

### Backends

- `IMAGES_BACKEND=mock` (default): returns a deterministic SVG placeholder.
- `IMAGES_BACKEND=http_a1111`: proxies to an Automatic1111-compatible server (`/sdapi/v1/txt2img`).
- `IMAGES_BACKEND=http_openai_images`: proxies to an OpenAI-style images server (`POST /v1/images/generations`) such as InvokeAI or ComfyUI.

### Production Setup (ada2 with InvokeAI)

**Recommended configuration** routes all image generation to ada2 (RTX 6000 Ada, 46GB VRAM) running InvokeAI with SDXL models:

```bash
IMAGES_BACKEND=http_openai_images
IMAGES_BACKEND_CLASS=gpu_heavy
IMAGES_HTTP_BASE_URL=http://ada2:7860
# Optional: if omitted, upstream may use its own default (e.g. InvokeAI shim)
# IMAGES_OPENAI_MODEL=sd-xl-base-1.0
UI_IMAGE_DIR=/var/lib/gateway/data/ui_images
```

**Policy enforcement:**
- Only `gpu_heavy` (ada2) supports images capability
- Concurrency limit: 2 simultaneous requests
- Images default to URL responses (content-addressed storage)
- No fallback to other backends (fail fast with 429)

See [IMAGE_BACKEND_SETUP.md](IMAGE_BACKEND_SETUP.md) for ada2 setup instructions and [NEXA_MIGRATION.md](NEXA_MIGRATION.md) for migration from Nexa/MLX.

### Legacy: A1111 (local)

Minimal config (A1111 running locally on the same host as the gateway):

```bash
IMAGES_BACKEND=http_a1111
IMAGES_HTTP_BASE_URL=http://127.0.0.1:7860
```

Notes:

- A1111 must be started with `--api` so `/sdapi/v1/txt2img` is available.
- A1111 typically has no auth; keep it bound to localhost or protect it with firewall/SSH tunnel.

### Testing

Smoke test (bearer-protected):

```bash
python tools/verify_images.py --gateway-base-url https://127.0.0.1:8800 --token <token> --also-check-a1111 http://127.0.0.1:7860 --insecure
```

Or run the full verifier (includes many gateway checks), with images enabled:

```bash
python tools/verify_gateway.py --check-images
```

Integration tests:

```bash
pytest tests/test_ada2_images.py -v
```

## Tool bus (high level)

- List tools: `GET /v1/tools`
- Execute tool: `POST /v1/tools/{name}`

The tool bus is designed to be deterministic and replayable:

- Each invocation returns `replay_id` and a deterministic `request_hash`.
- Tool logs can be written as NDJSON, per-invocation files, or both (see `TOOLS_LOG_MODE`, `TOOLS_LOG_PATH`, `TOOLS_LOG_DIR`).

Operational deployment docs live in `ai-infra/services/gateway/README.md`.

## Comprehensive verification (single command)

Run the full contract tests + a live HTTP smoke suite:

- `python tools/verify_gateway.py`

Options:

- Check an already-running gateway: `python tools/verify_gateway.py --base-url https://127.0.0.1:8800 --token <token> --insecure`
- Require a healthy backend (otherwise backend-dependent checks are skipped): `--require-backend`
- Appliance smoke-test mode (implies backend required): `--appliance`
- Skip pytest (HTTP checks only): `--skip-pytest`

## Agent runtime v1

Deterministic single-process agent loop with replayable transcripts.

Agent specs can be provided via a JSON file (set `AGENT_SPECS_PATH` in env).
An example is included at `env/agent_specs.json.example` and allows creating a `music` agent
that explicitly allowlists the `heartmula_generate` tool.

UI: the gateway exposes a tokenless, IP-restricted music playground at `/ui/music` (gated by `UI_IP_ALLOWLIST`) that calls `/ui/api/music` and proxies audio via `/ui/heartmula/audio/{filename}`.

## User accounts

Gateway UI endpoints can optionally require per-user login, with user settings and chat histories stored in SQLite (`USER_DB_PATH`). Account creation and password resets are handled by a local script:

- Create user: `python tools/manage_users.py create <username>`
- Reset password: `python tools/manage_users.py reset <username>`
- Disable/enable: `python tools/manage_users.py disable <username>` / `python tools/manage_users.py enable <username>`

When `USER_AUTH_ENABLED=true`, UI API calls require a session created via `POST /ui/api/auth/login` and will store user-specific settings (`/ui/api/user/settings`) and conversation history (`/ui/api/conversations/*`) in the backing database.

Note: the `users.sqlite` DB under `/var/lib/gateway/data` is owned by the `gateway` service user. To create users that the running gateway process can access, run the `manage_users.py` command as the `gateway` user (for example: `sudo -u gateway /var/lib/gateway/env/bin/python -m tools.manage_users create <username>`). Running the command as another account may produce "attempt to write a readonly database" errors.

Endpoints (bearer-protected):

- `POST /v1/agent/run`
- `GET /v1/agent/replay/{run_id}`

## Eval harness

On-demand/nightly eval runner (stdlib-only):

- `python tools/run_evals.py`

## Appliance manifest

Freeze a release manifest (stdlib-only):

- `python tools/freeze_release.py`
