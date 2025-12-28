from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request

from app.auth import require_bearer
from app.config import S
from app.models import (
    ChatCompletionRequest,
    ChatMessage,
    MemoryCompactRequest,
    MemorySearchRequest,
    MemoryUpsertRequest,
)
from app.router import decide_route
from app.router_cfg import router_cfg
from app import memory_v2
from app.upstreams import call_mlx_openai, call_ollama, embed_text_for_memory
from app.memory_legacy import memory_search as memory_search_v1, memory_upsert_async


router = APIRouter()


def _memory_v2_default_types() -> list[memory_v2.MemoryType]:
    raw = (S.MEMORY_V2_TYPES_DEFAULT or "").strip()
    if not raw:
        return ["fact", "preference", "project"]
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    out: list[memory_v2.MemoryType] = []
    for p in parts:
        if p in {"fact", "preference", "project", "ephemeral"}:
            out.append(p)  # type: ignore[arg-type]
    return out or ["fact", "preference", "project"]


async def inject_memory(messages: List[ChatMessage]) -> List[ChatMessage]:
    if not S.MEMORY_ENABLED:
        return messages

    last_user = None
    for m in reversed(messages):
        if m.role == "user":
            last_user = m.content
            break
    if not isinstance(last_user, str) or not last_user.strip():
        return messages

    chunks: list[str] = []
    total = 0

    if S.MEMORY_V2_ENABLED:
        qemb = await embed_text_for_memory(last_user)
        now = int(time.time())
        types = _memory_v2_default_types()
        max_age = int(S.MEMORY_V2_MAX_AGE_SEC)

        conn = sqlite3.connect(S.MEMORY_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        clause = " WHERE compacted_into IS NULL AND type IN (%s) AND ts >= ?" % ",".join(["?"] * len(types))
        args: list[Any] = [*types, int(now - max_age)]
        rows = conn.execute(f"SELECT id,type,source,text,emb,dim,ts FROM memory_v2{clause}", args).fetchall()
        conn.close()

        scored = []
        for (mid, mtype, source, text, emb_blob, dim, ts) in rows:
            if dim != len(qemb):
                continue
            emb = memory_v2.unpack_emb(emb_blob)
            s = memory_v2.cosine(qemb, emb)
            if s >= S.MEMORY_MIN_SIM:
                scored.append((s, mid, mtype, source, text, ts))
        scored.sort(key=lambda x: x[0], reverse=True)

        for (s, mid, mtype, source, text, ts) in scored[: S.MEMORY_TOP_K]:
            if not isinstance(text, str):
                continue
            line = f"- ({mtype}/{source}, {s:.3f}) {text}"
            if total + len(line) > S.MEMORY_MAX_CHARS:
                break
            chunks.append(line)
            total += len(line)
    else:
        res = await memory_search_v1(query=last_user, k=S.MEMORY_TOP_K, min_sim=S.MEMORY_MIN_SIM)
        if not res.get("ok") or not res.get("results"):
            return messages
        for r in res["results"]:
            t = r.get("text") or ""
            if not isinstance(t, str):
                continue
            line = f"- ({r.get('score'):.3f}) {t}"
            if total + len(line) > S.MEMORY_MAX_CHARS:
                break
            chunks.append(line)
            total += len(line)

    if not chunks:
        return messages

    mem_text = "Retrieved memory (may be relevant):\n" + "\n".join(chunks)
    return [ChatMessage(role="system", content=mem_text)] + messages


@router.post("/v1/memory/upsert")
async def v1_memory_upsert(req: Request):
    require_bearer(req)
    if not S.MEMORY_V2_ENABLED:
        raise HTTPException(status_code=400, detail="memory v2 disabled")

    body = await req.json()
    mr = MemoryUpsertRequest(**body)
    if not isinstance(mr.text, str) or not mr.text.strip():
        raise HTTPException(status_code=400, detail="text must be non-empty")

    emb = await embed_text_for_memory(mr.text)
    out = memory_v2.upsert(
        db_path=S.MEMORY_DB_PATH,
        embed=lambda _t: emb,
        text=mr.text,
        mtype=mr.type,
        source=(mr.source or "user"),
        meta=mr.meta,
        mid=mr.id,
        ts=mr.ts,
    )
    return out


@router.get("/v1/memory/list")
async def v1_memory_list(
    req: Request,
    type: Optional[str] = None,
    source: Optional[str] = None,
    since_ts: Optional[int] = None,
    max_age_sec: Optional[int] = None,
    limit: int = 50,
    include_compacted: bool = False,
):
    require_bearer(req)
    if not S.MEMORY_V2_ENABLED:
        raise HTTPException(status_code=400, detail="memory v2 disabled")

    types = None
    if type:
        parts = [p.strip().lower() for p in type.split(",") if p.strip()]
        types = [p for p in parts if p in {"fact", "preference", "project", "ephemeral"}]  # type: ignore[assignment]

    sources = None
    if source:
        parts = [p.strip().lower() for p in source.split(",") if p.strip()]
        sources = [p for p in parts if p in {"user", "system", "tool"}]  # type: ignore[assignment]

    return memory_v2.list_items(
        db_path=S.MEMORY_DB_PATH,
        types=types,
        sources=sources,
        since_ts=since_ts,
        max_age_sec=max_age_sec,
        limit=max(1, min(int(limit), 500)),
        include_compacted=bool(include_compacted),
    )


@router.post("/v1/memory/search")
async def v1_memory_search(req: Request):
    require_bearer(req)
    if not S.MEMORY_V2_ENABLED:
        raise HTTPException(status_code=400, detail="memory v2 disabled")

    body = await req.json()
    sr = MemorySearchRequest(**body)
    if not sr.query.strip():
        raise HTTPException(status_code=400, detail="query must be non-empty")

    qemb = await embed_text_for_memory(sr.query)

    types = sr.types
    sources = sr.sources
    top_k = int(sr.top_k or S.MEMORY_TOP_K)
    min_sim = float(sr.min_sim if sr.min_sim is not None else S.MEMORY_MIN_SIM)
    max_age = int(sr.max_age_sec if sr.max_age_sec is not None else S.MEMORY_V2_MAX_AGE_SEC)

    now = int(time.time())
    where = []
    args: list[Any] = []
    if not sr.include_compacted:
        where.append("compacted_into IS NULL")
    if types:
        where.append("type IN (%s)" % ",".join(["?"] * len(types)))
        args.extend(list(types))
    if sources:
        where.append("source IN (%s)" % ",".join(["?"] * len(sources)))
        args.extend(list(sources))
    if max_age > 0:
        where.append("ts >= ?")
        args.append(int(now - max_age))
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    conn = sqlite3.connect(S.MEMORY_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    rows = conn.execute(f"SELECT id,type,source,text,emb,dim,ts FROM memory_v2{clause}", args).fetchall()
    conn.close()

    scored = []
    for (mid, mtype, source, text, emb_blob, dim, ts) in rows:
        if dim != len(qemb):
            continue
        emb = memory_v2.unpack_emb(emb_blob)
        s = memory_v2.cosine(qemb, emb)
        if s >= min_sim:
            scored.append((s, mid, mtype, source, text, ts))
    scored.sort(key=lambda x: x[0], reverse=True)

    out = []
    for (s, mid, mtype, source, text, ts) in scored[: max(1, min(top_k, 100))]:
        out.append({"score": float(s), "id": mid, "type": mtype, "source": source, "text": text, "ts": ts})
    return {"ok": True, "results": out}


async def _summarize_for_compaction(items: list[dict], backend: Literal["ollama", "mlx"], model_name: str) -> str:
    lines = []
    for it in items:
        t = it.get("type")
        s = it.get("source")
        ts = it.get("ts")
        text = it.get("text")
        if not isinstance(text, str):
            continue
        lines.append(f"[{t}/{s} @ {ts}] {text}")

    sys_prompt = (
        "You are compacting an agent memory store. Produce a concise set of durable entries. "
        "Rules: (1) preserve factual correctness, (2) keep preferences explicit, (3) keep project context actionable, "
        "(4) avoid personal data, (5) do not invent. Output plain text, up to 25 bullet points."
    )
    user_text = "Memories to compact:\n" + "\n".join(lines)

    cc = ChatCompletionRequest(
        model=model_name,
        messages=[
            ChatMessage(role="system", content=sys_prompt),
            ChatMessage(role="user", content=user_text),
        ],
        stream=False,
    )

    resp = await (call_mlx_openai(cc) if backend == "mlx" else call_ollama(cc, model_name))
    msg = ((resp.get("choices") or [{}])[0].get("message") or {})
    content = msg.get("content")
    return content if isinstance(content, str) else ""


@router.post("/v1/memory/compact")
async def v1_memory_compact(req: Request):
    require_bearer(req)
    if not S.MEMORY_V2_ENABLED:
        raise HTTPException(status_code=400, detail="memory v2 disabled")

    body = await req.json()
    cr = MemoryCompactRequest(**body)

    now = int(time.time())
    max_age = int(cr.max_age_sec if cr.max_age_sec is not None else S.MEMORY_V2_MAX_AGE_SEC)
    types = cr.types or _memory_v2_default_types()
    max_items = max(1, min(int(cr.max_items), 200))

    where = []
    args: list[Any] = []
    if not cr.include_compacted:
        where.append("compacted_into IS NULL")
    if types:
        where.append("type IN (%s)" % ",".join(["?"] * len(types)))
        args.extend(list(types))
    if max_age > 0:
        where.append("ts < ?")
        args.append(int(now - max_age))
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    conn = sqlite3.connect(S.MEMORY_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    rows = conn.execute(
        f"SELECT id,type,source,text,meta,ts FROM memory_v2{clause} ORDER BY ts ASC LIMIT ?",
        (*args, max_items),
    ).fetchall()
    conn.close()

    items = []
    ids = []
    for (mid, mtype, source, text, meta, ts) in rows:
        ids.append(mid)
        items.append({"id": mid, "type": mtype, "source": source, "text": text, "meta": meta, "ts": ts})

    if len(items) < 2:
        return {"ok": True, "compacted": 0, "message": "not enough items to compact"}

    hdrs = {k.lower(): v for k, v in req.headers.items()}
    route = decide_route(
        cfg=router_cfg(),
        request_model="default",
        headers=hdrs,
        messages=[{"role": "user", "content": "\n".join([it["text"] for it in items if isinstance(it.get("text"), str)])}],
        has_tools=True,
    )
    backend: Literal["ollama", "mlx"] = route.backend
    model_name = route.model

    summary = await _summarize_for_compaction(items, backend, model_name)
    if not summary.strip():
        raise HTTPException(status_code=502, detail="compaction summarizer returned empty output")

    emb = await embed_text_for_memory(summary)
    new_meta = {"compacted_ids": ids, "router_reason": route.reason}
    out = memory_v2.upsert(
        db_path=S.MEMORY_DB_PATH,
        embed=lambda _t: emb,
        text=summary,
        mtype=cr.target_type,
        source=cr.target_source,
        meta=new_meta,
        mid=None,
        ts=int(time.time()),
    )
    new_id = out.get("id")
    if isinstance(new_id, str):
        memory_v2.mark_compacted(db_path=S.MEMORY_DB_PATH, ids=ids, into_id=new_id)
    return {"ok": True, "compacted": len(ids), "new_id": new_id}


# Legacy endpoints (kept for compatibility)
@router.post("/memory/upsert")
async def http_memory_upsert(req: Request):
    require_bearer(req)
    body = await req.json()
    text = body.get("text")
    meta = body.get("meta", {})
    mid = body.get("id")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="text must be non-empty string")
    if mid is not None and not isinstance(mid, str):
        raise HTTPException(status_code=400, detail="id must be string")
    if meta is not None and not isinstance(meta, dict):
        raise HTTPException(status_code=400, detail="meta must be object")
    return await memory_upsert_async(text=text, meta=meta, mid=mid)


@router.post("/memory/search")
async def http_memory_search(req: Request):
    require_bearer(req)
    body = await req.json()
    query = body.get("query")
    k = int(body.get("k", S.MEMORY_TOP_K))
    min_sim = float(body.get("min_sim", S.MEMORY_MIN_SIM))
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="query must be non-empty string")
    return await memory_search_v1(query=query, k=k, min_sim=min_sim)
