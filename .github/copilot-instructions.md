# Copilot instructions (gateway)

## Repo role and relationships
- `gateway/` is the standalone FastAPI Local AI Gateway implementation and contract-test source.
- `nexus/` is the primary operations/deploy source of truth; align operational guidance there.
- `ai-infra/` is historical reference for legacy host/runtime patterns.

## Scope boundaries
- The development host is never used as a deployment host.
- Development-host helper scripts may exist, but are not the default production/test deploy path.
- Changes here should preserve API and contract behavior unless explicitly requested.

## Commit and distribution policy
- If updates require rollout to test/production hosts, commit and push to `origin`.
- Prefer completing practical follow-on updates instead of leaving avoidable manual steps.

## Cross-platform script guidance (Linux + macOS)
- Any script that may run on Linux and macOS must auto-handle both OS families.
- Use OS detection and compatible alternatives (`sed -i` differences, `stat` differences, etc.).
- Keep commands portable and fail with clear, actionable messages.

## Reuse and consistency rules
- Before editing, search for similar route/handler/test patterns and reuse existing approaches.
- Apply equivalent fixes across duplicated logic in related modules when appropriate.
- Use structured maintenance markers where needed to tie related code paths, e.g. `SYNC-CHECK(<topic>)`.
- Extract reusable helpers when repeated logic impairs consistency.

## Security and hardening
- After updates, run a focused security sanity pass (auth checks, input validation, tool execution boundaries, secret handling).
- Apply basic hardening updates that are in scope.

## Response/hand-off expectations
- End with next practical steps.
- Provide runnable commands as one-liners when possible.
- If needed, split multi-step flows into separate single-command blocks.

## Key references
- API/behavior: `gateway/app/openai_routes.py`, `gateway/app/agent_routes.py`, `gateway/app/tools_bus.py`.
- Contracts: `gateway/tests/test_streaming_contract.py`, `gateway/tests/test_tools_bus_contract.py`.
- Nexus operational companion: `nexus/deploy/`, `nexus/docs/`.
