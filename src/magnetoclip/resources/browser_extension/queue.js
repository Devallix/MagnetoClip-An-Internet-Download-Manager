// queue.js — the active-page, deduplicated "detected" queue.
//
// Source of truth: ``storage.local`` so the service worker can restart without
// losing the current page's detections. Navigation, reload, tab switching and
// popup Refresh deliberately start a new scope and clear the previous page.
// Responsibilities:
//
//   * accept detections from every detector and classify them centrally
//   * deduplicate safely (URL-based, preserving signed query strings)
//   * keep storage bounded (entry cap + byte budget, oldest trimmed first)
//   * keep a pending/synced flag per entry so retries are idempotent
//   * expose per-category counts and drive the browser-action badge
//   * hand batches to MagnetoClip through an injected transport
//
// The module is transport-agnostic on purpose: the background script injects
// ``onSync``/``onChange``/``schedule`` adapters, which keeps this file loadable
// (and unit-testable) outside a service worker.

const MagnetoclipQueue = (function () {
  "use strict";

  const STORAGE_KEY = "mc_detections_v1";
  const STATE_VERSION = 1;
  const MAX_ENTRIES = 1000;
  const SYNC_BATCH_SIZE = 200;
  const SYNC_DEBOUNCE_MS = 400;
  const SYNC_ALARM_NAME = "magneto-detections-sync";
  const SYNC_RETRY_MINUTES = 1;
  // Leave headroom under the 5 MB local-storage quota for the rest of the
  // extension's settings.
  const MAX_BYTES = 3.5 * 1024 * 1024;
  const BADGE_MAX = 99;
  // Native messaging rejects a single message above roughly 1 MB, and one
  // oversized blob must not block the rest of the batch, so inline bytes are
  // only shipped up to this many base64 characters (~380 KB of data). Larger
  // blob:/data: payloads stay in the queue for the badge but are reported
  // without bytes, exactly like an unreadable blob: the app then only offers
  // them through the explicit flows (context menu, pasted blob URL).
  const MAX_INLINE_BASE64 = 512 * 1024;

  // --- injected adapters ----------------------------------------------------

  const categories =
    (typeof globalThis !== "undefined" && globalThis.MagnetoclipCategories) || null;

  const options = {
    storageKey: STORAGE_KEY,
    maxEntries: MAX_ENTRIES,
    batchSize: SYNC_BATCH_SIZE,
    maxBytes: MAX_BYTES,
    storage: null,
    onSync: null,
    onChange: null,
    schedule: null,
    now: function () {
      return Date.now();
    },
  };

  // --- in-memory state ------------------------------------------------------

  let state = null; // { version, entries: [...] }
  let loading = null; // promise of the in-flight load
  let writeChain = Promise.resolve(); // serialises read-modify-write cycles
let syncTimer = null;
let syncing = false;
let syncAgain = false;
// Category a deferred retry belongs to; "" means "everything".
let retryCategory = "";

  function requireCategories() {
    if (!categories) {
      throw new Error("categories.js must be loaded before queue.js");
    }
    return categories;
  }

  function configure(overrides) {
    const next = overrides || {};
    Object.keys(next).forEach(function (key) {
      if (key in options) {
        options[key] = next[key];
      }
    });
    // Re-read from storage when the storage backend changes.
    if (next.storage) {
      state = null;
      loading = null;
    }
    return api;
  }

  function storage() {
    if (options.storage) {
      return options.storage;
    }
    const compat = typeof globalThis !== "undefined" ? globalThis.MagnetoclipCompat : null;
    return compat ? compat.storage : null;
  }

  // --- storage access -------------------------------------------------------

  function emptyState() {
    return {
      version: STATE_VERSION,
      pageUrl: "",
      tabId: null,
      pageGeneration: null,
      entries: [],
    };
  }

  function sanitizeState(raw) {
    if (!raw || typeof raw !== "object") {
      return emptyState();
    }
    const value = raw[options.storageKey];
    const data = value && typeof value === "object" ? value : raw;
    if (!data || !Array.isArray(data.entries)) {
      return emptyState();
    }
    const entries = data.entries.filter(function (entry) {
      return entry && typeof entry === "object" && typeof entry.url === "string" && entry.url;
    });
    return {
      version: STATE_VERSION,
      pageUrl: typeof data.pageUrl === "string" ? data.pageUrl : "",
      tabId: data.tabId == null ? null : data.tabId,
      pageGeneration: data.pageGeneration == null ? null : data.pageGeneration,
      entries: entries,
    };
  }

  function ready() {
    if (state) {
      return Promise.resolve(state);
    }
    if (loading) {
      return loading;
    }
    const backend = storage();
    if (!backend) {
      state = emptyState();
      return Promise.resolve(state);
    }
    loading = Promise.resolve(backend.get(options.storageKey))
      .then(function (raw) {
        state = sanitizeState(raw);
        return state;
      })
      .catch(function () {
        state = emptyState();
        return state;
      })
      .then(function (result) {
        loading = null;
        return result;
      });
    return loading;
  }

  function persist() {
    const backend = storage();
    if (!backend) {
      return Promise.resolve();
    }
    return Promise.resolve(backend.set(makeRecord())).catch(function () {
      return undefined;
    });
  }

  function makeRecord() {
    const payload = {
      version: STATE_VERSION,
      pageUrl: state.pageUrl || "",
      tabId: state.tabId == null ? null : state.tabId,
      pageGeneration: state.pageGeneration == null ? null : state.pageGeneration,
      entries: state.entries,
    };
    const wrapped = {};
    wrapped[options.storageKey] = payload;
    return wrapped;
  }

  // Serialise every mutation so concurrent detectors cannot clobber each other.
  function mutate(task) {
    const run = function () {
      return ready().then(task);
    };
    writeChain = writeChain.then(run, run);
    return writeChain;
  }

  // --- entries --------------------------------------------------------------

  // Page/frame context remains part of detection identity. The visible queue
  // is nevertheless scoped to one active tab/document generation; old-page
  // entries are cleared rather than retained as selectable history.
  function normalizedPage(url) {
    const value = stripFragment(url || "");
    return value ? requireCategories().normalizeUrl(value) : "";
  }

  // The popup queue represents exactly one active tab/document generation.
  // Late reports from a previous navigation or a background tab are ignored.
  function belongsToActivePage(entry) {
    if (state.tabId != null) {
      if (entry.tab_id == null || String(entry.tab_id) !== String(state.tabId)) {
        return false;
      }
    }
    if (state.pageGeneration != null) {
      if (entry.page_generation == null ||
          String(entry.page_generation) !== String(state.pageGeneration)) {
        return false;
      }
    }
    const expectedPage = normalizedPage(state.pageUrl || "");
    const actualPage = normalizedPage(entry.scope_page_url || entry.page_url || "");
    if (expectedPage && actualPage && expectedPage !== actualPage) {
      return false;
    }
    return true;
  }

  function claimPage(built) {
    // Compatibility for a first detection that arrives before the background
    // has restored its active-page scope. Normal paths call resetForPage first.
    if (!state.pageUrl && built.page_url) state.pageUrl = String(built.page_url);
    if (state.tabId == null && built.tab_id != null) state.tabId = built.tab_id;
    if (state.pageGeneration == null && built.page_generation != null) {
      state.pageGeneration = built.page_generation;
    }
  }

  function stripFragment(url) {
    return String(url || "").split("#")[0];
  }

  function dedupeKey(entry) {
    const normalize = requireCategories().normalizeUrl;
    const url = normalize(entry.url);
    const page = normalize(stripFragment(entry.page_url || ""));
    const frame = normalize(stripFragment(entry.frame_url || entry.referrer || ""));
    const tab = entry.tab_id == null ? "" : String(entry.tab_id);
    return [url, page, frame, tab].join("\u001f");
  }

  function mergeInto(existing, incoming) {
    // Preserve the original detection identity/timestamp, but refresh session
    // credentials and frame context on a repeat capture. Signed media often
    // reuses its path while cookies/tokens rotate after a reload.
    const merged = Object.assign({}, existing);
    const refreshFields = new Set([
      "cookies", "headers", "user_agent", "referrer", "frame_url",
      "page_url", "scope_page_url", "tab_id", "frame_id", "page_generation",
      "mime_type", "request_context", "filename", "page_title", "last_seen",
    ]);
    Object.keys(incoming).forEach(function (key) {
      const value = incoming[key];
      if (value === undefined || value === null || value === "") return;
      if (refreshFields.has(key) || merged[key] === undefined || merged[key] === null || merged[key] === "") {
        merged[key] = value;
      }
    });
    // A fresh capture for this tab/page makes the entry eligible for the
    // active player again. The previous detection remains in the queue even
    // when a navigation had marked it stale.
    merged.stale_for_player = false;
    delete merged.stale_since;
    const generationChanged = Boolean(existing.stale_for_player) || (
      existing.page_generation != null &&
      incoming.page_generation != null &&
      String(existing.page_generation) !== String(incoming.page_generation)
    );
    merged.download_state = generationChanged ? "" : (incoming.download_state || existing.download_state || "");
    merged.download_state_message = generationChanged ? "" : (existing.download_state_message || "");
    merged.download_id = generationChanged ? 0 : (existing.download_id || 0);
    merged.download_requested_at = generationChanged ? 0 : (incoming.download_requested_at || existing.download_requested_at || 0);
    merged.synced = existing.synced ? 1 : incoming.synced ? 1 : 0;
    return merged;
  }

  function approximateBytes(entries) {
    try {
      return JSON.stringify(entries).length * 2;
    } catch (error) {
      return 0;
    }
  }

  // Oldest first, so a long session never evicts something the user just found.
  function trim() {
    if (state.entries.length > options.maxEntries) {
      state.entries = state.entries.slice(state.entries.length - options.maxEntries);
    }
    while (
      state.entries.length > 1 &&
      approximateBytes(state.entries) > options.maxBytes
    ) {
      state.entries.shift();
    }
  }

  // Copy of an entry for the wire: inline bytes are dropped when too large to
  // travel in one native message, so the batch size stays bounded and one big
  // blob cannot make the whole sync fail forever.
  function forSync(entry) {
    const copy = Object.assign({}, entry);
    const data = copy.data_base64;
    if (typeof data === "string" && data.length > MAX_INLINE_BASE64) {
      delete copy.data_base64;
      copy.data_omitted = 1;
    }
    return copy;
  }

  function notifyChange() {
    if (typeof options.onChange !== "function") {
      return;
    }
    try {
      options.onChange(api.counts(), state.entries.length);
    } catch (error) {
      // A badge failure must never break detection, but it must be visible in
      // the service-worker console.
      if (typeof console !== "undefined" && console.warn) {
        console.warn("[MagnetoClip] detection queue change handler failed", error);
      }
    }
  }

  function scheduleSync(delay, category) {
    if (typeof options.schedule === "function") {
      options.schedule(delay === undefined ? SYNC_DEBOUNCE_MS : delay, category || "");
      return;
    }
    if (syncTimer) {
      clearTimeout(syncTimer);
    }
    syncTimer = setTimeout(function () {
      syncTimer = null;
      api.sync(category);
    }, delay === undefined ? SYNC_DEBOUNCE_MS : delay);
  }

  // --- public API -----------------------------------------------------------

  const api = {
    STORAGE_KEY: STORAGE_KEY,
    SYNC_BATCH_SIZE: SYNC_BATCH_SIZE,
    MAX_ENTRIES: MAX_ENTRIES,
    MAX_INLINE_BASE64: MAX_INLINE_BASE64,
    SYNC_ALARM_NAME: SYNC_ALARM_NAME,
    SYNC_RETRY_MINUTES: SYNC_RETRY_MINUTES,

    configure: configure,
    ready: ready,

    /** Current active-page entries, oldest first. */
    all: function () {
      return ready().then(function () {
        return state.entries.slice();
      });
    },

    /** Entries MagnetoClip has not acknowledged yet. */
    pending: function () {
      return ready().then(function () {
        return state.entries.filter(function (entry) {
          return !entry.synced;
        });
      });
    },

    counts: function () {
      return requireCategories().countsByCategory(state ? state.entries : []);
    },

    /**
     * Add one raw detection. Returns the stored entry, or null when it is a
     * duplicate of something already queued.
     */
    add: function (detection) {
      return mutate(function () {
        const built = requireCategories().buildDetection(detection, options.now());
        if (detection && detection.scope_page_url) built.scope_page_url = String(detection.scope_page_url);
        claimPage(built);
        if (!belongsToActivePage(built)) return null;
        const key = dedupeKey(built);
        for (let index = 0; index < state.entries.length; index += 1) {
          if (dedupeKey(state.entries[index]) === key) {
            state.entries[index] = mergeInto(state.entries[index], built);
            return persist().then(function () {
              notifyChange();
              // Deliberately no sync here: nothing reaches MagnetoClip until
              // the user opens a category.
              return null;
            });
          }
        }
        built.synced = 0;
        state.entries.push(built);
        trim();
        return persist().then(function () {
          notifyChange();
          return built;
        });
      });
    },

    /** Add many detections, classifying and deduplicating each one. */
    addMany: function (detections) {
      const list = Array.isArray(detections) ? detections : [detections];
      if (!list.length) {
        return Promise.resolve([]);
      }
      return mutate(function () {
        const now = options.now();
        const added = [];
        const index = new Map();
        state.entries.forEach(function (entry, position) {
          index.set(dedupeKey(entry), position);
        });
        for (const detection of list) {
          if (!detection) {
            continue;
          }
          const built = requireCategories().buildDetection(detection, now);
          if (detection && detection.scope_page_url) built.scope_page_url = String(detection.scope_page_url);
          claimPage(built);
          if (!belongsToActivePage(built)) continue;
          const key = dedupeKey(built);
          if (index.has(key)) {
            const position = index.get(key);
            state.entries[position] = mergeInto(state.entries[position], built);
            continue;
          }
          built.synced = 0;
          state.entries.push(built);
          index.set(key, state.entries.length - 1);
          added.push(built);
        }
        trim();
        return persist().then(function () {
          notifyChange();
          return added;
        });
      });
    },

    remove: function (id) {
      return api.removeMany([id]);
    },

    removeMany: function (ids) {
      const wanted = new Set((ids || []).map(String));
      if (!wanted.size) {
        return Promise.resolve(0);
      }
      return mutate(function () {
        const before = state.entries.length;
        state.entries = state.entries.filter(function (entry) {
          return !wanted.has(String(entry.id));
        });
        const removed = before - state.entries.length;
        if (!removed) {
          return 0;
        }
        return persist().then(function () {
          notifyChange();
          return removed;
        });
      });
    },

    /** Compatibility helper for callers that want to suppress a tab's old entries. */
    markTabStale: function (tabId, pageUrl) {
      if (tabId == null) return Promise.resolve(0);
      const wanted = String(tabId);
      const pageKey = pageUrl
        ? requireCategories().normalizeUrl(stripFragment(pageUrl))
        : "";
      return mutate(function () {
        let changed = 0;
        const now = options.now();
        state.entries.forEach(function (entry) {
          const belongsToTab = entry.tab_id != null && String(entry.tab_id) === wanted;
          // Older v1.10.8 queue entries did not yet have tab IDs. On the first
          // navigation/rescan after upgrading, safely stale only those legacy
          // entries whose saved page URL matches this tab.
          const legacyPageMatch = entry.tab_id == null && pageKey &&
            requireCategories().normalizeUrl(stripFragment(entry.page_url || "")) === pageKey;
          if ((belongsToTab || legacyPageMatch) && !entry.stale_for_player) {
            entry.stale_for_player = true;
            entry.stale_since = now;
            changed += 1;
          }
        });
        if (!changed) return 0;
        return persist().then(function () {
          notifyChange();
          return changed;
        });
      });
    },

    /** Update the player-button hand-off lifecycle without deleting a capture. */
    setDownloadState: function (id, downloadState, details) {
      const wanted = String(id || "");
      if (!wanted) return Promise.resolve(false);
      const meta = details && typeof details === "object" ? details : {};
      return mutate(function () {
        const entry = state.entries.find(function (item) { return String(item.id) === wanted; });
        if (!entry) return false;
        entry.download_state = String(downloadState || "");
        entry.download_state_message = String(meta.message || "");
        entry.download_id = Number(meta.download_id || 0) || 0;
        entry.download_requested_at = Number(meta.timestamp || options.now());
        return persist().then(function () {
          notifyChange();
          return true;
        });
      });
    },

    /**
     * Drop everything (optionally only one category). Used by the "clear"
     * action and by tests; MagnetoClip's own removals arrive via ``handled``.
     */
    clear: function (category) {
      return mutate(function () {
        if (category && requireCategories().isCategory(category)) {
          state.entries = state.entries.filter(function (entry) {
            return entry.category !== category;
          });
        } else if (category === "synced") {
          state.entries = state.entries.filter(function (entry) {
            return !entry.synced;
          });
        } else {
          state.entries = [];
        }
        return persist().then(function () {
          notifyChange();
        });
      });
    },

    /**
     * Start a fresh detection scope for one tab/document generation.
     * Navigation, reload, tab activation and popup Refresh deliberately clear
     * every category so a player can never select a previous page's media.
     */
    resetForPage: function (pageUrl, scopeOptions) {
      const next = typeof pageUrl === "string" ? pageUrl : "";
      const meta = scopeOptions && typeof scopeOptions === "object" ? scopeOptions : {};
      return mutate(function () {
        const nextTabId = meta.tabId == null ? state.tabId : meta.tabId;
        const nextGeneration = meta.pageGeneration == null
          ? state.pageGeneration
          : meta.pageGeneration;
        const changedPage = Boolean(next && normalizedPage(next) !== normalizedPage(state.pageUrl || ""));
        const changedTab = nextTabId != null && String(nextTabId) !== String(state.tabId);
        const changedGeneration = nextGeneration != null &&
          String(nextGeneration) !== String(state.pageGeneration);
        const shouldClear = Boolean(meta.force || changedPage || changedTab || changedGeneration);
        if (shouldClear) state.entries = [];
        if (next) state.pageUrl = next;
        if (nextTabId != null) state.tabId = nextTabId;
        if (nextGeneration != null) state.pageGeneration = nextGeneration;
        return persist().then(function () {
          if (shouldClear) notifyChange();
          return shouldClear;
        });
      });
    },

    /** The active page/document scope currently represented by the queue. */
    scope: function () {
      return ready().then(function () {
        return {
          pageUrl: state.pageUrl || "",
          tabId: state.tabId == null ? null : state.tabId,
          pageGeneration: state.pageGeneration == null ? null : state.pageGeneration,
        };
      });
    },

    /** The page the queue currently belongs to. */
    pageUrl: function () {
      return ready().then(function () {
        return state.pageUrl || "";
      });
    },

    /**
     * Forget detections MagnetoClip has accepted so its badge clears on the
     * Open click. A successful sync means the app stored the batch, so nothing
     * is kept for retry; stale ``synced`` copies from older pushes of the same
     * scope are dropped along with it, or the badge would keep counting files
     * the app already stores.
     */
    dropDelivered: function (ids, category) {
      const idSet = new Set((ids || []).map(String));
      const scope = category ? String(category) : "";
      return mutate(function () {
        const before = state.entries.length;
        state.entries = state.entries.filter(function (entry) {
          if (idSet.has(String(entry.id))) {
            return false;
          }
          if (entry.synced && (!scope || entry.category === scope)) {
            return false;
          }
          return true;
        });
        const removed = before - state.entries.length;
        if (!removed) {
          return 0;
        }
        return persist().then(function () {
          notifyChange();
          return removed;
        });
      });
    },

    /**
     * MagnetoClip confirmed these detections (imported into the app, or
     * deleted there): drop the local copies and shrink the badge.
     */
    handled: function (payload) {
      const data = payload || {};
      const ids = (data.ids || []).map(String);
      const urls = (data.urls || []).map(function (url) {
        return requireCategories().normalizeUrl(url);
      });
      if (!ids.length && !urls.length) {
        return Promise.resolve(0);
      }
      const idSet = new Set(ids);
      const urlSet = new Set(urls);
      return mutate(function () {
        const before = state.entries.length;
        state.entries = state.entries.filter(function (entry) {
          if (idSet.size) return !idSet.has(String(entry.id));
          const normalizedUrl = requireCategories().normalizeUrl(entry.url);
          return !urlSet.has(normalizedUrl);
        });
        const removed = before - state.entries.length;
        if (!removed) {
          return 0;
        }
        return persist().then(function () {
          notifyChange();
          return removed;
        });
      });
    },

    /**
     * Push pending entries to MagnetoClip in bounded batches. A transport that
     * returns false (app closed) leaves the entries pending so the next alarm,
     * startup or detection retries them.
     */
    sync: function (category) {
      if (typeof options.onSync !== "function") {
        return Promise.resolve({ sent: 0, failed: 0 });
      }
      const wanted = category && requireCategories().isCategory(category)
        ? String(category)
        : "";
      if (syncing) {
        // Remember a broader request if one arrives mid-flight: a category
        // retry must not widen, but a full flush must not narrow either.
        if (!wanted || (retryCategory && !retryCategory)) {
          retryCategory = wanted;
        }
        syncAgain = true;
        return Promise.resolve({ sent: 0, failed: 0 });
      }
      syncing = true;
      return ready()
        .then(function () {
          const batch = state.entries
            .filter(function (entry) {
              if (entry.synced) {
                return false;
              }
              // A category Open hands over only what the user asked to see.
              return !wanted || entry.category === wanted;
            })
            .slice(0, options.batchSize);
          if (!batch.length) {
            return { sent: 0, failed: 0 };
          }
          return Promise.resolve(
            options.onSync(batch.map(forSync))
          ).then(function (accepted) {
            if (accepted) {
              return api
                .dropDelivered(
                  batch.map(function (entry) {
                    return entry.id;
                  }),
                  wanted
                )
                .then(function () {
                  return { sent: batch.length, failed: 0 };
                });
            }
            return { sent: 0, failed: batch.length };
          });
        })
        .catch(function () {
          return { sent: 0, failed: 0 };
        })
        .then(function (result) {
          syncing = false;
          const retryFor = retryCategory;
          retryCategory = "";
          if (syncAgain) {
            syncAgain = false;
            // A retry must stay scoped to the category that asked for it.
            scheduleSync(0, retryFor);
          } else if (result.failed) {
            scheduleSync(5000, wanted);
          }
          return result;
        });
    },

    /**
     * Hand one category to MagnetoClip. This is the only path that moves
     * detections into the app, and it runs when the user clicks Open.
     */
    syncCategory: function (category) {
      return api.sync(category);
    },

    /** Flush now, ignoring the debounce (startup, alarm, popup click). */
    flush: function (category) {
      if (syncTimer) {
        clearTimeout(syncTimer);
        syncTimer = null;
      }
      return api.sync(category);
    },

    /** Badge text for the current queue size. */
    badgeText: function () {
      if (!state) {
        return "";
      }
      const total = state.entries.length;
      if (!total) {
        return "";
      }
      return total > BADGE_MAX ? BADGE_MAX + "+" : String(total);
    },

    stats: function () {
      return ready().then(function () {
        return {
          total: state.entries.length,
          pending: state.entries.filter(function (entry) {
            return !entry.synced;
          }).length,
          counts: api.counts(),
          bytes: approximateBytes(state.entries),
        };
      });
    },
  };

  return api;
})();

if (typeof globalThis !== "undefined") {
  globalThis.MagnetoclipQueue = MagnetoclipQueue;
}
