# Backend Policy Enforcement

This document describes the gateway's enforcement of routing, concurrency, and payload policies.

## Overview

The gateway enforces hard limits on:
- **Routing**: Requests only go to backends that explicitly support the capability
- **Concurrency**: Per-backend, per-route inflight limits with fast-fail 429 responses
- **Payload**: Image responses default to URLs; base64 only when explicitly requested

## Backend Classes

Three backend classes are defined:

| Backend Class | Hardware | Capabilities | Concurrency Limits |
|--------------|----------|--------------|-------------------|
| `local_mlx` | macOS Nexa (MLX) | chat, embeddings | chat: 2, embeddings: 2 |
| `gpu_fast` | ai1 (RTX 5060 Ti, 16GB) | chat, embeddings | chat: 4, embeddings: 4 |
| `gpu_heavy` | ada2 (RTX 6000 Ada, 46GB) | **images only** | images: 2 |

Legacy backend names (`ollama`, `mlx`) are mapped to their backend classes for compatibility.

### Image Generation Backend (ada2)

**Important:** Image generation uses **only** `gpu_heavy` (ada2) with InvokeAI/ComfyUI. The previous Nexa/MLX image generation on macOS is **no longer used**.

- **Hardware**: RTX 6000 Ada (46GB VRAM)
- **Software**: InvokeAI (recommended) or ComfyUI
- **Models**: SDXL (1024x1024) or SD 1.5 (512x512)
- **Endpoint**: `http://ada2.local:7860`
- **Health endpoints**: `/healthz` (liveness), `/readyz` (readiness)

For setup instructions, see [IMAGE_BACKEND_SETUP.md](IMAGE_BACKEND_SETUP.md).

## Configuration

### Backend Configuration File

`app/backends_config.yaml` defines all backend classes, capabilities, and policies:

```yaml
backends:
  local_mlx:
    class: local_mlx
    base_url: http://127.0.0.1:10240/v1
    supported_capabilities:
      - chat
      - embeddings
    concurrency_limits:
      chat: 2
      embeddings: 2
    health:
      liveness: /healthz
      readiness: /readyz

  gpu_heavy:
    class: gpu_heavy
    base_url: http://ada2.local:7860
    supported_capabilities:
      - images
    concurrency_limits:
      images: 2
    payload_policy:
      images_format: url
      images_allow_base64: true
```

### Environment Variables

```bash
# Images backend class (for admission control)
IMAGES_BACKEND_CLASS=gpu_heavy

# Images backend implementation (InvokeAI or ComfyUI on ada2)
IMAGES_BACKEND=http_openai_images
IMAGES_HTTP_BASE_URL=http://ada2.local:7860
IMAGES_OPENAI_MODEL=sd-xl-base-1.0  # or sd-v1-5

# Image storage directory (content-addressed)
UI_IMAGE_DIR=/var/lib/gateway/data/ui_images
```

See [ada2-images.env.example](../ai-infra/services/gateway/env/ada2-images.env.example) for a complete configuration.

## Enforcement Behavior

### 1. Capability Gating

Requests are rejected if the target backend doesn't support the capability:

```bash
# Chat request to image-only backend
curl -X POST http://localhost:8800/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"model": "gpu_heavy", "messages": [...]}'

# Response: 400 Bad Request
{
  "error": "capability_not_supported",
  "backend_class": "gpu_heavy",
  "route_kind": "chat",
  "message": "Backend gpu_heavy does not support chat",
  "supported_capabilities": ["images"]
}
```

### 2. Admission Control

Concurrency limits are enforced per (backend_class, route_kind):

```bash
# When gpu_heavy.images is at capacity (2 inflight)
curl -X POST http://localhost:8800/v1/images/generations \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"prompt": "..."}'

# Response: 429 Too Many Requests
# Retry-After: 5
{
  "error": "backend_overloaded",
  "backend_class": "gpu_heavy",
  "route_kind": "images",
  "message": "Backend gpu_heavy is at capacity for images requests"
}
```

**Key characteristics:**
- No queueing: requests fail immediately when limit is reached
- Independent limits: different routes and backends don't interfere
- Semaphore-based: slots are released after request completion

### 3. Payload Policy (Images)

Images default to URL responses; base64 only when explicitly requested:

```bash
# Default: returns URLs
curl -X POST http://localhost:8800/v1/images/generations \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"prompt": "a cat", "n": 1}'

{
  "data": [
    {"url": "/ui/images/1234567890_abcdef123456.png"}
  ]
}

# Explicit base64 request
curl -X POST http://localhost:8800/v1/images/generations \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"prompt": "a cat", "response_format": "b64_json"}'

{
  "data": [
    {"b64_json": "iVBORw0KGgo..."}
  ]
}
```

**Storage:**
- Images are stored in `UI_IMAGE_DIR` (default: `/var/lib/gateway/data/ui_images`)
- Filenames are content-addressed: `{timestamp}_{sha256_prefix}.{ext}`
- URLs are served via `/ui/images/{filename}` (subject to `UI_IP_ALLOWLIST`)

### 4. Health and Readiness

Each backend must expose:
- `/healthz` - liveness check
- `/readyz` - readiness check (model loaded, ready to serve)

The gateway:
- Checks all backends every 30 seconds
- Refuses routing to backends that fail readiness
- Returns 503 with `Retry-After: 30` when backend is not ready

```bash
# When backend is not ready
curl -X POST http://localhost:8800/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"model": "local_mlx", "messages": [...]}'

# Response: 503 Service Unavailable
# Retry-After: 30
{
  "error": "backend_not_ready",
  "backend_class": "local_mlx",
  "message": "Backend local_mlx is not ready to accept requests",
  "health_error": "readiness check failed: Connection refused"
}
```

## Monitoring

### Gateway Status Endpoint

```bash
curl http://localhost:8800/v1/gateway/status \
  -H "Authorization: Bearer $TOKEN"
```

Returns:
```json
{
  "admission_control": {
    "gpu_heavy.images": {
      "limit": 2,
      "available": 1,
      "inflight": 1
    },
    "gpu_fast.chat": {
      "limit": 4,
      "available": 4,
      "inflight": 0
    }
  },
  "backend_health": {
    "gpu_heavy": {
      "healthy": true,
      "ready": true,
      "last_check": 1736697234.5,
      "error": null
    },
    "local_mlx": {
      "healthy": false,
      "ready": false,
      "last_check": 1736697234.2,
      "error": "liveness check failed: Connection refused"
    }
  }
}
```

### Debugging Headers

All responses include:
- `X-Backend-Used`: Backend name (ollama, mlx)
- `X-Model-Used`: Actual model name sent to upstream
- `X-Router-Reason`: Why this backend/model was selected

## Implementation Details

### Modules

- `app/backends.py` - Backend registry, admission controller, capability gating
- `app/backends_config.yaml` - Backend definitions (single source of truth)
- `app/health_checker.py` - Background health/readiness checking
- `app/image_storage.py` - Image storage and URL generation for payload policy
- `app/images_backend.py` - Image generation with response_format enforcement
- `app/openai_routes.py` - Chat completions with admission control
- `app/images_routes.py` - Image generation endpoint

### Admission Control Flow

```
Request arrives
    ↓
Check capability (backend supports route_kind)
    ↓
Check backend health/readiness
    ↓
Try to acquire semaphore slot
    ↓
    ├─ Success → Process request → Release slot
    └─ Failure → Return 429 immediately (no queueing)
```

### Health Check Flow

```
Background task (30s interval)
    ↓
For each backend:
    ├─ GET {base_url}/healthz
    ├─ GET {base_url}/readyz (if healthy)
    └─ Update status cache
    
Before routing:
    ├─ Check status cache
    └─ Return 503 if not ready
```

## Testing

Run policy enforcement tests:

```bash
cd gateway
pytest tests/test_policy_enforcement.py -v
```

Tests cover:
- Capability gating (chat on image backend fails, etc.)
- Admission control (429 on overload, release frees slots)
- Payload policy (URL default, base64 when requested)
- Health checks (unhealthy backends rejected)
- Deterministic routing (no auto-fallback)

## Migration from Legacy System

The legacy routing system (`app/router.py`, `app/routing_legacy.py`) still exists for backward compatibility but is now gated by the enforcement layer:

1. Router decides which backend/model to use (unchanged)
2. Enforcement layer verifies capability and capacity
3. Request proceeds only if both checks pass

To disable enforcement (not recommended):
- Remove backends_config.yaml → falls back to minimal defaults
- Enforcement still applies, but with legacy backend names

## Non-Goals

These are explicitly NOT implemented:

- ❌ Automatic fallback to other backends
- ❌ Load balancing across multiple instances
- ❌ Request queueing (we fail fast with 429)
- ❌ Dynamic model selection based on content
- ❌ Automatic retry logic (client's responsibility)

## Success Criteria

✅ Excess image requests to gpu_heavy return 429  
✅ Chat requests never hit gpu_heavy (capability gating)  
✅ Image responses return URLs by default  
✅ Overload fails fast and visibly (no silent degradation)  
✅ Behavior is deterministic and testable  
✅ Gateway behavior exactly matches declared policy  
