// The Chrome-family service worker cannot list shared scripts in the manifest,
// so it pulls them in itself; Firefox lists them in ``background.scripts`` and
// has no importScripts(). This must stay at the very top of the worker.
if (typeof importScripts === "function") {
  importScripts("compat.js", "categories.js", "queue.js");
}

// Firefox does not support "extraHeaders" in webRequest extraInfoSpec (Chrome
// uses it to read stripped headers; Firefox never strips them). The
// ``browser`` namespace exists only in Firefox.
const IS_FIREFOX = typeof browser !== "undefined";

const HOST_NAME = "com.magnetoclip.host";
const SETTINGS_STORAGE_KEY = "mc_settings";
// The app asks the extension to fetch blob: URLs the user pasted into the
// new-download field. The content script returns the whole base64 payload, but
// native messaging messages are size-limited (~1MB), so it is split into chunks
// here before being sent to the host.
const BLOB_FETCH_BASE64_CHUNK = 512 * 1024;
// Raw byte chunk for service-worker fetches: 400KB encodes to ~533KB of
// base64, safely under the ~1MB native-messaging message limit.
const BLOB_FETCH_BASE64_BYTES = 384 * 1024; // divisible by 3 so per-chunk base64 remains concatenable

function uint8ToBase64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

const pendingIntercepts = new Map();
const pendingResponses = new Map();
const tabDetections = new Map();
// Known streaming hosts are retained only for site-specific fallbacks.
// Generic media detection MUST NOT depend on this list: the browser can expose
// ordinary HLS/DASH/progressive-media requests on sites we have never seen.
const STREAMING_SITE_HOSTS = new Set([
  "anikai.watch", "anikai.vip", "anikai.rip", "anikai.to",
  "animekai.org.lv", "animekai.com.ro",
  "hdtoday24.at", "hdtodayz.app", "hdtoday.duckdns.org", "hdtoday.to",
  "4thdmovie.com",
  "yflix.in",
]);

function isStreamingSiteHost(host) {
  const normalized = String(host || "").toLowerCase().replace(/\.$/, "");
  if (!normalized) return false;
  for (const known of STREAMING_SITE_HOSTS) {
    if (normalized === known || normalized.endsWith("." + known)) return true;
  }
  return false;
}

function responseHeader(headers, wanted) {
  const target = String(wanted || "").toLowerCase();
  for (const item of headers || []) {
    if (String(item && item.name || "").toLowerCase() === target) {
      return String(item && item.value || "");
    }
  }
  return "";
}

function normalizeMime(mime) {
  return String(mime || "").split(";", 1)[0].trim().toLowerCase();
}

function mediaKindFromMime(mime) {
  const value = normalizeMime(mime);
  if ([
    "application/vnd.apple.mpegurl", "application/x-mpegurl",
    "application/mpegurl", "audio/mpegurl", "audio/x-mpegurl",
  ].includes(value)) return "hls";
  if (value === "application/dash+xml") return "dash";
  if (value.startsWith("video/")) return "video";
  if (value.startsWith("audio/")) return "audio";
  return "";
}

function extensionFromUrl(url) {
  try {
    const pathname = new URL(url).pathname.toLowerCase();
    const match = pathname.match(/\.([a-z0-9]{2,8})$/);
    return match ? match[1] : "";
  } catch (error) {
    return "";
  }
}

function mediaKindFromExtension(url) {
  const ext = extensionFromUrl(url);
  if (["m3u8", "m3u"].includes(ext)) return "hls";
  if (ext === "mpd") return "dash";
  if (["mp4", "m4v", "webm", "mkv", "mov", "avi", "flv", "3gp", "mpg", "mpeg", "ogv"].includes(ext)) return "video";
  if (["mp3", "m4a", "aac", "ogg", "opus", "wav", "flac", "oga"].includes(ext)) return "audio";
  return "";
}

function isSegmentUrl(url) {
  const ext = extensionFromUrl(url);
  return ["ts", "mts", "m2ts", "m4s", "cmfv", "cmfa"].includes(ext);
}

function isMediaRequestType(type) {
  return ["media", "object", "xmlhttprequest", "other"].includes(String(type || "").toLowerCase());
}


function getTabUrl(tabId, fallback) {
  if (tabId == null || !chrome.tabs || !chrome.tabs.get) {
    return Promise.resolve(fallback || "");
  }
  return new Promise((resolve) => {
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError || !tab || !tab.url) {
        resolve(fallback || "");
        return;
      }
      resolve(String(tab.url));
    });
  });
}
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
// Request context captured at the browser network boundary for media requests.
// This preserves the exact headers/session used to obtain tokenized manifests
// and direct media URLs instead of reconstructing them later.
const manifestRequestContexts = new Map();
// Recent media request context indexed by URL. Player buttons often surface
// extensionless social-CDN URLs after the request has completed, so the
// request-id context above may already be gone. Keep a short-lived URL map for
// explicit player captures; this is bounded and never used for generic scans.
const recentPlayerMediaContexts = new Map();
const MAX_RECENT_PLAYER_MEDIA_CONTEXTS = 500;

function rememberRecentPlayerMediaContext(url, context) {
  const key = String(url || "");
  if (!/^https?:/i.test(key)) return;
  recentPlayerMediaContexts.set(key, Object.assign({}, context || {}, {
    url: key,
    seenAt: Date.now(),
  }));
  while (recentPlayerMediaContexts.size > MAX_RECENT_PLAYER_MEDIA_CONTEXTS) {
    const first = recentPlayerMediaContexts.keys().next().value;
    if (first === undefined) break;
    recentPlayerMediaContexts.delete(first);
  }
}

function recentPlayerMediaContext(url) {
  const key = String(url || "");
  const item = recentPlayerMediaContexts.get(key);
  if (!item) return null;
  if (Date.now() - Number(item.seenAt || 0) > 120000) {
    recentPlayerMediaContexts.delete(key);
    return null;
  }
  return item;
}

function captureManifestHeaders(headers) {
  const allowed = new Set([
    "accept", "accept-language", "authorization", "cache-control",
    "cookie", "origin", "pragma", "referer", "user-agent",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
  ]);
  const result = {};
  for (const item of headers || []) {
    const name = String((item && item.name) || "").trim();
    if (!name || !allowed.has(name.toLowerCase())) continue;
    result[name] = String((item && item.value) || "");
  }
  return result;
}

function getFrameUrl(details, fallback) {
  if (!chrome.webNavigation || details.tabId == null || details.frameId == null) {
    return Promise.resolve(fallback || "");
  }
  return new Promise((resolve) => {
    chrome.webNavigation.getFrame(
      { tabId: details.tabId, frameId: details.frameId },
      (frame) => {
        if (chrome.runtime.lastError || !frame || !frame.url) {
          resolve(fallback || "");
          return;
        }
        resolve(String(frame.url));
      }
    );
  });
}
// Per service-worker wake: the old 25-item cap existed because every hit opened a
// capture dialog, and a burst of segments flooded the user. Detections now go to
// a bounded queue, so this only has to stop a pathological loop (a page replaying
// the same segments) from spinning; the queue itself caps the total at
// MAX_ENTRIES / 3.5 MB.
const MAX_WEB_CAPTURES = 500;
let webCaptureCount = 0;
let nativePort = null;
let nextId = 1;
let integrationEnabled = true;
let captureEnabled = true;
let defaultDownloader = false;
// The flags above only become authoritative once the app's native host answers
// a settings request ("settings_ok"): the app is the single source of truth for
// its settings. Until then the defaults here are guesses. Downloads must never
// be dropped on those guesses in onCreated — the app answers capture_skipped
// for anything it does not want, so the browser simply keeps the file. Trusting
// the cached chrome.storage copy instead is what let MagnetoClip miss non-media
// files after 'default downloader' was toggled on while this worker slept.
let settingsLoaded = false;

// Warm-start the flags from the last state we know. This is a best guess only:
// it never makes the flags authoritative, and it never overwrites values the
// host already confirmed this run (the cached snapshot can be stale if the app
// settings changed while this worker was asleep).
function loadStoredSettings() {
  if (!chrome.storage || settingsLoaded) {
    return;
  }
  chrome.storage.local.get(SETTINGS_STORAGE_KEY, (stored) => {
    if (chrome.runtime.lastError) {
      return;
    }
    const saved = stored && stored[SETTINGS_STORAGE_KEY];
    if (!saved || settingsLoaded) {
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

// ---- detection queue ----
//
// Every automatic detection (network traffic, page/DOM scan, Telegram redirect,
// context menu) lands in the persistent queue instead of creating its own
// capture dialog in the app. The queue classifies centrally, deduplicates,
// updates the toolbar badge, and syncs itself to MagnetoClip in bounded batches.
// Explicit browser downloads (downloads.onCreated) are NOT queued: those are
// deliberate user actions and keep the existing capture dialog behaviour.

const QUEUE = typeof MagnetoclipQueue !== "undefined" ? MagnetoclipQueue : null;
const BADGE_COLOR = "#8b5cf6";
const SYNC_ALARM = "magneto-detections-sync";

function compatBadge(text) {
  if (typeof MagnetoclipCompat === "undefined") {
    return;
  }
  MagnetoclipCompat.setBadge(text, BADGE_COLOR);
  MagnetoclipCompat.setBadgeTitle(
    text
      ? "MagnetoClip — " + text + " detected " + (text === "1" ? "file" : "files")
      : "MagnetoClip — no detected files yet"
  );
}

// Queue one detection. Returns the stored entry, or null for a duplicate.
// A detection carrying a page URL also claims the queue for that page, so the
// previous page's files are dropped the moment a new page reports something.
function queueDetection(detection) {
  if (!QUEUE || !detection || !detection.url) {
    return Promise.resolve(null);
  }
  const page = String(detection.page_url || detection.pageUrl || "");
  const readyForPage = /^https?:/i.test(page) ? adoptPage(page) : Promise.resolve(false);
  return readyForPage
    .then(() => QUEUE.add(detection))
    .catch(() => null);
}

function queueDetections(list, options) {
  const items = (Array.isArray(list) ? list : [list]).filter((item) => item && item.url);
  if (!QUEUE || !items.length) {
    return Promise.resolve([]);
  }
  // Only the top-level frame may claim (reset) the queue for its page. Iframe
  // and embed reports join current entries instead, or two frames on the same
  // tab would keep wiping each other's captures out.
  const adopt = !options || options.adopt !== false;
  let readyForPage = Promise.resolve(false);
  if (adopt) {
    const pages = items
      .map((item) => String(item.page_url || item.pageUrl || ""))
      .filter((page) => /^https?:/i.test(page));
    const page = pages.length ? pages[0] : "";
    readyForPage = page ? adoptPage(page) : Promise.resolve(false);
  }
  return readyForPage
    .then(() => QUEUE.addMany(items))
    .catch(() => []);
}

// Hand one batch to the app. Returning false keeps the entries queued so the
// next alarm, startup or detection retries them; nothing is ever discarded
// because MagnetoClip happens to be closed.
function sendDetectionsToApp(batch) {
  if (!batch || !batch.length) {
    return Promise.resolve(true);
  }
  return request("detection_sync", { detections: batch })
    .then((response) => {
      if (response && response.type === "error") {
        return false;
      }
      return !!response;
    })
    .catch(() => false);
}

function configureDetectionQueue() {
  if (!QUEUE) {
    return;
  }
  QUEUE.configure({
    onSync: sendDetectionsToApp,
    onChange: (counts, total) => {
      compatBadge(total ? QUEUE.badgeText() : "");
    },
    // Debounce through setTimeout: an MV3 event page may be torn down between
    // the request and the transfer. The category travels with it so a retry
    // cannot widen into a full sync.
    schedule: (delay, category) => {
      setTimeout(() => {
        QUEUE.flush(category);
      }, delay);
    },
  });
  QUEUE.ready().then(() => {
    compatBadge(QUEUE.badgeText());
  });
}

// ---- player overlay support ----
//
// The content-script overlay (player_overlay.js) asks the background worker
// which queued detections belong to a <video> it found, using only the URLs the
// player itself exposes plus the frame's network/scan evidence. It then sends a
// ``download_detection`` message (with the canonical detection id) that is
// forwarded to the native host. Everything here is read-only on the queue.

function queueEntryMatchesSignals(entry, signals, pageUrl) {
  if (!entry || !entry.url) {
    return false;
  }
  const entryUrl = String(entry.url);
  if (/^blob:/i.test(entryUrl)) {
    return false;
  }
  const entryExact = normalizeMatchKey(entryUrl);
  const entryLoose = looseMatchKey(entryExact);
  for (const signal of signals || []) {
    const value = String(signal || "");
    if (value.startsWith("blob:")) {
      continue;
    }
    const exact = normalizeMatchKey(value);
    if (exact && exact === entryExact) {
      return true;
    }
    const loose = looseMatchKey(exact);
    if (loose && entryLoose && loose === entryLoose) {
      return true;
    }
  }
  if (pageUrl && normalizeMatchKey(pageUrl) === entryExact) {
    return true;
  }
  return false;
}

function isTelegramAppShellUrl(url) {
  try {
    const parsed = new URL(String(url || ""));
    if (String(parsed.hostname || "").toLowerCase() !== "web.telegram.org") return false;
    // Telegram Web's /k/ and /a/ routes are app/chat shells, not media files.
    return /^\/(?:k|a)\/?$/i.test(String(parsed.pathname || "/"));
  } catch (error) {
    return false;
  }
}

function entriesForPlayer(entries, signals, pageUrl, frameUrl, blob) {
  const list = [];
  const seen = new Set();
  const hasBlobSignal = blob === true || (signals || []).some((value) => /^blob:/i.test(String(value || "")));
  const facebookPage = isFacebookPageUrl(pageUrl);
  const tiktokPage = isTikTokPageUrl(pageUrl);
  const xPage = isXPageUrl(pageUrl);
  const telegramAppShell = isTelegramAppShellUrl(pageUrl) || isTelegramAppShellUrl(frameUrl);
  const tiktokSignalKeys = new Set((signals || []).map((value) => tiktokVideoKey(value)).filter(Boolean));
  const xStatusSignalKeys = new Set((signals || []).map((value) => xStatusKey(value)).filter(Boolean));
  // Never use Telegram Web's chat shell as a video target. A blob/MSE video
  // must have an actual URL correlated by network/player signals; otherwise
  // the overlay must wait rather than offering /k/#chat-id (which downloads HTML).
  const matchPageUrl = telegramAppShell ? "" : pageUrl;

  // TikTok feed pages produce many CDN media URLs for autoplay previews. If the
  // player supplies a specific /@user/video/<id> hint, put that exact queued
  // permalink first so the generic blob-frame candidates cannot crowd it out of
  // the bounded response list. The overlay still verifies the same video key.
  if (tiktokPage && tiktokSignalKeys.size) {
    for (const entry of entries || []) {
      if (!entry || !entry.url || seen.has(entry.url)) continue;
      const key = tiktokVideoKey(entry.url);
      if (!key || !tiktokSignalKeys.has(key)) continue;
      seen.add(entry.url);
      list.push(forExtensionEntry(entry));
    }
  }

  // Prioritize the exact queued X status permalink for this player before any
  // same-frame CDN/media entries. X homepage videos are only downloadable via
  // the post URL that the extension already detected.
  if (xPage && xStatusSignalKeys.size) {
    for (const entry of entries || []) {
      if (!entry || !entry.url || seen.has(entry.url)) continue;
      const key = xStatusKey(entry.url);
      if (!key || !xStatusSignalKeys.has(key)) continue;
      seen.add(entry.url);
      list.push(forExtensionEntry(entry));
    }
  }

  for (const entry of entries || []) {
    if (!entry || !entry.url || seen.has(entry.url)) {
      continue;
    }
    if (telegramAppShell && isTelegramAppShellUrl(entry.url)) {
      continue;
    }
    const facebookReelMatch = facebookPage && facebookReelEntryInScope(entry, pageUrl, frameUrl);
    const matched = facebookReelMatch || queueEntryMatchesSignals(entry, signals, matchPageUrl);
    if (!matched && !hasBlobSignal) {
      continue;
    }
    if (!matched && hasBlobSignal) {
      // Do not expose arbitrary same-frame entries for Telegram: a chat view can
      // contain multiple videos, and a blob URL alone cannot identify which is
      // playing. Other sites retain their established same-frame MSE fallback.
      if (telegramAppShell) {
        continue;
      }
      const kind = String(entry.detected_type || "").toLowerCase();
      const frameEntry = String(entry.frame_url || entry.referrer || "");
      const sameFrame = !frameEntry || !frameUrl || looseMatchKey(frameEntry) === looseMatchKey(frameUrl);
      const isMedia = kind === "video" || kind === "stream" || kind === "hls" || kind === "dash" || kind === "audio";
      if (!isMedia || !sameFrame) {
        continue;
      }
    }
    seen.add(entry.url);
    list.push(forExtensionEntry(entry));
    if (list.length >= 12) {
      break;
    }
  }
  return list;
}

function forExtensionEntry(entry) {
  const copy = Object.assign({}, entry);
  const data = copy.data_base64;
  const maxInline = QUEUE && QUEUE.MAX_INLINE_BASE64 ? QUEUE.MAX_INLINE_BASE64 : 512 * 1024;
  if (typeof data === "string" && data.length > maxInline) {
    delete copy.data_base64;
    copy.data_omitted = 1;
  }
  return copy;
}

function normalizeMatchKey(url) {
  if (typeof MagnetoclipCategories !== "undefined" && MagnetoclipCategories.normalizeUrl) {
    return MagnetoclipCategories.normalizeUrl(url);
  }
  return String(url || "").split("#")[0];
}

function looseMatchKey(url) {
  return String(url || "").split(/[?#]/)[0];
}

function facebookReelId(url) {
  const value = String(url || "").trim();
  if (!value) return "";
  try {
    const parsed = new URL(value);
    const host = String(parsed.hostname || "").toLowerCase();
    if (host !== "facebook.com" && !host.endsWith(".facebook.com")) return "";
    const match = String(parsed.pathname || "").match(/^\/(?:reel|reels)\/(\d+)(?:\/|$)/i);
    return match ? match[1] : "";
  } catch (_) {
    return "";
  }
}

function isFacebookPageUrl(url) {
  try {
    const host = String(new URL(String(url || "")).hostname || "").toLowerCase();
    return host === "facebook.com" || host.endsWith(".facebook.com");
  } catch (_) {
    return false;
  }
}

function tiktokVideoKey(url) {
  try {
    const parsed = new URL(String(url || ""));
    const host = String(parsed.hostname || "").toLowerCase();
    if (host !== "tiktok.com" && !host.endsWith(".tiktok.com")) return "";
    const match = String(parsed.pathname || "").match(/^\/@([^/]+)\/video\/(\d+)(?:\/|$)/i);
    if (!match) return "";
    let username = match[1];
    try { username = decodeURIComponent(username); } catch (_) {}
    return username.toLowerCase() + ":" + match[2];
  } catch (_) {
    return "";
  }
}

function isTikTokPageUrl(url) {
  try {
    const host = String(new URL(String(url || "")).hostname || "").toLowerCase();
    return host === "tiktok.com" || host.endsWith(".tiktok.com");
  } catch (_) {
    return false;
  }
}

function xStatusKey(url) {
  try {
    const parsed = new URL(String(url || ""));
    const host = String(parsed.hostname || "").toLowerCase();
    if (!(host === "x.com" || host.endsWith(".x.com") || host === "twitter.com" || host.endsWith(".twitter.com"))) return "";
    const match = String(parsed.pathname || "").match(/^\/([^/]+)\/status\/(\d+)(?:\/|$)/i);
    if (!match || /^i$/i.test(match[1])) return "";
    let username = match[1];
    try { username = decodeURIComponent(username); } catch (_) {}
    return username.toLowerCase() + ":" + match[2];
  } catch (_) {
    return "";
  }
}

function isXPageUrl(url) {
  try {
    const host = String(new URL(String(url || "")).hostname || "").toLowerCase();
    return host === "x.com" || host.endsWith(".x.com") || host === "twitter.com" || host.endsWith(".twitter.com");
  } catch (_) {
    return false;
  }
}

function facebookReelEntryInScope(entry, pageUrl, frameUrl) {
  if (!entry || !facebookReelId(entry.url) || !isFacebookPageUrl(pageUrl)) {
    return false;
  }
  const entryPage = String(entry.page_url || entry.pageUrl || "");
  const entryFrame = String(entry.frame_url || entry.frameUrl || entry.referrer || "");
  const pageKey = normalizeMatchKey(pageUrl);
  const frameKey = normalizeMatchKey(frameUrl || "");
  if (entryPage && normalizeMatchKey(entryPage) === pageKey) return true;
  if (entryFrame && frameKey && normalizeMatchKey(entryFrame) === frameKey) return true;
  // When the tab itself is already on /reel/<id>, exact Reel matching is the
  // strongest possible association and avoids relying on DOM ordering.
  const pageReelId = facebookReelId(pageUrl);
  return !!pageReelId && facebookReelId(entry.url) === pageReelId;
}

async function resolvePlayerDetection(message, sender) {
  const detectionId = String(message.detection_id || "").trim();
  const raw = message.detection && typeof message.detection === "object" ? message.detection : null;
  // The live tab URL is the source of truth for Facebook, because Facebook can
  // navigate the same tab between different Reel URLs without recreating the
  // player overlay/controller. Prefer it over a stale queued entry context.
  const livePageUrl = (sender && sender.tab && sender.tab.url) || "";
  const pageUrl = livePageUrl || (raw && raw.page_url) || "";
  const frameUrl = (sender && sender.url) || (raw && raw.frame_url) || "";
  if (!QUEUE) {
    return { entry: null, error: "Detection is not ready in this browser." };
  }
  const entries = await QUEUE.all();

  // On X/Twitter the player target must be an actual queued status permalink.
  // Never re-create a CDN URL (or a generic page URL) for an autoplay/feed
  // preview. Match by username + status ID to tolerate www/twitter.com variants
  // while preserving the exact URL stored by the extension.
  if (isXPageUrl(pageUrl)) {
    const wantedStatusKey = xStatusKey(raw && raw.url) || xStatusKey(pageUrl);
    if (!wantedStatusKey) {
      return { entry: null, error: "Open the X post first, or wait until MagnetoClip detects its status URL." };
    }
    const statusEntry = entries.find((candidate) => xStatusKey(candidate && candidate.url) === wantedStatusKey) || null;
    if (statusEntry) {
      return { entry: statusEntry, error: "" };
    }
    return { entry: null, error: "The matching X post URL is not in the extension queue yet. Wait for the extension to detect the status URL, then try again." };
  }

  // Facebook player downloads must use the real Reel URL from the extension
  // queue. Never let an fbcdn.net media capture win over a queued /reel/<id>
  // entry. When the active tab itself is on a Reel, the Reel id is an exact
  // and unambiguous association.
  if (isFacebookPageUrl(pageUrl)) {
    const currentReelId = facebookReelId(pageUrl);
    if (currentReelId) {
      const reelEntry = entries.find((candidate) => facebookReelId(candidate && candidate.url) === currentReelId) || null;
      if (reelEntry) {
        return { entry: reelEntry, error: "" };
      }
    }

    // When the tab is a Facebook feed rather than a Reel page, preserve an
    // already-identified queued Reel target if the player supplied one. This
    // still requires that the target actually exists in the extension queue.
    const rawReelId = facebookReelId(raw && raw.url);
    if (rawReelId) {
      const rawReelEntry = entries.find((candidate) => facebookReelId(candidate && candidate.url) === rawReelId) || null;
      if (rawReelEntry) {
        return { entry: rawReelEntry, error: "" };
      }
    }
  }

  let entry = null;
  if (detectionId) {
    entry = entries.find((candidate) => String(candidate.id) === detectionId) || null;
  }
  if (!entry && raw && raw.url) {
    entry = entries.find((candidate) => queueEntryMatchesSignals(candidate, [raw.url], pageUrl)) || null;
  }
  if (!entry && raw && raw.url) {
    // The queue already delivered this detection (category opened), so its
    // entry was dropped. Re-create it from the player's own pointer — the only
    // allowed source — so the canonical detail is never invented.
    const recreated = await queueDetection({
      url: raw.url,
      filename: raw.filename || "",
      referrer: raw.referrer || frameUrl || pageUrl,
      page_url: pageUrl,
      frame_url: raw.frame_url || frameUrl || "",
      source: "player_button",
      detected_type: raw.detected_type || "video",
      mime_type: raw.mime_type || "",
      headers: raw.headers,
      cookies: raw.cookies,
      user_agent: raw.user_agent,
      data_base64: raw.data_base64 || undefined,
    });
    if (recreated) {
      entry = recreated;
    } else {
      // Deduped back into an existing entry; re-fetch to pick it up.
      const after = await QUEUE.all();
      entry = after.find((candidate) => queueEntryMatchesSignals(candidate, [raw.url], pageUrl)) || null;
    }
  }

  // Final Facebook safety check: if this is a Reel page and the selected entry
  // is not that Reel, replace it with the queued Reel entry rather than
  // downloading a CDN capture or another page resource.
  if (entry && isFacebookPageUrl(pageUrl)) {
    const currentReelId = facebookReelId(pageUrl);
    if (currentReelId && facebookReelId(entry.url) !== currentReelId) {
      const reelEntry = entries.find((candidate) => facebookReelId(candidate && candidate.url) === currentReelId) || null;
      if (reelEntry) {
        entry = reelEntry;
      }
    }
  }
  return { entry: entry, error: "" };
}

function handlePlayerDownloadResponse(response) {
  if (!response) {
    return { type: "download_detection_error", message: "MagnetoClip did not answer." };
  }
  if (response.type === "download_detection_ok") {
    return { type: "download_detection_ok", download_id: response.download_id || 0, message: response.message || "" };
  }
  if (response.type === "download_detection_pending") {
    return { type: "download_detection_pending", download_id: response.download_id || 0, message: response.message || "" };
  }
  if (response.type === "download_detection_skipped") {
    return { type: "download_detection_skipped", message: response.message || "'Skip all' is active in MagnetoClip." };
  }
  if (response.type === "download_detection_error") {
    return { type: "download_detection_error", message: response.message || "MagnetoClip did not accept the request." };
  }
  if (response.type === "error") {
    return { type: "download_detection_error", message: response.message || "MagnetoClip did not accept the request." };
  }
  return { type: "download_detection_error", message: (response.message) || "MagnetoClip did not answer." };
}

async function handlePlayerDownload(message, sender) {
  const resolved = await resolvePlayerDetection(message, sender);
  if (!resolved.entry || !resolved.entry.url) {
    return { type: "download_detection_error", message: resolved.error || "Could not associate this player with a detection." };
  }
  const url = String(resolved.entry.url);
  if (!/^https?:/i.test(url) && !resolved.entry.data_base64) {
    return { type: "download_detection_error", message: "This player's media cannot be downloaded." };
  }
  try {
    const response = await request("download_detection", {
      detection_id: String(resolved.entry.id || ""),
      detection: forExtensionEntry(resolved.entry),
      page_url: resolved.entry.page_url || "",
    });
    const mapped = handlePlayerDownloadResponse(response);
    if (mapped.type === "download_detection_ok" || mapped.type === "download_detection_pending") {
      QUEUE.remove(resolved.entry.id).then(() => {
        compatBadge(QUEUE.badgeText());
      });
    }
    return mapped;
  } catch (error) {
    // MagnetoClip is not running: the entry stays queued (nothing is lost),
    // and the button tells the user the request could not be sent.
    notifyAppNotRunning("files");
    return {
      type: "download_detection_error",
      message: (error && error.message) || "MagnetoClip is not running.",
    };
  }
}

// ---- page-scoped queue lifetime ----

// How long the rescan is given to re-report before the popup reads the counts.
const RESCAN_SETTLE_MS = 600;

// Detections describe the page they were found on, so the queue is tied to one
// page: reloading it, or moving anywhere else, throws the old entries away
// rather than leaving a stale badge offering files that are no longer on screen.
// Nothing is ever kept between pages.

// The network detector's own dedup memory has the same lifetime as the queue.
// Leaving it in place is what made a refresh look like "detection is broken":
// the page re-requests the same manifests, but every URL was already in
// webReportedUrls, so the reloaded page queued nothing at all.
function resetNetworkDetection() {
  webReportedUrls.clear();
  webCaptureCount = 0;
  manifestRequestContexts.clear();
}

function adoptPage(pageUrl, options) {
  const page = String(pageUrl || "");
  if (!QUEUE || !/^https?:/i.test(page)) {
    return Promise.resolve(false);
  }
  return QUEUE.resetForPage(page, options).then((changed) => {
    if (changed) {
      resetNetworkDetection();
      compatBadge("");
    }
    return changed;
  });
}

// Reloading the same URL must also reset. tabs.onUpdated reports a fresh
// document with a status but no URL change, so the load itself is what signals
// it.
if (chrome.tabs && chrome.tabs.onUpdated) {
  chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (!QUEUE) {
      return;
    }
    if (changeInfo.url) {
      adoptPage(changeInfo.url);
      return;
    }
    if (changeInfo.status === "loading" && tab && tab.url) {
      // A reload of the same URL: force the reset and adopt the page in a
      // single step, so nothing detected in between is thrown away.
      adoptPage(tab.url, { force: true });
    }
  });
}

if (chrome.tabs && chrome.tabs.onRemoved) {
  chrome.tabs.onRemoved.addListener(() => {
    if (QUEUE) {
      QUEUE.resetForPage("").then((changed) => {
        if (changed) {
          resetNetworkDetection();
          compatBadge("");
        }
      });
    }
  });
}

// ---- opening MagnetoClip's Detected page ----

const APP_SCHEME = "magneto://open-detected";

function notifyAppNotRunning(label) {
  notify(
    "MagnetoClip",
    "MagnetoClip isn't running. Open MagnetoClip to view and download your detected " +
      label +
      "."
  );
}

function detectedUri(category) {
  const key = typeof MagnetoclipCategories !== "undefined"
    ? MagnetoclipCategories.normalizeCategory(category)
    : "";
  return key ? APP_SCHEME + "?category=" + encodeURIComponent(key) : APP_SCHEME;
}

// Launch the custom protocol. A registered handler starts the app, so a cold
// MagnetoClip comes up and picks the queue up; an unregistered/failing handler
// must never be mistaken for success, and the queue is never cleared here.
function launchDetectedUri(uri, label) {
  chrome.tabs.create({ url: uri }, (tab) => {
    if (chrome.runtime.lastError || !tab) {
      notifyAppNotRunning(label);
      return;
    }
    // Give a cold start a moment to answer on the native port before telling
    // the user that nothing happened.
    setTimeout(() => {
      if (!ensurePort()) {
        notifyAppNotRunning(label);
      }
    }, 5000);
  });
}

// Show the app's Detected page, optionally filtered to one category. Called by
// the popup's category buttons.
function openDetectedInApp(category) {
  const key = typeof MagnetoclipCategories !== "undefined"
    ? MagnetoclipCategories.normalizeCategory(category)
    : "";
  // The message names the category the user clicked ("your detected Videos").
  const label = key && typeof MagnetoclipCategories !== "undefined"
    ? MagnetoclipCategories.CATEGORY_LABELS[key] || key
    : "files";
  if (QUEUE && key) {
    // Only the clicked category is handed over, and only now: nothing is sent
    // to MagnetoClip while the page is being browsed.
    QUEUE.syncCategory(key).then((result) => {
      if (result.sent) {
        compatBadge(QUEUE.badgeText());
      }
    });
  }
  // A live native port means MagnetoClip is running: ask it to raise its own
  // page instead of bouncing the user through a browser tab.
  if (ensurePort()) {
    request("open_detected", { category: key })
      .then(() => {})
      .catch(() => launchDetectedUri(detectedUri(key), label));
    return;
  }
  launchDetectedUri(detectedUri(key), label);
}

const MENU_ITEMS = [
  { id: "mc_download_link", title: "Download link with MagnetoClip", contexts: ["link"] },
  { id: "mc_download_video", title: "Download video with MagnetoClip", contexts: ["video"] },
  { id: "mc_download_audio", title: "Download audio with MagnetoClip", contexts: ["audio"] },
  { id: "mc_download_image", title: "Download image with MagnetoClip", contexts: ["image"] },
  { id: "mc_archive_page", title: "Archive this page with MagnetoClip", contexts: ["page"] }
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
        settingsLoaded = true;
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
            chrome.downloads.cancel(downloadId, () => {
              chrome.downloads.removeFile(downloadId, () => {});
            });
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
      // The app acknowledged detections it imported (or removed) from its
      // Detected page: drop those local copies so the badge shrinks.
      if (message.type === "detections_handled" || message.type === "detections_removed") {
        if (QUEUE) {
          QUEUE.handled({ ids: message.ids || [], urls: message.urls || [] }).then((removed) => {
            if (removed) {
              compatBadge(QUEUE.badgeText());
            }
          });
        }
        return;
      }
      // The app asked for everything still pending (it just started, or the user
      // opened the Detected page). A plain flush answers it: the batches flow
      // through the normal detection_sync request/response path.
      if (message.type === "fetch_detections") {
        if (QUEUE) {
          QUEUE.flush();
        }
        return;
      }
    });
    nativePort.onDisconnect.addListener(() => {
      nativePort = null;
      // Fail everything still waiting so the popup reports a clear error
      // right away instead of hanging until its own watchdog fires.
      const waiting = Array.from(pendingResponses.values());
      pendingResponses.clear();
      for (const entry of waiting) {
        entry.reject(new Error("MagnetoClip is not reachable. Is the app running?"));
      }
      for (const [requestId, watchdog] of interceptTimers) {
        clearTimeout(watchdog);
        interceptTimers.delete(requestId);
      }
      interceptRequests.clear();
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
    try {
      port.postMessage({ id, type, ...payload });
    } catch (error) {
      pendingResponses.delete(id);
      reject(new Error("MagnetoClip is not reachable. Is the app running?"));
    }
  });
}

// Captures can be refused by the app itself (integration disabled, capture
// disabled). Those rejections used to vanish into .catch(() => {}), which made
// every button feel dead. Surface them as a notification, rate-limited so a
// page firing twenty captures does not fire twenty toasts.
let lastRefusalNotice = 0;

function notifyCaptureRefusal(message) {
  const now = Date.now();
  if (now - lastRefusalNotice < 60000) {
    return;
  }
  lastRefusalNotice = now;
  notify("MagnetoClip", message);
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

// Resolve a pasted Telegram proxy link (cache, content-script probe, temporary
// tab) and hand the app the resolved CDN URL with its cookies. When nothing
// resolves — Telegram Web A serves one-shot stream tokens no page request can
// repeat — fall back to the browser's own downloader, which still owns the
// session while the video is on screen, and import the finished file.
async function resolvePastedTelegramUrl(requestId, url) {
  const entry = await resolveTelegramProxyUrl(url);
  if (entry && entry.target) {
    let cookies = "";
    try {
      cookies = (await getCookiesFor(entry.target)) || "";
    } catch (error) {
      cookies = "";
    }
    await request("blob_fetch_result", {
      request_id: requestId,
      resolved_url: entry.target,
      cookies: cookies,
      referrer: entry.referrer || "",
      mime_type: "",
    });
    return true;
  }
  browserDownloadFallback(requestId, url);
  return true;
}

// Finished files the browser downloaded on MagnetoClip's behalf:
// downloadId -> {requestId, url}.
const browserDownloads = new Map();
const BROWSER_DOWNLOAD_WAIT_MS = 10 * 60 * 1000;

function sanitizeDownloadFileName(name) {
  return String(name || "")
    .replace(/[\\/:*?"<>|\x00-\x1f]+/g, "_")
    .replace(/[\r\n]+/g, "")
    .trim();
}

function browserDownloadFallback(requestId, url) {
  if (!chrome.downloads) {
    sendFetchBlobError(requestId, "The browser could not download this link");
    return;
  }
  const suggestedName = sanitizeDownloadFileName(guessFilename(url)) || "telegram-media";
  chrome.downloads.download(
    {
      url: url,
      filename: suggestedName,
      conflictAction: "uniquify",
      saveAs: false,
    },
    (downloadId) => {
      if (chrome.runtime.lastError || downloadId == null) {
        sendFetchBlobError(
          requestId,
          String(
            (chrome.runtime.lastError && chrome.runtime.lastError.message) ||
              chrome.runtime.lastError ||
              "Could not start the browser download"
          )
        );
        return;
      }
      browserDownloads.set(downloadId, {
        requestId: requestId,
        url: url,
        started: Date.now(),
      });
      setTimeout(() => {
        if (browserDownloads.has(downloadId)) {
          browserDownloads.delete(downloadId);
          sendFetchBlobError(requestId, "The browser download timed out");
        }
      }, BROWSER_DOWNLOAD_WAIT_MS);
    }
  );
}

if (chrome.downloads && chrome.downloads.onChanged) {
  chrome.downloads.onChanged.addListener((delta) => {
    const entry = browserDownloads.get(delta.id);
    if (!entry || !delta.state) {
      return;
    }
    const state = delta.state.current;
    if (state === "complete") {
      browserDownloads.delete(delta.id);
      chrome.downloads.search({ id: delta.id }, (items) => {
        const item = items && items[0];
        const path = item && item.filename;
        if (item && path) {
          request("blob_fetch_result", {
            request_id: entry.requestId,
            external_path: path,
            filename: String(item.filename).split(/[\\/]/).pop() || "",
            mime_type: item.mime || "",
            source_url: entry.url,
          }).catch(() => {
            sendFetchBlobError(
              entry.requestId,
              "Could not deliver the downloaded file"
            );
          });
        } else {
          sendFetchBlobError(
            entry.requestId,
            "The downloaded file could not be located"
          );
        }
      });
    } else if (state === "interrupted") {
      browserDownloads.delete(delta.id);
      sendFetchBlobError(entry.requestId, "The browser download was interrupted");
    }
  });
}

function handleFetchBlob(message) {
  const requestId = message.request_id;
  const url = String(message.url || "");
  const isBlob = /^blob:/i.test(url);
  const isHttp = /^https?:/i.test(url);
  if (!isBlob && !isHttp) {
    sendFetchBlobError(requestId, "Unsupported URL");
    return;
  }
  let origin = "";
  try {
    origin = new URL(url).origin;
  } catch (error) {
    origin = "";
  }
  if (!origin) {
    sendFetchBlobError(requestId, "Invalid URL");
    return;
  }
  if (isHttp && message.background && !isTelegramMediaProxyUrl(url)) {
    // MagnetoClip asked for a fetch done by the browser itself (HLS
    // manifests/segments behind Cloudflare "Managed Challenge"). Prefer the
    // player page's own context: Cloudflare ties the challenge to the page
    // that plays the stream, so a content-script fetch (which carries the
    // page's cookies and Referer) succeeds where an extension-context request
    // would be challenged. Fall back to the service worker when no page
    // matches the captured referrer or the page cannot provide the content.
    fetchViaPageContextOrBackground(
      requestId,
      url,
      message.referrer || "",
      message.frame_url || "",
      message.headers || {}
    );
    return;
  }
  if (isHttp && isTelegramMediaProxyUrl(url)) {
    // Resolve the pasted Telegram proxy link to its CDN target before letting
    // the content script try to fetch it (which Telegram Web A denies).
    resolvePastedTelegramUrl(requestId, url).then((resolved) => {
      if (resolved) {
        return;
      }
      // No resolution discovered: fall through to the content-script probe,
      // which will report why the link cannot be supplied.
      probeViaContentScript(requestId, url);
    }).catch((error) => {
      sendFetchBlobError(
        requestId,
        String((error && error.message) || error || "Could not resolve the Telegram link")
      );
    });
    return;
  }
  probeViaContentScript(requestId, url);
}

// Fetch a URL from the service worker (Chrome's real network stack — TLS,
// certificates, cookies, User-Agent) and stream the body back to MagnetoClip
// as ``blob_fetch_chunk`` messages so the host reassembles it, the same way the
// tab-based ``fetch_blob`` path delivers content.
async function backgroundFetchAndStream(requestId, url, referrer, requestHeaders) {
  const options = { credentials: "include" };
  // Only forward headers that Fetch permits from an extension context. Cookie,
  // Referer, Origin and User-Agent remain browser-controlled.
  const safeHeaders = {};
  for (const [name, value] of Object.entries(requestHeaders || {})) {
    const lower = String(name).toLowerCase();
    if (["authorization", "accept", "accept-language", "cache-control", "pragma", "range"].includes(lower)) {
      safeHeaders[name] = String(value || "");
    }
  }
  if (Object.keys(safeHeaders).length) options.headers = safeHeaders;
  // ``Referer`` is a forbidden header in browsers, so it is sent through the
  // fetch ``referrer`` option with unsafe-url instead. The tokenized CDN URLs
  // validate the player page they were issued for.
  if (referrer) {
    options.referrerPolicy = "unsafe-url";
    options.referrer = referrer;
  }
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    sendFetchBlobError(
      requestId,
      String((error && error.message) || error || "Network error")
    );
    return;
  }
  if (!response.ok) {
    sendFetchBlobError(requestId, "service worker: HTTP " + response.status /*magnetoclip-rtr-v1*/);
    return;
  }
  const contentType = response.headers.get("content-type") || "";
  let buffer;
  try {
    buffer = await response.arrayBuffer();
  } catch (error) {
    sendFetchBlobError(
      requestId,
      String((error && error.message) || error || "Could not read the response")
    );
    return;
  }
  const bytes = new Uint8Array(buffer);
  const total = Math.max(1, Math.ceil(bytes.length / BLOB_FETCH_BASE64_BYTES));
  for (let index = 0; index < total; index++) {
    const start = index * BLOB_FETCH_BASE64_BYTES;
    const end = Math.min(bytes.length, start + BLOB_FETCH_BASE64_BYTES);
    const chunk = uint8ToBase64(bytes.subarray(start, end));
    request("blob_fetch_chunk", {
      request_id: requestId,
      index: index,
      total: total,
      chunk: chunk,
      url: url,
      filename: guessFilename(url),
      mime_type: contentType,
    }).catch(() => {});
  }
}

// Fetch an HLS URL from the player page's own context. Cloudflare stamps the
// challenge cookie for the page that plays the stream, so only requests sent
// from that page carry it; extension-context fetches are re-challenged. The
// captured referrer is the URL of the page/frame that requested the stream --
// on anikai.watch that is the megaplay.buzz/megaplay.su player IFRAME inside
// the tab, not the tab's own URL. So first match a tab whose top-level URL
// matches the referrer origin, then scan every frame of every open tab for a
// frame whose URL matches; ask that specific frame's content script to fetch
// and stream the bytes back. Otherwise fall back to the service-worker fetch
// (which still works for non-Cloudflare hosts).
function fetchViaPageContextOrBackground(requestId, url, referrer, frameUrl, requestHeaders) {
  let referrerOrigin = "";
  try {
    referrerOrigin = new URL(referrer).origin;
  } catch (error) {
    referrerOrigin = "";
  }
  if (!referrerOrigin) {
    backgroundFetchAndStream(requestId, url, referrer, requestHeaders);
    return;
  }
  chrome.tabs.query({}, (tabs) => {
    const openTabs = tabs || [];
    // A capture can pin the exact player iframe that issued the manifest.
    // Prefer that frame over an origin-only match; streaming sites commonly
    // embed several players from the same origin and only one has the valid
    // challenge/session state.
    if (frameUrl && chrome.webNavigation && chrome.webNavigation.getAllFrames) {
      findFrameForUrl(openTabs, frameUrl, (match) => {
        if (match) {
          sendPageContextFetch(match.tabId, requestId, url, referrer, 0, match.frameId, requestHeaders);
          return;
        }
        continueByOrigin(openTabs, requestId, url, referrer, referrerOrigin, requestHeaders);
      });
      return;
    }
    continueByOrigin(openTabs, requestId, url, referrer, referrerOrigin, requestHeaders);
  });
}

function continueByOrigin(openTabs, requestId, url, referrer, referrerOrigin, requestHeaders) {
    let target = null;
    let targetFrameId = 0;
    for (const tab of openTabs) {
      if (tab.id == null || !tab.url) {
        continue;
      }
      let tabOrigin = "";
      try {
        tabOrigin = new URL(tab.url).origin;
      } catch (error) {
        /* keep scanning */
      }
      if (tabOrigin === referrerOrigin) {
        target = tab;
        break;
      }
    }
    if (target) {
      sendPageContextFetch(target.id, requestId, url, referrer, 0, 0, requestHeaders);
      return;
    }
    findFrameForOrigin(openTabs, referrerOrigin, (match) => {
      if (!match) {
        sendDiag({
          source: "page_context_routing",
          request_id: requestId,
          event: "no_frame_for_referrer",
          referrer: referrer,
        });
        backgroundFetchAndStream(requestId, url, referrer, requestHeaders);
        return;
      }
      sendPageContextFetch(match.tabId, requestId, url, referrer, 0, match.frameId, requestHeaders);
    });
}

// Find the open tab + frameId whose document URL matches the captured player
// frame. Query matching ignores a trailing slash but otherwise prefers the
// exact URL so a site with multiple same-origin players does not receive a
// request through the wrong challenge/session context.
function findFrameForUrl(tabs, wantedUrl, callback) {
  let wanted = "";
  try {
    wanted = new URL(wantedUrl).href.replace(/\/$/, "");
  } catch (error) {
    callback(null);
    return;
  }
  let tabIndex = 0;
  const visit = () => {
    while (tabIndex < tabs.length) {
      const tab = tabs[tabIndex++];
      if (tab.id == null) continue;
      if (tab.url) {
        try {
          if (new URL(tab.url).href.replace(/\/$/, "") === wanted) {
            callback({ tabId: tab.id, frameId: 0 });
            return;
          }
        } catch (error) { /* keep scanning */ }
      }
      chrome.webNavigation.getAllFrames({ tabId: tab.id }, (frames) => {
        if (chrome.runtime.lastError) {
          visit();
          return;
        }
        for (const frame of frames || []) {
          if (!frame || frame.frameId == null || !frame.url) continue;
          try {
            if (new URL(frame.url).href.replace(/\/$/, "") === wanted) {
              callback({ tabId: tab.id, frameId: frame.frameId });
              return;
            }
          } catch (error) { /* keep scanning */ }
        }
        visit();
      });
      return;
    }
    callback(null);
  };
  visit();
}

// Find the open tab + frameId whose document URL origin equals the referrer
// origin (the player iframe). Runs the callback with null when nothing matches.
function findFrameForOrigin(tabs, referrerOrigin, callback) {
  let tabIndex = 0;
  const visit = () => {
    while (tabIndex < tabs.length) {
      const tab = tabs[tabIndex++];
      if (tab.id == null) {
        continue;
      }
      if (!chrome.webNavigation || !chrome.webNavigation.getAllFrames) {
        continue;
      }
      chrome.webNavigation.getAllFrames({ tabId: tab.id }, (frames) => {
        if (chrome.runtime.lastError) {
          visit();
          return;
        }
        for (const frame of frames || []) {
          if (frame == null || frame.frameId == null || !frame.url) {
            continue;
          }
          let frameOrigin = "";
          try {
            frameOrigin = new URL(frame.url).origin;
          } catch (error) {
            /* keep scanning */
          }
          if (frameOrigin === referrerOrigin) {
            callback({ tabId: tab.id, frameId: frame.frameId });
            return;
          }
        }
        visit();
      });
      return;
    }
    callback(null);
  };
  visit();
}

// Ask a specific frame's content script to fetch and stream the bytes. If the
// content script has not been injected yet (tabs opened before the extension
// was loaded/reloaded do not get one until they navigate), inject it on demand
// and retry once, then fall back to the service-worker fetch.
function sendPageContextFetch(tabId, requestId, url, referrer, attempt, frameId, requestHeaders) {
  const message = { type: "fetch_blob_page", request_id: requestId, url: url };
  const options = frameId == null ? {} : { frameId: frameId };
  chrome.tabs.sendMessage(tabId, message, options, (response) => {
    const fallback = () => backgroundFetchAndStream(requestId, url, referrer, requestHeaders);
    if (chrome.runtime.lastError && attempt === 0 && chrome.scripting) {
      const target = frameId == null
        ? { tabId: tabId }
        : { tabId: tabId, frameIds: [frameId] };
      chrome.scripting
        .executeScript({ target: target, files: ["content.js"] })
        .then(() => sendPageContextFetch(tabId, requestId, url, referrer, 1, frameId, requestHeaders))
        .catch(() => fallback());
      return;
    }
      if (
      chrome.runtime.lastError ||
      !response ||
      response.type !== "fetch_blob_response" ||
      !response.ok
    ) {
      fallback();
      return;
    }
    if (response.streamed) {
      // The content script already streamed the bytes back as
      // ``blob_fetch_chunk`` messages; nothing left to split here.
      return;
    }
    const encoded = String(response.data_base64 || "");
    if (!encoded) {
      fallback();
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
  });
}

function probeViaContentScript(requestId, url) {
  let origin = "";
  try {
    origin = new URL(url).origin;
  } catch (error) {
    origin = "";
  }
  if (!origin) {
    sendFetchBlobError(requestId, "Invalid URL");
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
      sendFetchBlobError(requestId, "No open page matches this URL");
      return;
    }
    chrome.tabs.sendMessage(
      target.id,
      { type: "fetch_blob", request_id: requestId, url: url },
      async (response) => {
        if (
          chrome.runtime.lastError ||
          !response ||
          response.type !== "fetch_blob_response"
        ) {
          sendFetchBlobError(requestId, "The page could not provide this content");
          return;
        }
        if (!response.ok) {
          sendFetchBlobError(
            requestId,
            response.message || "Could not read the content"
          );
          return;
        }
        if (response.resolved_url) {
          // The content script resolved a Telegram Web proxy link to its direct
          // CDN file. Hand the resolved URL back with the session cookies so
          // MagnetoClip can download it directly (it needs the same cookies the
          // page uses, and a Referer so the CDN accepts the request).
          const resolved = String(response.resolved_url);
          const cookies =
            (await getCookiesFor(resolved)) ||
            (requestId && (await getCookiesFor(url))) ||
            "";
          request("blob_fetch_result", {
            request_id: requestId,
            resolved_url: resolved,
            cookies: cookies,
            referrer: target.url || "",
            mime_type: response.mime_type || "",
          }).catch(() => {});
          return;
        }
        if (response.streamed) {
          // The content script already streamed the bytes to us as
          // ``blob_fetch_chunk`` messages (which we forward to the host), so
          // there is nothing left to split here.
          return;
        }
        const encoded = String(response.data_base64 || "");
        if (!encoded) {
          sendFetchBlobError(requestId, "Empty content");
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
    // Extensionless image CDNs (Google thumbnail host, etc.) serve real image
    // bytes with no filename hint in the URL. Give them a default name so we
    // don't save an empty-extension file; the app derives the exact type from
    // the response Content-Type on download.
    if (isExtensionlessImageHost(url)) {
      const base = name && name.length <= 40 ? name : "gstatic-image";
      try {
        return decodeURIComponent(base) + ".jpg";
      } catch (error) {
        return base + ".jpg";
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

// Google's image thumbnail hosts serve real image bytes from extensionless
// URLs (e.g. `encrypted-tbn0.gstatic.com/images?q=tbn:...`). When a capture
// points at one of these, we hand the app a .jpg default name instead of an
// empty-extension filename; the exact type is derived from Content-Type when
// the file downloads.
function isExtensionlessImageHost(url) {
  let host;
  try {
    host = new URL(url).hostname;
  } catch (error) {
    host = url.split("/")[2] || "";
  }
  return /(?:^|\.)(?:encrypted-tbn\d*|lh\d+|yt\d+|ytimg|ggpht)\.(?:gstatic|googleusercontent|ggpht)\.com$/i.test(
    host
  );
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

// Detection rows are downloaded later, from MagnetoClip's Detected page —
// possibly after the tab is closed. Session-gated URLs (Telegram Web serves
// media only with the page's cookies) need them stored alongside the entry.
function getCookieDictFor(url) {
  return new Promise((resolve) => {
    if (!chrome.cookies || !/^https?:/i.test(url)) {
      resolve(null);
      return;
    }
    chrome.cookies.getAll({ url: url }, (cookies) => {
      if (chrome.runtime.lastError || !cookies || !cookies.length) {
        resolve(null);
        return;
      }
      const dict = {};
      for (const cookie of cookies) {
        if (cookie.name && cookie.value !== undefined && cookie.value !== null) {
          dict[cookie.name] = cookie.value;
        }
      }
      resolve(Object.keys(dict).length ? dict : null);
    });
  });
}

function withCookies(payload) {
  payload.user_agent = payload.user_agent || navigator.userAgent || "";
  return getCookiesFor(payload.url).then((cookies) => {
    if (cookies) {
      payload.cookies = cookies;
    }
    return payload;
  });
}

async function capture(payload) {
  // A Telegram proxy link (context-menu srcUrl, downloads.onCreated, etc.)
  // cannot be downloaded directly by the app, so resolve it to the real CDN
  // file first. Silent on cache/probe miss: the raw URL still reaches the app,
  // which keeps a record for the user to retry.
  if (payload && isTelegramMediaProxyUrl(String(payload.url || ""))) {
    const entry = await resolveTelegramProxyUrl(String(payload.url));
    if (entry && entry.target) {
      payload.url = entry.target;
      if (!payload.referrer) {
        payload.referrer = entry.referrer || "";
      }
      if (!payload.filename) {
        payload.filename = guessFilename(entry.target) || guessFilename(payload.url);
      }
    }
  }
  return withCookies(payload).then((full) => request("capture", full));
}

// Ask MagnetoClip to fetch and inline a page into a self-contained offline
// .html file. The archiver runs entirely in the app (server-side fetch), so
// this only needs the page URL; the page's cookies are passed along so the
// archiver can see session-gated content, and the tab title becomes a friendly
// default filename hint on the app side.
function archivePage(url, pageUrl) {
  return getCookiesFor(url).then((cookies) => {
    const payload = { url: url, source: "context_menu", detected_type: "webpage" };
    if (cookies) {
      payload.cookies = cookies;
    }
    if (pageUrl) {
      payload.referrer = pageUrl;
    }
    return request("archive", payload);
  });
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
  configureDetectionQueue();
});
chrome.runtime.onStartup.addListener(() => {
  ensureMenus();
  reinjectContentScripts();
  configureDetectionQueue();
});

loadStoredSettings();
ensurePort();
configureDetectionQueue();

chrome.tabs.onRemoved.addListener((tabId) => {
  tabDetections.delete(tabId);
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "mc_archive_page") {
    // Archive the current page as a self-contained HTML file.
    const pageUrl = info.pageUrl || (tab && tab.url) || "";
    if (!/^https?:/i.test(pageUrl)) {
      notify("MagnetoClip", "Only http:// and https:// pages can be archived.");
      return;
    }
    archivePage(pageUrl, tab && tab.url ? tab.url : "")
      .catch((error) => {
        notify("MagnetoClip unavailable", error.message);
      });
    return;
  }
  // For media (image/video/audio) the actual resource is srcUrl; a wrapping
  // link (linkUrl) is usually just an info/description page. Prefer srcUrl so
  // we detect the media file itself rather than the enclosing page.
  const url = info.srcUrl || info.linkUrl || "";
  if (!/^https?:/i.test(url)) {
    return;
  }
  // Right-click means "I want this in MagnetoClip", but the resource is still
  // only *detected*: it joins the queue and shows up in the matching category
  // of the Detected page, where the user downloads it. No capture dialog.
  const pageUrl = tab && tab.url ? tab.url : "";
  const queueResource = (target, referrer, filename) =>
    getCookiesFor(target)
      .then((cookies) =>
        queueDetection({
          url: target,
          filename: filename || guessFilename(target),
          referrer: referrer,
          page_url: pageUrl,
          source: "context_menu",
          detected_type: info.mediaType || "",
          cookies: cookies,
        })
      )
      .then((entry) => {
        if (entry) {
          notify(
            "MagnetoClip — added to detected files",
            (entry.filename || target) + " was added to " + entry.category + "."
          );
        }
        return entry;
      });

  // Telegram Web proxy links (progressive/stream) are one-shot session tokens:
  // by the time the user confirms a download the token is dead, so neither the
  // app nor the browser can re-fetch it (302 / interrupted). The right-click
  // itself happens while the media is still on screen, so the resolved CDN
  // target is queued when we can learn it, and otherwise the bytes are streamed
  // into a chunked capture NOW via the content script — the app's capture
  // dialog then completes from the buffered bytes instead of re-requesting the
  // link. Fall back to the plain queue when the media is not playing.
  const isTelegramProxy = /^https:\/\/web\.telegram\.org\/(?:a|k)\/(?:progressive|stream)\//i.test(url);
  const mediaKind = String(info.mediaType || "").toLowerCase();
  if (isTelegramProxy) {
    resolveTelegramProxyUrl(url).then((entry) => {
      if (entry && entry.target) {
        queueResource(entry.target, entry.referrer || pageUrl, guessFilename(entry.target));
        return;
      }
      if ((mediaKind === "video" || mediaKind === "audio") && tab && tab.id != null) {
        chrome.tabs.sendMessage(
          tab.id,
          { type: "live_capture", url: url, filename: guessFilename(url) },
          (response) => {
            if (!chrome.runtime.lastError && response && response.ok === true) {
              // Bytes are streaming to MagnetoClip; its capture dialog will appear
              // once the stream is assembled. Nothing else to do here.
              sendDiag({ source: "live_capture_outcome", ok: 1, kind: mediaKind, url: url });
              return;
            }
            const reason =
              chrome.runtime.lastError
                ? "the content script did not answer"
                : (response && response.message) || "live capture rejected";
            sendDiag({ source: "live_capture_outcome", ok: 0, kind: mediaKind, url: url, status: reason });
            // Nothing durable to queue, but the record still reaches the app so
            // the user can retry while the video is still open.
            queueResource(url, pageUrl, guessFilename(url));
          }
        );
        return;
      }
      queueResource(url, pageUrl, guessFilename(url));
    });
    return;
  }
  queueResource(url, pageUrl, guessFilename(url));
});

// ---- telegram request observer (diagnostics) ----
// Reports what Telegram's server actually returns for live media requests —
// the page's own <video> requests (type "media") and the live-capture probes
// (type "xmlhttprequest") — so the capture path can mirror the page's request
// pattern from evidence instead of guesswork. Written to the app's event log
// via the "diag" message; debounced so a playing video does not flood it.
const TELEGRAM_MEDIA_RE = /^https:\/\/(?:web\.telegram\.org\/(?:(?:a|k)\/)?(?:progressive|stream|api\/files)\/|cdn\.telegram\.org\/|(?:[^/]+\.)?telesco\.pe\/)/i;
const TELEGRAM_DIAG_HEADERS = new Set([
  "content-type",
  "content-range",
  "content-length",
  "location",
  "access-control-allow-origin",
  "content-disposition",
  "x-origin",
]);
const recentTelegramDiag = new Map();

function sendDiag(payload) {
  request("diag", payload).catch(() => {});
}

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    const url = details.url || "";
    if (!TELEGRAM_MEDIA_RE.test(url)) {
      return;
    }
    if (!["media", "xmlhttprequest", "main_frame"].includes(details.type)) {
      return;
    }
    const headers = {};
    try {
      for (const header of details.responseHeaders || []) {
        const name = String(header.name || "").toLowerCase();
        if (TELEGRAM_DIAG_HEADERS.has(name)) {
          headers[name] = String(header.value || "").slice(0, 80);
        }
      }
    } catch (error) {
      // responseHeaders not readable for this request; skip header detail.
    }
    let range = "";
    try {
      for (const header of details.requestHeaders || []) {
        if (String(header.name || "").toLowerCase() === "range") {
          range = String(header.value || "").slice(0, 40);
        }
      }
    } catch (error) {
      // requestHeaders not readable for this request.
    }
    const queryStart = url.indexOf("?");
    const baseUrl = (queryStart >= 0 ? url.slice(0, queryStart) : url).slice(0, 110);
    const key = `${details.type}|${details.statusCode}|${baseUrl}`;
    const now = Date.now();
    const last = recentTelegramDiag.get(key);
    if (last && now - last < 1500) {
      return;
    }
    recentTelegramDiag.set(key, now);
    if (recentTelegramDiag.size > 200) {
      recentTelegramDiag.clear();
    }
    sendDiag({
      source: "telegram_media_request",
      kind: details.type,
      status: details.statusCode,
      url: baseUrl,
      range: range,
      headers: headers,
    });
  },
  { urls: ["*://web.telegram.org/*", "*://cdn.telegram.org/*"] },
  IS_FIREFOX ? ["responseHeaders"] : ["responseHeaders", "extraHeaders"]
);

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
    .catch((error) => {
      notifyCaptureRefusal(error.message);
    });
}

function detectMediaType(filename) {
  const ext = String(filename || "").split(".").pop().toLowerCase();
  if (["mp3", "wav", "ogg", "flac", "m4a", "aac", "opus", "wma"].includes(ext)) {
    return "audio";
  }
  if (["mp4", "webm", "mkv", "mov", "avi", "flv", "m4v", "3gp", "ts", "mpg", "mpeg"].includes(ext)) {
    return "video";
  }
  if (["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif", "ico", "heic"].includes(ext)) {
    return "image";
  }
  return "file";
}

// Blob: downloads (ad-network media pages, embedded viewers) resolve only
// inside the frame that created the blob. Instead of asking the host to
// re-download a URL it cannot fetch, read the bytes from the origin tab via
// the content script and stream them to the host as a chunked capture. The
// host answers with a normal capture_ok/pending/error/skipped, which the
// message loop turns into cancel+removeFile (or "let the browser keep it").
function captureBlobDownload(item, filename, tabUrl) {
  const url = item.url;
  if (!/^blob:/i.test(url)) {
    return;
  }
  let origin = "";
  try {
    origin = new URL(url).origin;
  } catch (error) {
    origin = "";
  }
  if (!origin) {
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
      return;
    }
    chrome.tabs.sendMessage(target.id, { type: "fetch_blob", url: url }, (response) => {
      if (chrome.runtime.lastError || !response || !response.ok || !response.data_base64) {
        return;
      }
      const b64 = String(response.data_base64);
      const key = "dl-" + item.id + "-" + Date.now();
      const total = Math.max(1, Math.ceil(b64.length / BLOB_FETCH_BASE64_CHUNK));
      let index = 0;
      const sendChunk = () => {
        const last = index === total - 1;
        const payload = {
          capture_key: key,
          index: index,
          total: total,
          chunk: b64.slice(index * BLOB_FETCH_BASE64_CHUNK, (index + 1) * BLOB_FETCH_BASE64_CHUNK),
          url: url,
          filename: filename || "",
          referrer: tabUrl || target.url || "",
          mime_type: response.mime_type || "",
          detected_type: detectMediaType(filename),
          last: last,
        };
        if (last) {
          request("capture_chunk", payload, (requestId) => {
            interceptRequests.set(requestId, item.id);
            const watchdog = setTimeout(() => {
              interceptRequests.delete(requestId);
              interceptTimers.delete(requestId);
              interceptedDownloads.delete(item.id);
            }, 30000);
            interceptTimers.set(requestId, watchdog);
          }).catch(() => {
            /* byte-fetch failures fall back to the browser copy */
          });
          return;
        }
        request("capture_chunk", payload)
          .then((ack) => {
            if (ack && (ack.type === "capture_chunk_ok" || ack.type === "capture_pending" || ack.type === "capture_ok" || ack.type === "capture_skipped")) {
              index += 1;
              sendChunk();
            } else {
              /* unexpected ack: stop streaming */
            }
          })
          .catch(() => {
            /* byte-fetch failures fall back to the browser copy */
          });
      };
      sendChunk();
    });
  });
}

// Route an intercepted download to the right capture path. http(s) downloads
// are offered to the host by URL; blob: downloads are shipped as bytes from the
// origin tab (falling back silently to the browser copy if that cannot be read).
function submitCapture(item, filename) {
  if (/^blob:/i.test(item.url)) {
    captureBlobDownload(item, filename, item.referrer || "");
    return;
  }
  sendCapture(item, filename);
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
      submitCapture(item, pending.fallback || fallback);
    }
  }, 2000);
  pendingIntercepts.set(item.id, { timer: timer, fallback: fallback, sent: false });
  return true;
}

chrome.downloads.onCreated.addListener((item) => {
  const isHttp = /^https?:/i.test(item.url);
  const isBlob = /^blob:/i.test(item.url);
  if (!isHttp && !isBlob) {
    return;
  }
  // Until settings_ok arrives from the native host, the flags hold only
  // guesses. Claim every http(s) download and let the app — which owns the
  // real settings — answer capture_ok/pending (cancel the browser copy) or
  // capture_skipped (the browser keeps the file). Only use the fast scope
  // gate below once the host has confirmed the current settings.
  const mime = (item.mime || "").toLowerCase();
  const isMedia = mime.startsWith("video/") || mime.startsWith("audio/") || mime.startsWith("image/");
  // HLS/DASH manifests (m3u8/mpd) are streaming transport files; treat them as
  // media so they are intercepted and offered to MagnetoClip for extraction.
  const isStream = /\.(m3u8|m3u|mpd)(\?|#|$)/i.test(item.url);
  // default_downloader: intercept everything MagnetoClip understands. Otherwise
  // intercept only media when capture is enabled, and leave other files to the
  // browser's own downloader.
  const shouldCapture = defaultDownloader || (captureEnabled && (isMedia || isStream));
  if (settingsLoaded && (!shouldCapture || !integrationEnabled)) {
    return;
  }
  // Blob: downloads (ad-network media pages, embedded viewers) can only be
  // resolved inside the frame that created the blob, so they are only claimed
  // when MagnetoClip is the default downloader: the bytes are pulled from the
  // origin tab and shipped inline, and the browser copy is cancelled.
  if (isBlob && !(settingsLoaded && defaultDownloader)) {
    return;
  }
  interceptDownload(item);
});

if (chrome.downloads && chrome.downloads.onDeterminingFilename) {
  chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  if (!/^https?:/i.test(item.url) && !/^blob:/i.test(item.url)) {
    return;
  }
  if (!interceptedDownloads.has(item.id)) {
    // Not owned by MagnetoClip: let the browser save it normally.
    return;
  }
  // Let the browser save under the resolved name for now; the browser copy is
  // cancelled only if MagnetoClip confirms it can download the URL.
  const pending = pendingIntercepts.get(item.id);
  // Prefer the browser-resolved filename (from Content-Disposition), fall back
  // to the URL-derived guess.  Strip any leading directory components that the
  // browser may prepend (e.g. "C:\Users\...\Downloads\file.zip").
  const resolvedName = item.filename ? baseName(item.filename) : "";
  const filename = resolvedName || (pending && pending.fallback) || guessFilename(item.url) || "";
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
  submitCapture(item, filename);
  });
}
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
            // Version this hook rather than relying on the old boolean. An
            // older extension build may have already patched URL.createObjectURL
            // in this long-lived Telegram SPA document; that old hook only sent
            // the URL and left revoked Blob URLs unreadable. Wrap the existing
            // hook once more so new captures include the finite Blob object.
            if (Number(window.__mcObjectUrlHookVersion || 0) >= 3) {
              return;
            }
            const original = URL.createObjectURL;
            URL.createObjectURL = function (obj) {
              const url = original.call(this, obj);
              if (url && url.indexOf("blob:") === 0) {
                try {
                  const onTelegram = /(^|\.)telegram\.org$/i.test(String(location.hostname || ""));
                  const isBlob = typeof Blob !== "undefined" && obj instanceof Blob;
                  const isMediaSource = typeof MediaSource !== "undefined" && obj instanceof MediaSource;
                  const isMediaStream = typeof MediaStream !== "undefined" && obj instanceof MediaStream;
                  const objectKind = isBlob ? "blob" : isMediaSource ? "media_source" : isMediaStream ? "media_stream" : "other";
                  const payload = {
                    source: "magnetoclip-blob",
                    url: url,
                    object_kind: objectKind,
                    hook_version: 3,
                  };
                  if (onTelegram && isBlob) {
                    // Pass the Blob handle to the isolated world so Telegram
                    // can revoke its object URL without destroying capture.
                    payload.blob = obj;
                    payload.blob_size = obj.size;
                    payload.mime_type = String(obj.type || "");
                  }
                  window.postMessage(payload, "*");
                } catch (error) {
                  /* noop */
                }
              }
              return url;
            };
            window.__mcObjectUrlHooked = true;
            window.__mcObjectUrlHookVersion = 3;
          },
        })
        .catch(() => {});
    }
    return;
  }
  if (message && message.type === "detection_status") {
    // The popup only ever needs the per-category counts: the six categories are
    // the whole user interface, no individual files.
    if (!QUEUE) {
      sendResponse({ type: "detection_status_ok", total: 0, counts: {} });
      return true;
    }
    QUEUE.stats().then((stats) => {
      sendResponse({
        type: "detection_status_ok",
        total: stats.total,
        pending: stats.pending,
        counts: stats.counts,
      });
    });
    return true;
  }
  if (message && message.type === "rescan_detections") {
    // Refresh in the popup: discard everything captured so far and detect the
    // page again. Nothing is kept from the previous pass.
    if (!QUEUE) {
      sendResponse({ type: "rescan_detections_ok", total: 0, counts: {} });
      return true;
    }
    QUEUE.resetForPage("", { force: true }).then(() => {
      // A refresh must re-detect from scratch, so the network detector's memory
      // of what it already reported has to go too.
      resetNetworkDetection();
      compatBadge("");
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const tab = tabs && tabs[0];
        if (!tab || tab.id == null) {
          QUEUE.stats().then((stats) => {
            sendResponse({ type: "rescan_detections_ok", total: stats.total, counts: stats.counts });
          });
          return;
        }
        // The content script re-reports from scratch; it answers on a tab that
        // cannot receive the message (chrome:// page, no script yet).
        chrome.tabs.sendMessage(tab.id, { type: "rescan_page" }, () => {
          void chrome.runtime.lastError;
        });
        setTimeout(() => {
          QUEUE.stats().then((stats) => {
            sendResponse({
              type: "rescan_detections_ok",
              total: stats.total,
              counts: stats.counts,
              page_url: tab.url || "",
            });
          });
        }, RESCAN_SETTLE_MS);
      });
    });
    return true;
  }
  if (message && (message.type === "open_detected" || message.type === "open_all_detected")) {
    openDetectedInApp(message.category || "");
    sendResponse({ type: "open_detected_ok" });
    return true;
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
        } else if (response.type === "capture_skipped") {
          notify(
            "MagnetoClip — downloads paused",
            "'Skip all' is active, so this file was not queued. Re-enable it in MagnetoClip's settings."
          );
          sendResponse({
            type: "capture_skipped",
            message: response.message || "'Skip all' is active in MagnetoClip.",
          });
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
  if (message && message.type === "blob_fetch_chunk") {
    // The content script streamed pasted blob:/Telegram media as base64 chunks
    // (larger than one content<->background message can carry). Forward each to
    // the native host, which reassembles them by request_id.
    request("blob_fetch_chunk", {
      request_id: message.request_id,
      index: message.index,
      total: message.total,
      chunk: message.chunk,
      url: message.url || "",
      filename: guessFilename(message.url) || "",
      mime_type: message.mime_type || "",
      last: !!message.last,
    })
      .then((response) => {
        if (response && response.type === "blob_fetch_chunk_ok") {
          sendResponse({ type: "blob_fetch_chunk_ok", ...response });
        } else {
          sendResponse({
            type: "blob_fetch_chunk_error",
            message: (response && response.message) || "Could not send the content to MagnetoClip.",
          });
        }
      })
      .catch((error) => {
        sendResponse({ type: "blob_fetch_chunk_error", message: error.message });
      });
    return true;
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
  if (message && message.type === "player_lookup") {
    // The player overlay asks which queued detections could belong to a
    // <video>. Answer with the trimmed entries, read-only.
    const signals = Array.isArray(message.signals)
      ? message.signals.map((value) => String(value))
      : [];
    // The blob flag travels separately from the hints (the blob: URL itself is
    // never a usable hint), so sniffing it out of the hints alone would miss
    // every MSE/blob player. Let the flag speak for itself.
    const blob = message.blob === 1 || signals.some((value) => /^blob:/i.test(value));
    if (!QUEUE || (signals.length === 0 && !blob)) {
      sendResponse({ type: "player_lookup_ok", entries: [], total: 0, queue_urls: [] });
      return true;
    }
    QUEUE.all()
      .then((entries) => {
        sendResponse({
          type: "player_lookup_ok",
          entries: entriesForPlayer(
            entries,
            signals,
            String(message.page_url || ""),
            String(message.frame_url || ""),
            blob
          ),
          // Read-only diagnostics so the overlay can explain why nothing
          // matched: how many entries the queue holds and what they are.
          total: entries.length,
          queue_urls: entries.slice(0, 6).map((entry) => String(entry.url || "")),
        });
      })
      .catch(() => {
        sendResponse({ type: "player_lookup_ok", entries: [], total: 0, queue_urls: [] });
      });
    return true;
  }
  if (message && message.type === "player_media_capture") {
    const url = String(message.url || "").trim();
    const detectedType = String(message.detected_type || "video").toLowerCase();
    if (!/^https?:/i.test(url) || (detectedType !== "video" && detectedType !== "audio")) {
      sendResponse({ type: "player_media_capture_error", message: "Invalid player media target." });
      return false;
    }
    const pageUrl = String(message.page_url || (sender && sender.tab && sender.tab.url) || "");
    const frameUrl = String(message.frame_url || (sender && sender.url) || pageUrl);
    const context = recentPlayerMediaContext(url) || {};
    const responseType = normalizeMime(context.response_content_type || "");
    const responseStatus = Number(context.response_status || 0);
    const isFacebookCdn = /\.fbcdn\.net$/i.test(hostOf(url));

    // For Facebook CDN player captures, do not trust a URL path alone. The
    // player may expose an /emg1/v/... resource that looks like video but the
    // actual response is an image, HTML error page, or other non-media body.
    // Such responses are the source of the tiny 808 B / thumbnail downloads.
    // If the browser gave us a response Content-Type, require it to agree with
    // the media type requested by the player. 206 is valid because Facebook
    // normally serves seek/buffer ranges to the browser.
    if (isFacebookCdn) {
      if (responseStatus && (responseStatus < 200 || responseStatus >= 400)) {
        sendResponse({
          type: "player_media_capture_error",
          message: "Facebook returned HTTP " + responseStatus + " for the player media.",
        });
        return false;
      }
      if (responseType) {
        const expectedPrefix = detectedType === "audio" ? "audio/" : "video/";
        if (!responseType.startsWith(expectedPrefix)) {
          sendResponse({
            type: "player_media_capture_error",
            message: "Facebook player URL returned " + responseType + ", not " + expectedPrefix.slice(0, -1) + ".",
          });
          return false;
        }
      }
    }

    const headers = Object.assign({}, context.headers || {});
    // Never replay the browser's byte-range request for an explicit player
    // download. Facebook frequently requests small ranges while buffering;
    // replaying that Range header turns the download into a tiny partial file.
    for (const key of Object.keys(headers)) {
      if (String(key).toLowerCase() === "range") {
        delete headers[key];
      }
    }
    const referrer = headers.Referer || headers.referer || frameUrl || pageUrl;
    const userAgent = headers["User-Agent"] || headers["user-agent"] || "";
    const filename = guessFilename(url) || (detectedType === "audio" ? "audio" : "video");
    getCookiesFor(url).then((cookies) => {
      return queueDetection({
        url: url,
        filename: filename,
        referrer: referrer,
        page_url: pageUrl,
        frame_url: frameUrl,
        page_title: String(message.page_title || ""),
        source: "player_button",
        detected_type: detectedType,
        mime_type: detectedType === "audio" ? "audio/*" : "video/*",
        headers: headers,
        cookies: cookies,
        user_agent: userAgent,
        request_context: context.type || "media",
      }).then((entry) => {
        if (!entry) {
          return QUEUE.all().then((entries) => entries.find((candidate) => queueEntryMatchesSignals(candidate, [url], pageUrl)) || null);
        }
        return entry;
      });
    }).then((entry) => {
      sendResponse({
        type: entry ? "player_media_capture_ok" : "player_media_capture_error",
        entry: entry ? forExtensionEntry(entry) : null,
        message: entry ? "Player media queued." : "Could not queue player media.",
      });
    }).catch((error) => {
      sendResponse({
        type: "player_media_capture_error",
        message: (error && error.message) || "Could not queue player media.",
      });
    });
    return true;
  }
  if (message && message.type === "download_detection") {
    // Explicit download from the player overlay button. The background worker
    // resolves the canonical detection id (queue lookup, then URL match, then
    // re-creating from the player's own pointer if the queue already delivered
    // it), and forwards the full entry to the native host.
    handlePlayerDownload(message, sender)
      .then((response) => {
        sendResponse(response);
      })
      .catch((error) => {
        sendResponse({
          type: "download_detection_error",
          message: (error && error.message) || "Could not start the download.",
        });
      });
    return true;
  }
    if (message && (message.type === "social_capture" || message.type === "page_scan")) {
      // Social/media pages and plain page scans are *detections*, not downloads:
      // they join the queue and surface in the matching Detected-page category.
      // Nothing here may create a capture dialog in the app.
      const files = Array.isArray(message.files) ? message.files : [];
      const pageUrl = String(message.url || "");
      const entries = [];
      const seen = new Set();
      for (const file of files) {
        if (!file || !file.url) {
          continue;
        }
        const isHttp = /^https?:/i.test(file.url);
        const hasData = Boolean(file.data_base64);
        if (!isHttp && !hasData) {
          continue;
        }
        seen.add(file.url);
        entries.push({
          url: file.url,
          filename: file.filename || "",
          referrer: pageUrl,
          page_url: pageUrl,
          page_title: file.page_title || message.title || "",
          source: "page_scan",
          detected_type: file.detected_type || "file",
          mime_type: file.mime_type || "",
          data_base64: hasData ? file.data_base64 : undefined,
        });
      }
      // Social and yt-dlp pages do not report their own URL unless media is
      // already playing, so record the page too: it is what MagnetoClip
      // resolves with yt-dlp. Plain page scans must not add the page itself.
      if (
        message.type === "social_capture" &&
        /^https?:/i.test(pageUrl) &&
        !seen.has(pageUrl)
      ) {
        entries.push({
          url: pageUrl,
          filename: message.title || "",
          referrer: pageUrl,
          page_url: pageUrl,
          source: "page_scan",
          detected_type: "video",
        });
      }
      if (entries.length) {
        // Session-gated hosts (social/Telegram) need the page cookies stored with
        // the entry: downloads happen later, possibly after the tab is closed.
        getCookieDictFor(pageUrl).then((cookies) => {
          if (cookies) {
            for (const entry of entries) {
              entry.cookies = cookies;
            }
          }
          return queueDetections(entries, { adopt: message.top_frame !== false });
        });
      }
      return;
    }
});

// Generic browser-network media detector. Do not gate this on a list of
// streaming websites. A modern player may request an HLS/DASH manifest or a
// progressive video from an arbitrary CDN with no useful file extension.
// Response MIME type is therefore the strongest signal, followed by URL and
// Content-Disposition extension hints. Segment files are deliberately ignored.
if (chrome.webRequest) {
  const requestHeaderInfo = IS_FIREFOX
    ? ["requestHeaders"]
    : ["requestHeaders", "extraHeaders"];

  chrome.webRequest.onBeforeSendHeaders.addListener(
    (details) => {
      if (details.requestId == null || !isMediaRequestType(details.type)) return;
      const url = String(details.url || "");
      const kindHint = mediaKindFromExtension(url);
      const pathHint = /(?:hls|dash|manifest|playlist|master|stream|video|media|chunk)/i.test(url);
      if (!kindHint && !pathHint && details.type !== "media" && details.type !== "object") {
        return;
      }
      const requestContext = {
        headers: captureManifestHeaders(details.requestHeaders),
        seenAt: Date.now(),
        url: url,
        type: String(details.type || ""),
      };
      manifestRequestContexts.set(String(details.requestId), requestContext);
      // Keep exact context for explicit player-button captures. Facebook and
      // similar CDNs commonly expose signed progressive URLs without a file
      // extension, so they may not be classified again after the request ends.
      if (/\.fbcdn\.net$/i.test(hostOf(url)) || details.type === "media") {
        rememberRecentPlayerMediaContext(url, requestContext);
      }
    },
    { urls: ["http://*/*", "https://*/*"] },
    requestHeaderInfo
  );

  chrome.webRequest.onHeadersReceived.addListener(
    (details) => {
      if (!integrationEnabled || !captureEnabled) return;

      const url = String(details.url || "");
      if (!/^https?:/i.test(url) || isSegmentUrl(url) || !isMediaRequestType(details.type)) return;

      const mime = responseHeader(details.responseHeaders, "content-type");
      const contentDisposition = responseHeader(details.responseHeaders, "content-disposition");
      const dispositionName = (() => {
        const match = contentDisposition.match(/filename\*?=(?:UTF-8''|\")?([^\";]+)/i);
        if (!match) return "";
        try { return decodeURIComponent(match[1].replace(/^\"|\"$/g, "")); }
        catch (_) { return match[1].replace(/^\"|\"$/g, ""); }
      })();

      const mimeKind = mediaKindFromMime(mime);
      const extKind = mediaKindFromExtension(url) || mediaKindFromExtension(dispositionName);
      const kind = mimeKind || extKind;

      const requestContext = manifestRequestContexts.get(String(details.requestId)) || {};
      const headers = requestContext.headers || {};
      if (/\.fbcdn\.net$/i.test(hostOf(url)) || requestContext.type === "media") {
        // Keep the response metadata even when the response is NOT media.
        // Facebook can expose image/HTML/error resources through paths that
        // look like progressive video URLs. The explicit player-button path
        // uses this metadata to reject those false positives instead of
        // turning a tiny error/thumbnail response into a "video" download.
        rememberRecentPlayerMediaContext(url, Object.assign({}, requestContext, {
          response_content_type: mime,
          response_content_length: responseHeader(details.responseHeaders, "content-length"),
          response_status: details.statusCode,
          response_kind: kind,
        }));
      }
      if (!kind) return;

      getTabUrl(details.tabId, details.documentUrl || details.initiator || "").then((tabUrl) => {
        const pageUrl = tabUrl || details.documentUrl || details.initiator || "";
        const fallbackReferrer = details.documentUrl || details.initiator || pageUrl;
        const referrer = headers.Referer || headers.referer || fallbackReferrer;
        const userAgent = headers["User-Agent"] || headers["user-agent"] || "";
        const frameFallback = details.documentUrl || details.initiator || pageUrl;

        // The queue deduplicates, so a media-heavy page cannot flood storage.
        if (webReportedUrls.has(url) || webCaptureCount >= MAX_WEB_CAPTURES) return;
        if (
          typeof MagnetoclipCategories !== "undefined" &&
          MagnetoclipCategories.isUiChromeUrl({ url: url })
        ) {
          return;
        }
        webReportedUrls.add(url);
        webCaptureCount += 1;

        getFrameUrl(details, frameFallback).then((frameUrl) => {
          const detectedType = kind === "hls" || kind === "dash" ? "video" : kind;
          const source = kind === "hls" || kind === "dash"
            ? "network_capture"
            : "network_media_capture";

          sendDiag({
            source: "generic_media_network_capture",
            media_kind: kind,
            mime_type: mime,
            status: details.statusCode,
            request_type: details.type,
            url: url,
            page_url: pageUrl,
            frame_url: frameUrl || frameFallback,
            has_headers: Object.keys(headers).length > 0,
            header_count: Object.keys(headers).length,
          });

          getCookiesFor(url).then((cookies) => {
            queueDetection({
              url: url,
              filename: dispositionName || guessFilename(url),
              referrer: referrer,
              page_url: pageUrl,
              frame_url: frameUrl || frameFallback,
              user_agent: userAgent,
              headers: headers,
              cookies: cookies,
              source: source,
              detected_type: detectedType,
              mime_type: mime,
              content_disposition: contentDisposition || "",
              request_context: String(details.type || ""),
            });
          });
        });
      });
    },
    { urls: ["http://*/*", "https://*/*"] },
    IS_FIREFOX ? ["responseHeaders"] : ["responseHeaders", "extraHeaders"]
  );

  const clearMediaRequestContext = (details) => {
    if (details && details.requestId != null) {
      manifestRequestContexts.delete(String(details.requestId));
    }
  };
  chrome.webRequest.onCompleted.addListener(clearMediaRequestContext, { urls: ["http://*/*", "https://*/*"] });
  chrome.webRequest.onErrorOccurred.addListener(clearMediaRequestContext, { urls: ["http://*/*", "https://*/*"] });
}

// Keep the extension's flags in sync with the MagnetoClip app's settings. The
// toggles live in the app's settings page and the extension can be asleep when
// they change, so poll the native host periodically instead of only reading
// settings when a new port is opened.
//
// Firefox MV3 event pages (background.scripts) unload the background when idle,
// killing setInterval timers. chrome.alarms survive page unloads and wake the
// event page when they fire, so use an alarm for the keep-alive + settings poll.
if (chrome.alarms) {
  chrome.alarms.create("settings_poll", { periodInMinutes: 0.25 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === SYNC_ALARM) {
      // Nothing to do: detections are only transferred when the user opens a
      // category, so there is nothing to retry in the background. The alarm is
      // left registered so an upgrade does not leave a stale one behind.
      return;
    }
    if (alarm.name !== "settings_poll") {
      return;
    }
    // Reuse the existing native connection. Firefox keeps its MV3 event page
    // alive while a connectNative() port is open, and onDisconnect() below
    // nulls nativePort when the host process exits, so this alarm simply
    // refreshes settings over whatever port is already established. We must
    // NOT null nativePort here: in Firefox every connectNative() spawns a
    // fresh MagnetoClip.exe --browser-host process, and force-reconnecting
    // every tick would kill the host and churn processes endlessly.
    try {
      const port = ensurePort();
      if (port) {
        port.postMessage({ type: "settings" });
      }
    } catch (error) {
      nativePort = null;
    }
  });
} else {
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
}

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "").toLowerCase();
  } catch (error) {
    return "";
  }
}

// ---- Telegram stream redirects ----

// Cache of Telegram proxy URL -> resolved CDN target, learned from
// onBeforeRedirect below. Lets the context-menu and paste flows resolve a proxy
// link to the real CDN file even when the content-script probe fails (page
// scripts cannot read cross-origin redirect targets without CORS).
const TELEGRAM_REDIRECT_CACHE_TTL_MS = 60 * 60 * 1000;
const telegramRedirectCache = new Map();

// Temporary tabs opened purely to let a Telegram proxy URL redirect to its CDN
// target: top-level navigation carries the full page session that page-level
// fetch() is denied, so Telegram serves the redirect that onBeforeRedirect
// observes. Captures from these tabs are suppressed so the user is never
// prompted about the resolution attempt itself.
const TEMP_TAB_WAIT_MS = 8000;
const resolveTabIds = new Set();

function openResolutionTab(url) {
  return new Promise((resolve) => {
    chrome.tabs.create({ url: url, active: false }, (tab) => {
      if (chrome.runtime.lastError || !tab || tab.id == null) {
        resolve(null);
        return;
      }
      resolveTabIds.add(tab.id);
      resolve(tab);
    });
  });
}

function closeResolutionTab(tab) {
  if (!tab || tab.id == null) {
    return;
  }
  resolveTabIds.delete(tab.id);
  chrome.tabs.remove(tab.id, () => {
    /* best-effort: the tab may already be gone */
  });
}

function isTelegramMediaProxyUrl(url) {
  if (!/^https?:/i.test(url || "")) {
    return false;
  }
  let host = "";
  try {
    host = new URL(url).hostname;
  } catch (error) {
    return false;
  }
  if (!host.endsWith("telegram.org")) {
    return false;
  }
  const path = String(url).split(/[?#]/)[0];
  // Web K proxies stream /k/stream/, Web A /a/stream/ and /a/progressive/,
  // plus same-origin /api/files/ parts.
  return (
    path.includes("/k/stream/") ||
    path.includes("/stream/") ||
    path.includes("/progressive/") ||
    path.includes("/api/files/")
  );
}

function findTelegramTab() {
  return new Promise((resolve) => {
    chrome.tabs.query({}, (tabs) => {
      for (const tab of tabs || []) {
        if (tab.id == null || !tab.url) {
          continue;
        }
        try {
          if (new URL(tab.url).hostname === "web.telegram.org") {
            resolve(tab);
            return;
          }
        } catch (error) {
          /* keep scanning */
        }
      }
      resolve(null);
    });
  });
}

// Resolve a Telegram proxy URL to the direct CDN file, using the redirect
// cache first and the content-script probe (which can see the final same-origin
// hop) as a fallback.
async function resolveTelegramProxyUrl(url) {
  const cached = telegramRedirectCache.get(url);
  if (cached && Date.now() - cached.ts < TELEGRAM_REDIRECT_CACHE_TTL_MS) {
    return cached;
  }
  const tab = await findTelegramTab();
  if (tab) {
    const response = await chrome.tabs
      .sendMessage(tab.id, { type: "probe_redirect", url: url })
      .catch(() => null);
    if (response && response.ok && response.resolved_url) {
      const entry = {
        target: String(response.resolved_url),
        referrer: tab.url || "",
        ts: Date.now(),
      };
      telegramRedirectCache.set(url, entry);
      return entry;
    }
  }
  // Last resort: navigate a short-lived tab to the proxy URL so Telegram serves
  // the redirect that page-level fetch() is denied, and wait for
  // onBeforeRedirect to record the CDN target in the cache.
  const resolveTab = await openResolutionTab(url);
  if (!resolveTab) {
    return null;
  }
  const deadline = Date.now() + TEMP_TAB_WAIT_MS;
  while (Date.now() < deadline) {
    const fresh = telegramRedirectCache.get(url);
    if (fresh && fresh.ts >= Date.now() - 4000) {
      closeResolutionTab(resolveTab);
      return fresh;
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 250));
  }
  closeResolutionTab(resolveTab);
  return null;
}

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
      if (!sourceHost.endsWith("telegram.org")) {
        return;
      }
      // Web K proxies media through /k/stream/, Web A through /a/stream/ and
      // /a/progressive/ plus same-origin /api/files/ parts; all resolve to real
      // CDN files.
      if (
        !source.includes("/k/stream/") &&
        !source.includes("/stream/") &&
        !source.includes("/progressive/") &&
        !source.includes("/api/files/")
      ) {
        return;
      }
      // Remember source -> CDN target even when the capture is deduplicated or
      // rate-limited, so the paste and context-menu flows can still resolve it.
      telegramRedirectCache.set(source, {
        target: target,
        referrer: details.initiator || details.originUrl || "",
        ts: Date.now(),
      });
      // Redirects caused by our own temporary resolution tabs must not double
      // as page-scan captures (they are not user-visible page activity).
      if (resolveTabIds.has(details.tabId)) {
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
      getCookiesFor(target).then((cookies) => {
        queueDetection({
          url: target,
          filename: guessFilename(target),
          referrer: details.initiator || details.originUrl || "",
          page_url: details.originUrl || details.initiator || "",
          source: "telegram",
          detected_type: detectedType,
          cookies: cookies,
        });
      });
    },
    { urls: ["http://*/*", "https://*/*"] }
  );
}
