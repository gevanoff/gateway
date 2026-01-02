(() => {
  const $ = (id) => document.getElementById(id);

  const modelEl = $("model");
  const loadModelsEl = $("loadModels");
  const inputEl = $("input");
  const sendEl = $("send");
  const clearEl = $("clear");
  const outEl = $("out");
  const metaEl = $("meta");

  function setBusy(busy) {
    sendEl.disabled = busy;
  }

  function setOutput(text) {
    outEl.textContent = text || "";
  }

  function setMeta(text) {
    metaEl.textContent = text || "";
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

  sendEl.addEventListener("click", () => void send());
  clearEl.addEventListener("click", () => {
    inputEl.value = "";
    setOutput("");
    setMeta("");
  });

  inputEl.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      void send();
    }
  });

  if (loadModelsEl) {
    loadModelsEl.addEventListener("click", () => void loadModels());
  }

  setOutput("Ready.");
  setMeta("Ctrl+Enter to send");
  void loadModels();
})();
