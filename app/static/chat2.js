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
  const AUTO_IMAGE_KEY = "gw_ui2_auto_image";
  let conversationId = "";

  function loadAutoImageSetting() {
    if (!autoImageEl) return;
    const raw = (localStorage.getItem(AUTO_IMAGE_KEY) || "").trim().toLowerCase();
    if (raw === "1" || raw === "true" || raw === "yes" || raw === "on") {
      autoImageEl.checked = true;
      return;
    }
    if (raw === "0" || raw === "false" || raw === "no" || raw === "off") {
      autoImageEl.checked = false;
      return;
    }
    // Default: off (avoid accidental image generation).
    autoImageEl.checked = false;
  }

  function saveAutoImageSetting() {
    if (!autoImageEl) return;
    localStorage.setItem(AUTO_IMAGE_KEY, autoImageEl.checked ? "1" : "0");
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function buildImageUiUrl({ prompt, image_request }) {
    const qs = new URLSearchParams();
    const p = typeof prompt === "string" ? prompt.trim() : "";
    if (p) qs.set("prompt", p);

    const req = image_request && typeof image_request === "object" ? image_request : {};
    const add = (k, v) => {
      if (v === undefined || v === null) return;
      const s = String(v).trim();
      if (!s) return;
      qs.set(k, s);
    };

    add("size", req.size);
    add("n", req.n);
    add("model", req.model);
    add("seed", req.seed);
    add("steps", req.steps);
    add("guidance_scale", req.guidance_scale);
    add("negative_prompt", req.negative_prompt);

    const q = qs.toString();
    return q ? `/ui/image?${q}` : "/ui/image";
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

      const link = buildImageUiUrl({ prompt: m.prompt, image_request: m.image_request });

      addMessage({
        role: role === "user" ? "user" : "assistant",
        meta: metaBits.length ? `Image • ${metaBits.join(" • ")}` : "Image",
        html: `<img class="gen" src="${escapeHtml(m.url.trim())}" alt="generated" />\n<div style="margin-top:8px"><a href="${escapeHtml(link)}">Open in Image UI</a></div>`,
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

  // Progress utilities for inline generation (simulated incremental progress)
  function _createProgressEl() {
    const wrap = document.createElement('div');
    wrap.className = 'progress-wrapper';
    const bar = document.createElement('div');
    bar.className = 'progress';
    const inner = document.createElement('div');
    inner.className = 'progress-inner';
    bar.appendChild(inner);
    const txt = document.createElement('div');
    txt.className = 'progress-text';
    txt.textContent = 'Processing...';
    wrap.appendChild(bar);
    wrap.appendChild(txt);
    return {wrap, inner, txt};
  }

  function _startProgress(inner, txt) {
    inner.classList.add('indeterminate');
    txt.textContent = 'Processing...';
    return () => {
      inner.classList.remove('indeterminate');
      txt.textContent = '';
    };
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

    // Heuristic (opt-in via checkbox): require explicit visual intent.
    // Keep conservative to avoid false positives like "draw conclusions".
    const hasImageWord = /\b(image|picture|photo|photograph|art|artwork|illustration|drawing|sketch|render|logo|icon|avatar|wallpaper|poster|banner)\b/.test(s);
    const hasMakeVerb = /\b(generate|create|make|draw|paint|illustrate|render|design)\b/.test(s);
    if (hasImageWord && hasMakeVerb) return true;

    // Common explicit patterns.
    if (/^(generate|create|make) (me )?(an |a )?(image|picture|photo|illustration|drawing|sketch|logo|icon|avatar|wallpaper|poster|banner)\b/.test(s)) {
      return true;
    }
    if (/^draw (me |us )?(an |a )?\b/.test(s)) {
      return true;
    }
    if (/^illustrate\b/.test(s)) {
      return true;
    }
    return false;
  }

  function isLikelyMusicRequest(text) {
    const s = String(text || "").trim().toLowerCase();
    if (!s) return false;
    // Explicit slash or prefix patterns
    if (s.startsWith("/music ") || s.startsWith("music:") || s.startsWith("/song ")) return true;

    // Require both a music keyword and an intent verb to avoid false positives.
    const hasMusicWord = /\b(music|song|tune|melody|track|beat|jam|riff)\b/.test(s);
    const hasMakeVerb = /\b(generate|create|make|compose|write|produce)\b/.test(s);
    if (hasMusicWord && hasMakeVerb) return true;

    // Some explicit phrasing.
    if (/^(generate|create|compose|make) (me )?(a |an )?\b/.test(s) && /\b(music|song|tune|melody|track)\b/.test(s)) return true;

    return false;
  }

  function extractMusicPrompt(text) {
    const raw = String(text || "").trim();
    if (raw.toLowerCase().startsWith("music:")) return raw.slice(6).trim();
    if (raw.toLowerCase().startsWith("/music ")) return raw.slice(7).trim();
    if (raw.toLowerCase().startsWith("/song ")) return raw.slice(6).trim();
    return raw;
  }

  async function generateMusicFromPrompt(prompt, durationSec = 15) {
    if (!prompt) return;

    // Append user message and perform request.
    history.push({ role: "user", content: prompt });
    addMessage({ role: "user", content: prompt });

    const assistant = addMessage({ role: "assistant", content: "", meta: "Assistant" });
    // show progress bar
    const {wrap, inner, txt} = _createProgressEl();
    assistant.contentEl.appendChild(wrap);
    const stopProgress = _startProgress(inner, txt);

    try {
      const body = { prompt, duration: durationSec };
      const resp = await fetch("/ui/api/music", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      const text = await resp.text();
      if (!resp.ok) {
        try { stopProgress(); } catch (e) {}
        try { wrap.remove(); } catch (e) {}
        assistant.contentEl.textContent = text;
        assistant.metaEl.textContent = `Music HTTP ${resp.status}`;
        return;
      }

      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        assistant.contentEl.textContent = String(text);
        return;
      }

      const url = typeof payload?.audio_url === "string" ? payload.audio_url.trim() : "";
      const metaBits = [];
      if (payload?._gateway?.backend) metaBits.push(`backend=${payload._gateway.backend}`);
      if (payload?._gateway?.backend_class) metaBits.push(`class=${payload._gateway.backend_class}`);

      await appendToConversation({ role: "assistant", type: "audio", url, prompt, backend: payload?._gateway?.backend, model: payload?._gateway?.upstream_model || payload?._gateway?.model });

      // Stop and remove progress, then render inline audio
      try { stopProgress(); } catch (e) {}
      try { wrap.remove(); } catch (e) {}
      assistant.contentEl.innerHTML = `<audio controls src="${escapeHtml(url)}"></audio>`;
      assistant.metaEl.textContent = metaBits.length ? `Audio • ${metaBits.join(" • ")}` : "Audio";
      history.push({ role: "assistant", content: `audio:${url}` });
    } catch (e) {
      try { stopProgress(); } catch (e2) {}
      try { wrap.remove(); } catch (e2) {}
      assistant.contentEl.textContent = String(e);
      assistant.metaEl.textContent = "error";
    }
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
    // Create assistant bubble and show progress
    const assistant = addMessage({ role: "assistant", content: "", meta: "Assistant" });
    const {wrap, inner, txt} = _createProgressEl();
    assistant.contentEl.appendChild(wrap);
    const stopProgress = _startProgress(inner, txt);

    const imageRequest = { prompt, size: "1024x1024", n: 1 };
    const resp = await fetch("/ui/api/image", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(imageRequest),
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
      try { stopProgress(); } catch (e) {}
      try { wrap.remove(); } catch (e) {}
      addMessage({ role: "system", content: JSON.stringify(payload, null, 2), meta: "Image OK (no data)" });
      return;
    }

    await appendToConversation({
      role: "assistant",
      type: "image",
      url: src,
      prompt: prompt,
      image_request: imageRequest,
      backend: payload?._gateway?.backend,
      model: payload?._gateway?.model,
      sha256: payload?._gateway?.ui_image_sha256,
      mime: payload?._gateway?.ui_image_mime,
    });

    const link = buildImageUiUrl({ prompt, image_request: imageRequest });

    // stop & remove progress
    try { stopProgress(); } catch (e) {}
    try { wrap.remove(); } catch (e) {}

    addMessage({
      role: "assistant",
      meta: metaBits.length ? `Image • ${metaBits.join(" • ")}` : "Image",
      html: `<img class="gen" src="${escapeHtml(src)}" alt="generated" />\n<div style="margin-top:8px"><a href="${escapeHtml(link)}">Open in Image UI</a></div>`,
    });
  }

  async function sendChatMessage(userText) {
    const model = (modelEl.value || "").trim() || "fast";

    history.push({ role: "user", content: userText });
    addMessage({ role: "user", content: userText });

    // Create assistant bubble immediately, then stream into it.
    const assistant = addMessage({ role: "assistant", content: "", meta: "Assistant" });
    assistant.contentEl.textContent = "";
    const thinkingLine = document.createElement("div");
    thinkingLine.className = "thinking-line";
    thinkingLine.style.display = "none";
    const contentText = document.createElement("div");
    contentText.className = "content-text";
    assistant.contentEl.appendChild(thinkingLine);
    assistant.contentEl.appendChild(contentText);

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
      let hasContent = false;
      let thinkingShown = false;
      let thinkingBuffer = "";
      let isOllama = backend === "ollama";

      if (!resp.ok) {
        const text = await resp.text();
        contentText.textContent = text;
        assistant.metaEl.textContent = `HTTP ${resp.status}`;
        return;
      }

      const setThinking = (text) => {
        if (!text) {
          thinkingLine.textContent = "";
          thinkingLine.style.display = "none";
          return;
        }
        thinkingLine.textContent = text;
        thinkingLine.style.display = "block";
        scrollToBottom();
      };

      const showThinking = () => {
        if (hasContent || thinkingShown || !isOllama) return;
        setThinking("Thinking…");
        thinkingShown = true;
      };

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
      showThinking();

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
              if (evt.backend) {
                isOllama = evt.backend === "ollama";
                showThinking();
              }
              continue;
            }

            if (evt.type === "thinking" && typeof evt.thinking === "string") {
              thinkingBuffer += evt.thinking;
              setThinking(`Thinking: ${thinkingBuffer}`);
              thinkingShown = true;
              continue;
            }

            if (evt.type === "delta" && typeof evt.delta === "string") {
              if (!hasContent) {
                hasContent = true;
                if (thinkingShown) setThinking("");
              }
              full += evt.delta;
              contentText.textContent = full;
              scrollToBottom();
              continue;
            }

            if (evt.type === "error") {
              contentText.textContent = `${full}\n\n[error]\n${JSON.stringify(evt.error || evt, null, 2)}`;
              updateMeta("error");
              continue;
            }

            if (evt.type === "done") {
              if (!hasContent && thinkingShown) {
                setThinking("");
              }
              updateMeta("done");
              continue;
            }
          }
        }
      }

      history.push({ role: "assistant", content: full });
    } catch (e) {
      contentText.textContent = String(e);
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

    // Music handling: explicit /music command performs the generation inline; auto-detect opens the Music UI.
    const shouldMusicAuto = autoMusicEl && autoMusicEl.checked ? isLikelyMusicRequest(text) : false;
    const isExplicitMusic = (String(text || "").trim().toLowerCase().startsWith("/music ") || String(text || "").trim().toLowerCase().startsWith("music:") || String(text || "").trim().toLowerCase().startsWith("/song "));

    if (isExplicitMusic) {
      const prompt = extractMusicPrompt(text);
      await generateMusicFromPrompt(prompt, 15);
      return;
    }

    if (shouldMusicAuto) {
      // Open the Music UI prefilled in a new tab so user can refine.
      const prompt = encodeURIComponent(extractMusicPrompt(text));
      const url = prompt ? `/ui/music?prompt=${prompt}` : `/ui/music`;
      window.open(url, "_blank");

      history.push({ role: "user", content: text });
      addMessage({ role: "system", content: "Opened Music UI" });
      await appendToConversation({ role: "user", content: text });
      return;
    }

    await sendChatMessage(text);
  }

  loadModelsEl.addEventListener("click", () => void loadModels());
  sendEl.addEventListener("click", () => void send());
  if (autoImageEl) {
    autoImageEl.addEventListener("change", () => saveAutoImageSetting());
  }
  // Auto-detect music setting
  const autoMusicEl = $("autoMusic");
  function loadAutoMusicSetting() {
    if (!autoMusicEl) return;
    const raw = (localStorage.getItem("gw_ui2_auto_music") || "").trim().toLowerCase();
    if (raw === "1" || raw === "true" || raw === "yes" || raw === "on") {
      autoMusicEl.checked = true;
      return;
    }
    autoMusicEl.checked = false;
  }
  function saveAutoMusicSetting() {
    if (!autoMusicEl) return;
    localStorage.setItem("gw_ui2_auto_music", autoMusicEl.checked ? "1" : "0");
  }
  if (autoMusicEl) {
    autoMusicEl.addEventListener("change", () => saveAutoMusicSetting());
  }

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
      loadAutoImageSetting();
      await loadModels();
      await ensureConversation();
      await loadConversation();
    } catch (e) {
      addMessage({ role: "system", content: String(e), meta: "Init error" });
    }
  })();
})();
