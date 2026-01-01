# gateway

FastAPI “Local AI Gateway” exposing OpenAI-ish endpoints and an internal tool bus.

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
- Skip pytest (HTTP checks only): `--skip-pytest`
