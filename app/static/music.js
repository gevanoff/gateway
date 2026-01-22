(() => {
  const $ = (id) => document.getElementById(id);

  const promptEl = $("prompt");
  const durationEl = $("duration");
  const modelEl = $("model");
  const tempEl = $("temperature");
  const topPEl = $("top_p");
  const topKEl = $("top_k");
  const tagsEl = $("tags");
  const extraEl = $("extra");
  const generateEl = $("generate");

  const statusEl = $("status");
  const metaEl = $("meta");
  const galleryEl = $("gallery");
  const debugEl = $("debug");

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.className = isError ? "hint error" : "hint";
  }

  function clearOutput() {
    metaEl.textContent = "";
    galleryEl.innerHTML = "";
  }

  function parseNum(value) {
    const s = String(value || "").trim();
    if (!s) return null;
    const n = Number(s);
    return Number.isFinite(n) ? n : null;
  }

  function buildRequestBody() {
    const prompt = String(promptEl.value || "").trim();
    if (!prompt) throw new Error("prompt required");

    const body = {
      prompt,
      duration: Math.max(1, Math.min(300, parseInt(String(durationEl.value || "15"), 10) || 15)),
    };

    const model = String(modelEl.value || "").trim();
    if (model) body.model = model;

    const temperature = parseNum(tempEl.value);
    if (temperature !== null) body.temperature = temperature;

    const top_p = parseNum(topPEl.value);
    if (top_p !== null) body.top_p = top_p;

    const top_k = parseInt(String(topKEl.value || "0"), 10);
    if (!Number.isNaN(top_k) && top_k > 0) body.top_k = top_k;

    const tags = String(tagsEl.value || "").trim();
    if (tags) body.tags = tags.split(/\s*,\s*/).filter(Boolean);

    const extraRaw = String(extraEl.value || "").trim();
    if (extraRaw) {
      let extra;
      try {
        extra = JSON.parse(extraRaw);
      } catch {
        throw new Error("extra JSON is invalid");
      }
      if (!extra || typeof extra !== "object" || Array.isArray(extra)) {
        throw new Error("extra JSON must be an object");
      }
      for (const [k, v] of Object.entries(extra)) {
        body[k] = v;
      }
    }

    return body;
  }

  function renderAudio(payload) {
    const url = payload?.audio_url;
    if (!url) return;

    const div = document.createElement("div");
    div.className = "thumb";
    div.innerHTML = `
      <div style="display:flex; gap:8px; align-items:center; justify-content:space-between;">
        <div style="flex:1 1 60%"><audio controls src="${url}"></audio></div>
        <div style="display:flex; gap:8px; flex-direction:column; align-items:flex-end;">
          <a href="${url}" target="_blank" rel="noreferrer">Open</a>
          <a href="#" data-copy="${url}">Copy URL</a>
        </div>
      </div>
    `;

    div.addEventListener("click", (e) => {
      const a = e.target;
      if (!(a instanceof HTMLAnchorElement)) return;
      const copy = a.getAttribute("data-copy");
      if (!copy) return;
      e.preventDefault();
      void navigator.clipboard?.writeText(copy);
      setStatus("Copied URL to clipboard", false);
    });

    galleryEl.appendChild(div);
  }

  function readQueryPrefill() {
    const qs = new URLSearchParams(location.search || "");

    const prompt = qs.get("prompt");
    const duration = qs.get("duration");
    const model = qs.get("model");
    const temperature = qs.get("temperature");
    const top_p = qs.get("top_p");
    const top_k = qs.get("top_k");

    if (prompt && promptEl) promptEl.value = prompt;
    if (duration && durationEl) durationEl.value = duration;
    if (model && modelEl) modelEl.value = model;
    if (temperature && tempEl) tempEl.value = temperature;
    if (top_p && topPEl) topPEl.value = top_p;
    if (top_k && topKEl) topKEl.value = top_k;

    // Optional: also accept a JSON blob for extra fields.
    const extraJson = qs.get("extra");
    if (extraJson && extraEl) extraEl.value = extraJson;
  }

  async function generate() {
    setStatus("", false);
    metaEl.textContent = "";
    clearOutput();

    let body;
    try {
      body = buildRequestBody();
    } catch (e) {
      setStatus(String(e?.message || e), true);
      return;
    }

    generateEl.disabled = true;
    setStatus("Generating...", false);

    try {
      const resp = await fetch("/ui/api/music", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      const text = await resp.text();
      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }

      debugEl.textContent = JSON.stringify({ request: body, response: payload }, null, 2);

      if (!resp.ok) {
        setStatus(`HTTP ${resp.status}: ${typeof payload === "string" ? payload : JSON.stringify(payload)}`, true);
        return;
      }

      const gw = payload?._gateway;
      const bits = [];
      if (gw?.backend) bits.push(`backend=${gw.backend}`);
      if (gw?.backend_class) bits.push(`class=${gw.backend_class}`);
      if (gw?.upstream_latency_ms) bits.push(`latency=${Math.round(gw.upstream_latency_ms)}ms`);
      metaEl.textContent = bits.join(" • ");

      setStatus("Done", false);
      renderAudio(payload);
    } catch (e) {
      setStatus(String(e), true);
    } finally {
      generateEl.disabled = false;
    }
  }

  generateEl.addEventListener("click", () => void generate());

  promptEl.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      void generate();
    }
  });

  // Prefill from query string when present (so /ui/music?prompt=... works)
  (function () {
    try {
      readQueryPrefill();
    } catch (e) {
      // ignore
    }
  })();
})();
