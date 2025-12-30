#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, List, Optional


def _maybe_reexec_into_gateway_venv() -> None:
    """If the gateway venv exists, ensure we run under it.

    On the macOS host, this script is typically executed as `tools/openai_sdk_stream_test.py`,
    which uses the system python from the shebang. The gateway runtime, however, uses
    /var/lib/gateway/env/bin/python. Re-exec into that interpreter if present so
    optional deps (like `openai`) are resolved from the correct environment.
    """

    if os.getenv("GATEWAY_SKIP_REEXEC") == "1":
        return

    venv_py = "/var/lib/gateway/env/bin/python"
    try:
        if os.path.exists(venv_py) and os.path.realpath(sys.executable) != os.path.realpath(venv_py):
            env = dict(os.environ)
            env["GATEWAY_SKIP_REEXEC"] = "1"
            os.execve(venv_py, [venv_py, *sys.argv], env)
    except Exception:
        # Fall back to current interpreter; the error message below will
        # explain how to install deps / run with the venv python.
        return


def _env_first(*keys: str) -> Optional[str]:
    for k in keys:
        v = (os.getenv(k) or "").strip()
        if v:
            return v
    return None


def main(argv: List[str]) -> int:
    _maybe_reexec_into_gateway_venv()

    p = argparse.ArgumentParser(description="Validate gateway SSE streaming via the OpenAI Python SDK.")
    p.add_argument("--base-url", default=_env_first("GATEWAY_OPENAI_BASE_URL", "OPENAI_BASE_URL") or "http://127.0.0.1:8800/v1")
    p.add_argument("--api-key", default=_env_first("GATEWAY_BEARER_TOKEN", "OPENAI_API_KEY") or "")
    p.add_argument("--model", default=os.getenv("GATEWAY_MODEL", "fast"))
    p.add_argument("--prompt", default="Count from 1 to 5, slowly.")
    p.add_argument("--max-chunks", type=int, default=10_000)
    ns = p.parse_args(argv)

    if not ns.api_key:
        print("ERROR: missing api key. Set GATEWAY_BEARER_TOKEN (recommended) or pass --api-key.", file=sys.stderr)
        return 2

    try:
        # openai>=1.x
        from openai import OpenAI  # type: ignore
    except Exception as e:
        print("ERROR: openai Python package not installed in this environment.", file=sys.stderr)
        if os.path.exists("/var/lib/gateway/env/bin/python"):
            print(
                "Install with: sudo -u gateway /var/lib/gateway/env/bin/python -m pip install -r /var/lib/gateway/app/tools/requirements.txt",
                file=sys.stderr,
            )
        else:
            print("Install with: pip install openai", file=sys.stderr)
        print(f"Import error: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    client = OpenAI(base_url=ns.base_url, api_key=ns.api_key)

    print(f"base_url={ns.base_url}")
    print(f"model={ns.model}")

    chunks = 0
    text_out = []
    finish_reason: Optional[str] = None

    try:
        stream = client.chat.completions.create(
            model=ns.model,
            stream=True,
            messages=[{"role": "user", "content": ns.prompt}],
        )

        for event in stream:
            chunks += 1
            if chunks > ns.max_chunks:
                print("ERROR: exceeded --max-chunks; stream may be hanging.", file=sys.stderr)
                return 4

            choice = (getattr(event, "choices", None) or [None])[0]
            if not choice:
                continue

            delta: Any = getattr(choice, "delta", None)
            if delta is not None:
                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    text_out.append(content)
                    print(content, end="", flush=True)

            fr = getattr(choice, "finish_reason", None)
            if isinstance(fr, str) and fr:
                finish_reason = fr

        print("\n")

    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as e:
        print(f"ERROR: SDK streaming call failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 5

    if chunks == 0:
        print("ERROR: received 0 streamed events.", file=sys.stderr)
        return 6

    if not finish_reason:
        print("ERROR: stream ended without a finish_reason.", file=sys.stderr)
        return 7

    print(f"OK: streamed {chunks} events; finish_reason={finish_reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
