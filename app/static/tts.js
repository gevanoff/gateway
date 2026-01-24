(() => {
  const $ = (id) => document.getElementById(id);

  const textEl = $("text");
  const voiceEl = $("voice");
  const speedEl = $("speed");
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
      try {
        URL.revokeObjectURL(activeObjectUrl);
      } catch (e) {
        // ignore
      }
      activeObjectUrl = null;
    }
    if (playerEl) playerEl.innerHTML = "";
  }

  function buildRequest() {
    const text = String(textEl.value || "").trim();
    if (!text) throw new Error("text is required");

    const voice = String(voiceEl.value || "").trim();
    const speedRaw = parseFloat(String(speedEl.value || "1"));
    const speed = Number.isFinite(speedRaw) ? Math.min(2, Math.max(0.5, speedRaw)) : 1;

    const body = { text, speed };
    if (voice) body.voice = voice;

    return body;
  }

  function formatTime(seconds) {
    const total = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
    const mins = Math.floor(total / 60);
    const secs = Math.floor(total % 60);
    return `${mins}:${secs.toString().padStart(2, "0")}`;
  }

  function renderAudio(url) {
    if (!url) return;
    if (!playerEl) return;

    const wrapper = document.createElement("div");
    wrapper.className = "audio-card";

    const audio = document.createElement("audio");
    audio.src = url;
    audio.preload = "metadata";
    audio.controls = true;

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

    const links = document.createElement("div");
    links.style.display = "flex";
    links.style.gap = "12px";
    links.style.justifyContent = "flex-end";
    links.innerHTML = `
      <a href="${url}" target="_blank" rel="noreferrer">Open</a>
      <a href="#" data-copy="${url}">Copy URL</a>
    `;

    wrapper.appendChild(audio);
    wrapper.appendChild(controls);
    wrapper.appendChild(links);

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

    wrapper.addEventListener("click", (e) => {
      const a = e.target;
      if (!(a instanceof HTMLAnchorElement)) return;
      const copy = a.getAttribute("data-copy");
      if (!copy) return;
      e.preventDefault();
      void navigator.clipboard?.writeText(copy);
      setStatus("Copied URL to clipboard", false);
    });

    playerEl.appendChild(wrapper);
  }

  function decodeBase64Audio(payload) {
    const raw = payload?.audio_base64 || payload?.audio || payload?.audio_data;
    if (!raw) return null;
    let b64 = String(raw);
    let contentType = payload?.content_type || payload?.mime_type || payload?.format || "audio/wav";
    if (b64.startsWith("data:")) {
      const parts = b64.split(",", 2);
      if (parts.length === 2) {
        const header = parts[0];
        b64 = parts[1];
        const mime = header.split(";")[0].replace("data:", "");
        if (mime) contentType = mime;
      }
    }
    try {
      const binary = atob(String(b64));
      const len = binary.length;
      const bytes = new Uint8Array(len);
      for (let i = 0; i < len; i += 1) {
        bytes[i] = binary.charCodeAt(i);
      }
      return new Blob([bytes], { type: contentType });
    } catch {
      return null;
    }
  }

  async function handleGenerate() {
    setStatus("", false);
    setMeta("");
    clearPlayer();

    let body;
    try {
      body = buildRequest();
    } catch (e) {
      setStatus(String(e?.message || e), true);
      return;
    }

    generateEl.disabled = true;
    setStatus("Generating...", false);

    try {
      const resp = await fetch("/ui/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      const contentType = resp.headers.get("content-type") || "";

      if (!resp.ok) {
        const text = await resp.text();
        setStatus(`HTTP ${resp.status}: ${text}`, true);
        return;
      }

      let url = "";
      if (contentType.includes("application/json")) {
        const payload = await resp.json();
        if (payload?.audio_url) {
          url = String(payload.audio_url || "").trim();
        } else {
          const blob = decodeBase64Audio(payload);
          if (blob) {
            url = URL.createObjectURL(blob);
            activeObjectUrl = url;
          }
        }

        if (payload?._gateway) {
          setMeta(`Backend: ${payload._gateway.backend_class || payload._gateway.backend}`);
        }

        if (!url) {
          setStatus("No audio returned in JSON response.", true);
          setMeta(JSON.stringify(payload));
          return;
        }
      } else {
        const blob = await resp.blob();
        url = URL.createObjectURL(blob);
        activeObjectUrl = url;
      }

      setStatus("Audio ready.", false);
      renderAudio(url);
    } catch (e) {
      setStatus(String(e?.message || e), true);
    } finally {
      generateEl.disabled = false;
    }
  }

  generateEl.addEventListener("click", handleGenerate);
})();
