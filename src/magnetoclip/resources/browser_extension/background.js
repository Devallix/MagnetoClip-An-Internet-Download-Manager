const HOST_NAME = "com.magnetoclip.host";

const pendingFilenames = new Map();
const pendingResponses = new Map();
let nativePort = null;
let nextId = 1;
let integrationEnabled = true;
let captureEnabled = true;

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
    iconUrl: "icons/download.png",
    title: title,
    message: message
  });
}

function ensureMenus() {
  chrome.contextMenus.removeAll(() => {
    for (const item of MENU_ITEMS) {
      chrome.contextMenus.create({ ...item, documentUrlPatterns: ["http://*/*", "https://*/*"] });
    }
  });
}

chrome.runtime.onInstalled.addListener(ensureMenus);
chrome.runtime.onStartup.addListener(ensureMenus);

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const url = info.linkUrl || info.srcUrl || "";
  if (!/^https?:/i.test(url)) {
    return;
  }
  request("capture", {
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

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  if (!/^https?:/i.test(item.url)) {
    return;
  }
  pendingFilenames.set(item.id, item.filename || "");
  try {
    item.cancel();
  } catch (error) {
    /* already removed */
  }
});

chrome.downloads.onCreated.addListener((item) => {
  if (!/^https?:/i.test(item.url)) {
    return;
  }
  if (!integrationEnabled) {
    return;
  }
  const mime = (item.mime || "").toLowerCase();
  const isMedia = mime.startsWith("video/") || mime.startsWith("audio/");
  if (!captureEnabled && isMedia) {
    return;
  }
  const port = ensurePort();
  if (!port) {
    return;
  }
  const filename = pendingFilenames.get(item.id) || "";
  pendingFilenames.delete(item.id);
  port.postMessage({
    type: "capture",
    url: item.url,
    filename: filename,
    referrer: item.referrer || "",
    source: "extension",
    detected_type: isMedia ? "media" : "file"
  });
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
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
      request("capture", {
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
