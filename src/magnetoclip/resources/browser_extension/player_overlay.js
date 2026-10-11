// player_overlay.js — the "Download with MagnetoClip" button on video players.
//
// Runs as a content script alongside content.js. It watches the page for
// meaningful <video> players, associates each one with the best matching entry
// in the detection queue (by looking the player's media signals up in the
// background worker), and overlays a small button that turns a click into a
// ``download_detection`` request to MagnetoClip. The button is explicit user
// intent: it never guesses a URL on its own, it only follows a detection that
// already exists (or that the background re-creates from the player's own
// source when the queue entry was already delivered).
//
// The detection pipeline, the queue, the native-messaging protocol and the
// download engine are all reused unchanged; this module only adds the player
// discovery/association and the overlay UI.

(function () {
  "use strict";

  if (window.__magnetoclipPlayerOverlayLoaded) {
    return;
  }
  window.__magnetoclipPlayerOverlayLoaded = true;

  const CATEGORIES = typeof MagnetoclipCategories !== "undefined"
    ? MagnetoclipCategories
    : null;

  function categorize(value) {
    return CATEGORIES && typeof CATEGORIES.classify === "function"
      ? CATEGORIES.classify(value)
      : "";
  }

  function normalizeUrl(url) {
    if (CATEGORIES && typeof CATEGORIES.normalizeUrl === "function") {
      return CATEGORIES.normalizeUrl(url);
    }
    return String(url || "").split("#")[0];
  }

  function extensionOf(url) {
    const raw = String(url || "").split(/[?#]/)[0];
    const match = /\.([a-z0-9]{1,12})$/i.exec(raw);
    if (match) {
      return match[1].toLowerCase();
    }
    const format = /[?&](?:format|ext|f|type)=([a-z0-9]{1,12})/i.exec(String(url || ""));
    return format ? format[1].toLowerCase() : "";
  }

  // Verbatim query strings are kept for signed/tokenised CDN URLs; only exact
  // (with and without the trailing fragment) and extensionless-path matches
  // are ever considered good enough to associate a player with a detection.
  function stripQuery(url) {
    return String(url || "").split(/[?#]/)[0];
  }

  const VIDEO_EXTS = new Set(["mp4", "webm", "mkv", "mov", "avi", "m4v", "3gp", "mpg", "mpeg", "ogv", "wmv", "asf", "f4v", "flv"]);
  const AUDIO_EXTS = new Set(["mp3", "m4a", "wav", "flac", "ogg", "oga", "opus", "aac", "wma", "aiff", "mid", "midi"]);
  const STREAM_EXTS = new Set(["m3u8", "m3u", "mpd"]);
  const SEGMENT_EXTS = new Set(["ts", "mts", "m2ts", "m4s", "cmfv", "cmfa"]);

  function kindOfUrl(url) {
    const ext = extensionOf(url);
    if (STREAM_EXTS.has(ext)) {
      return ext === "mpd" ? "dash" : "hls";
    }
    if (VIDEO_EXTS.has(ext)) {
      return "video";
    }
    if (AUDIO_EXTS.has(ext)) {
      return "audio";
    }
    return "";
  }

  /// --- pure association logic (unit-tested) -------------------------------

  const MIN_PLAYER_WIDTH = 200;
  const MIN_PLAYER_HEIGHT = 100;
  const RESOLUTION_TTL_MS = 8000;

  // A candidate is a genuinely playable, sized player that exposes a source.
  // Tiny hidden players (ads, feed autoplay chrome) never qualify.
  function meaningfulPlayer(video, rect, now) {
    if (!video || !rect) {
      return false;
    }
    const width = Number(rect.width || 0);
    const height = Number(rect.height || 0);
    if (width < MIN_PLAYER_WIDTH || height < MIN_PLAYER_HEIGHT) {
      return false;
    }
    const layouts = video.getBoundingClientRect && video.getBoundingClientRect();
    if (layouts && (layouts.width === 0 || layouts.height === 0)) {
      return false;
    }
    if (video.hasAttribute && video.hasAttribute("hidden")) {
      return false;
    }
    return true;
  }

  // Media URLs a player exposes directly (currentSrc, src, <source> children).
  function playerUrls(video) {
    const urls = [];
    const direct = video && (video.currentSrc || video.getAttribute && video.getAttribute("src")) || "";
    if (direct) {
      urls.push(direct);
    }
    if (video && video.querySelectorAll) {
      for (const source of video.querySelectorAll("source[src]")) {
        const value = source.getAttribute("src") || "";
        if (value) {
          urls.push(value);
        }
      }
    }
    return urls;
  }

  function isBlobUrl(url) {
    return /^blob:/i.test(url);
  }

  function facebookReelId(url) {
    const value = String(url || "").trim();
    if (!value) {
      return "";
    }
    try {
      const parsed = new URL(value, window.location && window.location.href ? window.location.href : undefined);
      const host = String(parsed.hostname || "").toLowerCase();
      if (!(host === "facebook.com" || host.endsWith(".facebook.com") || host === "fb.watch" || host.endsWith(".fb.watch"))) {
        return "";
      }
      const match = String(parsed.pathname || "").match(/\/(?:reel|reels)\/(\d+)(?:\/|$)/i);
      return match ? match[1] : "";
    } catch (error) {
      return "";
    }
  }

  function isTikTokHostUrl(url) {
    try {
      const host = String(new URL(String(url || ""), window.location && window.location.href ? window.location.href : undefined).hostname || "").toLowerCase();
      return host === "tiktok.com" || host.endsWith(".tiktok.com");
    } catch (_) {
      return false;
    }
  }

  function isXHostUrl(url) {
    try {
      const host = String(new URL(String(url || ""), window.location && window.location.href ? window.location.href : undefined).hostname || "").toLowerCase();
      return host === "x.com" || host.endsWith(".x.com") || host === "twitter.com" || host.endsWith(".twitter.com");
    } catch (_) {
      return false;
    }
  }

  // Match an actual X/Twitter post permalink. The key is used only to find an
  // existing extension-queue entry; the download target is never synthesized.
  function xStatusKey(url) {
    try {
      const parsed = new URL(String(url || ""), window.location && window.location.href ? window.location.href : undefined);
      const host = String(parsed.hostname || "").toLowerCase();
      if (!(host === "x.com" || host.endsWith(".x.com") || host === "twitter.com" || host.endsWith(".twitter.com"))) {
        return "";
      }
      const match = String(parsed.pathname || "").match(/^\/([^/]+)\/status\/(\d+)(?:\/|$)/i);
      if (!match || /^i$/i.test(match[1])) {
        return "";
      }
      let username = match[1];
      try { username = decodeURIComponent(username); } catch (_) {}
      return username.toLowerCase() + ":" + match[2];
    } catch (_) {
      return "";
    }
  }

  function xStatusPermalinkHref(value, baseUrl) {
    try {
      const parsed = new URL(String(value || ""), String(baseUrl || (window.location && window.location.href) || ""));
      if (!xStatusKey(parsed.href)) {
        return "";
      }
      return parsed.href;
    } catch (_) {
      return "";
    }
  }

  // On X's /home feed, each autoplay preview belongs to a post. Find the
  // status permalink from that specific player's nearest card and use it only
  // as a queue lookup key. A CDN URL alone is never sufficient for the button.
  function xStatusPermalinkForPlayer(player, pageUrl, doc) {
    if (!isXHostUrl(pageUrl)) {
      return "";
    }
    const currentPagePermalink = xStatusPermalinkHref(pageUrl, pageUrl);
    if (currentPagePermalink && !isHomeOrFeedPage(pageUrl)) {
      return currentPagePermalink;
    }
    if (!player) {
      return "";
    }
    const documentRef = doc || player.ownerDocument || document;
    let node = player;
    for (let depth = 0; node && depth < 9; depth += 1, node = node.parentElement) {
      const candidates = new Map();
      function add(href) {
        const resolved = xStatusPermalinkHref(href, pageUrl);
        const key = xStatusKey(resolved);
        if (resolved && key && !candidates.has(key)) {
          candidates.set(key, resolved);
        }
      }
      try {
        if (node.matches && node.matches("a[href]")) {
          add(node.href || node.getAttribute("href") || "");
        }
        if (node.querySelectorAll) {
          const links = node.querySelectorAll('a[href*="/status/"]');
          for (let index = 0; index < links.length && index < 24; index += 1) {
            const link = links[index];
            add(link.href || link.getAttribute("href") || "");
          }
        }
      } catch (_) {}
      if (candidates.size === 1) {
        return candidates.values().next().value;
      }
      // Quote tweets and nested cards can contain multiple status URLs. Refuse
      // to guess rather than associate this player with another post.
      if (candidates.size > 1) {
        return "";
      }
    }
    return "";
  }

  function queuedXStatusEntry(entries, canonicalUrl) {
    const wantedKey = xStatusKey(canonicalUrl);
    if (!wantedKey || !Array.isArray(entries)) {
      return null;
    }
    return entries.find(function (candidate) {
      return xStatusKey(candidate && candidate.url) === wantedKey;
    }) || null;
  }

  // A TikTok video permalink is the stable player target. Return a key based on
  // creator + video id so www/non-www variants and tracking query parameters do
  // not affect association. The download target itself must still come from the
  // extension queue; this key is only used to locate that queued entry.
  function tiktokVideoKey(url) {
    try {
      const parsed = new URL(String(url || ""), window.location && window.location.href ? window.location.href : undefined);
      const host = String(parsed.hostname || "").toLowerCase();
      if (!(host === "tiktok.com" || host.endsWith(".tiktok.com"))) {
        return "";
      }
      const match = String(parsed.pathname || "").match(/^\/@([^/]+)\/video\/(\d+)(?:\/|$)/i);
      if (!match) {
        return "";
      }
      let username = match[1];
      try { username = decodeURIComponent(username); } catch (_) {}
      return username.toLowerCase() + ":" + match[2];
    } catch (_) {
      return "";
    }
  }

  function tiktokPermalinkHref(value, baseUrl) {
    try {
      const parsed = new URL(String(value || ""), String(baseUrl || (window.location && window.location.href) || ""));
      if (!tiktokVideoKey(parsed.href)) {
        return "";
      }
      return parsed.href;
    } catch (_) {
      return "";
    }
  }

  // On TikTok's For You/home feed the page URL is not the video target. Resolve
  // the permalink attached to the specific player's card, but never manufacture
  // a fallback URL: the resolver later requires this exact video to exist in
  // the extension queue before the button is shown or clicked.
  function tiktokVideoPermalinkForPlayer(player, pageUrl, doc) {
    if (!isTikTokHostUrl(pageUrl)) {
      return "";
    }
    const currentPagePermalink = tiktokPermalinkHref(pageUrl, pageUrl);
    if (!player) {
      return currentPagePermalink;
    }
    const documentRef = doc || player.ownerDocument || document;
    let node = player;
    for (let depth = 0; node && depth < 9; depth += 1, node = node.parentElement) {
      const candidates = new Map();
      function add(href) {
        const resolved = tiktokPermalinkHref(href, pageUrl);
        const key = tiktokVideoKey(resolved);
        if (resolved && key && !candidates.has(key)) {
          candidates.set(key, resolved);
        }
      }
      try {
        if (node.matches && node.matches("a[href]")) {
          add(node.href || node.getAttribute("href") || "");
        }
        if (node.querySelectorAll) {
          const links = node.querySelectorAll('a[href*="/video/"]');
          for (let index = 0; index < links.length && index < 20; index += 1) {
            const link = links[index];
            add(link.href || link.getAttribute("href") || "");
          }
        }
      } catch (_) {}
      if (candidates.size === 1) {
        return candidates.values().next().value;
      }
      // A wrapper containing multiple distinct video links is ambiguous. Do
      // not keep walking up to a larger container and accidentally borrow the
      // permalink belonging to another video card.
      if (candidates.size > 1) {
        return "";
      }
    }
    // On a dedicated TikTok video page, use the page permalink only for a player
    // that is not inside a recommendation/feed card. Otherwise a sidebar preview
    // could accidentally inherit the main video's target.
    if (currentPagePermalink && !hasFeedCardAncestor(player)) {
      return currentPagePermalink;
    }
    // Deliberately do not scan the entire document: on a feed with many videos,
    // a document-wide permalink would be ambiguous and could select the wrong clip.
    return "";
  }

  function queuedTikTokVideoEntry(entries, canonicalUrl) {
    const wantedKey = tiktokVideoKey(canonicalUrl);
    if (!wantedKey || !Array.isArray(entries)) {
      return null;
    }
    return entries.find(function (candidate) {
      return tiktokVideoKey(candidate && candidate.url) === wantedKey;
    }) || null;
  }

  // Facebook has many CDN URLs for a single reel (often short-lived/ranged
  // .mp4 resources).  The download path that is stable for MagnetoClip is the
  // canonical Facebook reel page.  Resolve it per player from the current URL
  // or the nearest reel link in that player's DOM subtree.
  function facebookCanonicalReelForPlayer(player, pageUrl, doc) {
    const pageId = facebookReelId(pageUrl);
    if (pageId) {
      return "https://web.facebook.com/reel/" + pageId;
    }

    const documentRef = doc || (player && player.ownerDocument) || document;
    if (!documentRef) {
      return "";
    }

    const candidates = [];
    const seen = new Set();
    function add(value) {
      const id = facebookReelId(value);
      if (!id || seen.has(id)) {
        return;
      }
      seen.add(id);
      candidates.push(id);
    }

    // Prefer a link surrounding the actual player. Facebook commonly places
    // the reel permalink on an ancestor or a nearby overlay container.
    let node = player;
    for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
      if (node.matches) {
        try {
          const closest = node.matches("a[href]") ? node : node.closest("a[href]");
          if (closest) {
            add(closest.href || closest.getAttribute("href") || "");
          }
        } catch (_) {}
      }
      if (node.querySelectorAll) {
        try {
          const links = node.querySelectorAll('a[href*="/reel/"]');
          for (const link of links) {
            add(link.href || link.getAttribute("href") || "");
            if (candidates.length >= 2) {
              break;
            }
          }
        } catch (_) {}
      }
      if (candidates.length) {
        break;
      }
    }

    // A canonical/og:url is a strong page-level signal when this page itself
    // represents one reel. It is deliberately checked before a broad document
    // scan so one reel cannot be assigned to every feed video.
    try {
      const canonical = documentRef.querySelector('link[rel="canonical"][href]');
      add(canonical && (canonical.href || canonical.getAttribute("href")));
      const ogUrl = documentRef.querySelector('meta[property="og:url"][content]');
      add(ogUrl && ogUrl.getAttribute("content"));
    } catch (_) {}

    if (!candidates.length) {
      try {
        const links = documentRef.querySelectorAll('a[href*="/reel/"]');
        const pageIds = new Set();
        for (const link of links) {
          const id = facebookReelId(link.href || link.getAttribute("href") || "");
          if (id) {
            pageIds.add(id);
          }
          if (pageIds.size > 1) {
            break;
          }
        }
        // Only use a document-wide reel link when it is unambiguous.
        if (pageIds.size === 1) {
          candidates.push(Array.from(pageIds)[0]);
        }
      } catch (_) {}
    }

    return candidates.length ? "https://web.facebook.com/reel/" + candidates[0] : "";
  }

  // Snapchat's /web route is its home/feed experience. Videos previewed there
  // should be handled by the extension's normal detection/capture path; do not
  // overlay a second player-button download path on those previews.
  function isSnapchatWebHomePage(url) {
    try {
      const parsed = new URL(String(url || ""));
      const host = String(parsed.hostname || "").toLowerCase().replace(/^www\./, "");
      const path = (String(parsed.pathname || "/").replace(/\/+$/, "") || "/").toLowerCase();
      return (host === "snapchat.com" || host.endsWith(".snapchat.com")) && path === "/web";
    } catch (_) {
      return false;
    }
  }

  // Feed/home pages frequently contain autoplay previews. Their page URL is
  // not the video URL, so the player button must not send that page to the
  // downloader. Direct downloadable media files remain eligible.
  function isHomeOrFeedPage(url) {
    let parsed;
    try {
      parsed = new URL(String(url || ""));
    } catch (_) {
      return false;
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return false;
    }

    const host = String(parsed.hostname || "").toLowerCase().replace(/^www\./, "");
    const path = (String(parsed.pathname || "/").replace(/\/{2,}/g, "/").replace(/\/+$/, "") || "/").toLowerCase();
    const params = parsed.searchParams;
    const hasSpecificVideoId = ["v", "video_id", "video", "story_fbid", "fbid"].some(function (key) {
      return params.has(key) && String(params.get(key) || "").trim() !== "";
    });

    // Snapchat /web is a homepage/feed even though it is not the site root.
    // Do not show player overlays for its preview videos; the extension's
    // ordinary detection/capture flow remains available.
    if ((host === "snapchat.com" || host.endsWith(".snapchat.com")) && path === "/web") {
      return true;
    }

    // The root route is a homepage on every site. Query parameters used for
    // a specific video permalink keep it eligible (e.g. ?v=<id> routes).
    if (path === "/" && !hasSpecificVideoId) {
      return true;
    }

    const commonFeedPaths = new Set([
      "/home", "/home.php", "/feed", "/timeline", "/explore", "/trending",
      "/following", "/foryou", "/for-you", "/subscriptions", "/popular",
      "/all", "/best", "/new", "/top", "/reels", "/shorts", "/videos",
      "/dashboard", "/latest", "/search", "/search/pins", "/ideas",
      "/watch", "/watch/",
    ]);
    const isFeedRoute = commonFeedPaths.has(path) && !hasSpecificVideoId;
    if (isFeedRoute) {
      return true;
    }

    if (host === "youtube.com" || host.endsWith(".youtube.com")) {
      return /^\/feed(?:\/|$)/i.test(path) || path === "/results" || path === "/gaming";
    }
    if (host === "x.com" || host.endsWith(".x.com") || host === "twitter.com" || host.endsWith(".twitter.com")) {
      return /^\/i\/(?:bookmarks|lists)(?:\/|$)/i.test(path);
    }
    if (host === "facebook.com" || host.endsWith(".facebook.com")) {
      if (path === "/watch" || path === "/home.php") {
        return !hasSpecificVideoId;
      }
      return false;
    }
    if (host === "instagram.com" || host.endsWith(".instagram.com")) {
      return path === "/explore" || path === "/reels" || path === "/direct";
    }
    if (host === "tiktok.com" || host.endsWith(".tiktok.com")) {
      // Localized TikTok landing pages (e.g. /en/) are feeds, not video pages.
      // Video permalink routes /@user/video/<numeric-id> remain eligible.
      if (/^\/[a-z]{2}(?:-[a-z]{2})?$/i.test(path)) {
        return true;
      }
      if (/^\/(?:foryou|following|explore|live|search|trending)(?:\/|$)/i.test(path)) {
        return true;
      }
      if (/^\/@[^/]+\/?$/i.test(path)) {
        return true; // profile grid, not a single video permalink
      }
      return false;
    }
    if (host === "reddit.com" || host.endsWith(".reddit.com")) {
      return /^\/r\/[^/]+(?:\/(?:hot|new|top|rising|videos?))?$/i.test(path) && !/\/comments\//i.test(path);
    }
    if (host === "linkedin.com" || host.endsWith(".linkedin.com")) {
      return path === "/feed" || path === "/videos";
    }
    if (host === "twitch.tv" || host.endsWith(".twitch.tv")) {
      return path === "/directory" || path === "/downloads";
    }
    if (host === "pinterest.com" || host.endsWith(".pinterest.com")) {
      return path === "/search" || path === "/ideas";
    }
    return false;
  }

  // YouTube can consume its page-level detection from the extension queue after
  // the normal Detected-page sync, leaving player_lookup with zero entries even
  // though the current page is a valid individual video. On a true video route,
  // the canonical watch URL itself is a valid yt-dlp target; do not extend this
  // fallback to home/feed routes or social platforms.
  function isYouTubeStandaloneVideoPage(url) {
    try {
      const parsed = new URL(String(url || ""));
      const host = String(parsed.hostname || "").toLowerCase();
      const path = String(parsed.pathname || "/").replace(/\/+$/, "") || "/";
      if (host === "youtu.be") {
        return path.length > 1;
      }
      if (!(host === "youtube.com" || host.endsWith(".youtube.com"))) {
        return false;
      }
      if (path === "/watch") {
        return String(parsed.searchParams.get("v") || "").trim() !== "";
      }
      return /^\/(?:shorts|live|embed)\/[^/]+(?:\/|$)/i.test(path);
    } catch (_) {
      return false;
    }
  }

  function samePageUrl(candidateUrl, pageUrl) {
    if (!candidateUrl || !pageUrl) {
      return false;
    }
    return exactKey(candidateUrl) === exactKey(pageUrl) ||
      (!!looseKey(candidateUrl) && looseKey(candidateUrl) === looseKey(pageUrl));
  }

  // Only explicit, complete video/audio files count as direct media on a
  // homepage. A social-post permalink or an HLS/DASH manifest is not a static
  // file and should not make a feed-preview button appear.
  function isDirectStaticMediaEntry(entry) {
    const url = String(entry && entry.url || "");
    if (!/^https?:\/\//i.test(url)) {
      return false;
    }
    const ext = extensionOf(url);
    if (SEGMENT_EXTS.has(ext) || STREAM_EXTS.has(ext)) {
      return false;
    }
    if (VIDEO_EXTS.has(ext) || AUDIO_EXTS.has(ext)) {
      return true;
    }
    const mime = String(entry && (entry.mime_type || entry.content_type) || "").toLowerCase().split(";")[0].trim();
    return /^(?:video|audio)\//i.test(mime) &&
      !/(?:mpegurl|dash\+xml|mp2t)/i.test(mime);
  }

  function playerTargetSuppressionReason(entry, pageUrl, signals) {
    if (isSnapchatWebHomePage(pageUrl)) {
      return "snapchat_web_home_extension_only";
    }
    if (!isHomeOrFeedPage(pageUrl)) {
      return "";
    }
    if (isTikTokHostUrl(pageUrl)) {
      const targetKey = tiktokVideoKey(entry && entry.url);
      const hintedKey = tiktokVideoKey(signals && signals.tiktokCanonical);
      return targetKey && hintedKey && targetKey === hintedKey
        ? ""
        : "tiktok_feed_requires_queued_video_permalink";
    }
    if (isXHostUrl(pageUrl)) {
      const targetKey = xStatusKey(entry && entry.url);
      const hintedKey = xStatusKey(signals && signals.xCanonical);
      return targetKey && hintedKey && targetKey === hintedKey
        ? ""
        : "x_feed_requires_queued_status_permalink";
    }
    if (entry && samePageUrl(entry.url, pageUrl)) {
      return "homepage_page_url";
    }
    // Facebook feed previews must use the Reel permalink (the CDN .mp4
    // captures are not a reliable download target in MagnetoClip). Never show
    // a feed button based on a fbcdn capture; opening the Reel provides the
    // canonical /reel/<id> URL that the Facebook download path requires.
    try {
      const pageHost = new URL(String(pageUrl || "")).hostname.toLowerCase();
      const entryHost = new URL(String(entry && entry.url || "")).hostname.toLowerCase();
      const facebookPage = pageHost === "facebook.com" || pageHost.endsWith(".facebook.com");
      const facebookCdn = entryHost === "fbcdn.net" || entryHost.endsWith(".fbcdn.net");
      if (facebookPage && (facebookCdn || facebookReelId(entry && entry.url))) {
        return "facebook_feed_requires_reel_page";
      }
    } catch (_) {}
    if (!isDirectStaticMediaEntry(entry)) {
      return "homepage_without_direct_media_url";
    }
    return "";
  }

  // Homepage/feed video elements are often preview players inside cards. Their
  // dimensions can easily exceed the old 200x100 threshold, especially when
  // the mouse hovers over a thumbnail and the site swaps in an autoplay <video>.
  // Detect those wrappers separately from a real watch-page player.
  const FEED_PREVIEW_SELECTORS = [
    "ytd-thumbnail",
    "ytd-rich-item-renderer",
    "ytd-video-renderer",
    "ytd-compact-video-renderer",
    "ytd-grid-video-renderer",
    "ytd-reel-item-renderer",
    "ytm-rich-item-renderer",
    "ytm-video-with-context-renderer",
    ".yt-lockup-view-model",
    "[role='article']",
    "article",
    "shreddit-post",
    "[data-testid='tweet']",
    "[data-testid='cellInnerDiv']",
    "[data-e2e='recommend-list-item-container']",
    "[data-e2e='video-feed-item']",
    "[data-pagelet^='FeedUnit_']",
  ];

  function hasFeedCardAncestor(video) {
    if (!video) {
      return false;
    }
    const selector = FEED_PREVIEW_SELECTORS.join(",");
    let node = video.parentElement;
    for (let depth = 0; node && depth < 12; depth += 1, node = node.parentElement) {
      if (node.matches) {
        try {
          if (node.matches(selector)) {
            return true;
          }
        } catch (_) {}
      }
    }
    return false;
  }

  function hasFeedPermalinkAncestor(video, pageUrl) {
    if (!video || !video.closest || !pageUrl) {
      return false;
    }
    try {
      const current = new URL(String(pageUrl));
      const anchor = video.closest("a[href]");
      if (!anchor) {
        return false;
      }
      const target = new URL(anchor.href || anchor.getAttribute("href") || "", current.href);
      if ((target.protocol !== "http:" && target.protocol !== "https:") || target.origin !== current.origin) {
        return false;
      }
      const currentPath = (current.pathname || "/").replace(/\/+$/, "") || "/";
      const targetPath = (target.pathname || "/").replace(/\/+$/, "") || "/";
      // A different same-site destination surrounding a feed preview is a
      // permalink/card click target (e.g. YouTube /watch?v=..., X /status/...,
      // Facebook /reel/...). It is not the media URL for the preview itself.
      return targetPath !== currentPath || target.search !== current.search;
    } catch (_) {
      return false;
    }
  }

  function hasPrimaryPlayerGeometry(rect, winRef) {
    if (!rect) {
      return false;
    }
    const width = Number(rect.width || 0);
    const height = Number(rect.height || 0);
    const viewportWidth = Number(winRef && winRef.innerWidth || 0);
    const viewportHeight = Number(winRef && winRef.innerHeight || 0);
    if (viewportWidth <= 0 || viewportHeight <= 0) {
      return width >= 380 && height >= 214;
    }

    // On a homepage/feed, require a frame with meaningful presence in the
    // viewport. Normal feed cards and hover previews are usually much smaller.
    const landscape = width >= Math.max(380, viewportWidth * 0.32) &&
      height >= Math.max(214, viewportHeight * 0.24);
    const portrait = width >= 220 && height >= Math.max(320, viewportHeight * 0.38);
    return landscape || portrait;
  }

  function isExpandedPlayer(video, rect, winRef) {
    const documentRef = (winRef && winRef.document) || (video && video.ownerDocument) || null;
    if (documentRef && documentRef.fullscreenElement &&
        (documentRef.fullscreenElement === video ||
         (documentRef.fullscreenElement.contains && documentRef.fullscreenElement.contains(video)))) {
      return true;
    }
    if (video && video.closest) {
      try {
        if (video.closest("[role='dialog'] video, [aria-modal='true'] video, [data-testid='mediaViewer'] video")) {
          return true;
        }
      } catch (_) {}
    }
    const viewportWidth = Number(winRef && winRef.innerWidth || 0);
    const viewportHeight = Number(winRef && winRef.innerHeight || 0);
    return !!rect && viewportWidth > 0 && viewportHeight > 0 &&
      Number(rect.width || 0) >= viewportWidth * 0.70 &&
      Number(rect.height || 0) >= viewportHeight * 0.60;
  }

  function playerAttachmentSuppressionReason(video, rect, pageUrl, winRef) {
    // Suppress all player overlays on Snapchat's /web home/feed route, even
    // when a preview exposes a valid blob: URL or directly downloadable media.
    if (isSnapchatWebHomePage(pageUrl)) {
      return "snapchat_web_home_extension_only";
    }
    if (!meaningfulPlayer(video, rect)) {
      return "player_geometry_too_small_or_hidden";
    }
    if (!isHomeOrFeedPage(pageUrl)) {
      return "";
    }
    if (isTikTokHostUrl(pageUrl)) {
      return tiktokVideoPermalinkForPlayer(video, pageUrl, (video && video.ownerDocument) || document)
        ? ""
        : "tiktok_feed_without_player_permalink";
    }
    if (isXHostUrl(pageUrl)) {
      return xStatusPermalinkForPlayer(video, pageUrl, (video && video.ownerDocument) || document)
        ? ""
        : "x_feed_without_status_permalink";
    }
    // A fullscreen or modal viewer is a real viewing context even when the
    // site's URL remains on its homepage/feed route.
    if (isExpandedPlayer(video, rect, winRef)) {
      return "";
    }
    if (hasFeedCardAncestor(video)) {
      return "feed_card_container";
    }
    if (hasFeedPermalinkAncestor(video, pageUrl)) {
      return "feed_permalink_ancestor";
    }
    if (!hasPrimaryPlayerGeometry(rect, winRef)) {
      return "feed_frame_below_primary_size";
    }
    return "";
  }

  function shouldAttachPlayer(video, rect, pageUrl, winRef) {
    return !playerAttachmentSuppressionReason(video, rect, pageUrl, winRef);
  }

  // Networks the frame actually fetched (from the content-script signal bridge
  // or this module's own PerformanceObserver): media-kind resources that a
  // blob:/MSE player is backed by.
  function mediaNetworkUrls(network, allowed) {
    if (!Array.isArray(network)) {
      return [];
    }
    const out = [];
    const seen = new Set();
    for (const item of network) {
      const url = item && item.url ? String(item.url) : "";
      if (!url || seen.has(url)) {
        continue;
      }
      seen.add(url);
      const kind = item && item.kind ? String(item.kind) : kindOfUrl(url);
      if ((allowed && !allowed.has(kind)) || (kind !== "video" && kind !== "audio" && kind !== "hls" && kind !== "dash")) {
        continue;
      }
      if (SEGMENT_EXTS.has(extensionOf(url))) {
        continue;
      }
      out.push(url);
    }
    return out;
  }

  // All candidate URLs that could identify this player's media, in priority
  // order. Baked from what the player exposes plus the frame's network/scan
  // evidence (blob players cannot be read directly).
  function signalsForPlayer(player, environment) {
    const env = environment || {};
    const network = mediaNetworkUrls(env.network || []);
    const scan = Array.isArray(env.scan) ? env.scan : [];
    const sources = playerUrls(player);
    const blobPlayed = sources.some(isBlobUrl);
    const pageUrl = String(env.pageUrl || "");
    const facebookCanonical = facebookCanonicalReelForPlayer(player, pageUrl, env.document);
    const tiktokCanonical = tiktokVideoPermalinkForPlayer(player, pageUrl, env.document);
    const xCanonical = xStatusPermalinkForPlayer(player, pageUrl, env.document);
    const direct = sources.filter(function (url) {
      return /^https?:/i.test(url);
    });
    const hints = [];
    const seen = new Set();
    function push(url) {
      const key = normalizeUrl(url);
      if (!url || seen.has(key)) {
        return;
      }
      seen.add(key);
      hints.push(url);
    }
    if (facebookCanonical) {
      // On Facebook the CDN is not the player button's target. Keep the
      // canonical Reel only as an association hint; the actual target must be
      // an entry that already exists in the extension's persistent queue.
      push(facebookCanonical);
      return {
        hints: hints,
        blob: blobPlayed,
        direct: direct.length > 0,
        canResolve: true,
        facebookCanonical: facebookCanonical,
        tiktokCanonical: "",
      };
    }
    if (isXHostUrl(pageUrl)) {
      // On X, a post permalink is the player target. Do not let CDN URLs from
      // an autoplay preview become a fallback when no status URL is queued.
      if (xCanonical) {
        push(xCanonical);
      }
      return {
        hints: hints,
        blob: blobPlayed,
        direct: direct.length > 0,
        canResolve: !!xCanonical,
        facebookCanonical: "",
        tiktokCanonical: "",
        xCanonical: xCanonical,
      };
    }
    // TikTok's feed may expose many CDN resources. Put the video permalink
    // first, then let the resolver select only its matching queued entry.
    if (tiktokCanonical) {
      push(tiktokCanonical);
    }
    for (const url of direct) {
      push(url);
    }
    if (blobPlayed || direct.length === 0) {
      for (const url of network) {
        push(url);
      }
    }
    for (const url of scan) {
      push(url);
    }
    return {
      hints: hints,
      blob: blobPlayed,
      direct: direct.length > 0,
      canResolve: hints.length > 0,
      facebookCanonical: "",
      tiktokCanonical: tiktokCanonical,
      xCanonical: "",
    };
  }

  function exactKey(url) {
    return normalizeUrl(url);
  }

  function looseKey(url) {
    return stripQuery(normalizeUrl(url));
  }

  // Score how well one queued entry matches one player's hints.
  function scoreEntry(entry, hints, pageUrl, blobPlayer, suppressPageTarget) {
    if (!entry || !entry.url) {
      return -1;
    }
    const entryUrl = String(entry.url);
    const normalizedEntry = exactKey(entryUrl);
    if (normalizedEntry.indexOf("blob:") === 0) {
      return -1;
    }
    // A page URL is deliberately not a player target on a home/feed route.
    // Continue scoring other queued entries so a real static media URL can
    // still win even when a page-level capture is also present.
    if (suppressPageTarget && samePageUrl(entryUrl, pageUrl)) {
      return -1;
    }
    let best = -1;
    for (const hint of hints) {
      let score = -1;
      if (exactKey(hint) === normalizedEntry) {
        score = 100;
      } else if (looseKey(hint) === looseKey(entryUrl) && looseKey(hint)) {
        score = 80;
      }
      if (score > best) {
        best = score;
      }
    }
    // A page-level capture (yt-dlp/streaming hosts queue the page URL itself)
    // is a valid target even when this blob player exposes no readable media
    // URL. For a blob/MSE player it is also the *real* media, so it outranks a
    // coincidental hint match (e.g. a site UI sound the player never plays).
    const pageMatch = pageUrl && exactKey(pageUrl) === normalizedEntry;
    if (pageMatch) {
      best = blobPlayer ? 200 : Math.max(best, 40);
    }
    if (best > 0) {
      const kind = entry.detected_type === "stream" || entry.detected_type === "hls" || entry.detected_type === "dash" ? 20 : 0;
      best += kind;
    }
    return best;
  }

  // Best queued entry for a player. The page URL (yt-dlp hosts) is the weakest
  // signal: it only wins when nothing more specific exists. Blob players that
  // expose no media URL still fall back to the page capture.
  function bestAssociation(entries, signals, pageUrl) {
    const list = Array.isArray(entries) ? entries : [];
    if (!signals) {
      return null;
    }
    const suppressPageTarget = isHomeOrFeedPage(pageUrl);
    const pageFallback = !suppressPageTarget && signals.blob && pageUrl && !signals.canResolve;
    if (!signals.canResolve && !pageFallback) {
      return null;
    }
    let chosen = null;
    let bestScore = -1;
    for (const entry of list) {
      const score = scoreEntry(entry, signals.hints, pageUrl, !!signals.blob, suppressPageTarget);
      if (score > bestScore) {
        bestScore = score;
        chosen = entry;
      }
    }
    // Some browser/content-script combinations normalize the page URL
    // differently between the queue and the player document.  When the
    // background worker has already narrowed the result to exactly one
    // candidate for a blob/MSE player, that candidate has already passed the
    // media/page/frame safety checks in entriesForPlayer().  Do not discard
    // the canonical detection merely because the final URL normalization
    // step produced a zero score.
    if (!chosen && signals.blob && list.length === 1) {
      const sole = list[0];
      const kind = String(sole.detected_type || "").toLowerCase();
      const media = kind === "video" || kind === "stream" || kind === "hls" || kind === "dash" || kind === "audio";
      const soleUrl = String(sole.url || "");
      const samePage = samePageUrl(pageUrl, soleUrl);
      const suppressed = !!playerTargetSuppressionReason(sole, pageUrl);
      if (!suppressed && (media || samePage)) {
        chosen = sole;
        bestScore = 1;
      }
    }
    return chosen;
  }

  // Stable hash of a player's signals, used to short-circuit relookups while
  // nothing about the player changed.
  function signalsHash(signals) {
    if (!signals || !Array.isArray(signals.hints)) {
      return "";
    }
    return signals.hints.map(exactKey).join("\u0001");
  }

  // Build the download_detection message for one player/entry. ``detection_id``
  // is always the canonical queue id; the entry is context so the app can
  // validate and re-store it without ever trusting a bare URL.
  function buildMessage(player, entry, context) {
    const ctx = context || {};
    const detection = {
      id: entry && entry.id ? entry.id : "",
      url: entry ? entry.url : "",
      filename: entry && entry.filename ? entry.filename : "",
      detected_type: entry && entry.detected_type ? entry.detected_type : "video",
      mime_type: entry && entry.mime_type ? entry.mime_type : "",
      category: entry && entry.category ? entry.category : "",
      page_url: (entry && entry.page_url) || ctx.page_url || "",
      page_title: entry && entry.page_title ? entry.page_title : "",
      referrer: (entry && entry.frame_url) || (entry && entry.referrer) || ctx.frame_url || "",
      frame_url: (entry && entry.frame_url) || ctx.frame_url || "",
      source: "player_button",
    };
    if (entry && entry.cookies) {
      detection.cookies = entry.cookies;
    }
    if (entry && entry.headers && typeof entry.headers === "object") {
      detection.headers = entry.headers;
    }
    if (entry && entry.user_agent) {
      detection.user_agent = entry.user_agent;
    }
    if (entry && entry.data_base64) {
      detection.data_base64 = entry.data_base64;
    }
    return {
      type: "download_detection",
      detection_id: detection.id,
      detection: detection,
      page_url: ctx.page_url || detection.page_url,
      frame_url: ctx.frame_url || detection.frame_url,
    };
  }

  // Response type -> button state.
  const RESPONSE_STATES = {
    download_detection_ok: "sent",
    download_detection_pending: "sent",
  };
  const ERROR_STATES = {
    download_detection_error: "error",
    download_detection_skipped: "error",
  };

  function stateForResponse(response) {
    const type = response && response.type ? response.type : "";
    if (RESPONSE_STATES[type]) {
      return { state: RESPONSE_STATES[type], message: response.message || "" };
    }
    if (ERROR_STATES[type]) {
      return {
        state: ERROR_STATES[type],
        message: response.message || "MagnetoClip did not accept the request.",
      };
    }
    return { state: "error", message: "MagnetoClip did not answer." };
  }

  // Unique per-player cache so repeated lookups for the same player don't
  // hammer the background worker.
  function makeResolutionCache() {
    const cache = new Map();
    return {
      get(player) {
        if (!player || typeof player !== "object") {
          return null;
        }
        const item = cache.get(player);
        if (!item) {
          return null;
        }
        if (item.now + RESOLUTION_TTL_MS < Date.now()) {
          cache.delete(player);
          return null;
        }
        return item;
      },
      set(player, hash, entry) {
        if (player && typeof player === "object") {
          cache.set(player, { hash: hash, entry: entry, now: Date.now() });
        }
      },
      update(player, entry) {
        const item = cache.get(player);
        if (item) {
          item.entry = entry;
          item.now = Date.now();
        }
      },
      keys() {
        return Array.from(cache.keys());
      },
      clear() {
        cache.clear();
      },
      size() {
        return cache.size;
      },
    };
  }

  // ---- DOM overlay ---------------------------------------------------------

  // The overlay deliberately uses ordinary DOM rather than Shadow DOM. Some
  // media sites (notably YouTube) create stacking/containment contexts around
  // their player UI that can make a shadow host report as visible while its
  // child is not actually painted or clickable.
  function createOverlay(documentRef) {
    const doc = documentRef || document;
    const host = doc.createElement("div");
    host.className = "mc-dl-root";
    host.style.setProperty("position", "fixed", "important");
    host.style.setProperty("z-index", "2147483647", "important");
    host.style.setProperty("pointer-events", "none", "important");
    host.style.setProperty("display", "none", "important");
    host.style.setProperty("width", "auto", "important");
    host.style.setProperty("height", "auto", "important");
    host.style.setProperty("min-width", "0", "important");
    host.style.setProperty("min-height", "0", "important");
    host.style.setProperty("margin", "0", "important");
    host.style.setProperty("padding", "0", "important");
    host.style.setProperty("border", "0", "important");
    host.style.setProperty("background", "transparent", "important");
    host.style.setProperty("overflow", "visible", "important");
    host.style.setProperty("contain", "none", "important");
    host.style.setProperty("visibility", "visible", "important");
    host.style.setProperty("opacity", "1", "important");
    host.style.setProperty("font-family", "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif", "important");

    const button = doc.createElement("button");
    button.type = "button";
    button.className = "mc-dl-btn";
    button.setAttribute("data-state", "normal");
    button.style.setProperty("pointer-events", "auto", "important");
    button.style.setProperty("display", "flex", "important");
    button.style.setProperty("align-items", "center", "important");
    button.style.setProperty("gap", "6px", "important");
    button.style.setProperty("width", "auto", "important");
    button.style.setProperty("height", "34px", "important");
    button.style.setProperty("min-width", "0", "important");
    button.style.setProperty("padding", "6px 12px", "important");
    button.style.setProperty("margin", "0", "important");
    button.style.setProperty("border", "0", "important");
    button.style.setProperty("border-radius", "999px", "important");
    button.style.setProperty("background", "#7c3aed", "important");
    button.style.setProperty("color", "#fff", "important");
    button.style.setProperty("font-family", "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif", "important");
    button.style.setProperty("font-size", "12px", "important");
    button.style.setProperty("line-height", "16px", "important");
    button.style.setProperty("font-weight", "600", "important");
    button.style.setProperty("cursor", "pointer", "important");
    button.style.setProperty("box-shadow", "0 2px 8px rgba(0,0,0,.35)", "important");
    button.style.setProperty("white-space", "nowrap", "important");
    button.style.setProperty("appearance", "none", "important");
    button.style.setProperty("-webkit-appearance", "none", "important");
    button.style.setProperty("box-sizing", "border-box", "important");
    button.style.setProperty("visibility", "visible", "important");
    button.style.setProperty("opacity", "1", "important");
    button.style.setProperty("overflow", "visible", "important");

    const ico = doc.createElementNS("http://www.w3.org/2000/svg", "svg");
    ico.setAttribute("viewBox", "0 0 24 24");
    ico.setAttribute("fill", "none");
    ico.setAttribute("stroke", "currentColor");
    ico.setAttribute("stroke-width", "2.4");
    ico.style.setProperty("width", "14px", "important");
    ico.style.setProperty("height", "14px", "important");
    ico.style.setProperty("flex", "none", "important");
    const path = doc.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M12 3v12m0 0l-4-4m4 4l4-4M4 21h16");
    ico.appendChild(path);
    const label = doc.createElement("span");
    label.textContent = "Download with MagnetoClip";
    label.style.setProperty("display", "inline", "important");
    label.style.setProperty("font", "inherit", "important");
    label.style.setProperty("color", "inherit", "important");
    button.appendChild(ico);
    button.appendChild(label);
    host.appendChild(button);
    host.doc = doc;
    host.button = button;
    host.label = label;
    return host;
  }

  function overlayMountParent(doc) {
    if (!doc) return null;
    // Real fullscreen puts the page inside the browser's top layer. Mount there
    // when available so the button remains above fullscreen player chrome.
    return doc.fullscreenElement || doc.body || doc.documentElement || null;
  }

  function mountOverlay(root, doc) {
    if (!root) return false;
    const parent = overlayMountParent(doc || root.doc);
    if (!parent) return false;
    if (root.parentNode !== parent) {
      parent.appendChild(root);
    }
    return !!root.isConnected;
  }

  function setOverlayState(overlay, state, message) {
    const button = overlay && overlay.button;
    if (!button) {
      return;
    }
    button.setAttribute("data-state", state);
    button.disabled = state === "sending";
    const backgrounds = { normal: "#7c3aed", sending: "#4f46e5", sent: "#059669", error: "#b91c1c" };
    button.style.setProperty("background", backgrounds[state] || backgrounds.normal, "important");
    button.style.setProperty("opacity", state === "sending" ? "0.85" : "1", "important");
    button.style.setProperty("cursor", state === "sending" ? "default" : "pointer", "important");
    const text = state === "normal"
      ? "Download with MagnetoClip"
      : state === "sending"
        ? "Sending to MagnetoClip…"
        : state === "sent"
          ? "Sent to MagnetoClip"
          : state === "error"
            ? (message && message.indexOf("not running") !== -1
              ? "MagnetoClip isn't running"
              : message && /open the video first/i.test(message)
                ? "Open video first"
                : "Try again")
            : "Download with MagnetoClip";
    overlay.label.textContent = text;
    button.title = message || text;
  }

  // Reposition the overlay over the right edge of the player, vertically
// centred so it never covers the player's own controls. Hidden when the
// player leaves the viewport entirely.
  function positionOverlay(overlay, rect, viewport) {
    const root = overlay && overlay.root || overlay;
    if (!root) {
      return;
    }
    const view = viewport || {};
    const vw = view.width || 0;
    const vh = view.height || 0;
    if (!rect || rect.width <= 0 || rect.height <= 0) {
      root.style.setProperty("display", "none", "important");
      return;
    }
    const onscreen =
      rect.left < vw && rect.right > 0 && rect.top < vh && rect.bottom > 0;
    if (!onscreen) {
      root.style.setProperty("display", "none", "important");
      return;
    }
    const size = root._size || { width: 0, height: 0 };
    const bw = size.width || 170;
    const bh = size.height || 34;
    const inset = 10;
    // Right edge, vertically centred inside the player.
    let x = rect.right - bw - inset;
    let y = rect.top + (rect.height - bh) / 2;
    // Never leave the player for the sake of a clamp: a button pinned to the
    // page corner lands behind site chrome (e.g. YouTube's header logo).
    if (bw < rect.width) {
      x = Math.max(rect.left + inset, Math.min(x, rect.right - bw - inset));
    } else {
      x = rect.left;
    }
    if (bh < rect.height) {
      y = Math.max(rect.top + inset, Math.min(y, rect.bottom - bh - inset));
    } else {
      y = rect.top;
    }
    const left = x + "px";
    const top = y + "px";
    if (root.style.left !== left || root.style.top !== top) {
      root.style.setProperty("left", left, "important");
      root.style.setProperty("top", top, "important");
    }
    root.style.setProperty("display", "block", "important");
    root.style.setProperty("visibility", "visible", "important");
    root.style.setProperty("opacity", "1", "important");
  }

  // ---- controller ----------------------------------------------------------

  // Connects the module to a live page. ``env`` is the movable seam for tests:
  //   { doc, win, send, lookup, pageUrl, frameUrl, networkSignals, scanSignals }
  // Defaults point at the real content-script environment.
  function create(env) {
    const E = env || {};
    const doc = E.doc || (typeof document !== "undefined" ? document : null);
    const win = E.win || (typeof window !== "undefined" ? window : null);
    if (!doc || !win) {
      return null;
    }

    const send = E.send || function (message) {
      if (typeof chrome === "undefined" || !chrome.runtime || !chrome.runtime.sendMessage) {
        return Promise.resolve(null);
      }
      return new Promise(function (resolve) {
        try {
          const result = chrome.runtime.sendMessage(message);
          if (result && typeof result.then === "function") {
            result.then(resolve).catch(function () {
              resolve(null);
            });
          } else {
            resolve(result);
          }
        } catch (error) {
          resolve(null);
        }
      });
    };

    const suppliedPageUrl = E.pageUrl;
    const suppliedFrameUrl = E.frameUrl;

    // YouTube and other SPA sites can change the URL without replacing this
    // content-script/controller instance. Never freeze the page/frame URL at
    // controller creation time: the detection queue follows the new URL.
    function currentPageUrl() {
      if (typeof suppliedPageUrl === "function") {
        return String(suppliedPageUrl() || "");
      }
      if (suppliedPageUrl) {
        return String(suppliedPageUrl);
      }
      return (win.location && win.location.href) || "";
    }

    function currentFrameUrl() {
      if (typeof suppliedFrameUrl === "function") {
        return String(suppliedFrameUrl() || currentPageUrl());
      }
      if (suppliedFrameUrl) {
        return String(suppliedFrameUrl);
      }
      return (win.location && win.location.href) || currentPageUrl();
    }

    const debug = typeof console !== "undefined" && console.info
      ? function (action, detail) {
          try {
            let text = "";
            if (detail !== undefined) {
              const json = JSON.stringify(detail);
              text = typeof json === "string" && json.length <= 500 ? " " + json : " " + String(detail);
            }
            console.info("[mc-overlay] " + action + text);
          } catch (error) {
            // best-effort diagnostics
          }
        }
      : function () {};

    function networkSignals() {
      const fromContent = win.__mcContentSignals && win.__mcContentSignals.network
        ? win.__mcContentSignals.network()
        : [];
      return fromContent.length ? fromContent : (E.networkSignals ? E.networkSignals() : []);
    }

    function scanSignals() {
      const fromContent = win.__mcContentSignals && win.__mcContentSignals.scan
        ? win.__mcContentSignals.scan()
        : [];
      return fromContent.length ? fromContent : (E.scanSignals ? E.scanSignals() : []);
    }

    // A Map, not a WeakMap: the overlay needs keys()/size/clear() for sweeps and
    // teardown, and players stay alive in the document until they are removed.
    const overlays = new Map();
    const resolutionCache = makeResolutionCache();
    let placeholderForHidden = null;

    let overlaysStarted = 0;
    let lookups = 0;
    let lookupsResolved = 0;

    function lookupEntries(signals, context) {
      // A blob player with no readable media URL still asks, so the background
      // can offer the page-level capture (yt-dlp/streaming hosts).
      if (!signals || (!signals.canResolve && !signals.blob)) {
        return Promise.resolve([]);
      }
      const lookup = E.lookup || function (message) {
        return send(message);
      };
      const contextValue = context || {};
      const lookupPageUrl = contextValue.pageUrl || currentPageUrl();
      const lookupFrameUrl = contextValue.frameUrl || currentFrameUrl();
      lookups += 1;
      return Promise.resolve(
        lookup({
          type: "player_lookup",
          page_url: lookupPageUrl,
          frame_url: lookupFrameUrl,
          signals: signals.hints,
          blob: signals.blob ? 1 : 0,
        })
      ).then(function (response) {
        lookupsResolved += 1;
        if (!response || !Array.isArray(response.entries)) {
          return { entries: [], total: 0, queue_urls: [] };
        }
        return {
          entries: response.entries,
          total: Number(response.total) || 0,
          queue_urls: Array.isArray(response.queue_urls) ? response.queue_urls : [],
        };
      });
    }

    function resolve(player) {
      // Capture the URL once for this lookup. SPA navigation (especially
      // YouTube's /watch navigation) can happen while the async queue lookup
      // is in flight; using one snapshot keeps the lookup and cache key
      // internally consistent.
      const livePageUrl = currentPageUrl();
      const liveFrameUrl = currentFrameUrl();
      const signals = signalsForPlayer(player, {
        network: networkSignals(),
        scan: scanSignals(),
        pageUrl: livePageUrl,
        document: doc,
      });
      const hash = signalsHash(signals) + "\u0002" + exactKey(livePageUrl) + "\u0003" + exactKey(liveFrameUrl);
      const cached = resolutionCache.get(player);
      if (cached && cached.hash === hash && cached.entry) {
        const cachedSuppression = playerTargetSuppressionReason(cached.entry, livePageUrl, signals);
        if (!cachedSuppression) {
          debug("resolve:cached", { matched: cached.entry.url });
          return Promise.resolve({ signals: signals, entry: cached.entry });
        }
        resolutionCache.update(player, null);
        debug("player_hidden_homepage_target", {
          reason: cachedSuppression,
          selected_url: cached.entry.url,
          page_url: livePageUrl,
          cached: true,
        });
        return Promise.resolve({ signals: signals, entry: null, suppressionReason: cachedSuppression });
      }
      const homeFeedPage = isHomeOrFeedPage(livePageUrl);
      const pageFallback = !homeFeedPage && livePageUrl && signals.blob && !signals.canResolve;
      if (!signals.canResolve && !pageFallback) {
        resolutionCache.set(player, hash, null);
        const suppressionReason = homeFeedPage ? "homepage_without_direct_media_url" : "";
        debug(homeFeedPage ? "player_hidden_homepage_target" : "resolve:no-signals", {
          hints: signals.hints.slice(0, 8),
          blob: signals.blob,
          reason: suppressionReason,
          page_url: livePageUrl,
        });
        return Promise.resolve({ signals: signals, entry: null, suppressionReason: suppressionReason });
      }
      function trimUrl(url) {
        const text = String(url || "");
        return text.length > 88 ? text.slice(0, 85) + "..." : text;
      }
      function trimUrls(urls) {
        return (Array.isArray(urls) ? urls : []).slice(0, 5).map(trimUrl);
      }

      return lookupEntries(signals, { pageUrl: livePageUrl, frameUrl: liveFrameUrl }).then(function (resolved) {
        const entries = resolved.entries;
        let entry = null;
        if (signals.tiktokCanonical) {
          entry = queuedTikTokVideoEntry(entries, signals.tiktokCanonical);
          debug("tiktok:queue-video", {
            url: signals.tiktokCanonical,
            key: tiktokVideoKey(signals.tiktokCanonical),
            found: !!entry,
            queue_candidates: entries.filter(function (candidate) {
              return !!tiktokVideoKey(candidate && candidate.url);
            }).length,
          });
        }
        if (signals.xCanonical) {
          entry = queuedXStatusEntry(entries, signals.xCanonical);
          debug("x:queue-status", {
            url: signals.xCanonical,
            key: xStatusKey(signals.xCanonical),
            found: !!entry,
            queue_candidates: entries.filter(function (candidate) {
              return !!xStatusKey(candidate && candidate.url);
            }).length,
          });
        }
        if (!entry && signals.facebookCanonical) {
          const wantedReelId = facebookReelId(signals.facebookCanonical);
          if (wantedReelId) {
            entry = entries.find(function (candidate) {
              return facebookReelId(candidate && candidate.url) === wantedReelId;
            }) || null;
            debug("facebook:queue-reel", {
              reel_id: wantedReelId,
              found: !!entry,
              queue_candidates: entries.filter(function (candidate) {
                return !!facebookReelId(candidate && candidate.url);
              }).length,
            });
          }
        }
        // If Facebook did not expose a player-specific Reel identifier, accept
        // a Reel URL from the current-page queue only when it is unambiguous.
        if (!entry && !signals.facebookCanonical) {
          const reelEntries = entries.filter(function (candidate) {
            return !!facebookReelId(candidate && candidate.url);
          });
          if (reelEntries.length === 1) {
            entry = reelEntries[0];
            debug("facebook:queue-reel", {
              reel_id: facebookReelId(entry.url),
              found: true,
              queue_candidates: 1,
              mode: "sole_candidate",
            });
          }
        }
        if (!entry && !signals.tiktokCanonical && !signals.xCanonical) {
          entry = bestAssociation(entries, signals, livePageUrl);
        }
        // If YouTube has a real individual-video route but its extension queue
        // has already been drained (queue_total=0), the canonical watch URL is
        // still a valid player-button target. The background's existing
        // handlePlayerDownload path re-queues the supplied entry on click, then
        // sends it through the normal app download pipeline. Home/feed pages,
        // Facebook, Snapchat, and other platforms cannot use this fallback.
        if (!entry && !homeFeedPage && isYouTubeStandaloneVideoPage(livePageUrl)) {
          entry = {
            id: "",
            url: livePageUrl,
            filename: String((doc && doc.title) || "YouTube video"),
            detected_type: "video",
            source: "player_button",
            page_url: livePageUrl,
            frame_url: liveFrameUrl,
            referrer: liveFrameUrl || livePageUrl,
          };
          debug("youtube:watch-page-fallback", {
            url: livePageUrl,
            queue_total: resolved.total,
            matching_entries: entries.length,
          });
        }
        let suppressionReason = "";
        if (entry) {
          suppressionReason = playerTargetSuppressionReason(entry, livePageUrl, signals);
          if (suppressionReason) {
            debug("player_hidden_homepage_target", {
              reason: suppressionReason,
              selected_url: trimUrl(entry.url),
              page_url: trimUrl(livePageUrl),
              direct_static_media: isDirectStaticMediaEntry(entry),
            });
            entry = null;
          }
        } else if (homeFeedPage) {
          suppressionReason = "homepage_without_direct_media_url";
        }
        resolutionCache.set(player, hash, entry);
        if (signals.facebookCanonical) {
          debug("facebook:canonical_hint", { url: signals.facebookCanonical, queued: !!entry });
        }
        if (signals.tiktokCanonical) {
          debug("tiktok:canonical_hint", { url: signals.tiktokCanonical, queued: !!entry });
        }
        if (signals.xCanonical) {
          debug("x:canonical_hint", { url: signals.xCanonical, queued: !!entry });
        }
        debug("resolve", {
          hints_count: signals.hints.length,
          blob: signals.blob,
          canResolve: signals.canResolve,
          pageFallback: !!pageFallback,
          queue_total: resolved.total,
          queue_urls: trimUrls(resolved.queue_urls),
          page_url: trimUrl(livePageUrl),
          frame_url: trimUrl(liveFrameUrl),
          entries: entries.length,
          entry_types: entries.slice(0, 4).map(function (item) { return String(item && item.detected_type || ""); }),
          matched: entry ? trimUrl(entry.url) : null,
          suppression_reason: suppressionReason,
          probed_capture: entry ? false : (!homeFeedPage && requestPageCapture(signals)),
        });
        return { signals: signals, entry: entry, suppressionReason: suppressionReason };
      });
    }

    // Ask the content script (same isolated world) to re-offer the current page as
    // a yt-dlp capture. The queue can lose the page entry when the app consumed
    // the category or the page was reset, and the capture guard would never
    // re-emit it without this nudge. Throttled: it only needs to run until the
    // background queue is re-populated.
    let lastCaptureProbe = 0;
    function requestPageCapture(signals) {
      const contents = window.__mcContentSignals;
      if (!contents || typeof contents.requestCapture !== "function") {
        return false;
      }
      const kind = typeof contents.hostKind === "function" ? contents.hostKind() : "plain";
      if (!signals.blob && kind === "plain") {
        return false;
      }
      const now = (new Date()).getTime();
      if (now - lastCaptureProbe < 30000) {
        return false;
      }
      lastCaptureProbe = now;
      try {
        return contents.requestCapture();
      } catch (error) {
        return false;
      }
    }

    const retryTimers = new Map();

    function scheduleResolutionRetries(player) {
      if (!player || !overlays.has(player) || retryTimers.has(player)) {
        return;
      }
      const delays = [250, 600, 1200, 2500, 5000];
      const timers = [];
      retryTimers.set(player, timers);
      delays.forEach(function (delay) {
        const timer = win.setTimeout(function () {
          if (!overlays.has(player)) {
            return;
          }
          refresh(player);
        }, delay);
        timers.push(timer);
      });
    }

    function clearResolutionRetries(player) {
      const timers = retryTimers.get(player);
      if (!timers) {
        return;
      }
      timers.forEach(function (timer) {
        try { win.clearTimeout(timer); } catch (_) {}
      });
      retryTimers.delete(player);
    }

    function refresh(player) {
      if (!player || !overlays.has(player)) {
        return;
      }
      const overlay = overlays.get(player);
      const rect = player.getBoundingClientRect();
      if (!shouldAttachPlayer(player, rect, currentPageUrl(), win)) {
        overlay.root.style.display = "none";
        return;
      }
      resolve(player).then(function (resolved) {
        if (!overlays.has(player)) {
          return;
        }
        if (resolved.entry) {
          clearResolutionRetries(player);
          syncOverlay(overlay);
        } else {
          overlay.root.style.display = "none";
          scheduleResolutionRetries(player);
        }
        const hostRect = overlay.root && overlay.root.getBoundingClientRect
          ? overlay.root.getBoundingClientRect()
          : null;
        debug("view", {
          shown: !!resolved.entry,
          entry: resolved.entry ? resolved.entry.url : null,
          blob: resolved.signals && resolved.signals.blob,
          host: hostRect ? { left: hostRect.left, top: hostRect.top, width: hostRect.width, height: hostRect.height, z: win.getComputedStyle ? win.getComputedStyle(overlay.root).zIndex : "", display: win.getComputedStyle ? win.getComputedStyle(overlay.root).display : "", visibility: win.getComputedStyle ? win.getComputedStyle(overlay.root).visibility : "", connected: !!overlay.root.isConnected, parent: overlay.root.parentElement ? overlay.root.parentElement.tagName : null } : null,
          button: overlay.button && overlay.button.getBoundingClientRect ? (function () { var r = overlay.button.getBoundingClientRect(); var cs = win.getComputedStyle ? win.getComputedStyle(overlay.button) : null; return { left: r.left, top: r.top, width: r.width, height: r.height, display: cs ? cs.display : "", visibility: cs ? cs.visibility : "", opacity: cs ? cs.opacity : "" }; }()) : null,
        });
      });
    }

    function syncOverlay(overlay) {
      mountOverlay(overlay.root, doc);
      const rect = overlay.player.getBoundingClientRect();
      if (!shouldAttachPlayer(overlay.player, rect, currentPageUrl(), win) || !rect) {
        overlay.root.style.display = "none";
        return;
      }
      const viewport = { width: win.innerWidth || 0, height: win.innerHeight || 0 };
      // Measure the actual shadow button, then give the fixed host an explicit
      // geometry. This removes the last dependency on shadow-tree intrinsic
      // sizing, which can otherwise report 0x0 on some Chromium/Firefox pages.
      const buttonRect = overlay.button && overlay.button.getBoundingClientRect
        ? overlay.button.getBoundingClientRect()
        : null;
      const width = (buttonRect && buttonRect.width) || overlay.root.offsetWidth || 190;
      const height = (buttonRect && buttonRect.height) || overlay.root.offsetHeight || 34;
      overlay.root._size = { width: width, height: height };
      overlay.root.style.setProperty("width", Math.ceil(width) + "px", "important");
      overlay.root.style.setProperty("height", Math.ceil(height) + "px", "important");
      positionOverlay(overlay.root, rect, viewport);
    }

    function attach(player) {
      if (overlays.has(player) || !shouldAttachPlayer(player, player.getBoundingClientRect && player.getBoundingClientRect(), currentPageUrl(), win)) {
        return;
      }
      const root = createOverlay(doc);
      const overlay = root;
      overlay.player = player;
      overlay.root = root;
      let inFlight = false;
      debug("attach", { href: win.location && win.location.href });
      overlay.button.addEventListener("click", function () {
        if (inFlight) {
          return;
        }
        const before = (new Date()).getTime();
        resolve(player).then(function (resolved) {
          if (!resolved.entry) {
            const message = resolved.suppressionReason
              ? "This looks like a homepage or video feed. Open the video first, then click Download with MagnetoClip so the correct video URL can be detected."
              : "Could not match this player to a detection.";
            if (resolved.suppressionReason) {
              debug("player_click_homepage_blocked", {
                reason: resolved.suppressionReason,
                page_url: currentPageUrl(),
              });
              // A stale overlay can remain clickable for the fraction of a
              // second before the next refresh hides it. Use that click to give
              // the user a clear explanation instead of a generic failure.
              setOverlayState(overlay, "error", message);
              syncOverlay(overlay);
            } else {
              setOverlayState(overlay, "error", message);
            }
            setTimeout(function () {
              if (overlays.has(player)) {
                setOverlayState(overlay, "normal", "");
                refresh(player);
              }
            }, 3000);
            return;
          }
          const liveSuppression = playerTargetSuppressionReason(resolved.entry, currentPageUrl(), resolved.signals);
          if (liveSuppression) {
            const message = "This looks like a homepage or video feed. Open the video first, then click Download with MagnetoClip so the correct video URL can be detected.";
            debug("player_click_homepage_blocked", {
              reason: liveSuppression,
              selected_url: resolved.entry.url,
              page_url: currentPageUrl(),
            });
            setOverlayState(overlay, "error", message);
            syncOverlay(overlay);
            setTimeout(function () {
              if (overlays.has(player)) {
                setOverlayState(overlay, "normal", "");
                refresh(player);
              }
            }, 3000);
            return;
          }
          // A click is the latest truth: refresh the cache even when the
          // lookup result is identical, so a just-synced queue still works.
          const message = buildMessage(player, resolved.entry, {
            page_url: currentPageUrl(),
            frame_url: currentFrameUrl(),
          });
          inFlight = true;
          setOverlayState(overlay, "sending", "");
          send(message).then(function (response) {
            inFlight = false;
            const mapped = stateForResponse(response);
            setOverlayState(overlay, mapped.state, mapped.message);
            const revert = mapped.state === "sent" ? 2600 : 4200;
            setTimeout(function () {
              if (overlays.has(player)) {
                setOverlayState(overlay, "normal", "");
              }
            }, revert);
          }).catch(function () {
            inFlight = false;
            setOverlayState(overlay, "error", "MagnetoClip is not reachable.");
            setTimeout(function () {
              setOverlayState(overlay, "normal", "");
            }, 4200);
          });
        });
      });
      mountOverlay(root, doc);
      overlays.set(player, overlay);
      refresh(player);
    }

    function detach(player) {
      const overlay = overlays.get(player);
      if (!overlay) {
        return;
      }
      overlays.delete(player);
      clearResolutionRetries(player);
      resolutionCache.update(player, null);
      if (overlay.root && overlay.root.parentNode) {
        overlay.root.parentNode.removeChild(overlay.root);
      }
    }

    function sweep() {
      for (const player of resolutionCache.keys()) {
        if (!player.isConnected && player.isConnected !== undefined) {
          detach(player);
        }
      }
      for (const player of overlays.keys()) {
        if (player.isConnected === false) {
          detach(player);
        }
      }
    }

    let scheduled = null;
    function schedule() {
      if (scheduled) {
        return;
      }
      scheduled = win.requestAnimationFrame ? win.requestAnimationFrame(function () {
        scheduled = null;
        sweep();
        refreshAll();
      }) : setTimeout(function () {
        scheduled = null;
        sweep();
        refreshAll();
      }, 100);
    }

    function refreshAll() {
      const players = doc.querySelectorAll ? doc.querySelectorAll("video") : [];
      const existing = new Set(overlays.keys());
      const seen = new Set();
      overlaysStarted += 1;
      debug("scan", { players: players.length, attached: existing.size, run: overlaysStarted });
      for (let index = 0; index < players.length; index += 1) {
        const player = players[index];
        seen.add(player);
        const playerRect = player.getBoundingClientRect();
        const suppressionReason = playerAttachmentSuppressionReason(player, playerRect, currentPageUrl(), win);
        if (suppressionReason) {
          if (overlays.has(player)) {
            debug("detach", { reason: suppressionReason, width: Math.round(playerRect.width || 0), height: Math.round(playerRect.height || 0) });
            detach(player);
          } else if (isHomeOrFeedPage(currentPageUrl()) && suppressionReason !== "player_geometry_too_small_or_hidden") {
            debug("skip_feed_preview", {
              reason: suppressionReason,
              width: Math.round(playerRect.width || 0),
              height: Math.round(playerRect.height || 0),
              viewport_width: win.innerWidth || 0,
              viewport_height: win.innerHeight || 0,
            });
          }
          continue;
        }
        const current = overlays.get(player);
        // YouTube and other SPA players can remove/reparent extension DOM while
        // keeping the same <video> node alive. Recreate a disconnected host.
        if (current && (!current.root || !current.root.isConnected)) {
          debug("reattach", { reason: "overlay_host_disconnected" });
          detach(player);
        }
        if (!overlays.has(player)) {
          attach(player);
        } else {
          refresh(player);
        }
      }
      for (const player of existing) {
        if (!seen.has(player)) {
          detach(player);
        }
      }
    }

    let domainObserver = null;
    let reselect = null;
    let lifecycleTimer = null;
    let spaListener = function () {
      schedule();
    };
    let visibilityListener = function () {
      if (!doc.hidden) {
        schedule();
        setTimeout(schedule, 120);
        setTimeout(schedule, 600);
      }
    };

    function start() {
      if (win.__magnetoclipPlayerOverlayStarted && !E.force) {
        return stop;
      }
      win.__magnetoclipPlayerOverlayStarted = true;
      if (typeof MutationObserver !== "undefined") {
        domainObserver = new MutationObserver(function (records) {
          let touched = false;
          for (const record of records) {
            if (record.type === "childList" && record.addedNodes && record.addedNodes.length) {
              for (const node of record.addedNodes) {
                if (node && node.nodeType === 1) {
                  touched = true;
                  break;
                }
              }
              if (touched) {
                break;
              }
            }
            if (record.type === "attributes") {
              touched = true;
            }
          }
          if (touched) {
            schedule();
          }
        });
        domainObserver.observe(doc.documentElement, {
          childList: true,
          subtree: true,
          attributes: true,
          attributeFilter: ["src", "srcset", "style", "class", "hidden", "data-src"],
        });
      }
      if (win.addEventListener) {
        win.addEventListener("resize", schedule, { passive: true });
        win.addEventListener("scroll", schedule, { capture: true, passive: true });
        win.addEventListener("fullscreenchange", schedule, { passive: true });
        win.addEventListener("yt-navigate-start", schedule, true);
        win.addEventListener("yt-navigate-finish", schedule, true);
        win.addEventListener("popstate", schedule, true);
        win.addEventListener("hashchange", schedule, true);
        doc.addEventListener("fullscreenchange", schedule, { passive: true });
        doc.addEventListener("playing", schedule, true);
        doc.addEventListener("loadstart", schedule, true);
        doc.addEventListener("visibilitychange", visibilityListener, true);
      }
      // Re-run after client-side players settle. YouTube can construct the
      // <video> and rebuild its surrounding DOM several times.
      setTimeout(schedule, 100);
      setTimeout(schedule, 250);
      setTimeout(schedule, 600);
      setTimeout(schedule, 1200);
      setTimeout(schedule, 2500);
      setTimeout(schedule, 5000);
      // Lightweight self-healing if a site removes the extension host without
      // producing a useful navigation/mutation event.
      lifecycleTimer = win.setInterval(function () {
        if (!doc.hidden) {
          schedule();
        }
      }, 1800);
      return stop;
    }

    function stop() {
      if (runtimeDetectionListener && typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
        try { chrome.runtime.onMessage.removeListener(runtimeDetectionListener); } catch (_) {}
        runtimeDetectionListener = null;
      }
      if (domainObserver) {
        domainObserver.disconnect();
        domainObserver = null;
      }
      if (doc.documentElement) {
        doc.documentElement.removeEventListener("spa-detected", spaListener);
      }
      if (lifecycleTimer) {
        try { win.clearInterval(lifecycleTimer); } catch (_) {}
        lifecycleTimer = null;
      }
      if (win.removeEventListener) {
        win.removeEventListener("resize", schedule);
        win.removeEventListener("scroll", schedule, { capture: true });
        win.removeEventListener("fullscreenchange", schedule);
        win.removeEventListener("yt-navigate-start", schedule, true);
        win.removeEventListener("yt-navigate-finish", schedule, true);
        win.removeEventListener("popstate", schedule, true);
        win.removeEventListener("hashchange", schedule, true);
        doc.removeEventListener("fullscreenchange", schedule);
        doc.removeEventListener("playing", schedule, true);
        doc.removeEventListener("loadstart", schedule, true);
        doc.removeEventListener("visibilitychange", visibilityListener, true);
      }
      for (const player of overlays.keys()) {
        detach(player);
      }
      for (const player of Array.from(retryTimers.keys())) {
        clearResolutionRetries(player);
      }
      overlays.clear();
      resolutionCache.clear();
      win.__magnetoclipPlayerOverlayStarted = false;
    }

    doc.documentElement.addEventListener("spa-detected", spaListener);

    // Background queue updates are asynchronous relative to player creation.
    // Refresh immediately when a new detection arrives instead of depending on
    // a lucky reload/resize/playback event.
    let runtimeDetectionListener = null;
    if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
      runtimeDetectionListener = function (message) {
        if (!message || (message.type !== "detection_queue_updated" && message.type !== "detection_update")) {
          return;
        }
        schedule();
      };
      chrome.runtime.onMessage.addListener(runtimeDetectionListener);
    }

    return {
      start: start,
      stop: stop,
      refreshAll: refreshAll,
      resolve: resolve,
      attach: attach,
      detach: detach,
      overlayFor: function (player) {
        return overlays.get(player) || null;
      },
      overlayCount: function () {
        return overlays.size;
      },
      meaningfulPlayer: meaningfulPlayer,
      shouldAttachPlayer: shouldAttachPlayer,
      playerAttachmentSuppressionReason: playerAttachmentSuppressionReason,
      hasFeedCardAncestor: hasFeedCardAncestor,
      hasPrimaryPlayerGeometry: hasPrimaryPlayerGeometry,
      signalsForPlayer: signalsForPlayer,
      bestAssociation: bestAssociation,
      scoreEntry: scoreEntry,
      buildMessage: buildMessage,
      stateForResponse: stateForResponse,
      signalsHash: signalsHash,
      playerUrls: playerUrls,
      mediaNetworkUrls: mediaNetworkUrls,
      kindOfUrl: kindOfUrl,
      isHomeOrFeedPage: isHomeOrFeedPage,
      isSnapchatWebHomePage: isSnapchatWebHomePage,
      isYouTubeStandaloneVideoPage: isYouTubeStandaloneVideoPage,
      isTikTokHostUrl: isTikTokHostUrl,
      tiktokVideoKey: tiktokVideoKey,
      tiktokVideoPermalinkForPlayer: tiktokVideoPermalinkForPlayer,
      queuedTikTokVideoEntry: queuedTikTokVideoEntry,
      isXHostUrl: isXHostUrl,
      xStatusKey: xStatusKey,
      xStatusPermalinkForPlayer: xStatusPermalinkForPlayer,
      queuedXStatusEntry: queuedXStatusEntry,
      isDirectStaticMediaEntry: isDirectStaticMediaEntry,
      playerTargetSuppressionReason: playerTargetSuppressionReason,
      setOverlayState: setOverlayState,
      positionOverlay: positionOverlay,
    };
  }

  const api = {
    create: create,
    meaningfulPlayer: meaningfulPlayer,
    shouldAttachPlayer: shouldAttachPlayer,
    playerAttachmentSuppressionReason: playerAttachmentSuppressionReason,
    hasFeedCardAncestor: hasFeedCardAncestor,
    hasPrimaryPlayerGeometry: hasPrimaryPlayerGeometry,
    signalsForPlayer: signalsForPlayer,
    bestAssociation: bestAssociation,
    scoreEntry: scoreEntry,
    buildMessage: buildMessage,
    stateForResponse: stateForResponse,
    signalsHash: signalsHash,
    playerUrls: playerUrls,
    mediaNetworkUrls: mediaNetworkUrls,
    kindOfUrl: kindOfUrl,
    isHomeOrFeedPage: isHomeOrFeedPage,
    isSnapchatWebHomePage: isSnapchatWebHomePage,
    isYouTubeStandaloneVideoPage: isYouTubeStandaloneVideoPage,
    isTikTokHostUrl: isTikTokHostUrl,
    tiktokVideoKey: tiktokVideoKey,
    tiktokVideoPermalinkForPlayer: tiktokVideoPermalinkForPlayer,
    queuedTikTokVideoEntry: queuedTikTokVideoEntry,
    isXHostUrl: isXHostUrl,
    xStatusKey: xStatusKey,
    xStatusPermalinkForPlayer: xStatusPermalinkForPlayer,
    queuedXStatusEntry: queuedXStatusEntry,
    isDirectStaticMediaEntry: isDirectStaticMediaEntry,
    playerTargetSuppressionReason: playerTargetSuppressionReason,
    MIN_PLAYER_WIDTH: MIN_PLAYER_WIDTH,
    MIN_PLAYER_HEIGHT: MIN_PLAYER_HEIGHT,
  };

  if (typeof globalThis !== "undefined") {
    globalThis.MagnetoclipPlayerOverlay = api;
  }

  // Auto-install only in a real content-script environment.
  if (
    typeof document !== "undefined" &&
    typeof window !== "undefined" &&
    typeof chrome !== "undefined" &&
    chrome.runtime &&
    chrome.runtime.sendMessage
  ) {
    const controller = create();
    if (controller && !window.__magnetoclipPlayerController) {
      window.__magnetoclipPlayerController = controller;
      controller.start();
    }
  }
})();