const HOST_NAME = "com.magnetoclip.host";
const SETTINGS_STORAGE_KEY = "mc_settings";
// The app asks the extension to fetch blob: URLs the user pasted into the
// new-download field. The content script returns the whole base64 payload, but
// native messaging messages are size-limited (~1MB), so it is split into chunks
// here before being sent to the host.
const BLOB_FETCH_BASE64_CHUNK = 512 * 1024;

const pendingIntercepts = new Map();
const pendingResponses = new Map();
const tabDetections = new Map();
// Download ids the extension decided to intercept (cancelled + captured).
// Used so onDeterminingFilename only cancels downloads MagnetoClip actually
// owns; other downloads keep flowing to the browser's default downloader.
const interceptedDownloads = new Set();
// Maps a capture request id to the browser download it belongs to, so the
// response can decide whether to cancel the browser copy (or let it finish
// when MagnetoClip refuses the URL).
const interceptRequests = new Map();
// Watchdog timers per intercept request, in case the native host never answers.
const interceptTimers = new Map();
// URLs already offered to MagnetoClip from the webRequest fallback path.
const webReportedUrls = new Set();
const MAX_WEB_CAPTURES = 25;
let webCaptureCount = 0;
let nativePort = null;
let nextId = 1;
let integrationEnabled = true;
let captureEnabled = true;
let defaultDownloader = false;

function loadStoredSettings() {
  if (!chrome.storage) {
    return;
  }
  chrome.storage.local.get(SETTINGS_STORAGE_KEY, (stored) => {
    if (chrome.runtime.lastError) {
      return;
    }
    const saved = stored && stored[SETTINGS_STORAGE_KEY];
    if (!saved) {
      return;
    }
    if (typeof saved.integration_enabled === "boolean") {
      integrationEnabled = saved.integration_enabled;
    }
    if (typeof saved.capture_enabled === "boolean") {
      captureEnabled = saved.capture_enabled;
    }
    if (typeof saved.default_downloader === "boolean") {
      defaultDownloader = saved.default_downloader;
    }
  });
}

function saveSettingsToStorage() {
  if (!chrome.storage) {
    return;
  }
  chrome.storage.local.set({
    [SETTINGS_STORAGE_KEY]: {
      integration_enabled: integrationEnabled,
      capture_enabled: captureEnabled,
      default_downloader: defaultDownloader,
    },
  });
}

const MENU_ITEMS = [
  { id: "mc_download_link", title: "Download with MagnetoClip", contexts: ["link"] },
  { id: "mc_download_video", title: "Download video with MagnetoClip", contexts: ["video"] },
  { id: "mc_download_audio", title: "Download audio with MagnetoClip", contexts: ["audio"] },
  { id: "mc_download_image", title: "Download image with MagnetoClip", contexts: ["image"] }
];

function ensurePort() {
  if (nativePort) {
    return nativePort;
  }
  try {
    nativePort = chrome.runtime.connectNative(HOST_NAME);
    nativePort.onMessage.addListener((message) => {
      if (!message) {
        return;
      }
      if (message.type === "settings_ok") {
        integrationEnabled = message.integration_enabled !== false;
        captureEnabled = message.capture_enabled !== false;
        defaultDownloader = message.default_downloader === true;
        saveSettingsToStorage();
      }
      if (
        message.type === "capture_ok" ||
        message.type === "capture_pending" ||
        message.type === "capture_error" ||
        message.type === "capture_skipped"
      ) {
        const downloadId = interceptRequests.get(message.id);
        if (downloadId != null) {
          interceptRequests.delete(message.id);
          const watchdog = interceptTimers.get(message.id);
          if (watchdog) {
            clearTimeout(watchdog);
            interceptTimers.delete(message.id);
          }
          interceptedDownloads.delete(downloadId);
          if (message.type === "capture_error") {
            notify(
              "MagnetoClip",
              (message.message || "Could not download this file.") +
                " The file is downloading in your browser instead."
            );
          } else if (
            message.type === "capture_ok" ||
            message.type === "capture_pending"
          ) {
            chrome.downloads.cancel(downloadId, () => {});
          }
        }
      }
      const pending = pendingResponses.get(message.id);
      if (pending) {
        pendingResponses.delete(message.id);
        if (message.type === "error") {
          pending.reject(new Error(message.message || "MagnetoClip error"));
        } else {
          pending.resolve(message);
        }
      }
      if (message.type === "fetch_blob") {
        handleFetchBlob(message);
        return;
      }
    });
    nativePort.onDisconnect.addListener(() => {
      nativePort = null;
    });
    nativePort.postMessage({ type: "settings" });
  } catch (error) {
    nativePort = null;
  }
  return nativePort;
}

function request(type, payload, onRequestId) {
  return new Promise((resolve, reject) => {
    const port = ensurePort();
    if (!port) {
      reject(new Error("MagnetoClip is not running."));
      return;
    }
    const id = nextId++;
    if (onRequestId) {
      onRequestId(id);
    }
    pendingResponses.set(id, { resolve, reject });
    port.postMessage({ id, type, ...payload });
  });
}

// Fulfil an app request to fetch a ``blob:`` URL (pasted into the new-download
// field). The blob is routed to a content script on a tab of the same origin,
// which returns the bytes as base64; the background splits it into native
// messaging-sized chunks and streams them back to the host.
function sendFetchBlobError(requestId, message) {
  if (requestId == null) {
    return;
  }
  request("blob_fetch_result", { request_id: requestId, error: message }).catch(() => {});
}

function handleFetchBlob(message) {
  const requestId = message.request_id;
  const url = String(message.url || "");
  if (!/^blob:/i.test(url)) {
    sendFetchBlobError(requestId, "Not a blob URL");
    return;
  }
  let origin = "";
  try {
    origin = new URL(url).origin;
  } catch (error) {
    origin = "";
  }
  if (!origin) {
    sendFetchBlobError(requestId, "Invalid blob URL");
    return;
  }
  chrome.tabs.query({}, (tabs) => {
    let target = null;
    for (const tab of tabs || []) {
      if (tab.id == null || !tab.url) {
        continue;
      }
      let tabOrigin = "";
      try {
        tabOrigin = new URL(tab.url).origin;
      } catch (error) {
        /* keep scanning */
      }
      if (tabOrigin === origin) {
        target = tab;
        break;
      }
    }
    if (!target) {
      sendFetchBlobError(requestId, "No open page matches this blob URL");
      return;
    }
    chrome.tabs.sendMessage(
      target.id,
      { type: "fetch_blob", request_id: requestId, url: url },
      (response) => {
        if (
          chrome.runtime.lastError ||
          !response ||
          response.type !== "fetch_blob_response"
        ) {
          sendFetchBlobError(requestId, "The page could not provide this blob");
          return;
        }
        if (!response.ok) {
          sendFetchBlobError(
            requestId,
            response.message || "Could not read the blob"
          );
          return;
        }
        const encoded = String(response.data_base64 || "");
        if (!encoded) {
          sendFetchBlobError(requestId, "Empty blob data");
          return;
        }
        const total = Math.max(
          1,
          Math.ceil(encoded.length / BLOB_FETCH_BASE64_CHUNK)
        );
        for (let index = 0; index < total; index++) {
          const chunk = encoded.slice(
            index * BLOB_FETCH_BASE64_CHUNK,
            (index + 1) * BLOB_FETCH_BASE64_CHUNK
          );
          request("blob_fetch_chunk", {
            request_id: requestId,
            index: index,
            total: total,
            chunk: chunk,
            url: url,
            filename: guessFilename(url),
            mime_type: response.mime_type || "",
          }).catch(() => {});
        }
      }
    );
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

function guessFilename(url) {
  const clean = url.split(/[?#]/)[0];
  const name = clean.substring(clean.lastIndexOf("/") + 1);
  if (!name || !name.includes(".")) {
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

function detectFileType(url) {
  const ext = extensionOf(url);
  if (["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif", "ico", "heic"].includes(ext)) {
    return "image";
  }
  if (["mp4", "webm", "mkv", "mov", "avi", "flv", "m4v", "3gp", "ts", "mpg", "mpeg"].includes(ext)) {
    return "video";
  }
  if (["mp3", "wav", "ogg", "flac", "m4a", "aac", "opus", "wma"].includes(ext)) {
    return "audio";
  }
  return "file";
}

function notify(title, message) {
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icons/logo-128.png",
    title: title,
    message: message
  });
}

function getCookiesFor(url) {
  return new Promise((resolve) => {
    if (!chrome.cookies || !/^https?:/i.test(url)) {
      resolve("");
      return;
    }
    chrome.cookies.getAll({ url: url }, (cookies) => {
      if (chrome.runtime.lastError) {
        resolve("");
        return;
      }
      const parts = [];
      for (const cookie of cookies || []) {
        if (cookie.value === undefined || cookie.value === null) {
          continue;
        }
        parts.push(cookie.name + "=" + cookie.value);
      }
      resolve(parts.join("; "));
    });
  });
}

function withCookies(payload) {
  return getCookiesFor(payload.url).then((cookies) => {
    if (cookies) {
      payload.cookies = cookies;
    }
    return payload;
  });
}

function capture(payload) {
  return withCookies(payload).then((full) => request("capture", full));
}

function ensureMenus() {
  chrome.contextMenus.removeAll(() => {
    for (const item of MENU_ITEMS) {
      chrome.contextMenus.create({ ...item, documentUrlPatterns: ["http://*/*", "https://*/*"] });
    }
  });
}

// When the unpacked extension is reloaded, Chrome does NOT re-inject content
// scripts into already-open tabs: the old instance stays behind running on a
// dead context ("Extension context invalidated") and can no longer send
// captures, which is why downloads silently stop working on tabs that were open
// across the reload. Re-inject the current script into every http(s) tab; the
// script is idempotent, so double-injection is harmless.
function reinjectContentScripts() {
  if (!chrome.scripting) {
    return;
  }
  chrome.tabs.query({}, (tabs) => {
    for (const tab of tabs || []) {
      if (tab && tab.id != null && tab.url && /^https?:/i.test(tab.url)) {
        chrome.scripting
          .executeScript({ target: { tabId: tab.id }, files: ["content.js"] })
          .catch(() => {});
      }
    }
  });
}

chrome.runtime.onInstalled.addListener(() => {
  ensureMenus();
  reinjectContentScripts();
});
chrome.runtime.onStartup.addListener(() => {
  ensureMenus();
  reinjectContentScripts();
});

loadStoredSettings();
ensurePort();

chrome.tabs.onRemoved.addListener((tabId) => {
  tabDetections.delete(tabId);
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const url = info.linkUrl || info.srcUrl || "";
  if (!/^https?:/i.test(url)) {
    return;
  }
  capture({
    url: url,
    filename: guessFilename(url),
    referrer: tab && tab.url ? tab.url : "",
    source: "context_menu",
    detected_type: info.mediaType || "file"
  })
    .then((response) => {
      if (response.type === "capture_pending") {
        notify(
          "MagnetoClip — ready to download",
          (response.filename || url) + " is waiting for your confirmation in MagnetoClip."
        );
      } else if (response.type === "capture_error") {
        notify(
          "MagnetoClip",
          response.message || "Could not download this file."
        );
      }
    })
    .catch((error) => {
      notify("MagnetoClip unavailable", error.message);
    });
});

// ---- downloads ----

function baseName(path) {
  const parts = String(path || "").split(/[\\/]/);
  return parts[parts.length - 1] || "";
}

function sendCapture(item, filename) {
  const detectedType = detectFileType(item.url);
  withCookies({
    url: item.url,
    filename: filename,
    referrer: item.referrer || "",
    source: "extension",
    detected_type: detectedType,
  })
    .then((full) =>
      request("capture", full, (requestId) => {
        interceptRequests.set(requestId, item.id);
        // If the host never answers (e.g. it crashed mid-conversation), drop
        // the bookkeeping and let the browser download finish untouched.
        const watchdog = setTimeout(() => {
          interceptRequests.delete(requestId);
          interceptTimers.delete(requestId);
          interceptedDownloads.delete(item.id);
        }, 30000);
        interceptTimers.set(requestId, watchdog);
      })
    )
    .catch(() => {});
}

// Intercept a browser download: offer the file to MagnetoClip and cancel the
// browser copy only once MagnetoClip confirms it can download the URL. The
// browser download is left running so that a broken/error-page URL keeps its
// file (MagnetoClip answers with capture_error and the download proceeds in
// the browser). Chrome resolves the real filename in onDeterminingFilename,
// which fires just after onCreated, so the capture is deferred briefly to pick
// it up; a timeout falls back to a URL-derived name if that event never
// arrives. Returns true if the download was claimed.
function interceptDownload(item) {
  const port = ensurePort();
  if (!port) {
    // MagnetoClip is not running: let the browser keep the file instead of
    // cancelling it and losing the download.
    return false;
  }
  interceptedDownloads.add(item.id);
  const fallback = guessFilename(item.url) || baseName(item.filename);
  const timer = setTimeout(() => {
    const pending = pendingIntercepts.get(item.id);
    if (pending && !pending.sent) {
      pending.sent = true;
      pendingIntercepts.delete(item.id);
      sendCapture(item, pending.fallback || fallback);
    }
  }, 2000);
  pendingIntercepts.set(item.id, { timer: timer, fallback: fallback, sent: false });
  return true;
}

chrome.downloads.onCreated.addListener((item) => {
  if (!/^https?:/i.test(item.url)) {
    return;
  }
  if (!integrationEnabled) {
    return;
  }
  const mime = (item.mime || "").toLowerCase();
  const isMedia = mime.startsWith("video/") || mime.startsWith("audio/") || mime.startsWith("image/");
  // default_downloader: intercept everything MagnetoClip understands. Otherwise
  // intercept only media when capture is enabled, and leave other files to the
  // browser's own downloader.
  const shouldCapture = defaultDownloader || (captureEnabled && isMedia);
  if (!shouldCapture) {
    return;
  }
  interceptDownload(item);
});

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  if (!/^https?:/i.test(item.url)) {
    return;
  }
  if (!interceptedDownloads.has(item.id)) {
    // Not owned by MagnetoClip: let the browser save it normally.
    return;
  }
  // Let the browser save under the resolved name for now; the browser copy is
  // cancelled only if MagnetoClip confirms it can download the URL.
  const pending = pendingIntercepts.get(item.id);
  const filename = baseName(item.filename) || (pending && pending.fallback) || "";
  try {
    suggest(filename ? { filename: filename } : undefined);
  } catch (error) {
    /* browser keeps its default filename */
  }
  if (!pending || pending.sent) {
    return;
  }
  clearTimeout(pending.timer);
  pending.sent = true;
  pendingIntercepts.delete(item.id);
  sendCapture(item, filename);
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "ping") {
    sendResponse({ type: "pong" });
    return;
  }
  if (message && message.type === "status") {
    request("status").then(sendResponse).catch((error) => {
      sendResponse({ type: "error", message: error.message });
    });
    return true;
  }
  if (message && message.type === "detection_update") {
    if (sender.tab) {
      tabDetections.set(sender.tab.id, {
        url: message.url || "",
        title: message.title || "",
        files: Array.isArray(message.files) ? message.files : [],
        ts: Date.now()
      });
    }
    return;
  }
  if (message && message.type === "install_blob_hook") {
    // Telegram and similar blob-backed viewers create media object URLs on the
    // page's main thread. The content script lives in an isolated world and its
    // own URL.createObjectURL patch would not affect the page, so patch the
    // MAIN world instead (executeScript bypasses the page's CSP). The page
    // reports each created blob URL via window.postMessage, which the content
    // script sees.
    if (sender.tab && sender.tab.id != null && chrome.scripting) {
      chrome.scripting
        .executeScript({
          target: { tabId: sender.tab.id },
          world: "MAIN",
          func: () => {
            if (window.__mcObjectUrlHooked) {
              return;
            }
            window.__mcObjectUrlHooked = true;
            const original = URL.createObjectURL;
            URL.createObjectURL = function (obj) {
              const url = original.call(this, obj);
              if (url && url.indexOf("blob:") === 0) {
                try {
                  window.postMessage(
                    { source: "magnetoclip-blob", url: url },
                    "*"
                  );
                } catch (error) {
                  /* noop */
                }
              }
              return url;
            };
          },
        })
        .catch(() => {});
    }
    return;
  }
  if (message && message.type === "detected_files") {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs && tabs[0];
      const data = tab ? tabDetections.get(tab.id) : null;
      sendResponse({
        type: "detected_files_ok",
        url: data ? data.url : "",
        title: data ? data.title : "",
        files: data ? data.files : []
      });
    });
    return true;
  }
  if (message && message.type === "download_file") {
    capture({
      url: message.url,
      filename: message.filename || "",
      referrer: message.referrer || "",
      source: "popup",
      detected_type: message.detected_type || "file"
    })
      .then((response) => {
        if (response.type === "capture_pending") {
          notify(
            "MagnetoClip — ready to download",
            (response.filename || message.url) + " is waiting for your confirmation in MagnetoClip."
          );
          sendResponse({ type: "capture_pending" });
        } else if (response.type === "capture_error") {
          sendResponse({
            type: "capture_error",
            message: response.message || "Could not download this file.",
          });
        } else {
          sendResponse({ type: "capture_ok" });
        }
      })
      .catch((error) => {
        sendResponse({ type: "capture_error", message: error.message });
      });
    return true;
  }
  if (message && message.type === "page_scan") {
    request("page_scan", { url: message.url, files: message.files }).catch(() => {});
    return;
  }
  if (message && message.type === "capture_chunk") {
    // Large in-memory media (Telegram photos, blob-backed clips) is shipped as
    // base64 chunks; forward each to the native host. Only the final chunk
    // triggers the real capture, so intermediate chunks just relay back the
    // host's ack.
    request("capture_chunk", {
      capture_key: message.capture_key,
      index: message.index,
      total: message.total,
      chunk: message.chunk,
      url: message.url || "",
      filename: message.filename || "",
      referrer: message.referrer || "",
      mime_type: message.mime_type || "",
      detected_type: message.detected_type || "file",
      last: !!message.last,
    })
      .then((response) => {
        if (
          response &&
          (response.type === "capture_chunk_ok" ||
            response.type === "capture_pending" ||
            response.type === "capture_ok" ||
            response.type === "capture_skipped")
        ) {
          sendResponse({ type: "capture_chunk_ok", ...response });
        } else {
          sendResponse({
            type: "capture_chunk_error",
            message: (response && response.message) || "Could not capture this file.",
          });
        }
      })
      .catch((error) => {
        sendResponse({ type: "capture_chunk_error", message: error.message });
      });
    return true;
  }
  if (message && message.type === "social_capture") {
    const files = Array.isArray(message.files) ? message.files : [];
    for (const file of files) {
      if (!file || !file.url) {
        continue;
      }
      const isHttp = /^https?:/i.test(file.url);
      const hasData = Boolean(file.data_base64);
      if (!isHttp && !hasData) {
        continue;
      }
      capture({
        url: file.url,
        filename: file.filename || "",
        referrer: message.url || "",
        source: "page_scan",
        detected_type: file.detected_type || "file",
        mime_type: file.mime_type || "",
        data_base64: hasData ? file.data_base64 : undefined,
      }).catch(() => {});
    }
    return;
  }
});

// Keep the extension's flags in sync with the MagnetoClip app's settings. The
// toggles live in the app's settings page and the extension can be asleep when
// they change, so poll the native host periodically instead of only reading
// settings when a new port is opened.
setInterval(() => {
  try {
    const port = ensurePort();
    if (port) {
      port.postMessage({ type: "settings" });
    }
  } catch (error) {
    nativePort = null;
  }
}, 15000);

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "").toLowerCase();
  } catch (error) {
    return "";
  }
}

// ---- Telegram stream redirects ----

// Telegram Web plays videos through an internal /k/stream/ proxy that only
// works with the page's session cookie and answers with a Location-less 302
// (its own HTML shell) when the cookie is missing. It redirects to the real CDN
// file on cdn.telegram.org. The content script cannot read cross-origin
// redirect targets through fetch (the browser withholds them without CORS), but
// the background can observe every redirect, so the resolved CDN URL is captured
// here. This is the reliable path for Telegram videos.
if (chrome.webRequest) {
  chrome.webRequest.onBeforeRedirect.addListener(
    (details) => {
      if (!integrationEnabled) {
        return;
      }
      const source = details.url || "";
      const target = details.redirectUrl || "";
      if (!/^https?:/i.test(source) || !/^https?:/i.test(target)) {
        return;
      }
      const sourceHost = hostOf(source);
      if (!sourceHost.endsWith("telegram.org") || !source.includes("/k/stream/")) {
        return;
      }
      if (webReportedUrls.has(target)) {
        return;
      }
      webReportedUrls.add(target);
      if (webCaptureCount >= MAX_WEB_CAPTURES) {
        return;
      }
      webCaptureCount += 1;
      const detectedType = /audio|voice/i.test(source) ? "audio" : "video";
      capture({
        url: target,
        filename: guessFilename(target),
        referrer: details.initiator || details.originUrl || "",
        source: "page_scan",
        detected_type: detectedType,
      }).catch(() => {});
    },
    { urls: ["http://*/*", "https://*/*"] }
  );
}
