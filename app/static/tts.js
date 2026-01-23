(() => {
  const $ = (id) => document.getElementById(id);

  const textEl = $("text");
  const voiceEl = $("voice");
  const speedEl = $("speed");
  const generateEl = $("generate");
  const statusEl = $("status");
  const metaEl = $("meta");

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.className = isError ? "hint error" : "hint";
  }

  function setMeta(text) {
    metaEl.textContent = text || "";
  }

  function buildRequestPreview() {
    const text = String(textEl.value || "").trim();
    if (!text) throw new Error("text is required");

    const voice = String(voiceEl.value || "").trim();
    const speedRaw = parseFloat(String(speedEl.value || "1"));
    const speed = Number.isFinite(speedRaw) ? Math.min(2, Math.max(0.5, speedRaw)) : 1;
    return { text, voice, speed };
  }

  function handleGenerate() {
    setStatus("", false);
    setMeta("");

    let preview;
    try {
      preview = buildRequestPreview();
    } catch (e) {
      setStatus(String(e?.message || e), true);
      return;
    }

    setStatus("Text-to-speech stub: no backend configured yet.", false);
    setMeta(`Preview request → ${JSON.stringify(preview)}`);
  }

  generateEl.addEventListener("click", handleGenerate);
})();
