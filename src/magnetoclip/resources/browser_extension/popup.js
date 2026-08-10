const REFRESH_MS = 3000;
const TIMEOUT_MS = 4000;
const MAX_DETECTED = 8;

const TYPE_ICONS = {
  video: "▶",
  audio: "♪",
  image: "◈",
  document: "◳",
  archive: "▤",
  software: "⚙",
  stream: "⇢",
};

const els = {
  dot: document.getElementById("status-dot"),
  statusText: document.getElementById("status-text"),
  statActive: document.getElementById("stat-active"),
  statCompleted: document.getElementById("stat-completed"),
  badgeIntegration: document.getElementById("badge-integration"),
  badgeCapture: document.getElementById("badge-capture"),
  detectedList: document.getElementById("detected-list"),
  detectedEmpty: document.getElementById("detected-empty"),
  errorBox: document.getElementById("error-box"),
  errorText: document.getElementById("error-text"),
  btnRetry: document.getElementById("btn-retry"),
};

let inFlight = false;
let watchdog = 0;

function setOnline() {
  els.dot.classList.remove("offline");
  els.dot.classList.add("online");
  els.errorBox.style.display = "none";
}

function setOffline() {
  els.dot.classList.remove("online");
  els.dot.classList.add("offline");
}

function setChecking() {
  els.dot.classList.remove("online", "offline");
  els.statusText.textContent = "Checking connection…";
}

function setPill(el, enabled) {
  el.textContent = enabled ? "Enabled" : "Disabled";
  el.classList.toggle("enabled", enabled === true);
}

function setState(response) {
  setOnline();
  els.statusText.innerHTML = "Connected to <strong>MagnetoClip</strong>";
  els.statActive.textContent = String(response.active || 0);
  els.statCompleted.textContent = String(response.completed || 0);
  setPill(els.badgeIntegration, response.integration_enabled);
  setPill(els.badgeCapture, response.capture_enabled);
}

function setError(message) {
  setOffline();
  els.statusText.textContent = "MagnetoClip not reachable";
  els.errorText.textContent = message;
  els.errorBox.style.display = "block";
}

function typeLabel(type) {
  return TYPE_ICONS[type] || "◈";
}

function sendDownload(file, pageUrl) {
  chrome.runtime.sendMessage({
    type: "download_file",
    url: file.url,
    filename: file.filename || "",
    referrer: pageUrl,
    detected_type: file.detected_type || "file",
  });
}

function renderDetected(data) {
  const files = (data && data.files) || [];
  els.detectedList.replaceChildren();
  if (!files.length) {
    els.detectedEmpty.style.display = "block";
    els.detectedList.style.display = "none";
    return;
  }
  els.detectedEmpty.style.display = "none";
  els.detectedList.style.display = "flex";
  for (const file of files.slice(0, MAX_DETECTED)) {
    const item = document.createElement("div");
    item.className = "detected-item";

    const type = document.createElement("span");
    type.className = "detected-type " + (file.detected_type || "file");
    type.textContent = typeLabel(file.detected_type);

    const info = document.createElement("div");
    info.className = "detected-info";

    const name = document.createElement("div");
    name.className = "detected-name";
    name.textContent = file.filename || file.url;
    name.title = file.url;

    const url = document.createElement("div");
    url.className = "detected-url";
    url.textContent = file.url;

    info.append(name, url);

    const button = document.createElement("button");
    button.className = "detected-btn";
    button.textContent = "Download";
    button.addEventListener("click", () => {
      sendDownload(file, (data && data.url) || "");
      button.textContent = "Sent";
      button.classList.add("sent");
      button.disabled = true;
    });

    item.append(type, info, button);
    els.detectedList.append(item);
  }
}

function queryDetected() {
  chrome.runtime.sendMessage({ type: "detected_files" }, (response) => {
    if (chrome.runtime.lastError) {
      return;
    }
    if (response && response.type === "detected_files_ok") {
      renderDetected(response);
    }
  });
}

function query() {
  if (inFlight) {
    return;
  }
  inFlight = true;
  clearTimeout(watchdog);
  chrome.runtime.sendMessage({ type: "status" }, (response) => {
    clearTimeout(watchdog);
    inFlight = false;
    if (chrome.runtime.lastError) {
      setError(chrome.runtime.lastError.message);
      return;
    }
    if (!response) {
      setError("No response from the MagnetoClip background page.");
      return;
    }
    if (response.type !== "status_ok") {
      setError(response.message || "MagnetoClip returned an unexpected response.");
      return;
    }
    setState(response);
  });
  watchdog = setTimeout(() => {
    if (inFlight) {
      inFlight = false;
      setError("Timed out waiting for MagnetoClip. Is the app running?");
    }
  }, TIMEOUT_MS);
}

function refresh() {
  query();
  queryDetected();
}

els.btnRetry.addEventListener("click", refresh);

setChecking();
refresh();
setInterval(refresh, REFRESH_MS);
