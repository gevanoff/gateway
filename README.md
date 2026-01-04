# gateway

FastAPI “Local AI Gateway” exposing OpenAI-ish endpoints and an internal tool bus.

## Security notes

- All API routes are bearer-protected; treat `GATEWAY_BEARER_TOKEN` like a password.
- The example env file in `app/.env.example` binds to loopback by default. If you bind to `0.0.0.0` for LAN access, use IP allowlisting/firewall rules.
- Operational deployment and configuration guidance lives in `ai-infra/services/gateway/README.md`.

## Images (text-to-image)

The gateway can expose an OpenAI-ish images endpoint:

- `POST /v1/images/generations` (bearer-protected)

Backends:

- `IMAGES_BACKEND=mock` (default): returns a deterministic SVG placeholder.
- `IMAGES_BACKEND=http_a1111`: proxies to an Automatic1111-compatible server (`/sdapi/v1/txt2img`).
- `IMAGES_BACKEND=http_openai_images`: proxies to an OpenAI-style images server (`POST /v1/images/generations`) such as Nexa.

Minimal config (A1111 running locally on the same host as the gateway):

- `IMAGES_BACKEND=http_a1111`
- `IMAGES_HTTP_BASE_URL=http://127.0.0.1:7860`

Notes:

- A1111 must be started with `--api` so `/sdapi/v1/txt2img` is available.
- A1111 typically has no auth; keep it bound to localhost or protect it with firewall/SSH tunnel.

Smoke test (bearer-protected):

- `python tools/verify_images.py --gateway-base-url http://127.0.0.1:8800 --token <token> --also-check-a1111 http://127.0.0.1:7860`

Smoke test (Nexa / OpenAI-style images server):

- `python tools/verify_images.py --also-check-openai-images http://127.0.0.1:18181 --openai-images-model NexaAI/sdxl-turbo`

Nexa-through-gateway config (Nexa running locally on the same host as the gateway):

- `IMAGES_BACKEND=http_openai_images`
- `IMAGES_HTTP_BASE_URL=http://127.0.0.1:18181`
- `IMAGES_OPENAI_MODEL=NexaAI/sdxl-turbo` (so the UI can omit `model`)

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
