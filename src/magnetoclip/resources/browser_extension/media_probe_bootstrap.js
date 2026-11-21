// Install and relay the lightweight media-response probe before page scripts
// issue their first player requests. The normal content script still performs
// DOM/resource scans at document_idle; this bootstrap only preserves early
// HLS/DASH/progressive media evidence.
(function () {
  "use strict";
  if (window.__mcMediaProbeBootstrapInstalled) return;
  window.__mcMediaProbeBootstrapInstalled = true;

  const api = (typeof browser !== "undefined" && browser && browser.runtime)
    ? browser
    : ((typeof chrome !== "undefined" && chrome && chrome.runtime) ? chrome : null);
  if (!api || !api.runtime || typeof api.runtime.sendMessage !== "function") return;
  const send = function (message) {
    try {
      const result = api.runtime.sendMessage(message);
      if (result && typeof result.catch === "function") result.catch(function () {});
    } catch (_) {}
  };

  window.addEventListener("message", function (event) {
    if (event.source !== window || !event.data || event.data.source !== "magnetoclip-media-probe") return;
    const payload = event.data;
    const url = String(payload.url || "");
    const kind = String(payload.media_kind || "").toLowerCase();
    if (!/^https?:\/\//i.test(url) || !["hls", "dash", "video", "audio"].includes(kind)) return;
    // Retain early probe events in the isolated world too. If the background
    // worker has not restored the active-tab scope yet, content.js will replay
    // this small backlog once its normal scan machinery starts.
    const backlog = Array.isArray(window.__mcEarlyMediaProbeEvents)
      ? window.__mcEarlyMediaProbeEvents
      : (window.__mcEarlyMediaProbeEvents = []);
    if (backlog.length < 100) {
      backlog.push({
        url: url,
        media_kind: kind,
        mime_type: String(payload.mime_type || ""),
        evidence: String(payload.evidence || payload.signature || ""),
        confidence: String(payload.confidence || ""),
        source_url: String(payload.source_url || ""),
        filename: String(payload.filename || ""),
      });
    }
    send({
      type: "media_probe_detection",
      url: url,
      detected_type: kind,
      mime_type: String(payload.mime_type || ""),
      filename: String(payload.filename || ""),
      evidence: String(payload.evidence || payload.signature || ""),
      confidence: String(payload.confidence || ""),
      source_url: String(payload.source_url || ""),
      source: "document_start_media_probe",
    });
  }, false);

  send({ type: "install_media_probe_hook" });
})();
