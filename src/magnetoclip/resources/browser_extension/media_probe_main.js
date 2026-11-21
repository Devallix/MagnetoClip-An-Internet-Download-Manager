// media_probe_main.js — MAIN-world instrumentation for browser-accessible media.
// Installed in each frame at document_start by background.js. It observes the
// page's own Fetch/XHR responses and active media elements without consuming or
// modifying the response seen by the website.
(function () {
  "use strict";
  if (Number(window.__mcMediaProbeHookVersion || 0) >= 5) return;
  window.__mcMediaProbeHookVersion = 5;

  const Core = window.MagnetoclipMediaDetection || null;
  const emitted = new Map();
  const MAX_EMITTED = 900;
  const MAX_SAMPLE_BYTES = 65536;
  const MAX_JSON_REFS = 40;
  const MAX_BODY_INSPECTIONS = 80;
  const inspectedResponses = new Map();
  let bodyInspectionCount = 0;
  let currentPageUrl = String(location && location.href || "");
  let playerObserver = null;
  let playerTimer = null;

  function normalizeMime(value) {
    return Core ? Core.normalizeMime(value) : String(value || "").split(";", 1)[0].trim().toLowerCase();
  }

  function kindFromUrl(url) {
    if (Core) return Core.kindFromUrl(url);
    return "";
  }

  function refreshPageScope() {
    const page = String(location && location.href || "");
    if (page && page !== currentPageUrl) {
      currentPageUrl = page;
      emitted.clear();
      inspectedResponses.clear();
      bodyInspectionCount = 0;
    }
  }

  function isTextInspectable(mime) {
    if (Core) return Core.isTextInspectableMime(mime);
    const value = normalizeMime(mime);
    return !value || value === "text/plain" || value === "application/json" || value === "application/octet-stream" || value.includes("xml");
  }

  function responseKindFromHeaders(url, mime) {
    const normalized = normalizeMime(mime);
    const mimeKind = Core ? Core.mimeKind(normalized) : "";
    if (mimeKind) return mimeKind;
    // A declared non-media MIME (especially text/html) overrides a misleading
    // .m3u8/.mp4 suffix. The bounded body inspector below may still confirm an
    // incorrectly labelled manifest by its actual signature.
    const explicitNonMedia = !!normalized && Core && !Core.isGenericMime(normalized);
    return explicitNonMedia ? "" : kindFromUrl(url);
  }

  function reserveBodyInspection(url) {
    refreshPageScope();
    const key = String(url || "");
    if (!key || bodyInspectionCount >= MAX_BODY_INSPECTIONS) return false;
    const now = Date.now();
    const previous = inspectedResponses.get(key) || 0;
    if (now - previous < 30000) return false;
    inspectedResponses.set(key, now);
    bodyInspectionCount += 1;
    if (inspectedResponses.size > 500) {
      const first = inspectedResponses.keys().next().value;
      if (first !== undefined) inspectedResponses.delete(first);
    }
    return true;
  }

  function post(url, kind, mime, evidence, extra) {
    refreshPageScope();
    const value = String(url || "");
    const mediaKind = String(kind || "").toLowerCase();
    if (!/^https?:\/\//i.test(value) || !["hls", "dash", "video", "audio"].includes(mediaKind)) return;
    if (Core && Core.isSegmentUrl(value)) return;
    const key = value + "|" + mediaKind;
    const now = Date.now();
    const previous = emitted.get(key) || 0;
    if (now - previous < 30000) return;
    emitted.set(key, now);
    if (emitted.size > MAX_EMITTED) {
      const first = emitted.keys().next().value;
      if (first !== undefined) emitted.delete(first);
    }
    try {
      window.postMessage(Object.assign({
        source: "magnetoclip-media-probe",
        url: value,
        media_kind: mediaKind,
        mime_type: normalizeMime(mime),
        evidence: String(evidence || ""),
        confidence: evidence === "manifest_signature" || evidence === "response_mime" || evidence === "active_media_element" ? "high" : "medium",
        source_url: String((extra && extra.source_url) || ""),
        hook_version: 5,
      }, extra || {}), "*");
    } catch (_) {}
  }

  function inspectText(url, mime, text, sourceUrl) {
    const sample = String(text || "").slice(0, MAX_SAMPLE_BYTES).replace(/^\uFEFF/, "").trimStart();
    if (!sample) return false;
    const manifestKind = Core ? Core.manifestKindFromText(sample) :
      (/^#EXTM3U(?:\s|$)/i.test(sample) ? "hls" : (/^(?:<\?xml[^>]*>\s*)?<MPD\b/i.test(sample) ? "dash" : ""));
    if (manifestKind) {
      post(url, manifestKind, mime, "manifest_signature", { source_url: sourceUrl || "" });
      return true;
    }

    const normalizedMime = normalizeMime(mime);
    // Do not mine ordinary HTML error/login pages for URL-looking strings.
    // Only a real manifest signature can promote an HTML-labelled endpoint.
    if (normalizedMime === "text/html") return false;
    if (normalizedMime.includes("json") || /^[\[{]/.test(sample)) {
      if (Core) {
        const refs = Core.collectJsonMediaReferences(sample, url, MAX_JSON_REFS);
        refs.forEach((item) => {
          post(item.url, item.media_kind, "", item.evidence, { source_url: url });
        });
        if (refs.length) return true;
      }
    }

    // Some player APIs wrap a manifest URL inside a plain-text response rather
    // than JSON. Only explicit .m3u8/.mpd references are accepted here.
    const patterns = [
      /https?:\\?\/?\\?\/[^\s"'<>\\]+?\.m3u8(?:\?[^\s"'<>\\]*)?/ig,
      /https?:\\?\/?\\?\/[^\s"'<>\\]+?\.mpd(?:\?[^\s"'<>\\]*)?/ig,
    ];
    patterns.forEach((pattern, index) => {
      let match;
      while ((match = pattern.exec(sample)) && pattern.lastIndex < MAX_SAMPLE_BYTES) {
        let candidate = String(match[0] || "").replace(/\\\//g, "/").replace(/\\u0026/gi, "&");
        try { candidate = new URL(candidate, url).href; } catch (_) { continue; }
        post(candidate, index === 0 ? "hls" : "dash", "", "explicit_manifest_reference", { source_url: url });
      }
    });
    return false;
  }

  async function readResponseSample(response) {
    if (!response || !response.body || typeof response.body.getReader !== "function") return "";
    let reader;
    try { reader = response.clone().body.getReader(); } catch (_) { return ""; }
    const chunks = [];
    let total = 0;
    try {
      while (total < MAX_SAMPLE_BYTES) {
        const part = await reader.read();
        if (!part || part.done) break;
        const bytes = part.value || new Uint8Array(0);
        const remaining = MAX_SAMPLE_BYTES - total;
        const chunk = bytes.length > remaining ? bytes.slice(0, remaining) : bytes;
        chunks.push(chunk);
        total += chunk.length;
        if (total >= 4096) {
          const preview = new TextDecoder("utf-8", { fatal: false }).decode(concatBytes(chunks, total));
          if (Core && Core.manifestKindFromText(preview)) break;
        }
        if (bytes.length > remaining) break;
      }
    } catch (_) {
      // The clone may be unavailable for opaque/streaming responses. The page's
      // original response is unaffected because only the clone is read.
    } finally {
      try { await reader.cancel(); } catch (_) {}
    }
    const bytes = concatBytes(chunks, total);
    return { bytes: bytes, text: new TextDecoder("utf-8", { fatal: false }).decode(bytes) };
  }

  function concatBytes(chunks, total) {
    const output = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      output.set(chunk, offset);
      offset += chunk.length;
    }
    return output;
  }

  async function inspectFetchResponse(requestUrl, response, pageAtRequest) {
    try {
      if (pageAtRequest && pageAtRequest !== String(location && location.href || "")) return;
      const url = String((response && response.url) || requestUrl || "");
      if (!/^https?:\/\//i.test(url) || (response && response.status >= 400)) return;
      if (Core && Core.isSegmentUrl(url)) return;
      const mime = response && response.headers && response.headers.get("content-type") || "";
      const kind = responseKindFromHeaders(url, mime);
      if (kind) {
        post(url, kind, mime, Core && Core.mimeKind(mime) ? "response_mime" : "url_mime_or_extension");
        if (kind === "hls" && reserveBodyInspection(url)) {
          const playlistSample = await readResponseSample(response);
          if (pageAtRequest && pageAtRequest !== String(location && location.href || "")) return;
          if (playlistSample && playlistSample.text) inspectText(url, mime, playlistSample.text, url);
        }
        return;
      }
      const normalizedMime = normalizeMime(mime);
      const manifestEndpoint = Boolean(Core && (
        Core.isPotentialManifestEndpoint(url) || ["hls", "dash"].includes(Core.kindFromUrl(url))
      ));
      // Some providers incorrectly label playlist.php-style endpoints as HTML.
      // Only inspect their bounded response sample when the URL looks like a
      // manifest API; the body must prove HLS/DASH before it is emitted.
      if ((!isTextInspectable(mime) && !(normalizedMime === "text/html" && manifestEndpoint)) ||
          normalizedMime === "application/javascript" || normalizedMime === "text/javascript" ||
          normalizedMime === "text/css" || (normalizedMime === "text/html" && !manifestEndpoint)) return;
      if (!reserveBodyInspection(url)) return;
      const sample = await readResponseSample(response);
      if (pageAtRequest && pageAtRequest !== String(location && location.href || "")) return;
      if (sample && sample.bytes && Core) {
        const binaryKind = Core.mediaKindFromBytes(sample.bytes);
        if (binaryKind && !Core.isSegmentUrl(url)) {
          post(url, binaryKind, mime, "response_binary_signature");
          return;
        }
      }
      if (sample && sample.text) inspectText(url, mime, sample.text, url);
    } catch (_) {}
  }

  function xhrResponseSample(xhr) {
    try {
      if (typeof xhr.responseText === "string") return xhr.responseText.slice(0, MAX_SAMPLE_BYTES);
    } catch (_) {}
    try {
      if (xhr.response instanceof ArrayBuffer) {
        return new TextDecoder("utf-8", { fatal: false }).decode(new Uint8Array(xhr.response).slice(0, MAX_SAMPLE_BYTES));
      }
    } catch (_) {}
    try {
      if (typeof Blob !== "undefined" && xhr.response instanceof Blob) {
        return xhr.response.slice(0, MAX_SAMPLE_BYTES).text();
      }
    } catch (_) {}
    try {
      if (xhr.response && typeof xhr.response === "object") return JSON.stringify(xhr.response).slice(0, MAX_SAMPLE_BYTES);
    } catch (_) {}
    return "";
  }

  if (typeof window.fetch === "function" && Number(window.fetch.__mcMediaProbeWrappedVersion || 0) < 5) {
    const originalFetch = window.fetch;
    const wrappedFetch = function (...args) {
      let requestUrl = "";
      const pageAtRequest = String(location && location.href || "");
      try { requestUrl = args[0] && args[0].url ? args[0].url : String(args[0] || ""); } catch (_) {}
      return originalFetch.apply(this, args).then((response) => {
        // Inspect asynchronously; never delay or replace the site's response.
        Promise.resolve().then(() => inspectFetchResponse(requestUrl, response, pageAtRequest)).catch(() => {});
        return response;
      });
    };
    try { Object.defineProperty(wrappedFetch, "__mcMediaProbeWrappedVersion", { value: 5 }); }
    catch (_) { wrappedFetch.__mcMediaProbeWrappedVersion = 5; }
    window.fetch = wrappedFetch;
  }

  if (typeof XMLHttpRequest !== "undefined" && Number(XMLHttpRequest.prototype.__mcMediaProbeWrappedVersion || 0) < 5) {
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      this.__mcProbeRequestUrl = String(url || "");
      this.__mcProbePageAtRequest = String(location && location.href || "");
      return originalOpen.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function (...args) {
      if (!this.__mcProbeListener) {
        this.__mcProbeListener = true;
        this.addEventListener("loadend", () => {
          try {
            const url = String(this.responseURL || this.__mcProbeRequestUrl || "");
            if (!/^https?:\/\//i.test(url) || (this.status && this.status >= 400)) return;
            if (this.__mcProbePageAtRequest && this.__mcProbePageAtRequest !== String(location && location.href || "")) return;
            if (Core && Core.isSegmentUrl(url)) return;
            const mime = this.getResponseHeader("content-type") || "";
            const kind = responseKindFromHeaders(url, mime);
            if (kind) {
              post(url, kind, mime, Core && Core.mimeKind(mime) ? "response_mime" : "url_mime_or_extension");
              if (kind === "hls" && reserveBodyInspection(url)) {
                const playlistSample = xhrResponseSample(this);
                if (playlistSample && typeof playlistSample.then === "function") {
                  const xhr = this;
                  playlistSample.then((value) => {
                    if (xhr.__mcProbePageAtRequest === String(location && location.href || "")) inspectText(url, mime, value, url);
                  }).catch(() => {});
                } else if (playlistSample) {
                  inspectText(url, mime, playlistSample, url);
                }
              }
              return;
            }
            if (Core && this.response instanceof ArrayBuffer && !Core.isSegmentUrl(url)) {
              const binaryKind = Core.mediaKindFromBytes(new Uint8Array(this.response).slice(0, MAX_SAMPLE_BYTES));
              if (binaryKind) { post(url, binaryKind, mime, "response_binary_signature"); return; }
            }
            if (Core && typeof Blob !== "undefined" && this.response instanceof Blob && !Core.isSegmentUrl(url)) {
              if (!reserveBodyInspection(url)) return;
              const xhr = this;
              this.response.slice(0, MAX_SAMPLE_BYTES).arrayBuffer().then((buffer) => {
                if (xhr.__mcProbePageAtRequest && xhr.__mcProbePageAtRequest !== String(location && location.href || "")) return;
                const binaryKind = Core.mediaKindFromBytes(new Uint8Array(buffer));
                if (binaryKind) post(url, binaryKind, mime, "response_binary_signature");
                else if (isTextInspectable(mime) || (normalizeMime(mime) === "text/html" && Core && Core.isPotentialManifestEndpoint(url))) {
                  inspectText(url, mime, new TextDecoder().decode(new Uint8Array(buffer)), url);
                }
              }).catch(() => {});
              return;
            }
            const normalizedMime = normalizeMime(mime);
            const manifestEndpoint = Boolean(Core && (
              Core.isPotentialManifestEndpoint(url) || ["hls", "dash"].includes(Core.kindFromUrl(url))
            ));
            if ((!isTextInspectable(mime) && !(normalizedMime === "text/html" && manifestEndpoint)) ||
                normalizedMime === "application/javascript" || normalizedMime === "text/javascript" ||
                normalizedMime === "text/css" || (normalizedMime === "text/html" && !manifestEndpoint)) return;
            if (!reserveBodyInspection(url)) return;
            const sample = xhrResponseSample(this);
            if (sample && typeof sample.then === "function") {
              const xhr = this;
              sample.then((value) => {
                if (xhr.__mcProbePageAtRequest === String(location && location.href || "")) inspectText(url, mime, value, url);
              }).catch(() => {});
            } else if (sample) {
              inspectText(url, mime, sample, url);
            }
          } catch (_) {}
        }, { once: true });
      }
      return originalSend.apply(this, args);
    };
    try { Object.defineProperty(XMLHttpRequest.prototype, "__mcMediaProbeWrappedVersion", { value: 5 }); }
    catch (_) { XMLHttpRequest.prototype.__mcMediaProbeWrappedVersion = 5; }
  }

  function inspectMediaElement(element) {
    if (!element || !/^(VIDEO|AUDIO|SOURCE)$/.test(String(element.tagName || "").toUpperCase())) return;
    const candidates = [];
    try { candidates.push(String(element.currentSrc || "")); } catch (_) {}
    try { candidates.push(String(element.src || element.getAttribute("src") || "")); } catch (_) {}
    const declaredType = String(element.getAttribute && element.getAttribute("type") || "");
    for (const raw of candidates) {
      if (!raw || /^blob:|^data:/i.test(raw)) continue;
      let url;
      try { url = new URL(raw, location.href).href; } catch (_) { continue; }
      let kind = kindFromUrl(url) || (Core && Core.mimeKind(declaredType));
      if (!kind && element.tagName.toUpperCase() !== "SOURCE") {
        const ready = Number(element.readyState) >= 2;
        const hasDuration = Number.isFinite(Number(element.duration)) && Number(element.duration) > 0;
        const hasVideo = element.tagName.toUpperCase() === "VIDEO" && (Number(element.videoWidth) > 0 || Number(element.videoHeight) > 0);
        if (ready && (hasDuration || hasVideo)) kind = element.tagName.toUpperCase() === "AUDIO" ? "audio" : "video";
      }
      if (kind) post(url, kind, declaredType, "active_media_element");
    }
  }

  function scanMediaElements(rootNode) {
    const root = rootNode && rootNode.querySelectorAll ? rootNode : document;
    try {
      if (root.matches && root.matches("video,audio,source")) inspectMediaElement(root);
      root.querySelectorAll("video,audio,source").forEach(inspectMediaElement);
    } catch (_) {}
  }

  function schedulePlayerScan() {
    if (playerTimer != null) return;
    playerTimer = setTimeout(() => {
      playerTimer = null;
      scanMediaElements(document);
    }, 250);
  }

  function startPlayerMonitor() {
    scanMediaElements(document);
    if (typeof MutationObserver !== "undefined" && document.documentElement) {
      try {
        playerObserver = new MutationObserver((records) => {
          for (const record of records) {
            if (record.type === "attributes" && record.target) inspectMediaElement(record.target);
            for (const node of record.addedNodes || []) scanMediaElements(node);
          }
        });
        playerObserver.observe(document.documentElement, {
          subtree: true,
          childList: true,
          attributes: true,
          attributeFilter: ["src", "type"],
        });
      } catch (_) { playerObserver = null; }
    }
    document.addEventListener("loadstart", schedulePlayerScan, true);
    document.addEventListener("loadedmetadata", schedulePlayerScan, true);
    document.addEventListener("playing", schedulePlayerScan, true);
    document.addEventListener("durationchange", schedulePlayerScan, true);
    // Some players replace the source property without causing a DOM mutation.
    setInterval(schedulePlayerScan, 1800);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startPlayerMonitor, { once: true });
  } else {
    startPlayerMonitor();
  }
})();
