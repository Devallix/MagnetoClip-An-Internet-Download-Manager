// content.js — scans pages for downloadable files and media.
//
// On every page the scanner watches media elements, network resources and
// dynamic DOM changes. On social platforms (Twitter/X, Facebook, Instagram,
// Telegram, Snapchat) it sends every newly detected video/audio/image to
// MagnetoClip so it can present a capture popup. On streaming platforms that
// yt-dlp can resolve (YouTube, Vimeo, Twitch, TikTok, ...) it sends the page
// URL as soon as a video starts playing so MagnetoClip can grab it. On every
// other page it keeps reporting the files it finds as a plain page scan. The
// latest detection results are always reported to the background page so the
// extension popup can show them.
(() => {
  "use strict";

  // The background worker re-injects this script into already-open tabs after
  // the extension is reloaded (existing instances keep running on a dead
  // context). Guard so a tab never runs two scanning instances.
  if (window.__magnetoclipContentActive) {
    return;
  }
  window.__magnetoclipContentActive = true;

  const DOWNLOADABLE_EXTS = new Set([
    // video
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "ts", "mts",
    "m2ts", "3gp", "ogv", "rmvb", "asf",
    // audio
    "mp3", "wav", "flac", "aac", "ogg", "wma", "m4a", "opus", "mid", "midi",
    "ape", "aiff",
    // image
    "jpg", "jpeg", "png", "gif", "bmp", "svg", "webp", "tiff", "tif", "ico",
    "heic", "heif", "psd", "raw",
    // document
    "pdf", "doc", "docx", "txt", "xls", "xlsx", "ppt", "pptx", "odt", "ods",
    "odp", "rtf", "md", "csv", "epub", "mobi",
    // archive
    "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "tgz", "zst", "cab",
    // software
    "exe", "msi", "dmg", "pkg", "appimage", "deb", "rpm", "apk", "jar", "msix",
  ]);

  const TYPE_MAP = {
    video: ["mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "ts", "mts", "m2ts", "3gp", "ogv", "rmvb", "asf"],
    audio: ["mp3", "wav", "flac", "aac", "ogg", "wma", "m4a", "opus", "mid", "midi", "ape", "aiff"],
    image: ["jpg", "jpeg", "png", "gif", "bmp", "svg", "webp", "tiff", "tif", "ico", "heic", "heif", "psd", "raw"],
    document: ["pdf", "doc", "docx", "txt", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf", "md", "csv", "epub", "mobi"],
    archive: ["zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "tgz", "zst", "cab"],
    software: ["exe", "msi", "dmg", "pkg", "appimage", "deb", "rpm", "apk", "jar", "msix"],
  };

  // Social platforms where media is auto-detected and offered as a capture.
  const SOCIAL_HOSTS = new Set([
    "twitter.com", "x.com",
    "facebook.com", "fb.watch",
    "instagram.com",
    "t.me", "telegram.me", "web.telegram.org",
    "snapchat.com",
    "reddit.com",
    "linkedin.com",
    "pinterest.com", "pin.it",
    "tumblr.com",
    "threads.net",
    "discord.com",
    "web.whatsapp.com",
    "weibo.com",
    "vk.com",
    "9gag.com",
  ]);

  // Hosts where MagnetoClip can resolve a whole page/post URL to media (via
  // yt-dlp), used when the real media is hidden behind blob:/MSE streams.
  // NOTE: Facebook and X/Twitter are deliberately NOT listed here — yt-dlp's
  // extractors for them are unreliable, so offering the page URL only produces
  // failed downloads. Those platforms rely on direct media-CDN captures instead
  // (see MEDIA_CDN_HOSTS).
  const YTDLP_HOSTS = new Set([
    "youtube.com", "youtu.be",
    "vimeo.com",
    "twitch.tv",
    "tiktok.com",
    "dailymotion.com",
    "instagram.com",
    "reddit.com",
    "linkedin.com",
    "pinterest.com",
    "tumblr.com",
  ]);

  // Streaming sites (anime, movies) that serve HLS/DASH video but may not
  // have yt-dlp extractors. When video is detected on these sites, the page
  // URL is offered so MagnetoClip can attempt extraction via yt-dlp's generic
  // extractor or capture the m3u8/mpd manifest from network requests.
  const STREAMING_SITE_HOSTS = new Set([
    "anikai.watch", "anikai.vip", "anikai.rip", "anikai.to",
    "animekai.org.lv",
    "hdtoday24.at", "hdtodayz.app", "hdtoday.duckdns.org", "hdtoday.to",
    "4thdmovie.com",
  ]);

  // Media CDNs whose stream requests are often labelled with a generic
  // initiator type, so we capture them directly even when the video plays
  // through a blob:/MSE pipeline.
  const MEDIA_CDN_HOSTS = new Set([
    "fbcdn.net",            // Facebook
    "twimg.com",            // X / Twitter (video.twimg.com)
    "cdn.telegram.org",     // Telegram
    "telegram.org",
    "telesco.pe",           // Telegram file CDN (cdn1.telesco.pe, ...)
    "cdninstagram.com",     // Instagram
    "pinimg.com",           // Pinterest
    "cdn.discordapp.com",   // Discord
    "media.discordapp.net", // Discord
    "media.licdn.com",      // LinkedIn
    "redd.it",              // Reddit (i.redd.it, v.redd.it, preview.redd.it)
    "tumblr.com",           // Tumblr (64.media.tumblr.com)
    "whatsapp.net",         // WhatsApp Web media (mmg.whatsapp.net)
  ]);

  // HLS/DASH transport files that are useless to download on their own.
  const STREAM_EXTS = new Set(["m3u8", "m3u", "mpd"]);
  // Video segment files; captured from network resources they are almost always
  // pieces of a larger stream, so we do not offer them directly.
  const SEGMENT_EXTS = new Set(["ts", "mts", "m2ts", "m4s"]);
  // File extensions that identify a complete media file worth offering.
  const MEDIA_EXTS = new Set([
    "mp4", "m4v", "webm", "mkv", "mov", "avi",
    "mp3", "m4a", "aac", "ogg", "opus", "wav", "flac",
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "avif",
  ]);

  const MAX_CAPTURES_PER_SCAN = 4;
  // Page scans feed the detection queue, not the capture dialog, so this only
  // needs to stop a pathological page from producing a useless report. The old
  // limit of 20 hid most of a gallery or a long feed, which is what made
  // detection look unreliable. The queue is cleared on navigation and is
  // bounded on its own, so a higher ceiling costs nothing.
  const MAX_PAGE_SCAN_FILES = 300;
  const MIN_IMAGE_SIZE = 120;
  const TYPE_PRIORITY = { video: 0, audio: 1, image: 2 };
  // Blob-backed viewers (Telegram Web renders every photo as a blob: URL) can
  // only be captured by shipping the bytes themselves. A single native
  // messaging message is capped at ~1MB, so larger blobs are split into base64
  // chunks and reassembled in MagnetoClip. Small blobs still travel in one
  // message to avoid the extra round-trips.
  const BLOB_CHUNK_BYTES = 384 * 1024; // divisible by 3 so per-chunk base64 remains concatenable
  const SINGLE_BLOB_MESSAGE_BYTES = 512 * 1024;
  const MAX_BLOB_CAPTURE_BYTES = 15 * 1024 * 1024;
  // Telegram videos may be represented by a single blob: URL rather than a
  // visible CDN URL. Keep the small cap for images/unknown media, but allow a
  // real video/audio blob to use the same bounded chunk-transfer path as live
  // Telegram streams. The native host caps all chunked captures at 480 MiB of
  // base64 (~360 MiB of media bytes), so stay below that ceiling.
  const MAX_BLOB_VIDEO_CAPTURE_BYTES = 350 * 1024 * 1024;
  // Explicit fetches (blob: URLs or session-bound Telegram Web proxy links the
  // user pasted) are a deliberate action, so they are allowed to be larger than
  // auto-captured media. The bytes are streamed in chunks to avoid one giant JS
  // ArrayBuffer, and this cap guards against unreasonable downloads.
  const MAX_FETCH_BLOB_BYTES = 512 * 1024 * 1024;
  // Tiny image blobs are almost always avatars, emoji or thumbnails; skip them
  // unless the element reports a meaningful rendered size.
  const MIN_BLOB_IMAGE_BYTES = 24 * 1024;

  // A media URL for the player overlay: video/audio files, HLS/DASH manifests
  // and (never) transport segments. ``observedResources`` maps url -> initiator.
  function looksLikeMediaUrl(url) {
    const ext = extensionOf(url);
    if (STREAM_EXTS.has(ext)) {
      return true;
    }
    if (SEGMENT_EXTS.has(ext)) {
      return false;
    }
    const type = typeFor(ext);
    return MEDIA_EXTS.has(ext) && (type === "video" || type === "audio");
  }

  // URLs already handed to MagnetoClip during this page session.
  const reportedUrls = new Set();
  // Social hosts already covered by a "post URL" capture.
  const reportedPageHosts = new Set();

  // Network resources observed live by a PerformanceObserver. Scanning only
  // ``performance.getEntriesByType("resource")`` misses media on heavy pages:
  // the resource-timing buffer (~250 entries) evicts older requests and the
  // periodic poll can run after the entry it cares about has been dropped.
  // Observing resources as they load (plus a larger buffer) guarantees blob/MSE
  // players (Facebook reels, X videos, Telegram) hand their CDN file URLs to
  // us. Map: url -> initiatorType.
  const observedResources = new Map();
  let resourceObserver = null;
  const telegramNetworkDiagnosticsSeen = new Set();
  const telegramVideoDiagnostics = new WeakMap();
  const telegramVideoDiagnosticListeners = new WeakSet();

  function safeTelegramResourcePath(url) {
    try {
      const parsed = new URL(String(url || ""));
      if (parsed.protocol === "blob:") {
        let inner = null;
        try { inner = new URL(parsed.pathname); } catch (error) { /* opaque UUID */ }
        return inner ? "blob:" + inner.origin + "/<object-url>" : "blob:<object-url>";
      }
      return parsed.protocol + "//" + parsed.host + String(parsed.pathname || "").slice(0, 180);
    } catch (error) {
      return String(url || "").slice(0, 40);
    }
  }

  // Diagnose actual media requests from Telegram Web. CDN hosts can be outside
  // *.telegram.org, so do not filter by request host; only log relevant media
  // paths, media initiators, or large cross-origin fetch/XHR resources. Queries
  // (which can contain signed tokens) are never written to the console.
  function diagnoseTelegramNetworkResource(url, initiatorType, resourceEntry) {
    if (!/(^|\.)telegram\.org$/i.test(String(location.hostname || ""))) return;
    let parsed;
    try { parsed = new URL(String(url || "")); } catch (error) { return; }
    if (!/^https?:$/.test(parsed.protocol)) return;
    const host = String(parsed.hostname || "").toLowerCase();
    const path = String(parsed.pathname || "");
    const initiator = String(initiatorType || "").toLowerCase();
    const ext = extensionOf(parsed.href);
    const mediaExtension = MEDIA_EXTS.has(ext) || STREAM_EXTS.has(ext) || SEGMENT_EXTS.has(ext);
    const pathRelevant = /\/(?:stream|progressive|api\/files|file|video|videos|media|download|getfile)(?:\/|$)/i.test(path);
    const mediaInitiator = ["video", "audio", "media"].includes(initiator);
    const crossOriginFetch = parsed.origin !== location.origin &&
      (initiator === "fetch" || initiator === "xmlhttprequest") &&
      ((Number(resourceEntry && resourceEntry.transferSize) || 0) >= 65536 ||
       (Number(resourceEntry && resourceEntry.decodedBodySize) || 0) >= 65536 ||
       (Number(resourceEntry && resourceEntry.duration) || 0) >= 500);
    if (!mediaExtension && !pathRelevant && !mediaInitiator && !crossOriginFetch) return;
    const key = host + path + "|" + initiator;
    if (telegramNetworkDiagnosticsSeen.has(key)) return;
    telegramNetworkDiagnosticsSeen.add(key);
    if (telegramNetworkDiagnosticsSeen.size > 160) {
      const oldest = telegramNetworkDiagnosticsSeen.values().next().value;
      if (oldest) telegramNetworkDiagnosticsSeen.delete(oldest);
    }
    try {
      console.info("[mc-telegram] network_resource_seen", {
        host: host,
        path: path.slice(0, 180),
        initiator: initiator,
        duration_ms: Math.round(Number(resourceEntry && resourceEntry.duration) || 0),
        transfer_size: Number(resourceEntry && resourceEntry.transferSize) || 0,
        decoded_body_size: Number(resourceEntry && resourceEntry.decodedBodySize) || 0,
        cross_origin: parsed.origin !== location.origin,
        has_query: !!parsed.search,
        extension: ext || "",
      });
    } catch (error) {
      /* diagnostics must not interrupt scanning */
    }
  }

  function diagnoseTelegramVideoElement(element, reason) {
    if (!/(^|\.)telegram\.org$/i.test(String(location.hostname || "")) || !element) return;
    try {
      const rawSrc = String(element.getAttribute("src") || "");
      const currentSrc = String(element.currentSrc || "");
      const srcObject = element.srcObject || null;
      let srcObjectKind = "none";
      if (srcObject) {
        try { srcObjectKind = Object.prototype.toString.call(srcObject); } catch (error) { srcObjectKind = "present"; }
        if (srcObjectKind === "[object Object]" && srcObject.constructor && srcObject.constructor.name) {
          srcObjectKind = "[object " + String(srcObject.constructor.name) + "]";
        }
      }
      const errorCode = element.error && Number(element.error.code) || 0;
      const details = {
        reason: String(reason || "scan"),
        source: safeTelegramResourcePath(rawSrc),
        current_src: safeTelegramResourcePath(currentSrc),
        src_object_kind: srcObjectKind,
        paused: !!element.paused,
        ended: !!element.ended,
        ready_state: Number(element.readyState) || 0,
        network_state: Number(element.networkState) || 0,
        width: Number(element.videoWidth) || 0,
        height: Number(element.videoHeight) || 0,
        duration_seconds: Number.isFinite(Number(element.duration)) ? Math.round(Number(element.duration) * 10) / 10 : null,
        current_time_seconds: Math.floor(Number(element.currentTime) || 0),
        error_code: errorCode,
        source_count: element.querySelectorAll ? element.querySelectorAll("source[src]").length : 0,
      };
      const signature = JSON.stringify({
        source: details.source, current_src: details.current_src, src_object_kind: details.src_object_kind,
        paused: details.paused, ended: details.ended, ready_state: details.ready_state,
        network_state: details.network_state, width: details.width, height: details.height,
        duration_seconds: details.duration_seconds, error_code: details.error_code,
      });
      if (telegramVideoDiagnostics.get(element) === signature) return;
      telegramVideoDiagnostics.set(element, signature);
      console.info("[mc-telegram] video_element_state", details);
    } catch (error) {
      /* diagnostics must not interrupt media scanning */
    }
    if (!telegramVideoDiagnosticListeners.has(element)) {
      telegramVideoDiagnosticListeners.add(element);
      ["loadedmetadata", "loadeddata", "canplay", "play", "playing", "progress", "emptied", "error", "pause"].forEach((eventName) => {
        try { element.addEventListener(eventName, () => diagnoseTelegramVideoElement(element, eventName), { passive: true }); } catch (error) { /* noop */ }
      });
    }
  }

  function observeNetworkResources() {
    try {
      if (typeof performance.setResourceTimingBufferSize === "function") {
        performance.setResourceTimingBufferSize(4000);
      }
    } catch (error) {
      /* older browsers */
    }
    if (typeof PerformanceObserver === "undefined") {
      return;
    }
    try {
      resourceObserver = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          observedResources.set(
            String(entry.name || ""),
            String(entry.initiatorType || "").toLowerCase()
          );
          // Report at arrival time too: on a busy Telegram SPA an entry may be
          // evicted or reclassified before the next full detection scan.
          diagnoseTelegramNetworkResource(entry.name, entry.initiatorType, entry);
        }
        scheduleScan();
      });
      resourceObserver.observe({ type: "resource", buffered: true });
    } catch (error) {
      resourceObserver = null;
    }
  }

  let social = false;
  let scanTimer = null;
  let pollInterval = null;
  let domObserver = null;
  let contextGone = false;

  // ----- messaging -----

  // The extension context can be invalidated while this content script is still
  // injected (e.g. when the app refreshes the unpacked extension and Chrome
  // reloads it). Every later chrome.* call then throws "Extension context
  // invalidated." — a *synchronous* throw, not a rejected promise — which would
  // otherwise spam the console and kill the page scan. When that happens we
  // stop scanning cleanly instead of erroring forever.
  function safeSend(message, _retries) {
    if (contextGone) {
      return;
    }
    if (_retries === undefined) {
      _retries = 2;
    }
    try {
      const result = chrome.runtime.sendMessage(message);
      if (result && typeof result.catch === "function") {
        result.catch((error) => {
          if (/Extension context invalidated/i.test(String(error))) {
            stopScanning();
          } else if (/Receiving end does not exist/i.test(String(error)) && _retries > 0) {
            setTimeout(() => safeSend(message, _retries - 1), 500);
          }
        });
      }
    } catch (error) {
      if (/Extension context invalidated/i.test(String(error && error.message))) {
        stopScanning();
      } else if (/Receiving end does not exist/i.test(String(error && error.message)) && _retries > 0) {
        setTimeout(() => safeSend(message, _retries - 1), 500);
      }
    }
  }

  function stopScanning() {
    contextGone = true;
    if (scanTimer) {
      clearTimeout(scanTimer);
      scanTimer = null;
    }
    if (pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
    }
    if (domObserver) {
      domObserver.disconnect();
      domObserver = null;
    }
    if (resourceObserver) {
      resourceObserver.disconnect();
      resourceObserver = null;
    }
    if (objectUrlMessageHandler) {
      window.removeEventListener("message", objectUrlMessageHandler);
      objectUrlMessageHandler = null;
    }
  }

  // ----- url helpers -----

  function hostOf(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, "").toLowerCase();
    } catch (error) {
      return "";
    }
  }

  function isOnSocialHost(host) {
    return Array.from(SOCIAL_HOSTS).some(
      (candidate) => host === candidate || host.endsWith("." + candidate)
    );
  }

  function isOnYtdlpHost(host) {
    return Array.from(YTDLP_HOSTS).some(
      (candidate) => host === candidate || host.endsWith("." + candidate)
    );
  }

  function isOnStreamingSiteHost(host) {
    return Array.from(STREAMING_SITE_HOSTS).some(
      (candidate) => host === candidate || host.endsWith("." + candidate)
    );
  }

  function isMediaCdnUrl(url) {
    const host = hostOf(url);
    if (!host) {
      return false;
    }
    const onCdn = Array.from(MEDIA_CDN_HOSTS).some(
      (candidate) => host === candidate || host.endsWith("." + candidate)
    );
    if (!onCdn) {
      return false;
    }
    const path = (url.split(/[?#]/)[0] || "").toLowerCase();
    const ext = extensionOf(url);
    if (STREAM_EXTS.has(ext) || SEGMENT_EXTS.has(ext)) {
      return false;
    }
    // Telegram's internal stream proxies are not direct files.
    if (isTelegramStreamProxy(url)) {
      return false;
    }
    // Facebook: /v/ paths are progressive video files; image paths on fbcdn
    // are too noisy (avatars, previews) to offer from the network log — the
    // DOM scanner already covers images there.
    if (host.endsWith("fbcdn.net")) {
      return path.includes("/v/") || MEDIA_EXTS.has(ext);
    }
    if (host.endsWith("twimg.com")) {
      return (
        /\/ext_tw_video\/|\/amplify_video\/|\/tweet_video\/|\/media\//.test(path) ||
        MEDIA_EXTS.has(ext)
      );
    }
    // Telegram serves every uploaded file under /file/ (photos, videos, docs)
    // on its CDNs; web.telegram.org itself proxies parts under /api/files/.
    // Many are extensionless, so a /file/ or /api/files/ path is treated as a
    // real file.
    if (host.endsWith("cdn.telegram.org") || host.endsWith("telesco.pe")) {
      return path.includes("/file/");
    }
    if (host.endsWith("telegram.org")) {
      return path.includes("/file/") || path.includes("/api/files/");
    }
    return MEDIA_EXTS.has(ext);
  }

  // Telegram's internal stream proxies (Web K /k/stream/, Web A /a/stream/ and
  // /a/progressive/) only work with the page's session cookie and never serve a
  // direct file, so they are never treated as direct download URLs.
  function isTelegramStreamProxy(url) {
    if (!hostOf(url).endsWith("telegram.org")) {
      return false;
    }
    const path = url.split(/[?#]/)[0] || "";
    return (
      path.includes("/k/stream/") ||
      path.includes("/a/stream/") ||
      path.includes("/a/progressive/")
    );
  }

  // Telegram Web plays videos through an internal stream proxy that only
  // works with the page's session cookie; it responds with a Location-less 302
  // (its own HTML shell) when the cookie is missing, so downloading the proxy
  // URL directly always fails. From the content script the fetch is same-origin
  // (cookies are sent automatically), so we follow the redirect with a tiny
  // Range request and offer the resolved CDN file instead. The resolved URL is
  // cached per source so later scans re-offer it (the browser refuses to hand
  // over cross-origin redirect targets without CORS, so this is best-effort;
  // the background worker's webRequest fallback covers the cases that fail).
  // Map: src -> {url, type} (or null when resolution failed / is in flight).
  const resolvedStreams = new Map();

  function resolveTelegramStream(src, direct, fallbackType) {
    if (resolvedStreams.has(src)) {
      const cached = resolvedStreams.get(src);
      if (cached) {
        addDirect(direct, cached.url, cached.type);
      }
      return;
    }
    resolvedStreams.set(src, null);
    const controller = new AbortController();
    let probeTimedOut = false;
    telegramBlobDiagnostic("stream_probe_start", {
      source: safeTelegramResourcePath(src),
      fallback_type: String(fallbackType || ""),
      range_requested: "bytes=0-0",
    });
    const timer = setTimeout(() => {
      probeTimedOut = true;
      telegramBlobDiagnostic("stream_probe_timeout", {
        source: safeTelegramResourcePath(src),
        timeout_ms: 10000,
      });
      controller.abort();
    }, 10000);
    fetch(src, {
      credentials: "same-origin",
      headers: { "Range": "bytes=0-0" },
      signal: controller.signal,
    })
      .then((response) => {
        clearTimeout(timer);
        const finalUrl = String(response.url || "");
        // Record what the authenticated probe actually received. Telegram Web K
        // can return its own stream endpoint without exposing a CDN redirect to
        // page JavaScript; status and media headers distinguish that case from
        // a login/app-shell response without logging signed query parameters.
        try {
          const contentType = String(response.headers.get("Content-Type") || "");
          const contentRange = String(response.headers.get("Content-Range") || "");
          const acceptRanges = String(response.headers.get("Accept-Ranges") || "");
          telegramBlobDiagnostic("stream_probe_result", {
            status: Number(response.status) || 0,
            ok: !!response.ok,
            redirected: !!response.redirected,
            final_url: safeTelegramResourcePath(finalUrl),
            same_url: finalUrl === src,
            content_type: contentType.slice(0, 100),
            content_range: contentRange.slice(0, 100),
            accept_ranges: acceptRanges.slice(0, 40),
            fallback_type: String(fallbackType || ""),
          });
        } catch (error) {
          /* diagnostics must not interrupt stream resolution */
        }
        if (!isHttpUrl(finalUrl) || finalUrl === src) {
          return;
        }
        const ext = extensionOf(finalUrl);
        if (STREAM_EXTS.has(ext) || SEGMENT_EXTS.has(ext)) {
          return;
        }
        const type = typeFor(ext);
        const resolvedType = type === "file" ? fallbackType : type;
        resolvedStreams.set(src, { url: finalUrl, type: resolvedType });
        addDirect(direct, finalUrl, resolvedType);
      })
      .catch((error) => {
        clearTimeout(timer);
        telegramBlobDiagnostic("stream_probe_error", {
          source: safeTelegramResourcePath(src),
          error_name: String((error && error.name) || "Error").slice(0, 80),
          error_message: String((error && error.message) || "fetch failed").slice(0, 160),
          timed_out: probeTimedOut,
        });
        // A failed probe must not poison this source forever. Keep a short
        // cooldown to prevent rapid rescans from repeatedly issuing requests.
        setTimeout(() => {
          if (resolvedStreams.has(src) && resolvedStreams.get(src) === null) {
            resolvedStreams.delete(src);
          }
        }, 5000);
      });
  }

  function extensionOf(url) {
    try {
      const clean = url.split(/[?#]/)[0];
      const match = /\.([a-z0-9]{2,8})$/i.exec(clean);
      if (match) {
        return match[1].toLowerCase();
      }
      // Extensionless CDN URLs (Twitter images, Telegram files) declare their
      // format as a query parameter, e.g. pbs.twimg.com/media/x?format=jpg.
      const format = /[?&]format=([a-z0-9]{2,8})/i.exec(url);
      return format ? format[1].toLowerCase() : "";
    } catch (error) {
      return "";
    }
  }

  function typeFor(ext) {
    for (const [type, exts] of Object.entries(TYPE_MAP)) {
      if (exts.includes(ext)) {
        return type;
      }
    }
    return "file";
  }

  function filenameOf(url) {
    const clean = url.split(/[?#]/)[0];
    const name = clean.substring(clean.lastIndexOf("/") + 1);
    if (!name || !name.includes(".")) {
      // Extensionless CDN URLs carry their real format in the query, so the
      // filename can still end with a meaningful extension.
      const ext = extensionOf(url);
      if (ext && /[?&]format=/.test(url)) {
        const base = name ? name : "media";
        try {
          return decodeURIComponent(base) + "." + ext;
        } catch (error) {
          return base + "." + ext;
        }
      }
      return "";
    }
    try {
      return decodeURIComponent(name);
    } catch (error) {
      return name;
    }
  }

  function isHttpUrl(url) {
    return /^https?:/i.test(url || "");
  }

  function typeForMime(mime) {
    const type = String(mime || "").split("/")[0].toLowerCase();
    return type === "image" || type === "video" || type === "audio" ? type : "file";
  }

  // Telegram Web builds media blobs from decrypted bytes and often creates them
  // without a MIME type, so fetch(blob:) reports an empty Content-Type and the
  // blob used to be discarded as a generic file. Magic-byte sniffing recovers
  // the real kind; it also covers generic octet-stream labels.
  function sniffMediaType(bytes) {
    if (!bytes || bytes.length < 12) {
      return "";
    }
    const b = bytes;
    const ascii = (start, text) => {
      for (let i = 0; i < text.length; i++) {
        if (b[start + i] !== text.charCodeAt(i)) {
          return false;
        }
      }
      return true;
    };
    if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) {
      return "image/jpeg";
    }
    if (
      b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47 &&
      b[4] === 0x0d && b[5] === 0x0a && b[6] === 0x1a && b[7] === 0x0a
    ) {
      return "image/png";
    }
    if (ascii(0, "GIF87a") || ascii(0, "GIF89a")) {
      return "image/gif";
    }
    if (ascii(0, "RIFF") && ascii(8, "WEBP")) {
      return "image/webp";
    }
    if (b[0] === 0x42 && b[1] === 0x4d) {
      return "image/bmp";
    }
    if (b[0] === 0x00 && b[1] === 0x00 && ascii(4, "ftyp")) {
      const brand = String.fromCharCode(b[8], b[9], b[10], b[11]).toLowerCase();
      if (/^(avi|f4v|mif|hei|heic|msf)/.test(brand)) {
        return "image/heic";
      }
      return "video/mp4";
    }
    if (b[0] === 0x1a && b[1] === 0x45 && b[2] === 0xdf && b[3] === 0xa3) {
      return "video/webm";
    }
    if (ascii(0, "OggS")) {
      return "audio/ogg";
    }
    if (ascii(0, "ID3") || (b[0] === 0xff && (b[1] & 0xe0) === 0xe0)) {
      return "audio/mpeg";
    }
    if (ascii(0, "RIFF") && ascii(8, "WAVE")) {
      return "audio/wav";
    }
    if (ascii(0, "fLaC")) {
      return "audio/flac";
    }
    return "";
  }

  function mimeIsSpecific(mime) {
    const value = String(mime || "");
    const type = value.split("/")[0].toLowerCase();
    if (type !== "image" && type !== "video" && type !== "audio") {
      return false;
    }
    return !/octet-stream|generic/i.test(value);
  }

  // ---- blob media capture ----

  // Blob URLs handed to MagnetoClip are useless on their own (the app cannot
  // fetch them), so the blob is read here and shipped as base64. Only read on
  // social platforms: on ordinary pages blob images are site chrome (avatars,
  // generated thumbs) and would trigger noisy capture dialogs.
  const blobCapturesInFlight = new Set();
  // Telegram may revoke a Blob URL immediately after attaching it to a media
  // element. Keep the underlying finite Blob reference from the MAIN-world
  // createObjectURL hook for a short, bounded time so capture does not depend
  // on fetching a URL that may already be revoked.
  const telegramBlobObjectCache = new Map();
  const telegramNonBlobObjectUrls = new Set();
  const TELEGRAM_BLOB_OBJECT_CACHE_MAX_ENTRIES = 12;
  const TELEGRAM_BLOB_OBJECT_CACHE_MAX_BYTES = 96 * 1024 * 1024;
  const TELEGRAM_BLOB_OBJECT_CACHE_TTL_MS = 30000;

  function telegramCacheBlobObject(url, blob) {
    if (!url || !blob || typeof blob.size !== "number" || typeof blob.slice !== "function") {
      return false;
    }
    // Keep small typed blobs too: an object URL can be revoked before the DOM
    // scanner learns whether it belongs to an image or video. Successful
    // captures remove the reference; the cache is strictly size/count/age bound.
    if (blob.size > TELEGRAM_BLOB_OBJECT_CACHE_MAX_BYTES) {
      return false;
    }
    const now = Date.now();
    for (const [key, item] of telegramBlobObjectCache) {
      if (!item || now - item.createdAt > TELEGRAM_BLOB_OBJECT_CACHE_TTL_MS) {
        telegramBlobObjectCache.delete(key);
      }
    }
    let totalBytes = 0;
    for (const item of telegramBlobObjectCache.values()) {
      totalBytes += Number(item.blob && item.blob.size) || 0;
    }
    while (
      telegramBlobObjectCache.size >= TELEGRAM_BLOB_OBJECT_CACHE_MAX_ENTRIES ||
      totalBytes + blob.size > TELEGRAM_BLOB_OBJECT_CACHE_MAX_BYTES
    ) {
      const oldestKey = telegramBlobObjectCache.keys().next().value;
      if (oldestKey === undefined) break;
      const oldest = telegramBlobObjectCache.get(oldestKey);
      totalBytes -= Number(oldest && oldest.blob && oldest.blob.size) || 0;
      telegramBlobObjectCache.delete(oldestKey);
    }
    telegramBlobObjectCache.set(url, { blob: blob, createdAt: now });
    return true;
  }

  function cachedTelegramBlobObject(url) {
    const item = telegramBlobObjectCache.get(url);
    if (!item) return null;
    if (Date.now() - item.createdAt > TELEGRAM_BLOB_OBJECT_CACHE_TTL_MS) {
      telegramBlobObjectCache.delete(url);
      return null;
    }
    return item.blob || null;
  }

  function clearTelegramBlobObject(url) {
    telegramBlobObjectCache.delete(url);
  }

  function arrayBufferToBase64(bytes) {
    let binary = "";
    const CHUNK = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += CHUNK) {
      binary += String.fromCharCode.apply(
        null,
        bytes.subarray(offset, offset + CHUNK)
      );
    }
    return btoa(binary);
  }

  // Awaits a runtime message round-trip; resolves null when the background is
  // unreachable (extension context gone, native host down).
  function sendAwait(message, _retries) {
    if (_retries === undefined) {
      _retries = 2;
    }
    return new Promise((resolve) => {
      if (contextGone) {
        resolve(null);
        return;
      }
      try {
        const result = chrome.runtime.sendMessage(message);
        if (result && typeof result.then === "function") {
          result.then(resolve).catch((error) => {
            if (/Receiving end does not exist/i.test(String(error)) && _retries > 0) {
              setTimeout(() => sendAwait(message, _retries - 1).then(resolve), 500);
            } else {
              resolve(null);
            }
          });
        } else {
          resolve(result);
        }
      } catch (error) {
        if (/Receiving end does not exist/i.test(String(error && error.message)) && _retries > 0) {
          setTimeout(() => sendAwait(message, _retries - 1).then(resolve), 500);
        } else {
          resolve(null);
        }
      }
    });
  }

  // Ship a blob as base64 chunks; returns true when MagnetoClip accepted it.
  async function sendBlobChunks(captureKey, src, blob, meta) {
    // Slice the Blob and read one native-messaging-sized chunk at a time.
    // This keeps large Telegram videos from requiring a second full-sized
    // Uint8Array allocation in the extension page.
    const total = Math.max(1, Math.ceil(blob.size / BLOB_CHUNK_BYTES));
    for (let index = 0; index < total; index++) {
      const start = index * BLOB_CHUNK_BYTES;
      const end = Math.min(start + BLOB_CHUNK_BYTES, blob.size);
      const bytes = new Uint8Array(await blob.slice(start, end).arrayBuffer());
      const response = await sendAwait({
        type: "capture_chunk",
        capture_key: captureKey,
        index: index,
        total: total,
        chunk: arrayBufferToBase64(bytes),
        url: src,
        filename: meta.filename || "",
        mime_type: meta.mime_type || "",
        detected_type: meta.detected_type || "file",
        referrer: meta.referrer || location.href,
        last: index === total - 1,
      });
      if (!response || response.type === "capture_chunk_error") {
        return false;
      }
    }
    return true;
  }

  function telegramBlobDiagnostic(event, details) {
    if (!/telegram\.org$/i.test(String(location.hostname || ""))) {
      return;
    }
    try {
      console.info("[mc-telegram] " + event, details || {});
    } catch (error) {
      /* diagnostics must never interrupt capture */
    }
  }

  async function captureBlobUrl(src, meta, suppliedBlob) {
    if (!social || blobCapturesInFlight.has(src) || reportedUrls.has(src)) {
      return;
    }
    if (telegramNonBlobObjectUrls.has(src)) {
      // MediaSource/MediaStream object URLs do not contain a finite Blob that
      // fetch(blob:) can read. Their actual media must be discovered from the
      // browser network requests, not repeatedly fetched as a blob URL.
      return;
    }
    blobCapturesInFlight.add(src);
    const onTelegram = /telegram\.org$/i.test(String(location.hostname || ""));
    try {
      let blob = suppliedBlob || (onTelegram ? cachedTelegramBlobObject(src) : null);
      let responseMime = "";
      if (!blob) {
        const response = await fetch(src);
        if (!response.ok) {
          telegramBlobDiagnostic("blob_fetch_not_ok", { status: response.status });
          return;
        }
        blob = await response.blob();
        responseMime = response.headers.get("Content-Type") || "";
      }
      if (!blob || typeof blob.slice !== "function" || !Number.isFinite(Number(blob.size))) {
        telegramBlobDiagnostic("blob_capture_unreadable_object", { url_kind: "non_blob" });
        return;
      }
      let mimeType = responseMime || blob.type || "";
      let detectedType = typeForMime(mimeType);
      if (!mimeIsSpecific(mimeType)) {
        const sample = new Uint8Array(await blob.slice(0, 32).arrayBuffer());
        mimeType = sniffMediaType(sample) || mimeType;
        detectedType = typeForMime(mimeType);
      }
      const expectedType = String((meta && meta.expected_type) || "");
      // A URL can be observed from multiple DOM paths in Telegram's SPA. Never
      // let an image-typed preview blob become a video candidate when the
      // owning media element explicitly expects video/audio. MIME sniffing wins
      // over the element hint.
      if (
        (expectedType === "video" || expectedType === "audio") &&
        detectedType === "image"
      ) {
        telegramBlobDiagnostic("blob_skipped_type_mismatch", {
          expected_type: expectedType,
          actual_type: detectedType,
          mime_type: mimeType.slice(0, 100),
          bytes: blob.size,
        });
        clearTelegramBlobObject(src);
        return;
      }
      if (detectedType === "file") {
        // Telegram often leaves the Blob MIME empty. When the blob is attached
        // to a known media element, its expected type is a safe fallback only
        // when byte sniffing did not identify a conflicting media type.
        detectedType = expectedType;
        if (!detectedType || detectedType === "file") {
          telegramBlobDiagnostic("blob_skipped_untyped", { size: blob.size });
          return;
        }
      }
      const maxBytes =
        onTelegram && (detectedType === "video" || detectedType === "audio")
          ? MAX_BLOB_VIDEO_CAPTURE_BYTES
          : MAX_BLOB_CAPTURE_BYTES;
      if (!blob.size || blob.size > maxBytes) {
        reportedUrls.add(src);
        clearTelegramBlobObject(src);
        telegramBlobDiagnostic("blob_skipped_size", {
          detected_type: detectedType,
          bytes: blob.size,
          max_bytes: maxBytes,
        });
        return;
      }
      if (detectedType === "image") {
        const metaSize = meta && meta.width && meta.height;
        const knownLarge = metaSize && (meta.width >= MIN_IMAGE_SIZE || meta.height >= MIN_IMAGE_SIZE);
        if (!knownLarge && blob.size < MIN_BLOB_IMAGE_BYTES) {
          // If the objectURL hook fired before the media element was attached,
          // retain its Blob briefly so a later scan can apply the element's real
          // dimensions. Once the DOM explicitly identifies a tiny image, drop it.
          if (meta && meta.expected_type) {
            clearTelegramBlobObject(src);
          }
          return;
        }
      }
      const filename = (meta && meta.filename) || "";
      const referrer = (meta && meta.referrer) || location.href;
      const captureMeta = {
        filename: filename,
        mime_type: mimeType,
        detected_type: detectedType,
        referrer: referrer,
      };
      telegramBlobDiagnostic("blob_capture_start", {
        detected_type: detectedType,
        mime_type: mimeType,
        bytes: blob.size,
        expected_type: (meta && meta.expected_type) || "",
        source: suppliedBlob ? "main_world_blob_object" : "blob_url_fetch",
      });
      if (blob.size <= SINGLE_BLOB_MESSAGE_BYTES) {
        const bytes = new Uint8Array(await blob.arrayBuffer());
        safeSend({
          type: "social_capture",
          url: referrer,
          files: [{
            url: src,
            filename: filename,
            detected_type: detectedType,
            mime_type: mimeType,
            data_base64: arrayBufferToBase64(bytes),
          }],
        });
        reportedUrls.add(src);
        clearTelegramBlobObject(src);
        telegramBlobDiagnostic("blob_capture_sent", {
          detected_type: detectedType,
          bytes: blob.size,
          mode: "single_message",
        });
        return;
      }
      const captureKey =
        Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
      const ok = await sendBlobChunks(captureKey, src, blob, captureMeta);
      if (ok) {
        reportedUrls.add(src);
        clearTelegramBlobObject(src);
        telegramBlobDiagnostic("blob_capture_sent", {
          detected_type: detectedType,
          bytes: blob.size,
          mode: "chunks",
        });
      } else {
        telegramBlobDiagnostic("blob_capture_transfer_failed", {
          detected_type: detectedType,
          bytes: blob.size,
        });
      }
    } catch (error) {
      telegramBlobDiagnostic("blob_capture_failed", {
        message: String((error && error.message) || error || "fetch failed").slice(0, 180),
      });
    } finally {
      blobCapturesInFlight.delete(src);
    }
  }

  // Telegram (and similar viewers) create media blobs on the main thread but
  // may attach them to an element after the fact. A MAIN-world hook (injected
  // by the background worker, so the page CSP cannot block it) reports every
  // created object URL here; the byte/mime filters in captureBlobUrl keep the
  // noise down.
  let objectUrlMessageHandler = null;

  function installObjectUrlHook() {
    if (!social || objectUrlMessageHandler) {
      return;
    }
    safeSend({ type: "install_blob_hook" });
    objectUrlMessageHandler = (event) => {
      if (event.source !== window || !event.data) {
        return;
      }
      const payload = event.data;
      if (payload.source !== "magnetoclip-blob") {
        return;
      }
      const url = String(payload.url || "");
      if (!url.startsWith("blob:") || reportedUrls.has(url)) {
        return;
      }
      if (/telegram\.org$/i.test(String(location.hostname || ""))) {
        const objectKind = String(payload.object_kind || "unknown");
        const blob = payload.blob && typeof payload.blob.slice === "function" &&
          Number.isFinite(Number(payload.blob.size)) ? payload.blob : null;
        if (blob) {
          telegramCacheBlobObject(url, blob);
          telegramBlobDiagnostic("blob_object_received", {
            object_kind: objectKind,
            bytes: blob.size,
            mime_type: String(blob.type || ""),
            hook_version: Number(payload.hook_version) || 0,
          });
          // Capture directly from the Blob object handed over by the MAIN
          // world. This still works if Telegram revokes the URL immediately.
          captureBlobUrl(url, {}, blob);
        } else {
          // URL.createObjectURL(MediaSource/MediaStream) does not point to a
          // finite Blob. Mark it so the isolated world does not issue a doomed
          // fetch(blob:) request on every scan; the network detector will look
          // for the real Telegram media URL instead.
          if (objectKind === "media_source" || objectKind === "media_stream") {
            telegramNonBlobObjectUrls.add(url);
            if (telegramNonBlobObjectUrls.size > 512) {
              const oldest = telegramNonBlobObjectUrls.values().next().value;
              if (oldest) telegramNonBlobObjectUrls.delete(oldest);
            }
          }
          telegramBlobDiagnostic("blob_object_url_kind", {
            object_kind: objectKind,
            has_blob: false,
            hook_version: Number(payload.hook_version) || 0,
          });
        }
        scheduleScan();
        return;
      }
      captureBlobUrl(url, {});
    };
    window.addEventListener("message", objectUrlMessageHandler);
    telegramBlobDiagnostic("blob_hook_listener_ready", {
      page_path: String(location.pathname || "").slice(0, 100),
    });
  }

  // The app asks the extension to fetch a ``blob:`` URL or a session-bound
  // Telegram Web proxy link the user pasted into the new-download field.
  //
  // - ``blob:`` URLs resolve only inside the page that created them, so the
  //   bytes are read here and shipped back (small payloads in one response,
  //   larger ones streamed as ``blob_fetch_chunk`` messages the background
  //   forwards to the native host, which reassembles them).
  // - Telegram Web proxy links (``/a/progressive/``, ``/k/stream/``, ...) need
  //   the page's session cookie and redirect to a CDN file. Reading the CDN
  //   body cross-origin is CORS-blocked, so instead we resolve the redirect to
  //   the final CDN URL and hand it back; the app downloads it with the
  //   session cookies (matching how auto-captured Telegram media already works).
  async function shipBlobBytes(url, requestId, bytes, mimeType) {
    if (bytes.length > MAX_FETCH_BLOB_BYTES) {
      return { ok: false, message: "The content is too large to fetch" };
    }
    if (bytes.length > SINGLE_BLOB_MESSAGE_BYTES) {
      const total = Math.max(1, Math.ceil(bytes.length / BLOB_CHUNK_BYTES));
      for (let index = 0; index < total; index++) {
        const start = index * BLOB_CHUNK_BYTES;
        const end = Math.min(start + BLOB_CHUNK_BYTES, bytes.length);
        const response = await sendAwait({
          type: "blob_fetch_chunk",
          request_id: requestId,
          index: index,
          total: total,
          chunk: arrayBufferToBase64(bytes.subarray(start, end)),
          url: url,
          filename: "",
          mime_type: mimeType,
          last: index === total - 1,
        });
        if (!response || response.type === "blob_fetch_chunk_error") {
          return { ok: false, message: "Could not send the content to MagnetoClip" };
        }
      }
      return { ok: true, streamed: true, mime_type: mimeType };
    }
    return {
      ok: true,
      data_base64: arrayBufferToBase64(bytes),
      mime_type: mimeType,
    };
  }

  async function probeRedirect(url) {
    if (!/^https?:/i.test(url || "")) {
      return { ok: false };
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {
        credentials: "same-origin",
        headers: { "Range": "bytes=0-0" },
        signal: controller.signal,
      });
      clearTimeout(timer);
      const finalUrl = String(response.url || "");
      if (/^https?:/i.test(finalUrl) && finalUrl !== url) {
        return { ok: true, resolved_url: finalUrl };
      }
    } catch (error) {
      clearTimeout(timer);
      return { ok: false };
    }
    return { ok: false };
  }

  // ---- live Telegram Web proxy media capture ----

  // Telegram Web ``progressive``/``stream`` links are one-shot session tokens:
  // by the time a user pastes or confirms a download the token has expired, so
  // neither the app nor the browser can re-fetch them (302/interrupted). The
  // right-click happens while the video is still on screen, so this streams the
  // TOTAL bytes straight into a chunked capture while the token is alive; the
  // app's capture dialog then completes from the buffered bytes instead of
  // re-requesting the link.
  const STREAM_CAPTURE_MAX_BYTES = 512 * 1024 * 1024;

  function mimeToExtension(mime) {
    const type = String(mime || "").split("/")[0].toLowerCase();
    const subtype = String(mime || "").split("/", 2)[1] || "";
    const clean = subtype.split(";")[0].trim().toLowerCase();
    if (type === "video") {
      return {
        mp4: "mp4",
        webm: "webm",
        ogg: "ogv",
        "x-matroska": "mkv",
        quicktime: "mov",
        "x-flv": "flv",
        "mp2t": "ts",
        "3gpp": "3gp",
        "x-msvideo": "avi",
      }[clean] || "mp4";
    }
    if (type === "audio") {
      return {
        mpeg: "mp3",
        ogg: "ogg",
        opus: "opus",
        webm: "weba",
        wav: "wav",
        flac: "flac",
        aac: "aac",
      }[clean] || "mp3";
    }
    return "";
  }

  async function streamLiveMedia(url, filenameHint) {
    const readResponse = async (headers) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 90000);
      try {
        const response = await fetch(url, {
          credentials: "same-origin",
          headers: headers,
          signal: controller.signal,
        });
        clearTimeout(timer);
        return { response: response, error: "" };
      } catch (error) {
        clearTimeout(timer);
        return {
          response: null,
          error:
            error && error.name === "AbortError"
              ? "playback link timed out"
              : "playback link unavailable",
        };
      }
    };
    const statusText = (response) =>
      response ? "playback link rejected (HTTP " + response.status + ")" : "playback link unavailable";

    let outcome = await readResponse(undefined);
    let response = outcome.response;
    if (!response || !response.ok || !response.body) {
      // Some proxies reject a Range-less GET while serving a Range request
      // (older Telegram streaming endpoints); retry once before giving up.
      const ranged = await readResponse({ "Range": "bytes=0-" });
      if (ranged.response && ranged.response.ok && ranged.response.body) {
        response = ranged.response;
      } else {
        return { ok: false, message: statusText(response || ranged.response) };
      }
    }
    const mimeType = response.headers.get("Content-Type") || "";
    const detectedType = /^audio/i.test(mimeType) ? "audio" : "video";
    const extension = mimeToExtension(mimeType);
    const filename = (filenameHint && filenameHint.trim())
      ? filenameHint.trim()
      : "telegram-media" + (extension ? "." + extension : ".mp4");
    const key = "live-" + Date.now().toString(36) + "-" + Math.floor(Math.random() * 1e9).toString(36);
    const reader = response.body.getReader();
    let raw = new Uint8Array(0);
    let rawLen = 0;
    let index = 0;
    let totalBytes = 0;
    // Flush in multiples of 3 bytes so base64 quad boundaries never land in
    // the middle of a chunk: concatenated chunk payloads stay valid base64.
    const FLUSH = 3 * BLOB_CHUNK_BYTES;

    const abort = () => {
      try {
        reader.cancel().catch(() => {});
      } catch (error) {
        /* reader already closed */
      }
      return { ok: false, message: "stream interrupted" };
    };
    const flush = async () => {
      if (rawLen < 3) {
        return true;
      }
      const flushLen = Math.floor(rawLen / 3) * 3;
      const bytes = raw.subarray(0, flushLen);
      raw = raw.subarray(flushLen);
      rawLen = rawLen - flushLen;
      const ack = await sendAwait({
        type: "capture_chunk",
        capture_key: key,
        index: index,
        total: 0,
        last: false,
        chunk: arrayBufferToBase64(bytes),
        url: url,
        filename: filename,
        mime_type: mimeType,
        detected_type: detectedType,
        referrer: location.href,
        source: "context_menu",
      });
      index += 1;
      return !ack || ack.type !== "capture_chunk_error";
    };

    try {
      for (;;) {
        const step = await reader.read();
        if (step.done) {
          break;
        }
        totalBytes += step.value.byteLength;
        if (totalBytes > STREAM_CAPTURE_MAX_BYTES) {
          return abort();
        }
        const merged = new Uint8Array(rawLen + step.value.byteLength);
        merged.set(raw, 0);
        merged.set(step.value, rawLen);
        raw = merged;
        rawLen = merged.byteLength;
        if (rawLen >= FLUSH && !(await flush())) {
          return abort();
        }
      }
      if (!(await flush())) {
        return abort();
      }
      const last = await sendAwait({
        type: "capture_chunk",
        capture_key: key,
        index: index,
        total: 0,
        last: true,
        chunk: "",
        url: url,
        filename: filename,
        mime_type: mimeType,
        detected_type: detectedType,
        referrer: location.href,
        source: "context_menu",
      });
      if (!last || last.type === "capture_chunk_error") {
        return abort();
      }
    } catch (error) {
      return abort();
    }
    return { ok: true };
  }

  async function handleFetchBlob(message, sendResponse) {
    const url = String((message && message.url) || "");
    const requestId = String((message && message.request_id) || "");
    const isBlob = /^blob:/i.test(url);
    const isHttp = /^https?:/i.test(url);
    if (!isBlob && !isHttp) {
      sendResponse({ type: "fetch_blob_response", ok: false, message: "Unsupported URL" });
      return;
    }

    if (isHttp) {
      // Session-bound proxy link: follow the redirect (same-origin cookies are
      // sent automatically) and report the final CDN URL for the app to
      // download with the page's cookies.
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      try {
        const response = await fetch(url, {
          credentials: "same-origin",
          headers: { "Range": "bytes=0-0" },
          signal: controller.signal,
        });
        clearTimeout(timer);
        const finalUrl = String(response.url || "");
        if (/^https?:/i.test(finalUrl) && finalUrl !== url) {
          sendResponse({
            type: "fetch_blob_response",
            ok: true,
            resolved_url: finalUrl,
            mime_type: response.headers.get("Content-Type") || "",
          });
          return;
        }
        if (!response.ok) {
          // Some servers reject a Range probe yet accept a plain GET, and a
          // Telegram proxy link can answer with a fresh redirect on a real
          // request, so retry without the Range header before giving up.
          try {
            const plain = await fetch(url, { credentials: "same-origin" });
            if (plain.ok) {
              const plainFinal = String(plain.url || "");
              if (/^https?:/i.test(plainFinal) && plainFinal !== url) {
                sendResponse({
                  type: "fetch_blob_response",
                  ok: true,
                  resolved_url: plainFinal,
                  mime_type: plain.headers.get("Content-Type") || "",
                });
                return;
              }
              const plainBytes = new Uint8Array(await plain.arrayBuffer());
              if (plainBytes.length) {
                const plainResult = await shipBlobBytes(
                  url,
                  requestId,
                  plainBytes,
                  plain.headers.get("Content-Type") || ""
                );
                sendResponse({ type: "fetch_blob_response", ok: plainResult.ok, ...plainResult });
                return;
              }
            }
          } catch (error) {
            /* fall through to the message below */
          }
          sendResponse({
            type: "fetch_blob_response",
            ok: false,
            message: /web\.telegram\.org/i.test(url)
              ? "The Telegram link has expired; open the video in Telegram and copy its address again"
              : "The link is no longer valid",
          });
          return;
        }
        // No redirect: the proxy streams the bytes directly, same-origin, so
        // read and ship them like a blob. The probe above asked for a 1-byte
        // Range, so fetch the full body now.
        const full = await fetch(url, { credentials: "same-origin" });
        const buffer = await full.arrayBuffer();
        const bytes = new Uint8Array(buffer);
        if (!bytes.length) {
          sendResponse({ type: "fetch_blob_response", ok: false, message: "The content is empty" });
          return;
        }
        const result = await shipBlobBytes(
          url,
          requestId,
          bytes,
          full.headers.get("Content-Type") || ""
        );
        sendResponse({ type: "fetch_blob_response", ok: result.ok, ...result });
        return;
      } catch (error) {
        clearTimeout(timer);
        sendResponse({
          type: "fetch_blob_response",
          ok: false,
          message: String(error && error.message) || "Could not read the content",
        });
        return;
      }
    }

    try {
      const response = await fetch(url);
      if (!response.ok) {
        sendResponse({ type: "fetch_blob_response", ok: false, message: "The blob URL is no longer valid" });
        return;
      }
      const buffer = await response.arrayBuffer();
      const bytes = new Uint8Array(buffer);
      if (!bytes.length) {
        sendResponse({ type: "fetch_blob_response", ok: false, message: "The blob is empty" });
        return;
      }
      const result = await shipBlobBytes(
        url,
        requestId,
        bytes,
        response.headers.get("Content-Type") || ""
      );
      sendResponse({ type: "fetch_blob_response", ok: result.ok, ...result });
    } catch (error) {
      sendResponse({
        type: "fetch_blob_response",
        ok: false,
        message: String(error && error.message) || "Could not read the blob",
      });
    }
  }

  async function handlePageContextFetch(message, sendResponse) {
    const url = String((message && message.url) || "");
    const requestId = String((message && message.request_id) || "");
    if (!/^https?:/i.test(url)) {
      sendResponse({ type: "fetch_blob_response", ok: false, message: "Unsupported URL" });
      return;
    }
    // HLS manifests/segments behind Cloudflare validate the page that plays
    // them, so this runs in the player page's own context where the challenge
    // cookies live. ``unsafe-url`` keeps the page URL in the Referer the CDN
    // expects (the fetch ``referrer`` option is the only way to set it).
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120000);
    try {
      const response = await fetch(url, {
        credentials: "include",
        redirect: "follow",
        referrer: location.href,
        referrerPolicy: "unsafe-url",
        signal: controller.signal,
      });
      clearTimeout(timer);
      if (!response.ok) {
        // Tokenized HLS CDNs can briefly return 403 while the player context
        // is refreshing its challenge/session state. Give the real player
        // page a short opportunity to retry before handing the failure back
        // to the HLS engine. This does not bypass access controls; it simply
        // retries the request with the browser's existing session.
        if (response.status === 401 || response.status === 403) {
          await new Promise((resolve) => setTimeout(resolve, 750));
          try {
            const retry = await fetch(url, {
              credentials: "include",
              redirect: "follow",
              cache: "no-store",
              referrer: location.href,
              referrerPolicy: "unsafe-url",
              signal: controller.signal,
            });
            if (retry.ok) {
              const retryBuffer = await retry.arrayBuffer();
              const retryBytes = new Uint8Array(retryBuffer);
              if (retryBytes.length) {
                const retryResult = await shipBlobBytes(
                  url, requestId, retryBytes,
                  retry.headers.get("Content-Type") || ""
                );
                sendResponse({ type: "fetch_blob_response", ok: retryResult.ok, ...retryResult });
                return;
              }
            }
          } catch (retryError) {
            // Fall through to the original status below.
          }
        }
        sendResponse({
          type: "fetch_blob_response",
          ok: false,
          message: "page context: HTTP " + response.status,
          http_status: response.status,
        });
        return;
      }
      const buffer = await response.arrayBuffer();
      const bytes = new Uint8Array(buffer);
      if (!bytes.length) {
        sendResponse({ type: "fetch_blob_response", ok: false, message: "The content is empty" });
        return;
      }
      const result = await shipBlobBytes(
        url,
        requestId,
        bytes,
        response.headers.get("Content-Type") || ""
      );
      sendResponse({ type: "fetch_blob_response", ok: result.ok, ...result });
    } catch (error) {
      clearTimeout(timer);
      sendResponse({
        type: "fetch_blob_response",
        ok: false,
        message: String(error && error.message) || "Could not read the content",
      });
    }
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message && message.type === "fetch_blob") {
      handleFetchBlob(message, sendResponse);
      return true;
    }
    if (message && message.type === "fetch_blob_page") {
      handlePageContextFetch(message, sendResponse);
      return true;
    }
    if (message && message.type === "probe_redirect") {
      probeRedirect(String((message && message.url) || "")).then((result) => {
        sendResponse(result);
      });
      return true;
    }
    if (message && message.type === "live_capture") {
      streamLiveMedia(
        String((message && message.url) || ""),
        String((message && message.filename) || "")
      ).then((result) => {
        sendResponse(result);
      });
      return true;
    }
    if (message && message.type === "mc_player_capture_request") {
      // A blob/MSE player can be discovered before the normal page detector has
      // populated the persistent queue. The background worker sends this to
      // the exact frame containing the player so the page-level detection is
      // re-offered immediately.
      resetSession();
      scan();
      sendResponse({ type: "mc_player_capture_request_ok", url: location.href });
      return false;
    }
    if (message && message.type === "rescan_page") {
      // The popup's Refresh: forget what was already reported so the same page
      // is detected again from scratch.
      resetSession();
      scan();
      sendResponse({ type: "rescan_page_ok", url: location.href });
      return false;
    }
    return false;
  });

  function pageTitle() {
    const og = document.querySelector('meta[property="og:title"]');
    const title = (og && og.content) || document.title || "";
    return title.replace(/\s+/g, " ").trim().slice(0, 120);
  }

  // ----- entry helpers -----

  function directEntry(url, detectedType) {
    return {
      url: url,
      filename: filenameOf(url),
      detected_type: detectedType || typeFor(extensionOf(url)) || "file",
    };
  }

  function pageEntry() {
    return {
      url: location.href,
      filename: pageTitle(),
      detected_type: "video",
    };
  }

  // The shared classifier is the one place that decides what a detected
  // resource *is* (see categories.js). The content script only needs the noise
  // half of it: tracking pixels, spacers, favicons and 1x1 beacons are
  // technically images but would bury the real ones in the Images category.
  const HAS_CATEGORIES = typeof MagnetoclipCategories !== "undefined";

  function isDetectedNoise(url) {
    if (!HAS_CATEGORIES) {
      return false;
    }
    const category = MagnetoclipCategories.classify({ url: url });
    if (category !== "images") {
      return false;
    }
    return MagnetoclipCategories.isImageNoise({ url: url });
  }

  function addDirect(direct, url, detectedType) {
    // Site chrome masquerading as media: Telegram's notification sound and
    // bundled UI assets show up as ordinary resource entries.
    const clean = (url || "").split(/[?#]/)[0];
    if (/notification\.(mp3|ogg|wav)$/i.test(clean) || /\/(a|k)\/assets\//i.test(clean)) {
      return;
    }
    if (HAS_CATEGORIES && MagnetoclipCategories.isUiChromeUrl({ url: url })) {
      return;
    }
    // A tab that IS a bare Telegram media URL (opened purely for link
    // resolution) must not offer anything as a user-facing capture.
    if (isBareTelegramMediaPage()) {
      return;
    }
    if (isDetectedNoise(url)) {
      return;
    }
    if (!direct.has(url)) {
      direct.set(url, detectedType);
    }
  }

  function isBareTelegramMediaPage() {
    if (!location || !location.hostname) {
      return false;
    }
    if (!location.hostname.endsWith("telegram.org")) {
      return false;
    }
    const path = (location.pathname || "").toLowerCase();
    return (
      path.includes("/progressive/") ||
      path.includes("/stream/") ||
      path.includes("/api/files/")
    );
  }

  function mediaSrcs(element) {
    const sources = [];
    const direct = element.currentSrc || element.getAttribute("src") || "";
    if (direct) {
      sources.push(direct);
    }
    for (const source of element.querySelectorAll("source[src]")) {
      const value = source.getAttribute("src") || "";
      if (value) {
        sources.push(value);
      }
    }
    return sources;
  }

  // ----- detection -----

  function scanAnchors(direct, manifests) {
    for (const link of document.querySelectorAll("a[href]")) {
      let url = "";
      try {
        url = link.href || "";
      } catch (error) {
        continue;
      }
      if (!isHttpUrl(url)) {
        continue;
      }
      const ext = extensionOf(url);
      if (STREAM_EXTS.has(ext)) {
        manifests.add(url);
        continue;
      }
      if (!DOWNLOADABLE_EXTS.has(ext)) {
        continue;
      }
      addDirect(direct, url, typeFor(ext));
    }
  }

  function scanMediaElements(direct, manifests, blobDetected) {
    for (const element of document.querySelectorAll("video, audio")) {
      const isVideo = element.tagName.toLowerCase() === "video";
      const fallbackType = isVideo ? "video" : "audio";
      if (isVideo) {
        diagnoseTelegramVideoElement(element, "scan");
      }
      for (const src of mediaSrcs(element)) {
        if (src.startsWith("blob:")) {
          blobDetected.value = true;
          captureBlobUrl(src, {
            expected_type: fallbackType,
            width: element.videoWidth || 0,
            height: element.videoHeight || 0,
          }, cachedTelegramBlobObject(src));
          continue;
        }
        if (!isHttpUrl(src)) {
          continue;
        }
        const ext = extensionOf(src);
        if (STREAM_EXTS.has(ext)) {
          manifests.add(src);
          continue;
        }
        if (SEGMENT_EXTS.has(ext)) {
          continue;
        }
        if (DOWNLOADABLE_EXTS.has(ext)) {
          addDirect(direct, src, typeFor(ext));
          continue;
        }
        // Extensionless media URLs (Telegram/Snapchat CDNs) still count when
        // they come straight from a media element. Telegram's stream proxies
        // are resolved to their real CDN file before they can be offered.
        if (isTelegramStreamProxy(src)) {
          resolveTelegramStream(src, direct, fallbackType);
          continue;
        }
        addDirect(direct, src, fallbackType);
      }
    }
  }

  function scanImages(direct) {
    for (const image of document.querySelectorAll("img")) {
      const src = image.currentSrc || image.getAttribute("src") ||
        image.getAttribute("data-src") || "";
      if (src.startsWith("blob:")) {
        captureBlobUrl(src, {
          expected_type: "image",
          width: image.naturalWidth || parseInt(image.getAttribute("width"), 10) || 0,
          height: image.naturalHeight || parseInt(image.getAttribute("height"), 10) || 0,
        }, cachedTelegramBlobObject(src));
        continue;
      }
      if (!isHttpUrl(src) || direct.has(src)) {
        continue;
      }
      const size = image.naturalWidth || parseInt(image.getAttribute("width"), 10) || 0;
      if (size < MIN_IMAGE_SIZE) {
        continue;
      }
      addDirect(direct, src, "image");
    }
  }

  // Telegram Web K renders many photos as CSS background-image blob URLs on
  // plain divs instead of <img> elements; those never reach scanImages. Inline
  // style attributes serialize whatever CSSOM set, so a selector over them
  // finds these viewers cheaply.
  function scanBackgroundBlobs() {
    for (const element of document.querySelectorAll('[style*="blob:"]')) {
      const style = element.getAttribute("style") || "";
      const match = /url\((['"]?)(blob:[^)'"]+)\1\)/i.exec(style);
      if (!match) {
        continue;
      }
      captureBlobUrl(match[2], {
        expected_type: "image",
        width: element.clientWidth || 0,
        height: element.clientHeight || 0,
      }, cachedTelegramBlobObject(match[2]));
    }
  }

  // Anchors flagged as downloads (``download`` attribute or an aria-label that
  // mentions downloading). Media-sharing sites and Telegram document lists use
  // these instead of plain links with a file extension.
  function scanDownloadAnchors(direct) {
    for (const link of document.querySelectorAll("a[href]")) {
      const isDownloadLink =
        link.hasAttribute("download") ||
        /download/i.test(link.getAttribute("aria-label") || "");
      if (!isDownloadLink) {
        continue;
      }
      let url = "";
      try {
        url = link.href || "";
      } catch (error) {
        continue;
      }
      if (!isHttpUrl(url) || direct.has(url)) {
        continue;
      }
      const ext = extensionOf(url);
      addDirect(direct, url, ext ? typeFor(ext) : "file");
    }
  }

  // Streaming/anime/movie sites often embed the player in an <iframe> pointing
  // to a separate player host. When a video element or manifest is not visible
  // in the top-level DOM (the player lives in the iframe), capturing the iframe
  // URL lets MagnetoClip attempt extraction on the embedded player page too.
  function frameCandidateSrc(frame) {
    // Player embeds are frequently lazy-loaded: the visible `src` is
    // "about:blank" and the real URL lives in a data attribute (LiteSpeed
    // Cache on WordPress anime sites uses `data-litespeed-src`, hdtoday uses
    // `data-embed`). Try the real src first, then the lazy attributes.
    const candidates = [];
    try {
      if (frame.src) {
        candidates.push(frame.src);
      }
    } catch (error) {
      /* ignore */
    }
    for (const attr of [
      "data-litespeed-src",
      "data-src",
      "data-embed",
      "data-lazy-src",
      "data-original",
    ]) {
      const value = frame.getAttribute(attr) || "";
      if (value) {
        candidates.push(value);
      }
    }
    for (const candidate of candidates) {
      if (isHttpUrl(candidate)) {
        return candidate;
      }
    }
    return "";
  }

  function scanStreamingIframes(manifests, direct) {
    const pageHost = hostOf(location.href);
    if (!isOnStreamingSiteHost(pageHost)) {
      return;
    }
    for (const frame of document.querySelectorAll("iframe")) {
      const src = frameCandidateSrc(frame);
      if (!src) {
        continue;
      }
      const ext = extensionOf(src);
      if (STREAM_EXTS.has(ext)) {
        manifests.add(src);
        if (direct) {
          addDirect(direct, src, "video");
        }
        continue;
      }
      if (isStreamingEmbedIframe(src)) {
        // A recognized player embed is a strong signal that video media is on
        // this page, even when the <video> lives in the cross-origin iframe.
        manifests.add(src);
        if (direct) {
          addDirect(direct, src, "video");
        }
      }
    }
  }

  // Guess whether a URL points at a streaming video player embed, based on the
  // real embed host domains these sites use (animekai -> megaplay/megaup;
  // hdtoday/flixhq -> megacloud/mcloud/vidcloud/upstream/vidsrc/...).
  function isStreamingEmbedIframe(url) {
    const path = (url.split(/[?#]/)[0] || "").toLowerCase();
    const host = hostOf(url);
    const playerHostMarkers = [
      "e1-player", "ntplayer", "vidlink", "embedrum", "megaup", "megacloud",
      "megaplay", "streamhub", "vidcloud", "mcloud", "upstream", "nvide",
      "filelions", "maru", "vidsrc", "2embed", "gogo-play", "vidrock",
      "vidnest", "vidking", "vidfast", "vidup", "videasy", "multiembed",
      "superflix", "peachify", "111movies", "embedphp", "embedsb", "embedme",
      "/e/", "/embed/", "/embed-2/", "/play/", "/v/", "/stream/", "/player/",
      "embed.php", "/embed.php",
    ];
    if (playerHostMarkers.some((m) => path.includes(m))) {
      return true;
    }
    const playerHostSuffixes = ["player", "embed", "play", "host"];
    return playerHostSuffixes.some(
      (s) =>
        host === s ||
        host.endsWith("." + s + ".com") ||
        host.endsWith(s + ".cc") ||
        host.endsWith(s + ".to") ||
        host.endsWith(s + ".su") ||
        host.endsWith(s + ".buzz")
    );
  }

  // True when the current frame is a standalone video-player embed (megaplay,
  // megacloud, vidcloud, vidsrc, ...) hosting the actual <video> and its HLS
  // manifest. The content script runs in these frames (all_frames) so it can
  // read the real .m3u8/.mpd URL that the top-level streaming page hides inside
  // a cross-origin iframe.
  function isPlayerEmbedHost(host) {
    const playerHostMarkers = [
      "megaplay", "megacloud", "megaup", "mcloud", "vidcloud", "upstream",
      "vidsrc", "2embed", "ntplayer", "e1-player", "vidlink", "streamhub",
      "vidrock", "vidnest", "vidking", "vidfast", "vidup", "videasy",
      "multiembed", "superflix", "peachify", "gogo-play", "filelions",
      "maru", "nvide", "embedsb", "embedme", "rutube",
    ];
    return playerHostMarkers.some((m) => host.includes(m));
  }

  function isTopFrame() {
    try {
      return window === window.top;
    } catch (error) {
      // Cross-origin references to window.top throw; treat an isolated frame
      // as "not the top frame".
      return false;
    }
  }

  // In a player-embed frame the HLS/DASH manifests the player actually fetched
  // are the real media. Offer each manifest URL to MagnetoClip, which hands it
  // to yt-dlp's HLS extractor for a direct download.
  function buildPlayerFrameCaptures(direct, manifests) {
    const captures = [];
    const seen = new Set();
    for (const url of manifests) {
      if (reportedUrls.has(url) || seen.has(url)) {
        continue;
      }
      seen.add(url);
      captures.push({
        url: url,
        filename: filenameOf(url) || pageTitle(),
        detected_type: /\.mpd(?:[?#]|$)/i.test(url) ? "dash" : "hls",
      });
      if (captures.length >= MAX_CAPTURES_PER_SCAN) {
        break;
      }
    }
    return captures;
  }

  // HLS/DASH manifests are sometimes served without a discoverable extension
  // (tokenized URLs like /get_stream?token=... or playlists served as
  // application/vnd.apple.mpegurl). In a player-embed frame these path markers
  // and MIME hints are a strong signal the request is the media manifest.
  function isHlsManifestUrl(url) {
    const clean = (url.split(/[?#]/)[0] || "").toLowerCase();
    const full = url.toLowerCase();
    return (
      /\.m3u8?$/.test(clean) ||
      /\.mpd$/.test(clean) ||
      /\.mpd\?/.test(full) ||
      /\/(?:hls|live|dash|index|playlist|master)(?:[\/?]|$)/.test(full) ||
      /\.m3u8\?/.test(full) ||
      /playlist\.m3u8/.test(full)
    );
  }

  // Player embeds sometimes disguise the manifest behind a token URL or never
  // surface it on the <video> element. Sweep resource-timing entries for URLs
  // that look like HLS/DASH manifests regardless of their extension.
  function scanPlayerFrameManifests(manifests) {
    const now = performance.getEntriesByType("resource") || [];
    for (const entry of now) {
      const url = String(entry.name || "");
      if (!isHttpUrl(url)) {
        continue;
      }
      const initiator = String(entry.initiatorType || "").toLowerCase();
      if (initiator !== "xmlhttprequest" && initiator !== "fetch" && initiator !== "video" && initiator !== "media") {
        continue;
      }
      if (isHlsManifestUrl(url) || extensionOf(url) === "m3u8" || extensionOf(url) === "mpd") {
        if (SEGMENT_EXTS.has(extensionOf(url))) {
          continue;
        }
        manifests.add(url);
      }
    }
  }

  function scanPerformanceResources(direct, manifests) {    const now = performance.getEntriesByType("resource") || [];
    const seenNow = new Set();
    const entries = [];
    for (const entry of now) {
      const name = String(entry.name || "");
      if (seenNow.has(name)) {
        continue;
      }
      seenNow.add(name);
      entries.push({
        name: name,
        initiatorType: String(entry.initiatorType || "").toLowerCase(),
      });
    }
    for (const [name, initiatorType] of observedResources) {
      if (seenNow.has(name)) {
        continue;
      }
      seenNow.add(name);
      entries.push({ name: name, initiatorType: initiatorType });
    }
    for (const entry of entries) {
      const url = entry.name || "";
      diagnoseTelegramNetworkResource(url, entry.initiatorType, entry);
      if (!isHttpUrl(url)) {
        continue;
      }
      const initiator = entry.initiatorType || "";
      const ext = extensionOf(url);
      if (STREAM_EXTS.has(ext)) {
        manifests.add(url);
        continue;
      }
      // Telegram's stream proxy requests show up in the network log too; they
      // are resolved to their real CDN file regardless of initiator type.
      if (isTelegramStreamProxy(url)) {
        resolveTelegramStream(url, direct, initiator === "audio" ? "audio" : "video");
        continue;
      }
      if (initiator !== "video" && initiator !== "audio" && initiator !== "media") {
        // Blob-backed players (Facebook reels, X videos, Telegram) fetch their
        // media with a generic initiator type; media-CDN URLs are still real
        // files worth capturing.
        if (!isMediaCdnUrl(url)) {
          continue;
        }
      }
      if (SEGMENT_EXTS.has(ext)) {
        continue;
      }
      const type = typeFor(ext);
      addDirect(direct, url, type === "file" ? (initiator === "audio" ? "audio" : "video") : type);
    }
  }

  function buildCaptures(direct, manifests, blobDetected) {
    const captures = [];
    const pageHost = hostOf(location.href);

    // A blob/HLS media stream on a resolvable platform becomes a post-URL
    // capture so MagnetoClip can extract the media with its stream resolver.
    const wantsPageCapture = blobDetected.value || manifests.size > 0;
    if (wantsPageCapture && isOnYtdlpHost(pageHost) && !reportedPageHosts.has(pageHost)) {
      captures.push(pageEntry());
    }

    const candidates = [];
    for (const [url, type] of direct) {
      candidates.push(directEntry(url, type));
    }
    // Prefer video, then audio, then images so a feed of mixed media does not
    // bury the important entries.
    candidates.sort((a, b) => {
      const pa = TYPE_PRIORITY[a.detected_type] !== undefined ? TYPE_PRIORITY[a.detected_type] : 3;
      const pb = TYPE_PRIORITY[b.detected_type] !== undefined ? TYPE_PRIORITY[b.detected_type] : 3;
      return pa - pb;
    });

    for (const candidate of candidates) {
      if (captures.length >= MAX_CAPTURES_PER_SCAN) {
        break;
      }
      if (reportedUrls.has(candidate.url)) {
        continue;
      }
      captures.push(candidate);
    }
    return captures;
  }

  function reportCaptures(captures) {
    if (!captures.length) {
      return;
    }
    const pageHost = hostOf(location.href);
    for (const capture of captures) {
      if (capture.url === location.href) {
        reportedPageHosts.add(pageHost);
      }
      reportedUrls.add(capture.url);
    }
    safeSend({
      type: "social_capture",
      url: location.href,
      top_frame: isTopFrame(),
      files: captures,
    });
  }

  // A page capture for streaming platforms MagnetoClip can resolve with yt-dlp.
  // As soon as a video/audio element is actually playing (or a blob:/HLS stream
  // is present), the page URL is offered so MagnetoClip extracts the real media.
  // Also covers streaming sites (anime/movie) that serve HLS/DASH manifests that
  // MagnetoClip attempts to resolve via yt-dlp's generic extractor. An opaque
  // blob/MSE player on *any* host is treated the same way: there is no direct
  // URL to offer, so the page is the only handle, and yt-dlp may still resolve
  // it with its generic extractor.
  function buildYtdlpCaptures(manifests, blobDetected) {
    const pageHost = hostOf(location.href);
    const recognizedHost = isOnYtdlpHost(pageHost) || isOnStreamingSiteHost(pageHost);
    const isTelegramAppPage = /(^|\.)web\.telegram\.org$/i.test(String(location.hostname || ""));
    // Telegram Web's /k/#chat and /a/#chat shell URLs are not downloadable
    // videos. For Telegram, require a real queued media/network URL or a finite
    // Blob capture instead of adding the entire chat page as a video target.
    const onResolvableHost = recognizedHost || (blobDetected.value && !isTelegramAppPage);
    if (!onResolvableHost || reportedUrls.has(location.href)) {
      return [];
    }
    const anyPlaying = Array.from(
      document.querySelectorAll("video, audio")
    ).some((element) => !element.paused);
    if (!anyPlaying && !blobDetected.value && manifests.size === 0) {
      return [];
    }
    const captures = [pageEntry()];
    // Forward collected manifest/embed URLs so the app can try them directly
    // instead of only the page URL (which yt-dlp may not resolve correctly).
    for (const manifestUrl of manifests) {
      if (!reportedUrls.has(manifestUrl)) {
        captures.push({
          url: manifestUrl,
          detected_type: /\.mpd(?:[?#]|$)/i.test(manifestUrl) ? "dash" : "hls",
        });
      }
    }
    return captures;
  }

  function reportPageScan(direct, enabled) {
    if (!enabled) {
      return;
    }
    const files = [];
    const seen = new Set();
    for (const [url, type] of direct) {
      if (reportedUrls.has(url) || seen.has(url)) {
        continue;
      }
      seen.add(url);
      files.push(directEntry(url, type));
      if (files.length >= MAX_PAGE_SCAN_FILES) {
        break;
      }
    }
    if (!files.length) {
      return;
    }
    for (const file of files) {
      reportedUrls.add(file.url);
    }
    safeSend({
      type: "page_scan",
      url: location.href,
      top_frame: isTopFrame(),
      files: files,
    });
  }

  let lastDetectionReport = "";

  function reportDetectionUpdate(direct, manifests) {
    const files = [];
    const seen = new Set();
    for (const [url, type] of direct) {
      if (seen.has(url)) {
        continue;
      }
      seen.add(url);
      files.push(directEntry(url, type));
      if (files.length >= MAX_PAGE_SCAN_FILES) {
        break;
      }
    }
    for (const url of manifests) {
      if (seen.has(url) || files.length >= MAX_PAGE_SCAN_FILES) {
        continue;
      }
      seen.add(url);
      files.push({ url: url, filename: filenameOf(url), detected_type: "stream" });
    }
    prependPageCapture(files);
    const snapshot = JSON.stringify({
      url: location.href,
      title: pageTitle(),
      files: files,
    });
    if (snapshot === lastDetectionReport) {
      return;
    }
    lastDetectionReport = snapshot;
    safeSend({
      type: "detection_update",
      url: location.href,
      title: pageTitle(),
      files: files,
    });
  }

  // A page capture was just sent for a resolvable streaming page; surface it as
  // a detected video even though the real source is a hidden blob:/MSE stream.
  function prependPageCapture(files) {
    const pageHost = hostOf(location.href);
    if (
      (isOnYtdlpHost(pageHost) || isOnStreamingSiteHost(pageHost)) &&
      reportedUrls.has(location.href)
    ) {
      files.unshift({
        url: location.href,
        filename: pageTitle(),
        detected_type: "video",
      });
    }
    return files;
  }

  // ---- player overlay signals ----

  // The player overlay (player_overlay.js) needs this frame's media evidence to
  // associate a <video> with the right queue entry before it can show its
  // button. A tiny read-only bridge avoids duplicating the network observer
  // and the scan results; the overlay only ever *reads* these.
  function publishScanSignals(direct, manifests) {
    const urls = [];
    const seen = new Set();
    for (const [url] of direct) {
      if (!seen.has(url)) {
        seen.add(url);
        urls.push(url);
      }
    }
    for (const manifestUrl of manifests) {
      if (!seen.has(manifestUrl)) {
        seen.add(manifestUrl);
        urls.push(manifestUrl);
      }
    }
    window.__mcScanSignalUrls = urls;
  }

  function mediaNetworkSignalUrls() {
    const urls = [];
    for (const [url, initiator] of observedResources) {
      if (
        /^https?:/i.test(url) &&
        looksLikeMediaUrl(url) &&
        !(HAS_CATEGORIES && MagnetoclipCategories.isUiChromeUrl({ url: url }))
      ) {
        urls.push(url);
      }
    }
    return urls;
  }

  function ensureContentSignals() {
    if (window.__mcContentSignals) {
      return;
    }
    window.__mcContentSignals = {
      network: mediaNetworkSignalUrls,
      scan: function () {
        return window.__mcScanSignalUrls || [];
      },
      // Which kind of host this frame is on: resolvable (yt-dlp/streaming),
      // social, or a plain page. The overlay closes its empty-result loop only
      // when capturing the page could plausibly help.
      hostKind: function () {
        const host = hostOf(location.href);
        if (isOnYtdlpHost(host) || isOnStreamingSiteHost(host)) {
          return "resolvable";
        }
        return isOnSocialHost(host) ? "social" : "plain";
      },
      // The overlay asks for a fresh page capture when the queue no longer
      // holds the current page's yt-dlp target (the app consumed the category,
      // or the queue was reset). Forgetting the page re-runs the capture the
      // next scan; background dedupe keeps it a no-op when nothing changed.
      requestCapture: function () {
        if (contextGone) {
          return false;
        }
        reportedUrls.delete(location.href);
        reportedPageHosts.delete(hostOf(location.href));
        scheduleScan();
        return true;
      },
    };
  }

  function scan() {
    if (contextGone) {
      return;
    }
    const direct = new Map();
    const manifests = new Set();
    const blobDetected = { value: false };

    const pageHost = hostOf(location.href);
    const topFrame = isTopFrame();
    const embedFrame = !topFrame && isPlayerEmbedHost(pageHost);

    // Non-top frames that are not a recognized video-player embed have nothing
    // to contribute (ads, widgets, cross-origin chrome); skip them so their
    // noise never reaches MagnetoClip.
    if (!topFrame && !embedFrame) {
      return;
    }

    scanAnchors(direct, manifests);
    scanMediaElements(direct, manifests, blobDetected);
    scanImages(direct);
    scanBackgroundBlobs();
    scanDownloadAnchors(direct);
    scanStreamingIframes(manifests, direct);
    scanPerformanceResources(direct, manifests);
    publishScanSignals(direct, manifests);

    if (embedFrame) {
      // In a player-embed frame the fetched HLS/DASH manifests are the real
      // media: offer them directly instead of the (unresolvable) page URL.
      scanPlayerFrameManifests(manifests);
      reportCaptures(buildPlayerFrameCaptures(direct, manifests));
      reportDetectionUpdate(direct, manifests);
      return;
    }

    if (social) {
      reportCaptures(buildCaptures(direct, manifests, blobDetected));
    } else {
      reportCaptures(buildYtdlpCaptures(manifests, blobDetected));
    }
    reportPageScan(direct, !social && !isOnYtdlpHost(pageHost) && !isOnStreamingSiteHost(pageHost));
    reportDetectionUpdate(direct, manifests);
  }

  // ----- watching dynamic content -----

  function resetSession() {
    reportedUrls.clear();
    reportedPageHosts.clear();
    window.__mcScanSignalUrls = [];
    lastDetectionReport = "";
  }

  function scheduleScan() {
    if (contextGone || scanTimer) {
      return;
    }
    scanTimer = setTimeout(() => {
      scanTimer = null;
      scan();
    }, 700);
  }

  function startWatching() {
    observeNetworkResources();
    domObserver = new MutationObserver(scheduleScan);
    domObserver.observe(document.documentElement, {
      childList: true,
      subtree: true,
    });
    pollInterval = setInterval(scan, 6000);

    // Fire a scan the instant a video starts playing so YouTube & co. are
    // offered without waiting for the next polling tick.
    document.addEventListener("playing", scheduleScan, true);

    const wrap = (method) => {
      const original = history[method];
      history[method] = function (...args) {
        const result = original.apply(this, args);
        resetSession();
        scheduleScan();
        return result;
      };
    };
    wrap("pushState");
    wrap("replaceState");
    window.addEventListener("popstate", () => {
      resetSession();
      scheduleScan();
    });
  }

  // ----- startup -----

  social = isOnSocialHost(hostOf(location.href));

  function start() {
    ensureContentSignals();
    installObjectUrlHook();
    startWatching();
    scan();
  }

  if (document.readyState === "complete") {
    setTimeout(start, 1200);
  } else {
    window.addEventListener("load", () => setTimeout(start, 1200));
  }
})();
