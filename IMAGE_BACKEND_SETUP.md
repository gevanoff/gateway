# Image Generation Backend Setup for ada2

## Overview

This document describes how to set up a reliable image generation backend on ada2 (RTX 6000 Ada, 46GB VRAM) to replace Nexa/MLX.

## Backend Selection: ComfyUI vs InvokeAI

### Recommended: ComfyUI

**Pros:**
- Node-based workflow is more flexible for custom pipelines
- Better VRAM management for large models
- Active community with many custom nodes
- Can run SDXL efficiently on 46GB
- Good API for programmatic access
- Excellent for batch processing

**Cons:**
- More complex initial setup
- API requires understanding workflow JSON
- Less "batteries included" than InvokeAI

### Alternative: InvokeAI

**Pros:**
- More user-friendly web UI
- Built-in OpenAI-compatible API endpoint
- Easier initial setup
- Good model management
- Simpler REST API

**Cons:**
- Less flexible than ComfyUI for custom workflows
- Higher VRAM usage for same quality

**Recommendation:** Start with **InvokeAI** for faster deployment and simpler integration. Switch to ComfyUI later if you need advanced workflows.

## InvokeAI Setup on ada2 (Ubuntu + CUDA)

### 1. Install Prerequisites

```bash
# On ada2 (Ubuntu)
sudo apt update
sudo apt install -y python3.10 python3.10-venv python3-pip git

# Verify CUDA is available
nvidia-smi

# Create dedicated user
sudo useradd -r -m -s /bin/bash invokeai
sudo mkdir -p /var/lib/invokeai
sudo chown invokeai:invokeai /var/lib/invokeai
```

### 2. Install InvokeAI

```bash
# Switch to invokeai user
sudo -u invokeai -i

# Create environment
cd /var/lib/invokeai
python3.10 -m venv venv
source venv/bin/activate

# Install InvokeAI
pip install InvokeAI[xformers] --use-pep517 --extra-index-url https://download.pytorch.org/whl/cu121

# Configure (interactive)
invokeai-configure --yes

# Or non-interactive with defaults
invokeai-configure --yes --default_only
```

### 3. Download Models

```bash
# Download SDXL base (recommended for quality)
invokeai-model-install --add stabilityai/stable-diffusion-xl-base-1.0

# Or SD 1.5 (faster, less VRAM)
invokeai-model-install --add runwayml/stable-diffusion-v1-5
```

### 4. Configure for API Access

Create `/var/lib/invokeai/invokeai.yaml`:

```yaml
InvokeAI:
  Web Server:
    host: 0.0.0.0
    port: 7860
    allow_origins: ["https://ada2:8800", "https://127.0.0.1:8800"]
    
  Generation:
    sequential_guidance: false
    precision: float16
    max_cache_size: 6.0
    
  Model Cache:
    ram: 16.0
    vram: 40.0
    
  Paths:
    models_dir: models
    outputs_dir: outputs
```

### 5. Create systemd Service

Create `/etc/systemd/system/invokeai.service`:

```ini
[Unit]
Description=InvokeAI Image Generation Service
After=network.target

[Service]
Type=simple
User=invokeai
Group=invokeai
WorkingDirectory=/var/lib/invokeai
Environment="PATH=/var/lib/invokeai/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/var/lib/invokeai/venv/bin/invokeai-web --host 0.0.0.0 --port 7860
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable invokeai
sudo systemctl start invokeai
```

### 6. Add Health Endpoints

InvokeAI doesn't provide `/healthz` by default, and its native API is not OpenAI Images-compatible.

Recommended: run the InvokeAI OpenAI Images shim on the same host and add a simple nginx proxy that exposes:
- `/healthz` (liveness)
- `/readyz` (readiness; proxies to the shim)
- `POST /v1/images/generations` (OpenAI Images; proxies to the shim)

Create `/etc/nginx/sites-available/invokeai`:

```nginx
server {
    listen 7860;
    server_name ada2;

    # Health endpoints for gateway
    location /healthz {
        access_log off;
        return 200 "ok\n";
        add_header Content-Type text/plain;
    }

    location /readyz {
        access_log off;
      # Readiness is implemented by the shim (it checks InvokeAI + config)
      proxy_pass http://127.0.0.1:9091/readyz;
    }

    # OpenAI Images-compatible endpoint for the gateway
    location = /v1/images/generations {
      proxy_pass http://127.0.0.1:9091/v1/images/generations;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;

      # Increase timeouts for image generation
      proxy_connect_timeout 300s;
      proxy_send_timeout 300s;
      proxy_read_timeout 300s;
    }

    # Proxy to InvokeAI
    location / {
        proxy_pass http://127.0.0.1:9090;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Increase timeouts for image generation
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }
}
```

Update InvokeAI to run on port 9090, then:

```bash
sudo ln -s /etc/nginx/sites-available/invokeai /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## Gateway Configuration

### Update Gateway Config

Set in gateway environment (`.env` or environment variables):

```bash
# Point to ada2 InvokeAI
IMAGES_BACKEND=http_openai_images
IMAGES_BACKEND_CLASS=gpu_heavy
IMAGES_HTTP_BASE_URL=http://ada2:7860

# Optional: some OpenAI-ish image servers require a model, but the InvokeAI shim can use
# the default model embedded in its graph template.
# IMAGES_OPENAI_MODEL=sd-xl-base-1.0

# Or for SD 1.5:
# IMAGES_OPENAI_MODEL=sd-v1-5
```

### Backend Config (already correct)

The `backends_config.yaml` is already configured:
- `gpu_heavy` points to `http://ada2:7860`
- Supports `images` capability
- Concurrency limit: 2
- Payload policy: URL-default with base64 opt-in

## Testing

### 1. Test InvokeAI Directly

```bash
# Health check
curl http://ada2:7860/healthz

# Readiness check (nginx -> shim -> invokeai)
curl http://ada2:7860/readyz

# Generate test image (OpenAI Images via shim)
curl -X POST http://ada2:7860/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "a beautiful sunset over mountains",
    "n": 1,
    "size": "1024x1024",
    "response_format": "b64_json"
  }'
```

### 2. Test via Gateway

```bash
# Should route to gpu_heavy (ada2)
curl -k -X POST https://127.0.0.1:8800/v1/images/generations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "a red apple on a table",
    "size": "1024x1024",
    "n": 1
  }'

# Should return: {"data": [{"url": "/ui/images/..."}]}
```

### 3. Test Overload (429)

```bash
# Send 3 requests simultaneously (limit is 2)
for i in {1..3}; do
  curl -k -X POST https://127.0.0.1:8800/v1/images/generations \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"prompt": "test '$i'", "size": "512x512"}' &
done
wait

# One should return 429 with Retry-After: 5
```

## Monitoring

### Check Service Status

```bash
# On ada2
sudo systemctl status invokeai
sudo journalctl -u invokeai -f

# Check GPU usage
nvidia-smi
watch -n 1 nvidia-smi
```

### Check Gateway Stats

```bash
curl -k https://127.0.0.1:8800/v1/gateway/status \
  -H "Authorization: Bearer $TOKEN" | jq
```

Should show:
```json
{
  "admission_control": {
    "gpu_heavy.images": {
      "limit": 2,
      "available": 2,
      "inflight": 0
    }
  },
  "backend_health": {
    "gpu_heavy": {
      "healthy": true,
      "ready": true,
      "last_check": 1234567890.5,
      "error": null
    }
  }
}
```

## Removing Nexa/MLX

Once ada2 is working:

1. **Stop using local_mlx for images:**
   - Already done - `local_mlx` in backends_config.yaml doesn't list `images` as a capability

2. **Disable Nexa service on macOS:**
   ```bash
   # On macOS
   launchctl unload ~/Library/LaunchAgents/com.ai.nexa.plist
   # Or if system-wide:
   sudo launchctl unload /Library/LaunchDaemons/com.ai.nexa.plist
   ```

3. **Update documentation:**
   - Remove Nexa references from ai-infra INVENTORY
   - Update deployment guides

## Troubleshooting

### InvokeAI won't start
- Check CUDA: `nvidia-smi`
- Check logs: `sudo journalctl -u invokeai -n 100`
- Verify VRAM: Needs ~8GB for SDXL, ~4GB for SD 1.5

### Gateway returns 503 (backend not ready)
- Check health endpoints: `curl http://ada2:7860/healthz`
- Check InvokeAI API: `curl http://ada2:7860/api/v1/models`
- Wait 30s for gateway health check to update

## Ops Checklist (InvokeAI shim + gateway)

### On the image host (ada2)

```bash
# nginx up?
curl -sf http://127.0.0.1:7860/healthz

# shim readiness (nginx -> shim -> invokeai)
curl -sf http://127.0.0.1:7860/readyz

# OpenAI Images endpoint works and returns base64 when requested
curl -sS -X POST http://127.0.0.1:7860/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"prompt":"ops smoke test","response_format":"b64_json","n":1,"size":"512x512"}'
```

If using the “save last image” knob in the shim:

```bash
# Example path; depends on SHIM_SAVE_LAST_IMAGE_PATH
file /var/lib/invokeai/openai_images_shim/last.png
```

### On the gateway host

- Ensure env vars are set:
  - `IMAGES_BACKEND=http_openai_images`
  - `IMAGES_BACKEND_CLASS=gpu_heavy`
  - `IMAGES_HTTP_BASE_URL=http://ada2:7860`
  - `IMAGES_OPENAI_MODEL` is optional for the InvokeAI shim
- Ensure `/var/lib/gateway/data/ui_images` exists and is writable by the gateway user (for URL responses).

Verify end-to-end:

```bash
# From gateway/ (starts uvicorn if needed)
python tools/verify_gateway.py --check-images

# Or against a running gateway:
python tools/verify_gateway.py --base-url https://127.0.0.1:8800 --token "$GATEWAY_BEARER_TOKEN" --check-images --insecure
```

### Images are base64 instead of URLs
- Check request includes `"response_format": "url"` (or omit for default)
- Check `UI_IMAGE_DIR` exists and is writable: `/var/lib/gateway/data/ui_images`

### Slow generation
- Use SD 1.5 instead of SDXL for faster results
- Reduce steps: add `"steps": 20` to request options
- Check GPU isn't being used by other processes

## Performance Tuning

### For Speed (SD 1.5)
```bash
IMAGES_OPENAI_MODEL=sd-v1-5
# Default steps: 20
# Size: 512x512 or 768x768
```

### For Quality (SDXL)
```bash
IMAGES_OPENAI_MODEL=sd-xl-base-1.0
# Recommended steps: 30-50
# Size: 1024x1024
```

### Concurrency Tuning
Edit `backends_config.yaml`:
```yaml
gpu_heavy:
  concurrency_limits:
    images: 1  # Conservative, prevents VRAM OOM
    # or
    images: 2  # Aggressive, may cause OOM with SDXL
```

## Alternative: ComfyUI Setup

If you prefer ComfyUI:

```bash
# Install
git clone https://github.com/comfyanonymous/ComfyUI /var/lib/comfyui
cd /var/lib/comfyui
python3 -m venv venv
source venv/bin/activate
pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu121
pip install -r requirements.txt

# Run
python main.py --listen 0.0.0.0 --port 8188
```

Then use the ComfyUI API wrapper or create a simple FastAPI proxy that converts OpenAI-style requests to ComfyUI workflows.

## Next Steps

1. Set up InvokeAI on ada2 following this guide
2. Test health endpoints
3. Configure gateway environment variables
4. Run integration tests
5. Disable Nexa/MLX image generation
6. Monitor for 24-48 hours
7. Document production configuration in ai-infra
