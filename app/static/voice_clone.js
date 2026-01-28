(() => {
  const $ = (id) => document.getElementById(id);

  const textEl = $("text");
  const backendEl = $("backend");
  const promptAudioEl = $("promptAudio");
  const rmsEl = $("rms");
  const durationEl = $("duration");
  const numStepsEl = $("numSteps");
  const tShiftEl = $("tShift");
  const speedEl = $("speed");
  const returnSmoothEl = $("returnSmooth");
  const generateEl = $("generate");
  const statusEl = $("status");
  const metaEl = $("meta");
  const playerEl = $("player");

  let activeObjectUrl = null;

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.className = isError ? "hint error" : "hint";
  }

  function setMeta(text) {
    metaEl.textContent = text || "";
  }

  function clearPlayer() {
    if (activeObjectUrl) {
      try { URL.revokeObjectURL(activeObjectUrl); } catch (e) {}
      activeObjectUrl = null;
    }
    if (playerEl) playerEl.innerHTML = "";
  }

  function renderAudio(url) {
    if (!url || !playerEl) return;
    const wrapper = document.createElement("div");
    wrapper.className = "audio-card";
    const audio = document.createElement("audio");
    audio.src = url;
    audio.preload = "metadata";
    audio.controls = true;
    wrapper.appendChild(audio);
    playerEl.appendChild(wrapper);
  }

  async function loadBackends() {
    if (!backendEl) return;
    try {
      const resp = await fetch('/ui/api/tts/backends', { method: 'GET', credentials: 'same-origin' });
      if (!resp.ok) return;
      const payload = await resp.json();
      const list = Array.isArray(payload?.available_backends) ? payload.available_backends : [];
      backendEl.innerHTML = '<option value="">(default)</option>';
      for (const item of list) {
        const val = item?.backend_class;
        if (!val) continue;
        const opt = document.createElement('option');
        opt.value = val;
        opt.textContent = item?.description ? `${val} — ${item.description}` : String(val);
        backendEl.appendChild(opt);
      }
    } catch (e) {}
  }

  function buildFormData() {
    const text = String(textEl.value || "").trim();
    if (!text) throw new Error("text is required");

    const file = promptAudioEl?.files && promptAudioEl.files[0];
    if (!file) throw new Error("prompt audio file is required");

    const fd = new FormData();
    fd.append("text", text);
    fd.append("prompt_audio", file, file.name);

    const backendClass = String(backendEl?.value || "").trim();
    if (backendClass) fd.append("backend_class", backendClass);

    const rms = String(rmsEl?.value || "").trim();
    if (rms) fd.append("rms", rms);
    const duration = String(durationEl?.value || "").trim();
    if (duration) fd.append("duration", duration);
    const numSteps = String(numStepsEl?.value || "").trim();
    if (numSteps) fd.append("num_steps", numSteps);
    const tShift = String(tShiftEl?.value || "").trim();
    if (tShift) fd.append("t_shift", tShift);
    const speed = String(speedEl?.value || "").trim();
    if (speed) fd.append("speed", speed);
    const returnSmooth = String(returnSmoothEl?.value || "").trim();
    if (returnSmooth) fd.append("return_smooth", returnSmooth);

    return fd;
  }

  async function handleGenerate() {
    setStatus("", false);
    setMeta("");
    clearPlayer();

    let formData;
    try {
      formData = buildFormData();
    } catch (e) {
      setStatus(String(e?.message || e), true);
      return;
    }

    generateEl.disabled = true;
    setStatus("Generating...", false);

    try {
      const resp = await fetch('/ui/api/tts/clone', {
        method: 'POST',
        credentials: 'same-origin',
        body: formData,
      });

      const contentType = resp.headers.get('content-type') || '';
      if (!resp.ok) {
        const err = await resp.text();
        setStatus(err || `HTTP ${resp.status}`, true);
        return;
      }

      if (contentType.includes('application/json')) {
        const payload = await resp.json();
        const raw = payload?.audio_base64 || payload?.audio || payload?.audio_data;
        if (raw) {
          let b64 = String(raw || "");
          if (b64.startsWith('data:')) {
            renderAudio(b64);
          } else {
            const binary = atob(b64);
            const len = binary.length;
            const bytes = new Uint8Array(len);
            for (let i = 0; i < len; i += 1) bytes[i] = binary.charCodeAt(i);
            const blob = new Blob([bytes], { type: payload?.content_type || 'audio/wav' });
            const url = URL.createObjectURL(blob);
            activeObjectUrl = url;
            renderAudio(url);
          }
        } else if (payload?.audio_url) {
          renderAudio(String(payload.audio_url));
        } else {
          setMeta(JSON.stringify(payload, null, 2));
        }
      } else {
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        activeObjectUrl = url;
        renderAudio(url);
      }

      setStatus("Audio ready.", false);
    } catch (e) {
      setStatus(String(e), true);
    } finally {
      generateEl.disabled = false;
    }
  }

  generateEl.addEventListener('click', handleGenerate);
  loadBackends();
})();
