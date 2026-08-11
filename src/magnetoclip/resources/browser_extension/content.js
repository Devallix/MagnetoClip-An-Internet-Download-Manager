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

  // Media CDNs whose stream requests are often labelled with a generic
  // initiator type, so we capture them directly even when the video plays
  // through a blob:/MSE pipeline.
  const MEDIA_CDN_HOSTS = new Set([
    "fbcdn.net",            // Facebook
    "twimg.com",            // X / Twitter (video.twimg.com)
    "cdn.telegram.org",     // Telegram
    "telegram.org",
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
  const MAX_PAGE_SCAN_FILES = 20;
  const MIN_IMAGE_SIZE = 120;
  const TYPE_PRIORITY = { video: 0, audio: 1, image: 2 };

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
  function safeSend(message) {
    if (contextGone) {
      return;
    }
    try {
      const result = chrome.runtime.sendMessage(message);
      if (result && typeof result.catch === "function") {
        result.catch((error) => {
          if (/Extension context invalidated/i.test(String(error))) {
            stopScanning();
          }
        });
      }
    } catch (error) {
      if (/Extension context invalidated/i.test(String(error && error.message))) {
        stopScanning();
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
    // Telegram's internal stream proxy is not a direct file.
    if (host.endsWith("telegram.org") && path.includes("/k/stream/")) {
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
        /\/ext_tw_video\/|\/amplify_video\/|\/tweet_video\//.test(path) ||
        MEDIA_EXTS.has(ext)
      );
    }
    // Telegram serves every uploaded file under /file/ (photos, videos, docs).
    if (host.endsWith("cdn.telegram.org")) {
      return path.includes("/file/") && MEDIA_EXTS.has(ext);
    }
    return MEDIA_EXTS.has(ext);
  }

  // Telegram Web plays videos through an internal /k/stream/ proxy that only
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
    const timer = setTimeout(() => controller.abort(), 10000);
    fetch(src, {
      credentials: "same-origin",
      headers: { "Range": "bytes=0-0" },
      signal: controller.signal,
    })
      .then((response) => {
        clearTimeout(timer);
        const finalUrl = String(response.url || "");
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
      .catch(() => {
        clearTimeout(timer);
      });
  }

  function extensionOf(url) {
    try {
      const clean = url.split(/[?#]/)[0];
      const match = /\.([a-z0-9]{2,8})$/i.exec(clean);
      return match ? match[1].toLowerCase() : "";
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

  function addDirect(direct, url, detectedType) {
    if (!direct.has(url)) {
      direct.set(url, detectedType);
    }
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
      for (const src of mediaSrcs(element)) {
        if (src.startsWith("blob:")) {
          blobDetected.value = true;
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
        // they come straight from a media element. Telegram's /k/stream proxy
        // URL is resolved to its real CDN file before it can be offered.
        if (hostOf(src).endsWith("telegram.org") && src.includes("/k/stream/")) {
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

  function scanPerformanceResources(direct, manifests) {
    const now = performance.getEntriesByType("resource") || [];
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
      if (hostOf(url).endsWith("telegram.org") && url.includes("/k/stream/")) {
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
      files: captures,
    });
  }

  // A page capture for streaming platforms MagnetoClip can resolve with yt-dlp.
  // As soon as a video/audio element is actually playing (or a blob:/HLS stream
  // is present), the page URL is offered so MagnetoClip extracts the real media.
  function buildYtdlpCaptures(manifests, blobDetected) {
    const pageHost = hostOf(location.href);
    if (!isOnYtdlpHost(pageHost) || reportedUrls.has(location.href)) {
      return [];
    }
    const anyPlaying = Array.from(
      document.querySelectorAll("video, audio")
    ).some((element) => !element.paused);
    if (!anyPlaying && !blobDetected.value && manifests.size === 0) {
      return [];
    }
    return [pageEntry()];
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
    if (isOnYtdlpHost(pageHost) && reportedUrls.has(location.href)) {
      files.unshift({
        url: location.href,
        filename: pageTitle(),
        detected_type: "video",
      });
    }
    return files;
  }

  function scan() {
    if (contextGone) {
      return;
    }
    const direct = new Map();
    const manifests = new Set();
    const blobDetected = { value: false };

    scanAnchors(direct, manifests);
    scanMediaElements(direct, manifests, blobDetected);
    scanImages(direct);
    scanDownloadAnchors(direct);
    scanPerformanceResources(direct, manifests);

    const pageHost = hostOf(location.href);
    if (social) {
      reportCaptures(buildCaptures(direct, manifests, blobDetected));
    } else {
      reportCaptures(buildYtdlpCaptures(manifests, blobDetected));
    }
    reportPageScan(direct, !social && !isOnYtdlpHost(pageHost));
    reportDetectionUpdate(direct, manifests);
  }

  // ----- watching dynamic content -----

  function resetSession() {
    reportedUrls.clear();
    reportedPageHosts.clear();
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
    startWatching();
    scan();
  }

  if (document.readyState === "complete") {
    setTimeout(start, 1200);
  } else {
    window.addEventListener("load", () => setTimeout(start, 1200));
  }
})();
