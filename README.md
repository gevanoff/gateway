# gateway

FastAPI “Local AI Gateway” exposing OpenAI-ish endpoints and an internal tool bus.

## Security notes

- All API routes are bearer-protected; treat `GATEWAY_BEARER_TOKEN` like a password.
- The example env file in `app/.env.example` binds to loopback by default. If you bind to `0.0.0.0` for LAN access, use IP allowlisting/firewall rules.
- Operational deployment and configuration guidance lives in `ai-infra/services/gateway/README.md`.

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
IMAGES_HTTP_BASE_URL=http://ada2.local:7860
IMAGES_OPENAI_MODEL=sd-xl-base-1.0
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
python tools/verify_images.py --gateway-base-url http://127.0.0.1:8800 --token <token> --also-check-a1111 http://127.0.0.1:7860
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

- Check an already-running gateway: `python tools/verify_gateway.py --base-url http://127.0.0.1:8800 --token <token>`
- Require a healthy backend (otherwise backend-dependent checks are skipped): `--require-backend`
- Appliance smoke-test mode (implies backend required): `--appliance`
- Skip pytest (HTTP checks only): `--skip-pytest`

## Agent runtime v1

Deterministic single-process agent loop with replayable transcripts.

Endpoints (bearer-protected):

- `POST /v1/agent/run`
- `GET /v1/agent/replay/{run_id}`

## Eval harness

On-demand/nightly eval runner (stdlib-only):

- `python tools/run_evals.py`

## Appliance manifest

Freeze a release manifest (stdlib-only):

- `python tools/freeze_release.py`
