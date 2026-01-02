(() => {
  const $ = (id) => document.getElementById(id);

  const modelEl = $("model");
  const loadModelsEl = $("loadModels");
  const inputEl = $("input");
  const sendEl = $("send");
  const clearEl = $("clear");
  const outEl = $("out");
  const metaEl = $("meta");

  const imgPromptEl = $("imgPrompt");
  const imgSizeEl = $("imgSize");
  const imgGenerateEl = $("imgGenerate");
  const imgClearEl = $("imgClear");
  const imgOutEl = $("imgOut");
  const imgMetaEl = $("imgMeta");

  function setBusy(busy) {
    sendEl.disabled = busy;
  }

  function setOutput(text) {
    outEl.textContent = text || "";
  }

  function setMeta(text) {
    metaEl.textContent = text || "";
  }

  function setImgOutputHtml(html) {
    imgOutEl.innerHTML = html || "";
  }

  function setImgMeta(text) {
    imgMetaEl.textContent = text || "";
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

  let _modelsTimer = null;
  function _scheduleLoadModels() {
    if (_modelsTimer) {
      clearTimeout(_modelsTimer);
      _modelsTimer = null;
    }
    _modelsTimer = setTimeout(() => {
      _modelsTimer = null;
      void loadModels();
    }, 250);
  }

  async function loadModels() {
    try {
      setMeta("Loading models...");
      const resp = await fetch("/ui/api/models", {
        method: "GET",
      });
      const text = await resp.text();
      if (!resp.ok) {
        _setModelOptions(["fast"], "fast");
        setMeta(`Models: HTTP ${resp.status}`);
        setOutput(text);
        return;
      }

      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        _setModelOptions(["fast"], "fast");
        setMeta("Models: invalid JSON");
        return;
      }

      const data = payload && Array.isArray(payload.data) ? payload.data : [];
      const ids = data.map((m) => m && m.id).filter((x) => typeof x === "string");
      _setModelOptions(ids, "fast");
      setMeta(`Models loaded (${ids.length})`);
    } catch (e) {
      _setModelOptions(["fast"], "fast");
      setMeta(`Models: ${String(e)}`);
    }
  }

  async function send() {
    const model = (modelEl.value || "").trim() || "fast";
    const content = (inputEl.value || "").trim();

    setMeta("");
    if (!content) {
      setOutput("Empty message.");
      return;
    }

    setBusy(true);
    setOutput("...");

    try {
      const resp = await fetch("/ui/api/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model,
          message: content,
        }),
      });

      const text = await resp.text();
      if (!resp.ok) {
        setOutput(text);
        setMeta(`HTTP ${resp.status}`);
        return;
      }

      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        setOutput(text);
        setMeta(`OK${backend ? ` • ${backend}` : ""}${usedModel ? ` • ${usedModel}` : ""}`);
        return;
      }

      const msg = payload?.choices?.[0]?.message?.content;
      setOutput(typeof msg === "string" ? msg : JSON.stringify(payload, null, 2));
      const bits = [];
      const gw = payload?._gateway;
      if (gw?.backend) bits.push(`backend=${gw.backend}`);
      if (gw?.model) bits.push(`model=${gw.model}`);
      if (gw?.reason) bits.push(`reason=${gw.reason}`);
      setMeta(bits.join(" • "));
    } catch (e) {
      setOutput(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function generateImage() {
    const prompt = (imgPromptEl.value || "").trim();
    const size = (imgSizeEl.value || "1024x1024").trim();

    setImgMeta("");
    if (!prompt) {
      setImgOutputHtml("Empty prompt.");
      return;
    }

    imgGenerateEl.disabled = true;
    setImgOutputHtml("...");

    try {
      const resp = await fetch("/ui/api/image", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, size, n: 1 }),
      });

      const text = await resp.text();
      if (!resp.ok) {
        setImgOutputHtml(text);
        setImgMeta(`HTTP ${resp.status}`);
        return;
      }

      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        setImgOutputHtml(text);
        setImgMeta("OK (non-JSON)");
        return;
      }

      const b64 = payload?.data?.[0]?.b64_json;
      const url = payload?.data?.[0]?.url;
      const mime = payload?._gateway?.mime || "image/png";

      if (typeof url === "string" && url.trim()) {
        const src = url.trim();
        setImgOutputHtml(
          `<img src="${src}" alt="generated" style="max-width:100%;height:auto;display:block;border-radius:12px;border:1px solid rgba(231,237,246,0.12)" />`
        );
        setImgMeta(payload?._gateway?.backend ? `backend=${payload._gateway.backend}` : "OK");
        return;
      }

      if (typeof b64 !== "string" || !b64.trim()) {
        setImgOutputHtml(JSON.stringify(payload, null, 2));
        setImgMeta("OK (no image data)");
        return;
      }

      const b64s = b64.trim();
      const src = b64s.startsWith("data:") ? b64s : `data:${mime};base64,${b64s}`;
      setImgOutputHtml(`<img src="${src}" alt="generated" style="max-width:100%;height:auto;display:block;border-radius:12px;border:1px solid rgba(231,237,246,0.12)" />`);
      setImgMeta(payload?._gateway?.backend ? `backend=${payload._gateway.backend}` : "OK");
    } catch (e) {
      setImgOutputHtml(String(e));
    } finally {
      imgGenerateEl.disabled = false;
    }
  }

  sendEl.addEventListener("click", () => void send());
  clearEl.addEventListener("click", () => {
    inputEl.value = "";
    setOutput("");
    setMeta("");
  });

  if (imgClearEl) {
    imgClearEl.addEventListener("click", () => {
      imgPromptEl.value = "";
      setImgOutputHtml("");
      setImgMeta("");
    });
  }

  if (imgGenerateEl) {
    imgGenerateEl.addEventListener("click", () => void generateImage());
  }

  inputEl.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    if (e.shiftKey) return; // allow newline
    // Enter sends
    e.preventDefault();
    void send();
  });

  if (imgPromptEl) {
    imgPromptEl.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      if (e.shiftKey) return;
      e.preventDefault();
      void generateImage();
    });
  }

  if (loadModelsEl) {
    loadModelsEl.addEventListener("click", () => void loadModels());
  }

  setOutput("Ready.");
  setMeta("Ctrl+Enter to send");
  void loadModels();
})();
