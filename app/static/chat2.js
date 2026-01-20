(() => {
  const $ = (id) => document.getElementById(id);

  const chatEl = $("chat");
  const modelEl = $("model");
  const loadModelsEl = $("loadModels");
  const inputEl = $("input");
  const sendEl = $("send");
  const clearEl = $("clear");
  const autoImageEl = $("autoImage");

  /** @type {{role:'user'|'assistant'|'system', content:string}[]} */
  let history = [];

  const CONVO_KEY = "gw_ui2_conversation_id";
  let conversationId = "";

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function scrollToBottom() {
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  function addMessage({ role, content, meta, html }) {
    const wrap = document.createElement("div");
    wrap.className = `msg ${role}`;

    const metaEl = document.createElement("div");
    metaEl.className = "meta";
    metaEl.textContent = meta || (role === "user" ? "You" : role === "assistant" ? "Assistant" : "System");

    const contentEl = document.createElement("div");
    contentEl.className = "content";
    if (html) {
      contentEl.innerHTML = html;
    } else {
      contentEl.textContent = content || "";
    }

    wrap.appendChild(metaEl);
    wrap.appendChild(contentEl);
    chatEl.appendChild(wrap);
    scrollToBottom();

    return { wrap, metaEl, contentEl };
  }

  function renderStoredMessage(m) {
    if (!m || typeof m !== "object") return;
    const role = typeof m.role === "string" ? m.role : "assistant";
    const type = typeof m.type === "string" ? m.type : "";
    const content = typeof m.content === "string" ? m.content : "";

    if (type === "image" && typeof m.url === "string" && m.url.trim()) {
      const metaBits = [];
      if (m.backend) metaBits.push(`backend=${m.backend}`);
      if (m.model) metaBits.push(`model=${m.model}`);
      if (m.sha256) metaBits.push(`sha=${String(m.sha256).slice(0, 12)}`);
      addMessage({
        role: role === "user" ? "user" : "assistant",
        meta: metaBits.length ? `Image • ${metaBits.join(" • ")}` : "Image",
        html: `<img class="gen" src="${escapeHtml(m.url.trim())}" alt="generated" />`,
      });
      return;
    }

    const metaBits = [];
    if (m.backend) metaBits.push(`backend=${m.backend}`);
    if (m.model) metaBits.push(`model=${m.model}`);
    if (m.reason) metaBits.push(`reason=${m.reason}`);
    addMessage({ role, content, meta: metaBits.length ? metaBits.join(" • ") : undefined });
  }

  function setBusy(busy) {
    sendEl.disabled = busy;
    inputEl.disabled = busy;
    modelEl.disabled = busy;
    loadModelsEl.disabled = busy;
  }

  function _setModelOptions(modelIds, preferred) {
    const prev = modelEl.value;
    modelEl.innerHTML = "";

    const ids = Array.isArray(modelIds) ? modelIds.filter((x) => typeof x === "string" && x.trim()) : [];
    const unique = Array.from(new Set(ids));
    unique.sort((a, b) => a.localeCompare(b));

    for (const id of unique) {
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = id;
      modelEl.appendChild(opt);
    }

    const want = (preferred || "").trim();
    if (want && unique.includes(want)) {
      modelEl.value = want;
      return;
    }
    if (prev && unique.includes(prev)) {
      modelEl.value = prev;
      return;
    }
    if (unique.includes("fast")) {
      modelEl.value = "fast";
      return;
    }
    if (unique.length) {
      modelEl.value = unique[0];
    }
  }

  async function loadModels() {
    try {
      const resp = await fetch("/ui/api/models", { method: "GET" });
      const text = await resp.text();
      if (!resp.ok) {
        _setModelOptions(["fast"], "fast");
        addMessage({ role: "system", content: text, meta: `Models HTTP ${resp.status}` });
        return;
      }

      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        _setModelOptions(["fast"], "fast");
        addMessage({ role: "system", content: "Models: invalid JSON" });
        return;
      }

      const data = payload && Array.isArray(payload.data) ? payload.data : [];
      const ids = data.map((m) => m && m.id).filter((x) => typeof x === "string");
      _setModelOptions(ids, "fast");
    } catch (e) {
      _setModelOptions(["fast"], "fast");
      addMessage({ role: "system", content: `Models error: ${String(e)}` });
    }
  }

  async function ensureConversation() {
    const fromStorage = (localStorage.getItem(CONVO_KEY) || "").trim();
    if (fromStorage) {
      conversationId = fromStorage;
      return;
    }

    const resp = await fetch("/ui/api/conversations/new", { method: "POST" });
    const text = await resp.text();
    if (!resp.ok) {
      addMessage({ role: "system", content: text, meta: `Conversation HTTP ${resp.status}` });
      return;
    }
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      addMessage({ role: "system", content: "Conversation: invalid JSON" });
      return;
    }
    const cid = payload && typeof payload.conversation_id === "string" ? payload.conversation_id.trim() : "";
    if (!cid) {
      addMessage({ role: "system", content: "Conversation: missing id" });
      return;
    }
    conversationId = cid;
    localStorage.setItem(CONVO_KEY, cid);
  }

  async function loadConversation() {
    if (!conversationId) return;
    const resp = await fetch(`/ui/api/conversations/${encodeURIComponent(conversationId)}`, { method: "GET" });
    const text = await resp.text();
    if (!resp.ok) {
      addMessage({ role: "system", content: text, meta: `Load convo HTTP ${resp.status}` });
      return;
    }
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      addMessage({ role: "system", content: text, meta: "Load convo OK (non-JSON)" });
      return;
    }

    const msgs = payload && Array.isArray(payload.messages) ? payload.messages : [];
    for (const m of msgs) {
      renderStoredMessage(m);
    }
  }

  async function appendToConversation(message) {
    if (!conversationId) return;
    await fetch(`/ui/api/conversations/${encodeURIComponent(conversationId)}/append`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
  }

  function isLikelyImageRequest(text) {
    const s = String(text || "").trim().toLowerCase();
    if (!s) return false;
    if (s.startsWith("/image ") || s.startsWith("/img ") || s.startsWith("image:")) return true;

    // Very small heuristic: treat explicit "generate an image" / "create an image" as image requests.
    if (s.startsWith("generate an image") || s.startsWith("create an image") || s.startsWith("make an image")) {
      return true;
    }
    return false;
  }

  function extractImagePrompt(text) {
    const raw = String(text || "").trim();
    if (raw.toLowerCase().startsWith("image:")) return raw.slice(6).trim();
    if (raw.toLowerCase().startsWith("/image ")) return raw.slice(7).trim();
    if (raw.toLowerCase().startsWith("/img ")) return raw.slice(5).trim();

    const lowered = raw.toLowerCase();
    for (const prefix of ["generate an image of ", "create an image of ", "make an image of ", "generate an image ", "create an image ", "make an image "]) {
      if (lowered.startsWith(prefix)) return raw.slice(prefix.length).trim();
    }
    return raw;
  }

  async function generateImageFromPrompt(prompt) {
    const resp = await fetch("/ui/api/image", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, size: "1024x1024", n: 1 }),
    });

    const text = await resp.text();
    if (!resp.ok) {
      addMessage({ role: "system", content: text, meta: `Image HTTP ${resp.status}` });
      return;
    }

    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      addMessage({ role: "system", content: text, meta: "Image OK (non-JSON)" });
      return;
    }

    const url = payload?.data?.[0]?.url;
    const b64 = payload?.data?.[0]?.b64_json;
    const mime = payload?._gateway?.mime || "image/png";

    let src = null;
    if (typeof url === "string" && url.trim()) {
      src = url.trim();
    } else if (typeof b64 === "string" && b64.trim()) {
      const b64s = b64.trim();
      src = b64s.startsWith("data:") ? b64s : `data:${mime};base64,${b64s}`;
    }

    const metaBits = [];
    if (payload?._gateway?.backend) metaBits.push(`backend=${payload._gateway.backend}`);
    if (payload?._gateway?.model) metaBits.push(`model=${payload._gateway.model}`);
    if (payload?._gateway?.ui_image_sha256) metaBits.push(`sha=${String(payload._gateway.ui_image_sha256).slice(0, 12)}`);

    if (!src) {
      addMessage({ role: "system", content: JSON.stringify(payload, null, 2), meta: "Image OK (no data)" });
      return;
    }

    await appendToConversation({
      role: "assistant",
      type: "image",
      url: src,
      backend: payload?._gateway?.backend,
      model: payload?._gateway?.model,
      sha256: payload?._gateway?.ui_image_sha256,
      mime: payload?._gateway?.ui_image_mime,
    });

    addMessage({
      role: "assistant",
      meta: metaBits.length ? `Image • ${metaBits.join(" • ")}` : "Image",
      html: `<img class="gen" src="${escapeHtml(src)}" alt="generated" />`,
    });
  }

  async function sendChatMessage(userText) {
    const model = (modelEl.value || "").trim() || "fast";

    history.push({ role: "user", content: userText });
    addMessage({ role: "user", content: userText });

    // Create assistant bubble immediately, then stream into it.
    const assistant = addMessage({ role: "assistant", content: "", meta: "Assistant" });

    setBusy(true);

    try {
      const resp = await fetch("/ui/api/chat_stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model, conversation_id: conversationId, message: userText }),
      });

      const backend = resp.headers.get("x-backend-used") || "";
      const usedModel = resp.headers.get("x-model-used") || "";
      const reason = resp.headers.get("x-router-reason") || "";

      if (!resp.ok) {
        const text = await resp.text();
        assistant.contentEl.textContent = text;
        assistant.metaEl.textContent = `HTTP ${resp.status}`;
        return;
      }

      // Stream SSE from fetch().body.
      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buf = "";
      let full = "";

      function updateMeta(extra) {
        const bits = [];
        if (backend) bits.push(`backend=${backend}`);
        if (usedModel) bits.push(`model=${usedModel}`);
        if (reason) bits.push(`reason=${reason}`);
        if (extra) bits.push(extra);
        assistant.metaEl.textContent = bits.length ? bits.join(" • ") : "Assistant";
      }

      updateMeta("streaming");

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });

        // SSE events are separated by a blank line.
        while (true) {
          const idx = buf.indexOf("\n\n");
          if (idx < 0) break;
          const rawEvent = buf.slice(0, idx);
          buf = buf.slice(idx + 2);

          const lines = rawEvent.split("\n");
          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed.startsWith("data:")) continue;
            const data = trimmed.slice(5).trim();
            if (data === "[DONE]") {
              updateMeta("done");
              continue;
            }

            let evt;
            try {
              evt = JSON.parse(data);
            } catch {
              continue;
            }

            if (!evt || typeof evt !== "object") continue;

            if (evt.type === "route") {
              // Server-emitted route event; prefer that if present.
              const bits = [];
              if (evt.backend) bits.push(`backend=${evt.backend}`);
              if (evt.model) bits.push(`model=${evt.model}`);
              if (evt.reason) bits.push(`reason=${evt.reason}`);
              assistant.metaEl.textContent = bits.join(" • ") || assistant.metaEl.textContent;
              continue;
            }

            if (evt.type === "delta" && typeof evt.delta === "string") {
              full += evt.delta;
              assistant.contentEl.textContent = full;
              scrollToBottom();
              continue;
            }

            if (evt.type === "error") {
              assistant.contentEl.textContent = `${full}\n\n[error]\n${JSON.stringify(evt.error || evt, null, 2)}`;
              updateMeta("error");
              continue;
            }

            if (evt.type === "done") {
              updateMeta("done");
              continue;
            }
          }
        }
      }

      history.push({ role: "assistant", content: full });
    } catch (e) {
      assistant.contentEl.textContent = String(e);
      assistant.metaEl.textContent = "error";
    } finally {
      setBusy(false);
    }
  }

  async function send() {
    const text = String(inputEl.value || "").trim();
    if (!text) return;

    inputEl.value = "";

    const shouldImage = autoImageEl && autoImageEl.checked ? isLikelyImageRequest(text) : false;
    if (shouldImage) {
      const prompt = extractImagePrompt(text);

      history.push({ role: "user", content: text });
      addMessage({ role: "user", content: text });

      await appendToConversation({ role: "user", content: text });
      await generateImageFromPrompt(prompt);
      return;
    }

    await sendChatMessage(text);
  }

  loadModelsEl.addEventListener("click", () => void loadModels());
  sendEl.addEventListener("click", () => void send());
  clearEl.addEventListener("click", () => {
    history = [];
    chatEl.innerHTML = "";
    inputEl.value = "";
    conversationId = "";
    localStorage.removeItem(CONVO_KEY);
  });

  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  });

  // Load models on startup.
  (async () => {
    try {
      await loadModels();
      await ensureConversation();
      await loadConversation();
    } catch (e) {
      addMessage({ role: "system", content: String(e), meta: "Init error" });
    }
  })();
})();
