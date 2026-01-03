from __future__ import annotations

import base64
import time
from typing import Any, Dict, List, Literal, Tuple

import httpx

from app.config import S


def _parse_size(size: str) -> Tuple[int, int]:
    s = (size or "").strip().lower()
    if not s:
        s = "1024x1024"
    if "x" not in s:
        raise ValueError("size must be like '1024x1024'")
    a, b = s.split("x", 1)
    w = int(a.strip())
    h = int(b.strip())
    if w <= 0 or h <= 0:
        raise ValueError("size must be positive")
    max_px = int(getattr(S, "IMAGES_MAX_PIXELS", 2_000_000) or 2_000_000)
    if w * h > max_px:
        raise ValueError("size too large")
    return w, h


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _mock_svg(prompt: str, width: int, height: int) -> bytes:
    # Minimal placeholder image: preserves negative space (no crop) and is deterministic.
    p = (prompt or "").strip()
    if len(p) > 400:
        p = p[:400] + "…"

    # Escape basic XML characters.
    p = p.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    svg = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"{width}\" height=\"{height}\" viewBox=\"0 0 {width} {height}\">
  <rect width=\"100%\" height=\"100%\" fill=\"#0b0d10\"/>
  <rect x=\"24\" y=\"24\" width=\"{max(0, width - 48)}\" height=\"{max(0, height - 48)}\" fill=\"#0e1217\" stroke=\"rgba(231,237,246,0.18)\"/>
  <text x=\"48\" y=\"72\" fill=\"#e7edf6\" font-family=\"ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial\" font-size=\"20\" font-weight=\"600\">Mock image backend</text>
  <text x=\"48\" y=\"104\" fill=\"#a9b4c3\" font-family=\"ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial\" font-size=\"14\">No image engine configured. Set IMAGES_BACKEND to http_a1111 to use a real generator.</text>
  <foreignObject x=\"48\" y=\"132\" width=\"{max(0, width - 96)}\" height=\"{max(0, height - 180)}\">
    <div xmlns=\"http://www.w3.org/1999/xhtml\" style=\"color:#c1ccdb;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;font-size:14px;line-height:1.5;white-space:pre-wrap;\">{p}</div>
  </foreignObject>
</svg>
"""
    return svg.encode("utf-8")


async def generate_images(
    *,
    prompt: str,
    size: str = "1024x1024",
    n: int = 1,
    model: str | None = None,
    options: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Generate images in an OpenAI-ish response shape.

        Backends:
            - mock: returns a placeholder SVG (always available)
            - http_a1111: proxies to an Automatic1111-compatible API (txt2img)
            - http_openai_images: proxies to an OpenAI-style images server (POST /v1/images/generations)
    """

    n = int(n or 1)
    n = max(1, min(n, 4))
    width, height = _parse_size(size)

    backend: str = (getattr(S, "IMAGES_BACKEND", "mock") or "mock").strip().lower()

    def _filtered_options(opts: Dict[str, Any] | None) -> Dict[str, Any]:
        if not isinstance(opts, dict) or not opts:
            return {}

        # Conservative allowlist: upstream servers vary widely.
        allowed = {
            # Common knobs across SD/SDXL style servers.
            "seed",
            "steps",
            "num_inference_steps",
            "guidance",
            "guidance_scale",
            "cfg_scale",
            "negative_prompt",
            "sampler",
            "scheduler",
            "style",
            "quality",
        }

        out: Dict[str, Any] = {}
        for k, v in opts.items():
            if k not in allowed:
                continue
            if v is None:
                continue
            if isinstance(v, str):
                vv = v.strip()
                if not vv:
                    continue
                out[k] = vv
                continue
            out[k] = v

        return out

    if backend == "http_a1111":
        base = (getattr(S, "IMAGES_HTTP_BASE_URL", "") or "").strip().rstrip("/")
        if not base:
            raise RuntimeError("IMAGES_HTTP_BASE_URL is required for http_a1111")

        timeout = float(getattr(S, "IMAGES_HTTP_TIMEOUT_SEC", 120.0) or 120.0)
        payload = {
            "prompt": prompt,
            "width": width,
            "height": height,
            "batch_size": n,
            # Keep defaults conservative; user can tune on the server.
            "steps": int(getattr(S, "IMAGES_A1111_STEPS", 20) or 20),
        }

        payload.update(_filtered_options(options))

        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/sdapi/v1/txt2img", json=payload)
            r.raise_for_status()
            out = r.json()

        images = out.get("images") if isinstance(out, dict) else None
        if not (isinstance(images, list) and images and all(isinstance(x, str) for x in images)):
            raise RuntimeError("unexpected response from image backend")

        data = [{"b64_json": images[i]} for i in range(min(n, len(images)))]
        resp: Dict[str, Any] = {"created": int(time.time()), "data": data}
        resp["_gateway"] = {"backend": backend, "mime": "image/png"}
        return resp

    if backend == "http_openai_images":
        base = (getattr(S, "IMAGES_HTTP_BASE_URL", "") or "").strip().rstrip("/")
        if not base:
            raise RuntimeError("IMAGES_HTTP_BASE_URL is required for http_openai_images")

        timeout = float(getattr(S, "IMAGES_HTTP_TIMEOUT_SEC", 120.0) or 120.0)
        chosen_model = (model or "").strip() or (getattr(S, "IMAGES_OPENAI_MODEL", "") or "").strip()
        if not chosen_model:
            raise RuntimeError("model is required for http_openai_images (set IMAGES_OPENAI_MODEL or pass model)")

        payload: Dict[str, Any] = {
            "model": chosen_model,
            "prompt": prompt,
            "n": n,
            "size": f"{width}x{height}",
            "response_format": "b64_json",
        }

        # Only include extra knobs if explicitly provided by the caller.
        payload.update(_filtered_options(options))

        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/v1/images/generations", json=payload)
            r.raise_for_status()
            out = r.json()

        data = out.get("data") if isinstance(out, dict) else None
        if not (isinstance(data, list) and data and all(isinstance(x, dict) for x in data)):
            raise RuntimeError("unexpected response from image backend")

        # Normalize to OpenAI-ish shape with b64_json.
        normalized: List[Dict[str, Any]] = []
        for item in data[:n]:
            b64 = item.get("b64_json")
            if isinstance(b64, str) and b64:
                normalized.append({"b64_json": b64})
        if not normalized:
            raise RuntimeError("image backend did not return b64_json")

        resp2: Dict[str, Any] = {"created": int(out.get("created") or time.time()), "data": normalized}
        resp2["_gateway"] = {"backend": backend, "mime": "image/png", "model": chosen_model}
        return resp2

    # Default: mock
    svg_bytes = _mock_svg(prompt, width, height)
    data = [{"b64_json": _b64(svg_bytes)} for _ in range(n)]
    resp = {"created": int(time.time()), "data": data, "_gateway": {"backend": "mock", "mime": "image/svg+xml"}}
    return resp
