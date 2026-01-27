(() => {
  const $ = (id) => document.getElementById(id);

  const modelEl = $("model");
  const loadModelsEl = $("loadModels");
  const inputEl = $("input");
  const sendEl = $("send");
  const clearEl = $("clear");
  const outEl = $("out");
  const metaEl = $("meta");

  function setOutput(text) {
    try {
      if (outEl) outEl.textContent = String(text || "");
    } catch (e) {}
  }

  function setMeta(text) {
    try {
      if (metaEl) metaEl.textContent = String(text || "");
    } catch (e) {}
  }

  const imgPromptEl = $("imgPrompt");
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
    let conversationResetting = false;

    function handle401(resp) {
      if (resp && resp.status === 401) {
        const back = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = `/ui/login?next=${back}`;
        return true;
      }
      return false;
    }

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
      if (!chatEl) return;
      chatEl.scrollTop = chatEl.scrollHeight;
    }

    function addMessage({ role, content, meta, html }) {
      if (!chatEl) return { wrap: null, metaEl: null, contentEl: null };
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

    function formatTime(seconds) {
      const total = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
      const mins = Math.floor(total / 60);
      const secs = Math.floor(total % 60);
      return `${mins}:${secs.toString().padStart(2, "0")}`;
    }

    function createAudioPlayer(url) {
      const wrap = document.createElement("div");
      wrap.className = "audio-card";

      const audio = document.createElement("audio");
      audio.src = url;
      audio.preload = "metadata";

      const controls = document.createElement("div");
      controls.className = "audio-controls";

      const meta = document.createElement("div");
      meta.className = "audio-meta";
      const currentEl = document.createElement("span");
      currentEl.textContent = "0:00";
      const totalEl = document.createElement("span");
      totalEl.textContent = "0:00";
      meta.appendChild(currentEl);
      meta.appendChild(totalEl);

      const sliders = document.createElement("div");
      sliders.className = "audio-sliders";

      const seek = document.createElement("input");
      seek.type = "range";
      seek.min = "0";
      seek.max = "0";
      seek.value = "0";
      seek.step = "0.01";

      const volume = document.createElement("input");
      volume.type = "range";
      volume.min = "0";
      volume.max = "1";
      volume.step = "0.01";
      volume.value = String(audio.volume);
      volume.title = "Volume";

      sliders.appendChild(seek);
      sliders.appendChild(volume);

      controls.appendChild(meta);
      controls.appendChild(sliders);

      wrap.appendChild(audio);
      wrap.appendChild(controls);

      audio.addEventListener("loadedmetadata", () => {
        if (Number.isFinite(audio.duration)) {
          seek.max = String(audio.duration);
          totalEl.textContent = formatTime(audio.duration);
        }
      });

      audio.addEventListener("timeupdate", () => {
        currentEl.textContent = formatTime(audio.currentTime);
        if (!seek.matches(":active")) {
          seek.value = String(audio.currentTime);
        }
      });

      seek.addEventListener("input", () => {
        audio.currentTime = Number(seek.value);
      });

      volume.addEventListener("input", () => {
        audio.volume = Number(volume.value);
      });

      return wrap;
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
      if (sendEl) sendEl.disabled = busy;
      if (inputEl) inputEl.disabled = busy;
      if (modelEl) modelEl.disabled = busy;
      if (loadModelsEl) loadModelsEl.disabled = busy;
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
        const resp = await fetch("/ui/api/models", { method: "GET", credentials: "same-origin" });
        const text = await resp.text();
        if (handle401(resp)) return;
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

      const resp = await fetch("/ui/api/conversations/new", { method: "POST", credentials: "same-origin" });
      const text = await resp.text();
      if (handle401(resp)) return;
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

    async function resetConversationId(reason) {
      if (conversationResetting) return;
      conversationResetting = true;
      try {
        localStorage.removeItem(CONVO_KEY);
        conversationId = "";
        if (reason) {
          addMessage({ role: "system", content: reason });
        }
        await ensureConversation();
      } finally {
        conversationResetting = false;
      }
    }

    async function loadConversation() {
      if (!conversationId) return;
      const resp = await fetch(`/ui/api/conversations/${encodeURIComponent(conversationId)}`, { method: "GET", credentials: "same-origin" });
      const text = await resp.text();
      if (handle401(resp)) return;
      if (!resp.ok) {
        if (resp.status === 404) {
          await resetConversationId("Conversation expired or missing. Starting a new one.");
          return;
        }
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
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
    }

    function isLikelyImageRequest(text) {
      const s = String(text || "").trim().toLowerCase();
      if (!s) return false;
      if (s.startsWith("/image ") || s.startsWith("/img ") || s.startsWith("image:")) return true;

      const hasImageWord = /\b(image|picture|photo|photograph|art|artwork|illustration|drawing|sketch|render|logo|icon|avatar|wallpaper|poster|banner)\b/.test(s);
      const hasMakeVerb = /\b(generate|create|make|draw|paint|illustrate|render|design)\b/.test(s);
      if (hasImageWord && hasMakeVerb) return true;

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
      if (s.startsWith("/music ") || s.startsWith("music:") || s.startsWith("/song ")) return true;

      const hasMusicWord = /\b(music|song|tune|melody|track|beat|jam|riff)\b/.test(s);
      const hasMakeVerb = /\b(generate|create|make|compose|write|produce)\b/.test(s);
      if (hasMusicWord && hasMakeVerb) return true;

      if (/^(generate|create|compose|make) (me )?(a |an )?\b/.test(s) && /\b(music|song|tune|melody|track)\b/.test(s)) return true;
      return false;
    }

    async function sendChatMessage(userText) {
      const model = (modelEl.value || "").trim() || "fast";

      history.push({ role: "user", content: userText });
      addMessage({ role: "user", content: userText });

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
        const sendRequest = () =>
          fetch("/ui/api/chat_stream", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model, conversation_id: conversationId, message: userText }),
          });

        let resp = await sendRequest();
        if (resp.status === 404 && conversationId) {
          await resetConversationId("Conversation expired or missing. Retrying with a new one.");
          resp = await sendRequest();
        }

        if (handle401(resp)) return;

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
        addMessage({ role: "system", content: String(e) });
      } finally {
        setBusy(false);
      }
    }

    async function generateMusic(body) {
      try {
        const resp = await fetch("/ui/api/music", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const text = await resp.text();
        if (handle401(resp)) return;
        if (!resp.ok) {
          addMessage({ role: "system", content: text, meta: `Music HTTP ${resp.status}` });
          return;
        }
        try {
          const payload = JSON.parse(text);
          addMessage({ role: "system", content: `Music response: ${JSON.stringify(payload)}` });
        } catch {
          addMessage({ role: "system", content: text });
        }
      } catch (e) {
        addMessage({ role: "system", content: String(e) });
      }
    }

    async function generateImage(prompt, image_request) {
      const body = { prompt, ...image_request };
      try {
        const resp = await fetch("/ui/api/image", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const text = await resp.text();
        if (handle401(resp)) return;
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
        const b64 = payload?.data?.[0]?.b64_json;
        const url = payload?.data?.[0]?.url;
        if (typeof url === "string" && url.trim()) {
          addMessage({ role: "assistant", html: `<img class="gen" src="${escapeHtml(url.trim())}" alt="generated" />` });
          return;
        }
        if (typeof b64 === "string" && b64.trim()) {
          const src = b64.trim().startsWith("data:") ? b64.trim() : `data:${payload?._gateway?.mime||'image/png'};base64,${b64.trim()}`;
          addMessage({ role: "assistant", html: `<img class="gen" src="${escapeHtml(src)}" alt="generated" />` });
          return;
        }
        addMessage({ role: "system", content: JSON.stringify(payload, null, 2) });
      } catch (e) {
        addMessage({ role: "system", content: String(e) });
      }
    }

    document.addEventListener('DOMContentLoaded', () => {
      if (!chatEl) return;
      loadAutoImageSetting();
      void loadModels();
      (async () => { await ensureConversation(); await loadConversation(); })();
      if (sendEl) sendEl.addEventListener('click', async () => {
        const text = (inputEl.value || '').trim();
        if (!text) return;
        inputEl.value = '';
        if (isLikelyImageRequest(text) && (autoImageEl && autoImageEl.checked)) {
          await generateImage(text, {});
          return;
        }
        await sendChatMessage(text);
      });
      if (clearEl) clearEl.addEventListener('click', () => { if (inputEl) inputEl.value = ''; });
    });
  })();
  setOutput("Ready.");
  setMeta("Ctrl+Enter to send");
  void loadModels();
})();
