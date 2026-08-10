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
  ]);

  // Hosts where MagnetoClip can resolve a whole page/post URL to media (via
  // yt-dlp), used when the real media is hidden behind blob:/MSE streams.
  const YTDLP_HOSTS = new Set([
    "twitter.com", "x.com",
    "facebook.com", "fb.watch",
    "instagram.com",
    "youtube.com", "youtu.be",
    "vimeo.com",
    "twitch.tv",
    "tiktok.com",
    "dailymotion.com",
    "reddit.com",
  ]);

  // HLS/DASH transport files that are useless to download on their own.
  const STREAM_EXTS = new Set(["m3u8", "m3u", "mpd"]);
  // Video segment files; captured from network resources they are almost always
  // pieces of a larger stream, so we do not offer them directly.
  const SEGMENT_EXTS = new Set(["ts", "mts", "m2ts"]);

  const MAX_CAPTURES_PER_SCAN = 4;
  const MAX_PAGE_SCAN_FILES = 20;
  const MIN_IMAGE_SIZE = 120;
  const TYPE_PRIORITY = { video: 0, audio: 1, image: 2 };

  // URLs already handed to MagnetoClip during this page session.
  const reportedUrls = new Set();
  // Social hosts already covered by a "post URL" capture.
  const reportedPageHosts = new Set();

  let social = false;
  let scanTimer = null;

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
        // they come straight from a media element.
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

  function scanPerformanceResources(direct, manifests) {
    const entries = performance.getEntriesByType("resource") || [];
    for (const entry of entries) {
      const url = String(entry.name || "");
      if (!isHttpUrl(url)) {
        continue;
      }
      const ext = extensionOf(url);
      if (STREAM_EXTS.has(ext)) {
        manifests.add(url);
        continue;
      }
      const initiator = String(entry.initiatorType || "").toLowerCase();
      if (initiator !== "video" && initiator !== "audio" && initiator !== "media") {
        continue;
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
    chrome.runtime.sendMessage({
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
    chrome.runtime.sendMessage({
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
    chrome.runtime.sendMessage({
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
    const direct = new Map();
    const manifests = new Set();
    const blobDetected = { value: false };

    scanAnchors(direct, manifests);
    scanMediaElements(direct, manifests, blobDetected);
    scanImages(direct);
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
    if (scanTimer) {
      return;
    }
    scanTimer = setTimeout(() => {
      scanTimer = null;
      scan();
    }, 700);
  }

  function startWatching() {
    const observer = new MutationObserver(scheduleScan);
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
    });
    setInterval(scan, 6000);

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
