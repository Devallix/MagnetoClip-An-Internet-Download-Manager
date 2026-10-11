// popup.js — the six-category dashboard.
//
// The popup deliberately shows no individual files: detection is now the
// queue's job, and the queue is browsed and downloaded in MagnetoClip's
// Detected page. The popup's whole job is to answer "what did the companion
// find, and where do I download it?" with one button per category.
"use strict";

const REFRESH_MS = 2000;
const TIMEOUT_MS = 4000;

const ICONS = {
  images: "◈",
  videos: "▶",
  audio: "♪",
  compressed: "▤",
  documents: "◳",
  other: "⋯",
};

const LABELS = {
  images: "Images",
  videos: "Videos",
  audio: "Audio",
  compressed: "Compressed",
  documents: "Documents",
  other: "Other",
};

const CATEGORIES = Array.isArray(MagnetoclipCategories.CATEGORIES)
  ? MagnetoclipCategories.CATEGORIES
  : Object.keys(LABELS);

const els = {
  dot: document.getElementById("status-dot"),
  statusText: document.getElementById("status-text"),
  total: document.getElementById("total-count"),
  list: document.getElementById("categories"),
  errorBox: document.getElementById("error-box"),
  errorText: document.getElementById("error-text"),
  btnAll: document.getElementById("btn-all"),
  btnRefresh: document.getElementById("btn-refresh"),
};

let inFlight = false;
let watchdog = 0;

function showError(message) {
  els.errorText.textContent = message;
  els.errorBox.style.display = "block";
}

function clearError() {
  els.errorText.textContent = "";
  els.errorBox.style.display = "none";
}

function setConnected(connected) {
  els.dot.classList.toggle("online", connected === true);
  els.dot.classList.toggle("offline", connected === false);
}

// The six rows are built once and only their counts change afterwards, so
// opening a category never rebuilds (or loses hover/focus) the list.
const rows = {};

function buildRows() {
  els.list.replaceChildren();
  for (const category of CATEGORIES) {
    const item = document.createElement("div");
    item.className = "category empty";

    const icon = document.createElement("span");
    icon.className = "category-icon " + category;
    icon.textContent = ICONS[category] || "◈";

    const label = document.createElement("span");
    label.className = "category-label";
    label.textContent = LABELS[category] || category;

    const count = document.createElement("span");
    count.className = "category-count zero";
    count.textContent = "0";

    const button = document.createElement("button");
    button.className = "category-btn";
    button.textContent = "Open";
    button.disabled = true;
    button.addEventListener("click", () => openDetected(category, button));

    item.append(icon, label, count, button);
    els.list.append(item);
    rows[category] = { item: item, count: count, button: button };
  }
}

function renderCounts(counts, total) {
  for (const category of CATEGORIES) {
    const row = rows[category];
    if (!row) {
      continue;
    }
    const value = Number(counts && counts[category]) || 0;
    row.count.textContent = String(value);
    row.count.classList.toggle("zero", value === 0);
    row.item.classList.toggle("empty", value === 0);
    // A category with nothing in it still opens the page — the user may want
    // to add downloads there — but it is not the primary action.
    row.button.disabled = false;
  }
  const sum = Number(total) || 0;
  els.total.textContent = String(sum);
  els.btnAll.disabled = false;
}

// Ask the background script to raise MagnetoClip's Detected page, filtered to
// this category. The background owns the queue flush, the protocol launch and
// the "MagnetoClip isn't running" notification; the popup only relays.
function openDetected(category, button) {
  if (button) {
    button.disabled = true;
  }
  const label = LABELS[category] || category;
  try {
    chrome.runtime.sendMessage({ type: "open_detected", category: category }, (response) => {
      if (chrome.runtime.lastError) {
        if (button) {
          button.disabled = false;
        }
        showError(chrome.runtime.lastError.message);
        return;
      }
      if (!response) {
        if (button) {
          button.disabled = false;
        }
        showError("No response from the MagnetoClip background script.");
        return;
      }
      if (button) {
        button.textContent = "Sent";
        setTimeout(() => {
          button.textContent = "Open";
          button.disabled = false;
        }, 1200);
      }
      clearError();
      if (label) {
        window.close();
      }
    });
  } catch (error) {
    if (button) {
      button.disabled = false;
    }
    if (/Extension context invalidated/i.test(String(error && error.message))) {
      showError("The extension was updated. Reload this page and try again.");
    }
  }
}

function refresh() {
  if (inFlight) {
    return;
  }
  inFlight = true;
  clearTimeout(watchdog);
  try {
    chrome.runtime.sendMessage({ type: "detection_status" }, (response) => {
      clearTimeout(watchdog);
      inFlight = false;
      if (chrome.runtime.lastError) {
        setConnected(false);
        showError(chrome.runtime.lastError.message);
        return;
      }
      if (!response || response.type !== "detection_status_ok") {
        setConnected(false);
        showError(
          (response && response.message) ||
            "MagnetoClip isn't running. Open MagnetoClip to view and download your detected files."
        );
        return;
      }
      setConnected(true);
      clearError();
      renderCounts(response.counts, response.total);
    });
  } catch (error) {
    inFlight = false;
    setConnected(false);
  }
  watchdog = setTimeout(() => {
    if (inFlight) {
      inFlight = false;
      setConnected(false);
      showError("Timed out waiting for the MagnetoClip background script.");
    }
  }, TIMEOUT_MS);
}

// The Refresh button discards everything captured so far and detects the
// current page again, so the counts always describe one fresh pass.
function rescan() {
  if (inFlight) {
    return;
  }
  inFlight = true;
  clearTimeout(watchdog);
  chrome.runtime.sendMessage({ type: "rescan_detections" }, (response) => {
    clearTimeout(watchdog);
    inFlight = false;
    if (chrome.runtime.lastError) {
      setConnected(false);
      showError(chrome.runtime.lastError.message);
      return;
    }
    if (!response || response.type !== "rescan_detections_ok") {
      setConnected(false);
      showError((response && response.message) || "Could not refresh detections.");
      return;
    }
    setConnected(true);
    clearError();
    renderCounts(response.counts, response.total);
  });
  watchdog = setTimeout(() => {
    if (inFlight) {
      inFlight = false;
      setConnected(false);
      showError("Timed out waiting for the MagnetoClip background script.");
    }
  }, TIMEOUT_MS);
}

buildRows();
els.btnAll.addEventListener("click", () => openDetected("", els.btnAll));
els.btnRefresh.addEventListener("click", rescan);
refresh();
setInterval(refresh, REFRESH_MS);
