(() => {
  const $ = (id) => document.getElementById(id);

  const textEl = $("text");
  const backendEl = $("backend");
  const backendHealthEl = $("backendHealth");
  const promptAudioEl = $("promptAudio");
  const savedVoiceEl = $("savedVoice");
  const voiceNameEl = $("voiceName");
  const saveVoiceEl = $("saveVoice");
  const deleteVoiceEl = $("deleteVoice");
  const presetEl = $("preset");
  const languageEl = $("language");
  const refTextEl = $("refText");
  const refAudioEl = $("refAudio");
  const xVectorOnlyEl = $("xVectorOnly");
  const voiceClonePromptEl = $("voiceClonePrompt");
  const maxNewTokensEl = $("maxNewTokens");
  const topPEl = $("topP");
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
  let backendCache = [];

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
      backendCache = list;
      backendEl.innerHTML = '<option value="">(default)</option>';
      for (const item of list) {
        const val = item?.backend_class;
        if (!val) continue;
        const opt = document.createElement('option');
        opt.value = val;
        const health = item?.ready === false ? 'not ready' : (item?.healthy === false ? 'unhealthy' : 'ready');
        opt.textContent = item?.description ? `${val} — ${item.description} (${health})` : `${val} (${health})`;
        backendEl.appendChild(opt);
      }
      updateBackendHealth();
    } catch (e) {}
  }

  function updateBackendHealth() {
    if (!backendHealthEl) return;
    const list = backendCache || [];
    const selected = list.find((b) => b.backend_class === backendEl.value) || list[0];
    if (selected) {
      const health = selected?.ready === false ? 'not ready' : (selected?.healthy === false ? 'unhealthy' : 'ready');
      backendHealthEl.textContent = `${selected.backend_class}: ${health}`;
    } else {
      backendHealthEl.textContent = 'unknown';
    }
  }

  async function loadVoiceLibrary() {
    if (!savedVoiceEl) return;
    try {
      const resp = await fetch('/ui/api/tts/voice-library', { method: 'GET', credentials: 'same-origin' });
      if (!resp.ok) return;
      const payload = await resp.json();
      const list = Array.isArray(payload?.voices) ? payload.voices : [];
      savedVoiceEl.innerHTML = '<option value="">(none)</option>';
      for (const item of list) {
        const id = item?.id || '';
        if (!id) continue;
        const opt = document.createElement('option');
        opt.value = id;
        opt.textContent = id;
        savedVoiceEl.appendChild(opt);
      }
    } catch (e) {}
  }

  function buildFormData() {
    const text = String(textEl.value || "").trim();
    if (!text) throw new Error("text is required");

    const file = promptAudioEl?.files && promptAudioEl.files[0];

    const fd = new FormData();
    fd.append("text", text);
    if (file) fd.append("prompt_audio", file, file.name);

    const voiceId = String(savedVoiceEl?.value || "").trim();
    const refAudio = String(refAudioEl?.value || "").trim();
    const voiceClonePrompt = String(voiceClonePromptEl?.value || "").trim();
    if (!file && !voiceId && !refAudio && !voiceClonePrompt) {
      throw new Error("prompt audio file, saved voice, ref_audio, or voice clone prompt is required");
    }
    if (voiceId) fd.append("voice_id", voiceId);

    const voiceName = String(voiceNameEl?.value || "").trim();
    if (voiceName) fd.append("voice_name", voiceName);

    const language = String(languageEl?.value || "").trim();
    if (language) fd.append("language", language);
    const refText = String(refTextEl?.value || "").trim();
    if (refText) fd.append("ref_text", refText);
    if (refAudio) fd.append("ref_audio", refAudio);
    if (voiceClonePrompt) fd.append("voice_clone_prompt", voiceClonePrompt);
    const xVectorOnly = String(xVectorOnlyEl?.value || "").trim();
    if (xVectorOnly) fd.append("x_vector_only_mode", xVectorOnly);
    const maxNewTokens = String(maxNewTokensEl?.value || "").trim();
    if (maxNewTokens) fd.append("max_new_tokens", maxNewTokens);
    const topP = String(topPEl?.value || "").trim();
    if (topP) fd.append("top_p", topP);

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

  async function handleSaveVoice() {
    setStatus("", false);
    setMeta("");
    const name = String(voiceNameEl?.value || "").trim();
    const file = promptAudioEl?.files && promptAudioEl.files[0];
    if (!name) { setStatus("voice name is required", true); return; }
    if (!file) { setStatus("prompt audio file is required", true); return; }
    const fd = new FormData();
    fd.append("voice_name", name);
    fd.append("prompt_audio", file, file.name);
    try {
      const resp = await fetch('/ui/api/tts/voice-library', { method: 'POST', credentials: 'same-origin', body: fd });
      const text = await resp.text();
      if (!resp.ok) { setStatus(text || `HTTP ${resp.status}`, true); return; }
      await loadVoiceLibrary();
      setStatus("Saved voice sample.", false);
    } catch (e) { setStatus(String(e), true); }
  }

  async function handleDeleteVoice() {
    const voiceId = String(savedVoiceEl?.value || "").trim();
    if (!voiceId) { setStatus("select a saved voice", true); return; }
    try {
      const resp = await fetch(`/ui/api/tts/voice-library/${encodeURIComponent(voiceId)}`, { method: 'DELETE', credentials: 'same-origin' });
      if (!resp.ok) { setStatus(await resp.text(), true); return; }
      await loadVoiceLibrary();
      setStatus("Deleted voice sample.", false);
    } catch (e) { setStatus(String(e), true); }
  }

  function applyPreset() {
    const preset = String(presetEl?.value || "").trim();
    if (preset === "fast") {
      if (numStepsEl) numStepsEl.value = "3";
      if (tShiftEl) tShiftEl.value = "0.8";
    } else if (preset === "balanced") {
      if (numStepsEl) numStepsEl.value = "4";
      if (tShiftEl) tShiftEl.value = "0.9";
    } else if (preset === "quality") {
      if (numStepsEl) numStepsEl.value = "6";
      if (tShiftEl) tShiftEl.value = "0.95";
    }
  }

  generateEl.addEventListener('click', handleGenerate);
  if (saveVoiceEl) saveVoiceEl.addEventListener('click', handleSaveVoice);
  if (deleteVoiceEl) deleteVoiceEl.addEventListener('click', handleDeleteVoice);
  if (backendEl) backendEl.addEventListener('change', updateBackendHealth);
  if (presetEl) presetEl.addEventListener('change', applyPreset);
  loadBackends();
  loadVoiceLibrary();
})();
