#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _maybe_reexec_into_gateway_venv() -> None:
    """Re-exec into the gateway venv python when available.

    This mirrors the deployment layout used by ai-infra on macOS/Linux.
    """

    if os.getenv("GATEWAY_SKIP_REEXEC") == "1":
        return

    candidates: list[str] = []
    override = (os.getenv("GATEWAY_VENV_PY") or "").strip()
    if override:
        candidates.append(override)

    candidates.extend(
        [
            "/var/lib/gateway/env/bin/python",
            "/var/lib/gateway/venv/bin/python",
        ]
    )

    try:
        for venv_py in candidates:
            if os.path.exists(venv_py) and os.path.realpath(sys.executable) != os.path.realpath(venv_py):
                env = dict(os.environ)
                env["GATEWAY_SKIP_REEXEC"] = "1"
                os.execve(venv_py, [venv_py, *sys.argv], env)
    except Exception:
        return


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return int(s.getsockname()[1])


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    timeout_sec: float = 20.0,
    max_body_bytes: int = 200_000,
) -> tuple[int, dict[str, str], bytes]:
    body: bytes | None = None
    req_headers: dict[str, str] = dict(headers or {})

    if json_body is not None:
        body = json.dumps(json_body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        req_headers.setdefault("content-type", "application/json")

    request = Request(url=url, data=body, headers=req_headers, method=method.upper())

    try:
        with urlopen(request, timeout=timeout_sec) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            data = resp.read(max_body_bytes + 1)
            return status, resp_headers, data[:max_body_bytes]
    except HTTPError as e:
        status = int(getattr(e, "code", 0) or 0)
        try:
            data = e.read(max_body_bytes + 1)
        except Exception:
            data = b""
        return status, {k.lower(): v for k, v in getattr(e, "headers", {}).items()}, data[:max_body_bytes]
    except URLError as e:
        raise RuntimeError(f"{type(e).__name__}: {e}")


def _http_stream_until_done(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    timeout_sec: float = 20.0,
    max_bytes: int = 128_000,
) -> tuple[bool, str]:
    body: bytes | None = None
    req_headers: dict[str, str] = dict(headers or {})

    if json_body is not None:
        body = json.dumps(json_body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        req_headers.setdefault("content-type", "application/json")

    request = Request(url=url, data=body, headers=req_headers, method="POST")

    buf = bytearray()
    try:
        with urlopen(request, timeout=timeout_sec) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            if status != 200:
                return False, f"status={status}"
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"data: [DONE]" in buf:
                    return True, ""
                if len(buf) > max_bytes:
                    break
        return False, "did not observe 'data: [DONE]' within limit"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _wait_for_health(base_url: str, token: str, *, timeout_sec: float = 15.0) -> None:
    headers = {"authorization": f"Bearer {token}"}
    deadline = time.time() + timeout_sec
    last_err: Optional[str] = None

    while time.time() < deadline:
        try:
            status, _h, body = _http_request("GET", base_url.rstrip("/") + "/health", headers=headers, timeout_sec=2.5)
            if status == 200:
                return
            last_err = f"status={status} body={(body[:200] or b'').decode('utf-8', errors='replace')}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(0.25)

    raise RuntimeError(f"gateway did not become healthy: {last_err}")


def _run_pytest(*, cwd: str) -> CheckResult:
    cp = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode == 0:
        return CheckResult(name="pytest", ok=True, detail=cp.stdout.strip())
    out = (cp.stdout or "") + "\n" + (cp.stderr or "")
    return CheckResult(name="pytest", ok=False, detail=out.strip()[-8000:])


def _start_uvicorn(*, cwd: str, port: int, env: dict[str, str]) -> subprocess.Popen:
    argv = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]

    # Avoid opening a new console window on Windows; harmless elsewhere.
    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
    )


def _stop_process(proc: subprocess.Popen, *, timeout_sec: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout_sec)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_http_checks(*, base_url: str, token: str, require_backend: bool) -> list[CheckResult]:
    results: list[CheckResult] = []

    def ok(name: str, detail: str = "") -> None:
        results.append(CheckResult(name=name, ok=True, detail=detail))

    def bad(name: str, detail: str) -> None:
        results.append(CheckResult(name=name, ok=False, detail=detail))

    bearer = {"authorization": f"Bearer {token}"}

    # /health (GET + HEAD)
    try:
        status, _h, body = _http_request("GET", base_url.rstrip("/") + "/health", headers=bearer, timeout_sec=10.0)
        if status == 200:
            ok("health_get")
        else:
            bad("health_get", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        bad("health_get", f"{type(e).__name__}: {e}")
        return results

    try:
        status, _h, _body = _http_request("HEAD", base_url.rstrip("/") + "/health", headers=bearer, timeout_sec=10.0)
        if status == 200:
            ok("health_head")
        else:
            bad("health_head", f"status={status}")
    except Exception as e:
        bad("health_head", f"{type(e).__name__}: {e}")

    # /metrics (auth-protected)
    try:
        status, _h, body = _http_request("GET", base_url.rstrip("/") + "/metrics", headers=bearer, timeout_sec=10.0)
        if status == 200 and body.strip():
            ok("metrics")
        else:
            bad("metrics", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        bad("metrics", f"{type(e).__name__}: {e}")

    # OpenAI-ish endpoints
    v1 = base_url.rstrip("/") + "/v1"

    try:
        status, _h, body = _http_request("GET", v1 + "/models", headers=bearer)
        if status == 200:
            ok("models")
        else:
            bad("models", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        bad("models", f"{type(e).__name__}: {e}")

    # Tool bus listing should be available regardless of tool enables.
    try:
        status, _h, body = _http_request("GET", v1 + "/tools", headers=bearer)
        if status == 200:
            ok("tools_list")
        else:
            bad("tools_list", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        bad("tools_list", f"{type(e).__name__}: {e}")

    # Backend-dependent checks
    backends_ok = False
    try:
        status, _h, body = _http_request("GET", base_url.rstrip("/") + "/health/upstreams", headers=bearer)
        if status == 200:
            try:
                payload = json.loads(body.decode("utf-8"))
                statuses = payload.get("upstreams") if isinstance(payload, dict) else None
                if isinstance(statuses, list):
                    backends_ok = any((isinstance(x, dict) and x.get("ok") is True) for x in statuses)
            except Exception:
                backends_ok = False
            ok("health_upstreams", detail=("backend_ok" if backends_ok else "no_backend_ok"))
        else:
            bad("health_upstreams", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        bad("health_upstreams", f"{type(e).__name__}: {e}")

    if require_backend and not backends_ok:
        bad("backend_required", "no healthy upstreams reported")
        return results

    if backends_ok:
        # Non-streaming chat completion
        payload = {"model": "fast", "stream": False, "messages": [{"role": "user", "content": "Say hi."}]}
        try:
            status, _h, body = _http_request("POST", v1 + "/chat/completions", headers=bearer, json_body=payload)
            if status == 200:
                ok("chat_non_stream")
            else:
                bad("chat_non_stream", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
        except Exception as e:
            bad("chat_non_stream", f"{type(e).__name__}: {e}")

        # Streaming chat completion: verify we see the DONE marker.
        stream_headers = dict(bearer)
        stream_headers["accept"] = "text/event-stream"
        payload = {"model": "fast", "stream": True, "messages": [{"role": "user", "content": "Count 1..3."}]}
        ok_stream, detail = _http_stream_until_done(v1 + "/chat/completions", headers=stream_headers, json_body=payload)
        if ok_stream:
            ok("chat_stream")
        else:
            bad("chat_stream", detail)

    return results


def _print_results(results: list[CheckResult]) -> int:
    width = max((len(r.name) for r in results), default=10)
    failed = [r for r in results if not r.ok]

    for r in results:
        status = "OK" if r.ok else "FAIL"
        detail = ("" if not r.detail else f" - {r.detail}")
        print(f"{r.name.ljust(width)}  {status}{detail}")

    if failed:
        print(f"\nFAILED: {len(failed)} check(s)")
        return 1
    print("\nALL OK")
    return 0


def main(argv: list[str]) -> int:
    _maybe_reexec_into_gateway_venv()

    p = argparse.ArgumentParser(description="Comprehensive verification for the Local AI Gateway.")
    p.add_argument(
        "--base-url",
        default="",
        help="If set, do HTTP checks against an already-running gateway (e.g. http://127.0.0.1:8800).",
    )
    p.add_argument(
        "--token",
        default=(os.getenv("GATEWAY_BEARER_TOKEN") or "test-token"),
        help="Bearer token for /health, /v1/*, /metrics (default: $GATEWAY_BEARER_TOKEN or test-token).",
    )
    p.add_argument("--skip-pytest", action="store_true", help="Skip running pytest.")
    p.add_argument(
        "--require-backend",
        action="store_true",
        help="Fail if no healthy upstream backend is available (otherwise backend-dependent checks are skipped).",
    )
    p.add_argument(
        "--no-start",
        action="store_true",
        help="Do not auto-start uvicorn (requires --base-url).",
    )
    ns = p.parse_args(argv)

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    results: list[CheckResult] = []

    if not ns.skip_pytest:
        results.append(_run_pytest(cwd=repo_root))

    proc: Optional[subprocess.Popen] = None
    base_url = (ns.base_url or "").strip()

    if not base_url:
        if ns.no_start:
            results.append(CheckResult(name="start_server", ok=False, detail="--no-start requires --base-url"))
            return _print_results(results)

        port = _find_free_port()
        base_url = f"http://127.0.0.1:{port}"

        env = dict(os.environ)
        env.setdefault("GATEWAY_BEARER_TOKEN", ns.token)
        # Keep runtime checks self-contained and fast.
        env.setdefault("MEMORY_ENABLED", "false")
        env.setdefault("MEMORY_V2_ENABLED", "false")
        env.setdefault("METRICS_ENABLED", "true")

        proc = _start_uvicorn(cwd=repo_root, port=port, env=env)
        try:
            _wait_for_health(base_url, ns.token)
            results.append(CheckResult(name="start_server", ok=True, detail=base_url))
        except Exception as e:
            results.append(CheckResult(name="start_server", ok=False, detail=f"{type(e).__name__}: {e}"))
            if proc and proc.stderr:
                try:
                    err_tail = (proc.stderr.read() or "")[-4000:]
                    if err_tail.strip():
                        results.append(CheckResult(name="server_stderr", ok=False, detail=err_tail.strip()))
                except Exception:
                    pass
            _stop_process(proc)
            return _print_results(results)

    try:
        http_results = _run_http_checks(base_url=base_url, token=ns.token, require_backend=ns.require_backend)
        results.extend(http_results)
    finally:
        if proc is not None:
            _stop_process(proc)

    return _print_results(results)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
