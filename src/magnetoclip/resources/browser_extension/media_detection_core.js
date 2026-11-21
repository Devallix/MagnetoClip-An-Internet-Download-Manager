// media_detection_core.js — shared evidence-based media classification.
// Loaded by the background and isolated content-script worlds. This module
// deliberately does not infer a stream from a vague path such as /video or
// /playlist alone; response MIME/body or a structured media reference is needed.
(function (root) {
  "use strict";

  const VIDEO_EXTS = new Set([
    "mp4", "m4v", "webm", "mkv", "mov", "avi", "flv", "3gp", "mpg", "mpeg", "ogv", "wmv", "asf", "rmvb",
  ]);
  const AUDIO_EXTS = new Set([
    "mp3", "m4a", "aac", "ogg", "oga", "opus", "wav", "flac", "wma", "aiff", "ape",
  ]);
  const HLS_MIMES = new Set([
    "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/mpegurl",
    "audio/mpegurl", "audio/x-mpegurl", "application/vnd.apple.mpegurl.audio",
  ]);
  const SEGMENT_EXTS = new Set(["ts", "mts", "m2ts", "m4s", "cmfv", "cmfa", "m4vseg", "aacseg"]);
  const GENERIC_MIMES = new Set([
    "", "application/octet-stream", "binary/octet-stream", "application/binary",
    "application/x-binary", "text/plain", "application/json", "text/json",
    "application/problem+json", "application/manifest+json", "application/xml", "text/xml",
  ]);

  function normalizeMime(value) {
    return String(value || "").split(";", 1)[0].trim().toLowerCase();
  }

  function mimeKind(value) {
    const mime = normalizeMime(value);
    if (HLS_MIMES.has(mime) || /mpegurl/.test(mime)) return "hls";
    if (mime === "application/dash+xml") return "dash";
    if (mime === "video/mp2t" || mime === "application/mp2t") return "";
    if (mime.startsWith("video/")) return "video";
    if (mime.startsWith("audio/")) return "audio";
    return "";
  }

  function isGenericMime(value) {
    return GENERIC_MIMES.has(normalizeMime(value));
  }

  function queryMime(url) {
    try {
      const parsed = new URL(String(url || ""));
      return normalizeMime(
        parsed.searchParams.get("mime") || parsed.searchParams.get("mime_type") ||
        parsed.searchParams.get("content_type") || parsed.searchParams.get("response-content-type") || ""
      );
    } catch (_) {
      return "";
    }
  }

  function extensionFromUrl(url) {
    try {
      const parsed = new URL(String(url || ""));
      const match = String(parsed.pathname || "").toLowerCase().match(/\.([a-z0-9]{2,8})$/);
      if (match) return match[1];
      const format = String(parsed.searchParams.get("format") || parsed.searchParams.get("ext") || parsed.searchParams.get("type") || "").toLowerCase();
      if (["m3u8", "m3u", "mpd"].includes(format)) return format;
      const mimeExt = queryMime(url);
      if (mimeExt.includes("mpegurl")) return "m3u8";
      if (mimeExt === "application/dash+xml") return "mpd";
      if (mimeExt.startsWith("video/")) return mimeExt.split("/")[1].split("+")[0];
      if (mimeExt.startsWith("audio/")) {
        const subtype = mimeExt.split("/")[1].split("+")[0];
        return subtype === "mpeg" ? "mp3" : subtype;
      }
    } catch (_) {}
    return "";
  }

  function isSegmentUrl(url) {
    try {
      const parsed = new URL(String(url || ""));
      const path = String(parsed.pathname || "").toLowerCase();
      const ext = extensionFromUrl(url);
      if (SEGMENT_EXTS.has(ext)) return true;
      if (/(?:^|\/)(?:segment|segments|chunk|chunks|frag|fragments|part|parts|init|initialization)(?:\/|$)/i.test(path)) return true;
      if (/(?:^|\/)init(?:ialization)?\.[a-z0-9]{2,8}$/i.test(path)) return true;
      if (/[?&](?:segment|seg|chunk|fragment|frag|part)=\d+(?:&|$)/i.test(parsed.search)) return true;
      return false;
    } catch (_) {
      return false;
    }
  }

  function kindFromUrl(url) {
    if (!/^https?:/i.test(String(url || "")) || isSegmentUrl(url)) return "";
    const queryKind = mimeKind(queryMime(url));
    if (queryKind) return queryKind;
    const ext = extensionFromUrl(url);
    if (["m3u8", "m3u"].includes(ext)) return "hls";
    if (ext === "mpd") return "dash";
    if (VIDEO_EXTS.has(ext)) return "video";
    if (AUDIO_EXTS.has(ext)) return "audio";
    return "";
  }

  function manifestKindFromText(value) {
    const sample = String(value || "").replace(/^\uFEFF/, "").replace(/^\s+/, "");
    if (/^#EXTM3U(?:\s|$)/i.test(sample) || /^#EXT-X-(?:STREAM-INF|TARGETDURATION|MEDIA|PLAYLIST-TYPE)\b/i.test(sample)) return "hls";
    if (/^(?:<\?xml\b[\s\S]{0,1024}?\?>\s*)?(?:<!--[\s\S]{0,1024}?-->\s*)*<MPD\b/i.test(sample)) return "dash";
    return "";
  }

  function mediaKindFromBytes(input) {
    let bytes;
    try {
      bytes = input instanceof Uint8Array ? input : new Uint8Array(input || []);
    } catch (_) {
      return "";
    }
    if (bytes.length >= 12) {
      const box = String.fromCharCode(bytes[4], bytes[5], bytes[6], bytes[7]);
      if (box === "ftyp") return "video";
      // An EBML header identifies Matroska/WebM. It may contain video or audio;
      // treat it as video only when the browser's active media element also
      // confirms it. The network-only probe leaves this ambiguous.
      if (bytes[0] === 0x1a && bytes[1] === 0x45 && bytes[2] === 0xdf && bytes[3] === 0xa3) return "video";
    }
    if (bytes.length >= 3 && bytes[0] === 0x49 && bytes[1] === 0x44 && bytes[2] === 0x33) return "audio";
    if (bytes.length >= 2 && bytes[0] === 0xff && (bytes[1] & 0xe0) === 0xe0) return "audio";
    return "";
  }

  function filenameKind(filename) {
    const match = String(filename || "").trim().match(/\.([a-z0-9]{2,8})(?:$|[?#])/i);
    if (!match) return "";
    const ext = match[1].toLowerCase();
    if (["m3u8", "m3u"].includes(ext)) return "hls";
    if (ext === "mpd") return "dash";
    if (VIDEO_EXTS.has(ext)) return "video";
    if (AUDIO_EXTS.has(ext)) return "audio";
    return "";
  }

  function isPotentialManifestEndpoint(url) {
    try {
      const parsed = new URL(String(url || ""));
      const path = String(parsed.pathname || "").toLowerCase();
      const query = String(parsed.search || "").toLowerCase();
      return /(?:^|\/)(?:playlist|master|manifest|stream|hls|dash|playback|source)(?:[._/-]|$)/i.test(path) ||
        /(?:^|\/)(?:playlist|master|manifest)\.php$/i.test(path) ||
        /[?&](?:format|type|source|mime|mime_type)=(?:m3u8|m3u|mpd|hls|dash)(?:&|$)/i.test(query);
    } catch (_) {
      return false;
    }
  }

  function collectJsonMediaReferences(value, baseUrl, limit) {
    const max = Math.max(1, Math.min(Number(limit) || 40, 100));
    const found = [];
    const seen = new Set();
    const urlKeys = /^(?:url|uri|src|source|file|link|href|manifest|manifesturl|manifest_url|playlist|playlisturl|playlist_url|hls|hlsurl|hls_url|m3u8|dash|dashurl|dash_url|mpd)$/i;
    const hlsKeys = /(?:hls|m3u8|playlist)/i;
    const dashKeys = /(?:dash|mpd)/i;

    function declaredKind(mimeOrType) {
      const raw = String(mimeOrType || "").toLowerCase();
      if (/mpegurl|\bhls\b|m3u8/.test(raw)) return "hls";
      if (/dash|mpd|application\/dash\+xml/.test(raw)) return "dash";
      return mimeKind(raw);
    }

    function add(raw, key, inheritedKind) {
      const input = String(raw || "").trim();
      if (!input || input.length > 8192) return;
      let resolved;
      try { resolved = new URL(input, baseUrl).href; } catch (_) { return; }
      if (!/^https?:/i.test(resolved) || isSegmentUrl(resolved) || seen.has(resolved)) return;
      let kind = kindFromUrl(resolved) || inheritedKind || "";
      if (!kind && hlsKeys.test(String(key || ""))) kind = "hls";
      if (!kind && dashKeys.test(String(key || ""))) kind = "dash";
      if (!kind) return;
      seen.add(resolved);
      found.push({ url: resolved, media_kind: kind, evidence: "json_media_reference", source_url: String(baseUrl || "") });
    }

    function walk(node, key, depth, inheritedKind) {
      if (found.length >= max || depth > 8 || node == null) return;
      if (typeof node === "string") {
        if (urlKeys.test(String(key || "")) || kindFromUrl(node)) add(node, key, inheritedKind);
        return;
      }
      if (Array.isArray(node)) {
        for (let i = 0; i < node.length && found.length < max; i += 1) walk(node[i], key, depth + 1, inheritedKind);
        return;
      }
      if (typeof node === "object") {
        const localKind = declaredKind(node.mime_type || node.mimeType || node.content_type || node.contentType || node.type || node.format) || inheritedKind;
        for (const name of Object.keys(node)) {
          if (found.length >= max) break;
          walk(node[name], name, depth + 1, localKind);
        }
      }
    }

    try {
      const parsed = typeof value === "string" ? JSON.parse(value) : value;
      walk(parsed, "", 0, "");
    } catch (_) {}
    return found;
  }

  function extractMediaReferencesFromText(value, baseUrl, limit) {
    const max = Math.max(1, Math.min(Number(limit) || 40, 100));
    const text = String(value || "").slice(0, 128 * 1024)
      .replace(/\\\//g, "/")
      .replace(/\\u0026/gi, "&")
      .replace(/&amp;/gi, "&");
    const pattern = /(?:https?:\/\/|\.\.\/|\.\/|\/)[^\s"'<>\\]+?\.(?:m3u8?|mpd)(?:\?[^\s"'<>\\]*)?/ig;
    const found = [];
    const seen = new Set();
    let match;
    while (found.length < max && (match = pattern.exec(text))) {
      let raw = String(match[0] || "").replace(/[),;]+$/g, "");
      let url;
      try { url = new URL(raw, baseUrl).href; } catch (_) { continue; }
      if (!/^https?:/i.test(url) || isSegmentUrl(url) || seen.has(url)) continue;
      const kind = kindFromUrl(url);
      if (kind !== "hls" && kind !== "dash") continue;
      seen.add(url);
      found.push({ url: url, media_kind: kind, evidence: "text_manifest_reference", source_url: String(baseUrl || "") });
    }
    return found;
  }

  function classifyResponse(url, mime, body, filename) {
    const actualMime = normalizeMime(mime);
    const bodyKind = manifestKindFromText(body);
    if (bodyKind) return { kind: bodyKind, confidence: "high", evidence: "manifest_signature" };

    const mimeTypeKind = mimeKind(actualMime);
    if (mimeTypeKind) {
      if (!isSegmentUrl(url)) return { kind: mimeTypeKind, confidence: "high", evidence: "response_mime" };
      return { kind: "", confidence: "none", evidence: "media_segment" };
    }

    // Do not trust URL or filename extensions when the server positively says
    // this is HTML or another non-media representation (login/error pages).
    const explicitNonMedia = !!actualMime && !isGenericMime(actualMime);
    if (explicitNonMedia) return { kind: "", confidence: "none", evidence: "non_media_mime" };

    const fromUrl = kindFromUrl(url);
    if (fromUrl) return { kind: fromUrl, confidence: "medium", evidence: "url_hint" };
    const fromName = filenameKind(filename);
    if (fromName) return { kind: fromName, confidence: "medium", evidence: "content_disposition" };
    return { kind: "", confidence: "none", evidence: "unclassified" };
  }

  function isTextInspectableMime(mime) {
    const value = normalizeMime(mime);
    return isGenericMime(value) || value.startsWith("text/") || value.includes("xml") || value.includes("json");
  }

  root.MagnetoclipMediaDetection = Object.freeze({
    normalizeMime,
    mimeKind,
    isGenericMime,
    extensionFromUrl,
    isSegmentUrl,
    kindFromUrl,
    manifestKindFromText,
    mediaKindFromBytes,
    filenameKind,
    isPotentialManifestEndpoint,
    collectJsonMediaReferences,
    extractMediaReferencesFromText,
    classifyResponse,
    isTextInspectableMime,
  });
})(typeof globalThis !== "undefined" ? globalThis : this);
