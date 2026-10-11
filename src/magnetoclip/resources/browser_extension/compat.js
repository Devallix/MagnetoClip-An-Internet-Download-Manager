// compat.js — the single cross-browser (WebExtensions) compatibility layer.
//
// Firefox exposes a promise-based ``browser.*`` namespace, Chromium/Edge/Brave/
// Opera/Vivaldi expose the callback-based ``chrome.*`` namespace. The rest of
// the extension must never test for a browser itself, so every API this
// extension touches is resolved here once:
//
//   * namespace selection (``browser.*`` when available, otherwise ``chrome.*``)
//   * promise/callback difference handling (``call`` never passes a callback to
//     a promise-only namespace, because Firefox rejects surplus arguments)
//   * ``action`` vs the legacy ``browserAction`` browser action
//   * ``lastError`` swallowing (reading it is what clears it in Chromium)
//
// The file is a plain script (no ESM) so it can be loaded three ways:
// ``importScripts`` from a Chromium service worker, the ``background.scripts``
// array of the Firefox manifest, and ``content_scripts.js`` in a page.

const MagnetoclipCompat = (function () {
  "use strict";

  // ``browser`` only exists in Gecko. Chrome-family browsers leave it undefined
  // and expose the legacy callback namespace as ``chrome`` (which Firefox also
  // provides as a callback-style alias).
  const PROMISE_NAMESPACE =
    typeof browser !== "undefined" && browser && browser.runtime ? browser : null;
  const CALLBACK_NAMESPACE =
    typeof chrome !== "undefined" && chrome && chrome.runtime ? chrome : null;
  const api = PROMISE_NAMESPACE || CALLBACK_NAMESPACE;

  const isFirefox = !!PROMISE_NAMESPACE;

  // ---- lastError -----------------------------------------------------------

  // Chromium only clears ``runtime.lastError`` when it is read inside the
  // callback that owns it, so every callback wrapper reads it unconditionally.
  function lastError() {
    const runtime = api && api.runtime;
    if (!runtime) {
      return null;
    }
    try {
      return runtime.lastError || null;
    } catch (error) {
      return null;
    }
  }

  // ---- promise/callback bridge --------------------------------------------

  // Call a WebExtensions method as a promise regardless of the engine's style.
  // ``fn`` is invoked with ``thisArg``; when the namespace is promise-only no
  // callback is appended (Firefox's argument validation would reject it).
  function call(fn, thisArg, args) {
    if (typeof fn !== "function") {
      return Promise.resolve(undefined);
    }
    const params = Array.isArray(args) ? args : [];
    if (PROMISE_NAMESPACE) {
      try {
        return Promise.resolve(fn.apply(thisArg, params));
      } catch (error) {
        return Promise.reject(error);
      }
    }
    return new Promise(function (resolve) {
      let settled = false;
      const done = function (value) {
        if (settled) {
          return;
        }
        settled = true;
        resolve(value);
      };
      try {
        const returned = fn.apply(
          thisArg,
          params.concat([
            function (value) {
              lastError();
              done(value);
            },
          ])
        );
        // Chromium MV3 APIs return a promise when no callback is given; a few
        // do both. Prefer the promise when one comes back.
        if (returned && typeof returned.then === "function") {
          returned.then(done, function () {
            done(undefined);
          });
        } else if (fn.length <= params.length) {
          // Callback-less signature that produced a plain value.
          done(returned);
        }
      } catch (error) {
        done(undefined);
      }
    });
  }

  // ---- namespaces ----------------------------------------------------------

  function namespace(name) {
    if (!api) {
      return null;
    }
    return api[name] || null;
  }

  // MV3 renamed ``browserAction`` to ``action``; Firefox 115 (our declared
  // minimum) only knows the legacy name, and the manifest is rewritten per
  // engine by the installer, so the namespace is resolved defensively.
  function actionNamespace() {
    if (!api) {
      return null;
    }
    return api.action || api.browserAction || null;
  }

  // ---- storage -------------------------------------------------------------

  const storage = {
    get: function (keys) {
      const area = namespace("storage") && namespace("storage").local;
      if (!area) {
        return Promise.resolve({});
      }
      return call(area.get, area, [keys]).then(function (value) {
        return value || {};
      });
    },
    set: function (items) {
      const area = namespace("storage") && namespace("storage").local;
      if (!area) {
        return Promise.resolve();
      }
      return call(area.set, area, [items]);
    },
    remove: function (keys) {
      const area = namespace("storage") && namespace("storage").local;
      if (!area) {
        return Promise.resolve();
      }
      return call(area.remove, area, [keys]);
    },
  };

  // ---- browser action badge ------------------------------------------------

  const BADGE_COLOR = "#8b5cf6";

  function setBadge(text, color) {
    const action = actionNamespace();
    if (!action || !action.setBadgeText) {
      return Promise.resolve();
    }
    return call(action.setBadgeText, action, [{ text: String(text || "") }]).then(
      function () {
        if (String(text || "") && action.setBadgeBackgroundColor) {
          return call(action.setBadgeBackgroundColor, action, [
            { color: color || BADGE_COLOR },
          ]);
        }
        return undefined;
      }
    );
  }

  function setBadgeTitle(title) {
    const action = actionNamespace();
    if (!action || !action.setTitle) {
      return Promise.resolve();
    }
    return call(action.setTitle, action, [{ title: String(title || "") }]);
  }

  // ---- notifications -------------------------------------------------------

  function notify(title, message, iconUrl) {
    const notifications = namespace("notifications");
    if (!notifications || !notifications.create) {
      return Promise.resolve();
    }
    return call(notifications.create, notifications, [
      {
        type: "basic",
        iconUrl: iconUrl || "icons/logo-128.png",
        title: String(title || "MagnetoClip"),
        message: String(message || ""),
      },
    ]);
  }

  // ---- tabs ----------------------------------------------------------------

  const tabs = {
    get: function (tabId) {
      const target = namespace("tabs");
      return call(target && target.get, target, [tabId]);
    },
    query: function (query) {
      const target = namespace("tabs");
      return call(target && target.query, target, [query || {}]).then(function (result) {
        return result || [];
      });
    },
    create: function (props) {
      const target = namespace("tabs");
      return call(target && target.create, target, [props]);
    },
    remove: function (tabId) {
      const target = namespace("tabs");
      return call(target && target.remove, target, [tabId]);
    },
    sendMessage: function (tabId, message, options) {
      const target = namespace("tabs");
      if (!target) {
        return Promise.resolve(undefined);
      }
      return call(target.sendMessage, target, [tabId, message, options]).catch(
        function () {
          return undefined;
        }
      );
    },
  };

  // ---- context menus -------------------------------------------------------

  const contextMenus = {
    removeAll: function () {
      const menus = namespace("contextMenus");
      if (!menus || !menus.removeAll) {
        return Promise.resolve();
      }
      return call(menus.removeAll, menus, []);
    },
    create: function (item) {
      const menus = namespace("contextMenus");
      if (!menus || !menus.create) {
        return undefined;
      }
      return menus.create(item);
    },
    onClicked: {
      addListener: function (listener) {
        const menus = namespace("contextMenus");
        if (menus && menus.onClicked) {
          menus.onClicked.addListener(listener);
        }
      },
    },
  };

  // ---- runtime -------------------------------------------------------------

  const runtime = {
    id: function () {
      return api && api.runtime ? api.runtime.id || "" : "";
    },
    getURL: function (relative) {
      return api && api.runtime && api.runtime.getURL
        ? api.runtime.getURL(relative)
        : String(relative || "");
    },
    connectNative: function (hostName) {
      if (!api || !api.runtime || !api.runtime.connectNative) {
        return null;
      }
      return api.runtime.connectNative(hostName);
    },
    sendMessage: function (message) {
      if (!api || !api.runtime) {
        return Promise.resolve(undefined);
      }
      return call(api.runtime.sendMessage, api.runtime, [message]).catch(function () {
        return undefined;
      });
    },
    onMessage: {
      addListener: function (listener) {
        if (api && api.runtime && api.runtime.onMessage) {
          api.runtime.onMessage.addListener(listener);
        }
      },
    },
    onInstalled: {
      addListener: function (listener) {
        if (api && api.runtime && api.runtime.onInstalled) {
          api.runtime.onInstalled.addListener(listener);
        }
      },
    },
    onStartup: {
      addListener: function (listener) {
        if (api && api.runtime && api.runtime.onStartup) {
          api.runtime.onStartup.addListener(listener);
        }
      },
    },
  };

  // ---- webRequest / webNavigation / downloads / alarms / cookies ------------
  // These are observational or callback-only in both engines; they are exposed
  // as passthroughs so the rest of the extension never names a global directly.
  const webRequest = namespace("webRequest");
  const webNavigation = namespace("webNavigation");
  const downloads = namespace("downloads");
  const alarms = namespace("alarms");
  const cookies = namespace("cookies");
  const scripting = namespace("scripting");

  return {
    api: api,
    isFirefox: isFirefox,
    promiseNamespace: PROMISE_NAMESPACE,
    call: call,
    lastError: lastError,
    namespace: namespace,
    action: actionNamespace,
    setBadge: setBadge,
    setBadgeTitle: setBadgeTitle,
    storage: storage,
    notifications: { create: notify },
    tabs: tabs,
    contextMenus: contextMenus,
    runtime: runtime,
    webRequest: webRequest,
    webNavigation: webNavigation,
    downloads: downloads,
    alarms: alarms,
    cookies: cookies,
    scripting: scripting,
  };
})();

if (typeof globalThis !== "undefined") {
  globalThis.MagnetoclipCompat = MagnetoclipCompat;
}
