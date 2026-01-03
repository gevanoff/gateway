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
  const imgStepsEl = $("imgSteps");
  const imgSeedEl = $("imgSeed");
  const imgAutoGuidanceEl = $("imgAutoGuidance");
  const imgNegEl = $("imgNeg");
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

    const stepsRaw = imgStepsEl ? String(imgStepsEl.value || "").trim() : "";
    const seedRaw = imgSeedEl ? String(imgSeedEl.value || "").trim() : "";
    const negative_prompt = imgNegEl ? String(imgNegEl.value || "").trim() : "";

    setImgMeta("");
    if (!prompt) {
      setImgOutputHtml("Empty prompt.");
      return;
    }

    imgGenerateEl.disabled = true;
    setImgOutputHtml("...");
    if (imgAutoGuidanceEl) imgAutoGuidanceEl.textContent = "Guidance: (auto)";

    try {
      const body = { prompt, size, n: 1 };

      if (stepsRaw) {
        const steps = Number(stepsRaw);
        if (Number.isFinite(steps) && steps > 0) body.steps = Math.floor(steps);
      }
      if (seedRaw) {
        const seed = Number(seedRaw);
        if (Number.isFinite(seed)) body.seed = Math.floor(seed);
      }
      if (negative_prompt) {
        body.negative_prompt = negative_prompt;
      }

      const resp = await fetch("/ui/api/image", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
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

      const gwGuidance = payload?._gateway?.guidance_scale;
      const gwGuidanceAuto = payload?._gateway?.guidance_auto;
      if (imgAutoGuidanceEl) {
        if (typeof gwGuidance === "number" && Number.isFinite(gwGuidance)) {
          imgAutoGuidanceEl.textContent = `Guidance: ${gwGuidance}${gwGuidanceAuto ? " (auto)" : ""}`;
        } else {
          imgAutoGuidanceEl.textContent = "Guidance: (not provided)";
        }
      }

      const req = payload?._gateway?.request;
      const metaBits = [];
      if (payload?._gateway?.backend) metaBits.push(`backend=${payload._gateway.backend}`);
      if (payload?._gateway?.model) metaBits.push(`model=${payload._gateway.model}`);
      if (req && typeof req === "object") {
        if (req.size) metaBits.push(`size=${req.size}`);
        if (req.steps !== undefined) metaBits.push(`steps=${req.steps}`);
        if (req.num_inference_steps !== undefined) metaBits.push(`nis=${req.num_inference_steps}`);
        if (req.seed !== undefined) metaBits.push(`seed=${req.seed}`);
        if (req.negative_prompt) metaBits.push(`neg=yes`);
        if (req.guidance_scale !== undefined) metaBits.push(`gs=${req.guidance_scale}`);
        if (req.cfg_scale !== undefined) metaBits.push(`cfg=${req.cfg_scale}`);
      }

      const upstream = payload?._gateway?.upstream;
      if (upstream && typeof upstream === "object") {
        if (upstream.seed !== undefined) metaBits.push(`up_seed=${upstream.seed}`);
        if (upstream.steps !== undefined) metaBits.push(`up_steps=${upstream.steps}`);
        if (upstream.num_inference_steps !== undefined) metaBits.push(`up_nis=${upstream.num_inference_steps}`);
        if (upstream.guidance_scale !== undefined) metaBits.push(`up_gs=${upstream.guidance_scale}`);
        if (upstream.cfg_scale !== undefined) metaBits.push(`up_cfg=${upstream.cfg_scale}`);
      }

      const uiSha = payload?._gateway?.ui_image_sha256;
      const uiMime = payload?._gateway?.ui_image_mime;
      if (typeof uiMime === "string" && uiMime.trim()) metaBits.push(`mime=${uiMime.trim()}`);
      if (typeof uiSha === "string" && uiSha.trim()) metaBits.push(`sha=${uiSha.trim().slice(0, 12)}`);

      if (typeof url === "string" && url.trim()) {
        const src = url.trim();
        setImgOutputHtml(
          `<img src="${src}" alt="generated" style="max-width:100%;height:auto;display:block;border-radius:12px;border:1px solid rgba(231,237,246,0.12)" />`
        );
        setImgMeta(metaBits.length ? metaBits.join(" • ") : "OK");
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
      setImgMeta(metaBits.length ? metaBits.join(" • ") : "OK");
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
      if (imgStepsEl) imgStepsEl.value = "";
      if (imgSeedEl) imgSeedEl.value = "";
      if (imgNegEl) imgNegEl.value = "";
      if (imgAutoGuidanceEl) imgAutoGuidanceEl.textContent = "Guidance: (auto)";
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
