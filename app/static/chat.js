(() => {
  const $ = (id) => document.getElementById(id);

  const tokenEl = $("token");
  const modelEl = $("model");
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

  async function send() {
    const token = (tokenEl.value || "").trim();
    const model = (modelEl.value || "").trim() || "fast";
    const content = (inputEl.value || "").trim();

    setMeta("");
    if (!token) {
      setOutput("Missing bearer token.");
      return;
    }
    if (!content) {
      setOutput("Empty message.");
      return;
    }

    setBusy(true);
    setOutput("...");

    try {
      const resp = await fetch("/v1/chat/completions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          model,
          stream: false,
          messages: [{ role: "user", content }],
        }),
      });

      const backend = resp.headers.get("x-backend-used") || "";
      const usedModel = resp.headers.get("x-model-used") || "";
      const reason = resp.headers.get("x-router-reason") || "";

      const text = await resp.text();
      if (!resp.ok) {
        setOutput(text);
        setMeta(`HTTP ${resp.status}${backend ? ` • ${backend}` : ""}${usedModel ? ` • ${usedModel}` : ""}`);
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
      if (backend) bits.push(`backend=${backend}`);
      if (usedModel) bits.push(`model=${usedModel}`);
      if (reason) bits.push(`reason=${reason}`);
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

  setOutput("Ready.");
  setMeta("Ctrl+Enter to send");
})();
