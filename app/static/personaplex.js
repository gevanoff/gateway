(() => {
  const $ = (id) => document.getElementById(id);

  const personaEl = $("persona");
  const messageEl = $("message");
  const temperatureEl = $("temperature");
  const maxTokensEl = $("maxTokens");
  const sendEl = $("send");
  const statusEl = $("status");
  const metaEl = $("meta");
  const responseEl = $("response");

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.className = isError ? "hint error" : "hint";
  }

  function setMeta(text) {
    metaEl.textContent = text || "";
  }

  function clearResponse() {
    responseEl.textContent = "";
  }

  function buildPayload() {
    const persona = String(personaEl.value || "").trim();
    const message = String(messageEl.value || "").trim();
    if (!message) throw new Error("message is required");

    const tempRaw = parseFloat(String(temperatureEl.value || "0.7"));
    const temperature = Number.isFinite(tempRaw) ? Math.min(2, Math.max(0, tempRaw)) : 0.7;

    const maxTokensRaw = parseInt(String(maxTokensEl.value || "512"), 10);
    const max_tokens = Number.isFinite(maxTokensRaw) ? Math.max(1, maxTokensRaw) : 512;

    const messages = [];
    if (persona) messages.push({ role: "system", content: persona });
    messages.push({ role: "user", content: message });

    return { messages, temperature, max_tokens };
  }

  async function handleSend() {
    setStatus("", false);
    setMeta("");
    clearResponse();

    let payload;
    try {
      payload = buildPayload();
    } catch (e) {
      setStatus(String(e?.message || e), true);
      return;
    }

    sendEl.disabled = true;
    setStatus("Sending...", false);

    try {
      const resp = await fetch("/ui/api/personaplex/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const text = await resp.text();
      if (!resp.ok) {
        setStatus(text, true);
        return;
      }

      let data;
      try {
        data = JSON.parse(text);
      } catch {
        responseEl.textContent = text;
        return;
      }

      const choice = data?.choices && data.choices[0];
      const content = choice?.message?.content || choice?.text || JSON.stringify(data, null, 2);
      responseEl.textContent = String(content || "");
      setMeta(`Model: ${data?.model || "personaplex"}`);
    } catch (e) {
      setStatus(String(e), true);
    } finally {
      sendEl.disabled = false;
      if (!statusEl.textContent) setStatus("Ready", false);
    }
  }

  if (sendEl) sendEl.addEventListener("click", handleSend);
})();
