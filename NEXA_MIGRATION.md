# Migration: Nexa (macOS MLX) → ada2 (InvokeAI/ComfyUI)

## Summary

This migration removes image generation from the macOS Nexa/MLX backend and routes all image requests to ada2 (RTX 6000 Ada, 46GB VRAM) running InvokeAI or ComfyUI.

## What Changed

### Backend Configuration
- **Before**: `local_mlx` supported chat, embeddings, and images
- **After**: `local_mlx` supports chat and embeddings only; `gpu_heavy` (ada2) is the **sole** image backend

### Image Generation
- **Before**: Images generated via Nexa on macOS (limited models, unreliable)
- **After**: Images generated via InvokeAI/ComfyUI on ada2 (SDXL, SD 1.5, reliable GPU)

### Routing
- **Before**: Images could route to local_mlx or gpu_heavy depending on availability
- **After**: Images **only** route to gpu_heavy (ada2); no fallback, fail fast on overload

## Files Created/Modified

### New Files
1. `gateway/IMAGE_BACKEND_SETUP.md` - Complete setup guide for InvokeAI on ada2
2. `ai-infra/services/gateway/env/ada2-images.env.example` - Example configuration
3. `gateway/tests/test_ada2_images.py` - Integration tests for ada2 routing

### Modified Files
1. `gateway/POLICY_ENFORCEMENT.md` - Updated with ada2 image backend documentation
2. `gateway/app/backends_config.yaml` - Already correct (gpu_heavy for images)

## Deployment Checklist

### On ada2 (Ubuntu + CUDA)

1. **Install InvokeAI**
   ```bash
   # Follow IMAGE_BACKEND_SETUP.md
   sudo apt install python3.10 python3.10-venv
   sudo useradd -r -m -s /bin/bash invokeai
   # ... (see full setup guide)
   ```

2. **Download models**
   ```bash
   # SDXL (recommended)
   invokeai-model-install --add stabilityai/stable-diffusion-xl-base-1.0
   
   # Or SD 1.5 (faster)
   invokeai-model-install --add runwayml/stable-diffusion-v1-5
   ```

3. **Set up health endpoints**
   ```bash
   # Configure nginx proxy for /healthz and /readyz
   # See IMAGE_BACKEND_SETUP.md for nginx config
   ```

4. **Start service**
   ```bash
   sudo systemctl enable invokeai
   sudo systemctl start invokeai
   ```

5. **Verify**
   ```bash
   curl http://ada2:7860/healthz
   curl -X POST http://ada2:7860/v1/images/generations \
     -H "Content-Type: application/json" \
     -d '{"model": "sd-xl-base-1.0", "prompt": "test", "size": "512x512"}'
   ```

### On Gateway (macOS ai2)

1. **Update environment**
   ```bash
   # In /var/lib/gateway/app/.env:
   IMAGES_BACKEND=http_openai_images
   IMAGES_BACKEND_CLASS=gpu_heavy
   IMAGES_HTTP_BASE_URL=http://ada2:7860
   IMAGES_OPENAI_MODEL=sd-xl-base-1.0
   UI_IMAGE_DIR=/var/lib/gateway/data/ui_images
   ```

2. **Deploy gateway**
   ```bash
   cd ai-infra/services/gateway
   ./scripts/deploy.sh
   ```

3. **Verify routing**
   ```bash
   # Wait 30s for health check
   sleep 30
   
   # Check status
   curl -k https://127.0.0.1:8800/v1/gateway/status \
     -H "Authorization: Bearer $TOKEN" | jq
   
   # Should show gpu_heavy.images: {healthy: true, ready: true}
   ```

4. **Test image generation**
   ```bash
   curl -k -X POST https://127.0.0.1:8800/v1/images/generations \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"prompt": "a beautiful sunset", "size": "1024x1024"}' | jq
   
   # Should return: {"data": [{"url": "/ui/images/..."}]}
   # Should have headers: X-Backend-Used: gpu_heavy
   ```

5. **Create image storage directory**
   ```bash
   sudo mkdir -p /var/lib/gateway/data/ui_images
   sudo chown gateway:gateway /var/lib/gateway/data/ui_images
   ```

### Disable Nexa Image Generation (macOS)

1. **Verify Nexa is not used**
   ```bash
   # Check backends_config.yaml
   grep -A 5 "local_mlx:" gateway/app/backends_config.yaml
   # Should NOT list "images" in supported_capabilities
   ```

2. **Optional: Unload Nexa service**
   ```bash
   # If Nexa was running as a separate service for images
   launchctl unload ~/Library/LaunchAgents/com.ai.nexa.images.plist
   ```

## Testing

### Run Integration Tests
```bash
cd gateway
pytest tests/test_ada2_images.py -v

# Expected: 15 tests pass
# - Routing to gpu_heavy
# - URL responses by default
# - base64 opt-in works
# - 429 on overload (limit: 2)
# - 503 when backend not ready
# - No fallback to other backends
# - Content-addressed storage
# - Health monitoring
```

### Manual Verification
```bash
# 1. Check gateway status
curl -k https://127.0.0.1:8800/v1/gateway/status \
  -H "Authorization: Bearer $TOKEN" | jq

# Should show:
# - admission_control.gpu_heavy.images: {limit: 2, available: 2}
# - backend_health.gpu_heavy: {healthy: true, ready: true}

# 2. Generate test image
curl -k -X POST https://127.0.0.1:8800/v1/images/generations \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"prompt": "red apple", "size": "512x512"}' | jq

# Should return URL: /ui/images/{timestamp}_{hash}.png
# Headers: X-Backend-Used: gpu_heavy

# 3. Test overload (send 3 requests, limit is 2)
for i in {1..3}; do
   curl -k -X POST https://127.0.0.1:8800/v1/images/generations \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"prompt": "test '$i'", "size": "512x512"}' &
done
wait

# One should return 429 with Retry-After: 5

# 4. Verify local_mlx NOT used for images
curl -k -X POST https://127.0.0.1:8800/v1/images/generations \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Backend: local_mlx" \
  -d '{"prompt": "test", "size": "512x512"}'

# Should return 400: "Backend local_mlx does not support images"
```

## Monitoring

### Check ada2 Service
```bash
# On ada2
sudo systemctl status invokeai
sudo journalctl -u invokeai -f
nvidia-smi  # Check GPU usage
```

### Check Gateway Logs
```bash
# On ai2 (macOS)
tail -f /var/lib/gateway/logs/gateway.log | grep -i image
```

### Check Health Status
```bash
# Every 30s, gateway checks:
# - GET http://ada2:7860/healthz
# - GET http://ada2:7860/readyz

curl -k https://127.0.0.1:8800/v1/gateway/status \
  -H "Authorization: Bearer $TOKEN" | jq .backend_health.gpu_heavy
```

## Rollback Plan

If ada2 has issues:

1. **Temporary: Revert to mock backend**
   ```bash
   # In /var/lib/gateway/app/.env:
   IMAGES_BACKEND=mock
   
   # Restart gateway
   sudo launchctl stop com.ai.gateway
   sudo launchctl start com.ai.gateway
   ```

2. **Investigate ada2**
   ```bash
   # Check InvokeAI logs
   sudo journalctl -u invokeai -n 100
   
   # Check health endpoints
   curl http://ada2:7860/healthz
   curl http://ada2:7860/readyz
   ```

3. **Restore ada2 and re-enable**
   ```bash
   # Fix ada2 issue
   sudo systemctl restart invokeai
   
   # Wait 30s for health check
   sleep 30
   
   # Restore images backend
   IMAGES_BACKEND=http_openai_images
   sudo launchctl restart com.ai.gateway
   ```

## Performance Tuning

### For Speed (SD 1.5)
```bash
IMAGES_OPENAI_MODEL=sd-v1-5
# Generation time: ~5-10s for 512x512
# VRAM: ~4GB
```

### For Quality (SDXL)
```bash
IMAGES_OPENAI_MODEL=sd-xl-base-1.0
# Generation time: ~15-30s for 1024x1024
# VRAM: ~8GB
```

### Concurrency Tuning
Edit `backends_config.yaml`:
```yaml
gpu_heavy:
  concurrency_limits:
    images: 1  # Conservative, prevents OOM
    # or
    images: 2  # Default, safe for SDXL
    # or
    images: 4  # Aggressive, only for SD 1.5
```

## Success Criteria

✅ ada2 InvokeAI running and responding to health checks  
✅ Gateway routes all images to gpu_heavy (ada2)  
✅ Images return URLs by default  
✅ Overload returns 429 (no silent queuing)  
✅ No fallback to local_mlx (fail fast)  
✅ Health monitoring prevents requests to unhealthy backend  
✅ All integration tests pass  
✅ Production deployment successful  

## Documentation

- Setup: [IMAGE_BACKEND_SETUP.md](IMAGE_BACKEND_SETUP.md)
- Policy: [POLICY_ENFORCEMENT.md](POLICY_ENFORCEMENT.md)
- Example config: [ada2-images.env.example](../ai-infra/services/gateway/env/ada2-images.env.example)
- Tests: [test_ada2_images.py](tests/test_ada2_images.py)

## Questions?

- InvokeAI not starting? Check [IMAGE_BACKEND_SETUP.md](IMAGE_BACKEND_SETUP.md) troubleshooting section
- Gateway returning 503? Check health endpoints: `curl http://ada2:7860/healthz`
- Images are base64 instead of URLs? Check `response_format` parameter (default is URL)
- Slow generation? Try SD 1.5 instead of SDXL, or reduce steps

## Timeline

- **Day 0**: Set up InvokeAI on ada2, verify health endpoints
- **Day 1**: Deploy gateway with new config, test routing
- **Day 2-3**: Monitor production, tune concurrency limits
- **Day 4**: Disable Nexa image service (if separate)
- **Day 7**: Remove Nexa image generation code (optional cleanup)
