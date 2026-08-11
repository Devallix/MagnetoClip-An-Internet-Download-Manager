const HOST_NAME = "com.magnetoclip.host";

const pendingFilenames = new Map();
const pendingResponses = new Map();
const tabDetections = new Map();
// Download ids the extension decided to intercept (cancelled + captured).
// Used so onDeterminingFilename only cancels downloads MagnetoClip actually
// owns; other downloads keep flowing to the browser's default downloader.
const interceptedDownloads = new Set();
// URLs already offered to MagnetoClip from the webRequest fallback path.
const webReportedUrls = new Set();
const MAX_WEB_CAPTURES = 25;
let webCaptureCount = 0;
let nativePort = null;
let nextId = 1;
let integrationEnabled = true;
let captureEnabled = true;
let defaultDownloader = false;

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

function request(type, payload) {
  return new Promise((resolve, reject) => {
    const port = ensurePort();
    if (!port) {
      reject(new Error("MagnetoClip is not running."));
      return;
    }
    const id = nextId++;
    pendingResponses.set(id, { resolve, reject });
    port.postMessage({ id, type, ...payload });
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

function guessFilename(url) {
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
    if (!chrome.cookies) {
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
      }
    })
    .catch((error) => {
      notify("MagnetoClip unavailable", error.message);
    });
});

// ---- downloads ----

// Intercept a browser download: offer it to MagnetoClip and cancel the browser
// copy. Returns true if the download was claimed.
function interceptDownload(item) {
  const port = ensurePort();
  if (!port) {
    // MagnetoClip is not running: let the browser keep the file instead of
    // cancelling it and losing the download.
    return false;
  }
  interceptedDownloads.add(item.id);
  const filename = pendingFilenames.get(item.id) || item.filename || "";
  pendingFilenames.delete(item.id);
  const detectedType = detectFileType(item.url);
  withCookies({
    url: item.url,
    filename: filename,
    referrer: item.referrer || "",
    source: "extension",
    detected_type: detectedType,
  })
    .then((full) => capture(full.url, full))
    .catch(() => {});
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
  interceptedDownloads.delete(item.id);
  pendingFilenames.set(item.id, item.filename || "");
  try {
    item.cancel();
  } catch (error) {
    /* already removed */
  }
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
        }
      })
      .catch((error) => {
        notify("MagnetoClip unavailable", error.message);
      });
    return;
  }
  if (message && message.type === "page_scan") {
    request("page_scan", { url: message.url, files: message.files }).catch(() => {});
    return;
  }
  if (message && message.type === "social_capture") {
    const files = Array.isArray(message.files) ? message.files : [];
    for (const file of files) {
      if (!file || !file.url || !/^https?:/i.test(file.url)) {
        continue;
      }
      capture({
        url: file.url,
        filename: file.filename || "",
        referrer: message.url || "",
        source: "page_scan",
        detected_type: file.detected_type || "file"
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
  if (nativePort) {
    try {
      nativePort.postMessage({ type: "settings" });
    } catch (error) {
      nativePort = null;
    }
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
