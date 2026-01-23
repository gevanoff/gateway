(() => {
  const $ = (id) => document.getElementById(id);

  const promptEl = $("prompt");
  const durationEl = $("duration");
  const resolutionEl = $("resolution");
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
    const prompt = String(promptEl.value || "").trim();
    if (!prompt) throw new Error("prompt is required");

    const duration = Math.max(1, Math.min(30, parseInt(String(durationEl.value || "6"), 10) || 6));
    const resolution = String(resolutionEl.value || "720p").trim();
    return { prompt, duration, resolution };
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

    setStatus("Video generation stub: no backend configured yet.", false);
    setMeta(`Preview request → ${JSON.stringify(preview)}`);
  }

  generateEl.addEventListener("click", handleGenerate);
})();
