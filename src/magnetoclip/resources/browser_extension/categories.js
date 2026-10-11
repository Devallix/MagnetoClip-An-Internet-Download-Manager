// categories.js — the single place where a detected resource is classified.
//
// Every detector in the extension (network/webRequest, page/DOM scan, Telegram
// redirect + proxy resolution, blob media, context menu, download interception)
// funnels its findings through ``classify()`` so a resource always lands in
// exactly one of the six user-facing categories and the badge, the popup and
// MagnetoClip's Detected page agree on the name.
//
// Classification order (strongest signal first):
//   1. explicit detection type (image/video/audio/hls/dash/stream/...)
//   2. HLS/DASH transport indicators (m3u8/mpd + streaming MIME types)
//   3. MIME type
//   4. URL extension (including ``?format=`` hints on extensionless CDNs)
//   5. Content-Disposition filename
//   6. request context (resource type + path hints) for extensionless media
//   7. fallback: other
//
// The file is a plain script (no ESM) so it can be loaded via ``importScripts``
// (Chromium service worker), the Firefox ``background.scripts`` array, the
// content script list, and the popup page.

const MagnetoclipCategories = (function () {
  "use strict";

  const CATEGORIES = [
    "images",
    "videos",
    "audio",
    "compressed",
    "documents",
    "other",
  ];
  const DEFAULT_CATEGORY = "other";

  const CATEGORY_LABELS = {
    images: "Images",
    videos: "Videos",
    audio: "Audio",
    compressed: "Compressed",
    documents: "Documents",
    other: "Other",
  };

  // The app stores ``detected_type`` in its own vocabulary (icons, sidebar
  // categories, download rows). Mapping lives here so the extension and the
  // application never disagree about what a category means.
  const CATEGORY_TO_TYPE = {
    images: "image",
    videos: "video",
    audio: "audio",
    compressed: "archive",
    documents: "document",
    other: "other",
  };
  const TYPE_TO_CATEGORY = {
    image: "images",
    video: "videos",
    audio: "audio",
    archive: "compressed",
    compressed: "compressed",
    document: "documents",
    other: "other",
  };

  // --- extension tables -----------------------------------------------------

  const IMAGE_EXTS = [
    "jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg", "ico",
    "heic", "heif", "tiff", "tif", "jfif", "apng",
  ];
  const VIDEO_EXTS = [
    "mp4", "webm", "mkv", "mov", "avi", "m4v", "3gp", "flv", "ts", "mpg",
    "mpeg", "ogv", "wmv", "asf", "rmvb", "m2ts", "mts", "vob", "divx",
  ];
  const AUDIO_EXTS = [
    "mp3", "m4a", "wav", "flac", "ogg", "oga", "opus", "aac", "wma", "aiff",
    "aif", "ape", "mid", "midi", "mka", "amr", "alac",
  ];
  // Archives only. Installers and disk images (exe/msi/dmg/deb/rpm/apk) are
  // deliberately NOT compressed: they belong to the "other" bucket, which is
  // exactly what the six-category model promises for anything unlisted.
  const COMPRESSED_EXTS = [
    "zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "tbz", "tbz2", "xz", "txz",
    "zst", "lz", "lzma", "lzh", "lha", "z", "cab", "arj", "jar", "war", "ear",
    "crx", "xpi",
  ];
  const DOCUMENT_EXTS = [
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtf",
    "odt", "ods", "odp", "epub", "mobi", "azw3", "md", "json", "xml", "html",
    "htm",
  ];
  // HLS/DASH transport files are never downloadable on their own; they are
  // classified as videos so the app resolves the stream instead of saving a
  // playlist.
  const STREAMING_EXTS = ["m3u8", "m3u", "mpd"];

  const EXTENSION_CATEGORIES = {};
  function registerExtensions(list, category) {
    for (const ext of list) {
      // Multi-part names such as "tar.gz" win over their last component, so a
      // ".tar.gz" URL is compressed even though ".gz" is also compressed.
      EXTENSION_CATEGORIES[ext] = category;
    }
  }
  registerExtensions(IMAGE_EXTS, "images");
  registerExtensions(VIDEO_EXTS, "videos");
  registerExtensions(AUDIO_EXTS, "audio");
  registerExtensions(COMPRESSED_EXTS, "compressed");
  registerExtensions(DOCUMENT_EXTS, "documents");
  registerExtensions(STREAMING_EXTS, "videos");

  const MIME_CATEGORIES = {
    // images
    "image/jpeg": "images",
    "image/pjpeg": "images",
    "image/png": "images",
    "image/apng": "images",
    "image/gif": "images",
    "image/webp": "images",
    "image/avif": "images",
    "image/bmp": "images",
    "image/x-ms-bmp": "images",
    "image/svg+xml": "images",
    "image/x-icon": "images",
    "image/vnd.microsoft.icon": "images",
    "image/heic": "images",
    "image/heif": "images",
    "image/tiff": "images",
    // video
    "video/mp4": "videos",
    "video/webm": "videos",
    "video/quicktime": "videos",
    "video/x-matroska": "videos",
    "video/x-msvideo": "videos",
    "video/mp2t": "videos",
    "video/3gpp": "videos",
    "video/x-flv": "videos",
    "video/ogg": "videos",
    "video/avi": "videos",
    "video/mpeg": "videos",
    // audio
    "audio/mpeg": "audio",
    "audio/mp4": "audio",
    "audio/aac": "audio",
    "audio/x-aac": "audio",
    "audio/wav": "audio",
    "audio/x-wav": "audio",
    "audio/wave": "audio",
    "audio/webm": "audio",
    "audio/ogg": "audio",
    "audio/opus": "audio",
    "audio/flac": "audio",
    "audio/x-flac": "audio",
    "audio/amr": "audio",
    "audio/midi": "audio",
    // compressed / installers
    "application/zip": "compressed",
    "application/x-zip-compressed": "compressed",
    "application/x-rar-compressed": "compressed",
    "application/vnd.rar": "compressed",
    "application/x-7z-compressed": "compressed",
    "application/x-tar": "compressed",
    "application/gzip": "compressed",
    "application/x-gzip": "compressed",
    "application/x-bzip2": "compressed",
    "application/x-xz": "compressed",
    "application/zstd": "compressed",
    "application/x-iso9660-image": "compressed",
    "application/vnd.android.package-archive": "compressed",
    "application/x-msdownload": "compressed",
    "application/vnd.microsoft.portable-executable": "compressed",
    "application/java-archive": "compressed",
    // documents
    "application/pdf": "documents",
    "application/msword": "documents",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "documents",
    "application/vnd.ms-excel": "documents",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "documents",
    "application/vnd.ms-powerpoint": "documents",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "documents",
    "application/vnd.oasis.opendocument.text": "documents",
    "application/vnd.oasis.opendocument.spreadsheet": "documents",
    "application/vnd.oasis.opendocument.presentation": "documents",
    "application/rtf": "documents",
    "application/epub+zip": "documents",
    "application/x-mobipocket-ebook": "documents",
    "text/plain": "documents",
    "text/csv": "documents",
    "text/markdown": "documents",
    "text/rtf": "documents",
  };

  // Streaming transports. Both HLS and DASH are videos by definition; there is
  // no HLS/DASH prompt anywhere in the product.
  const STREAMING_MIMES = [
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/mpegurl",
    "application/mp2t",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "application/dash+xml",
  ];

  // Detection types the detectors report before classification. ``file`` and
  // ``unknown`` are "no opinion" and fall through to the weaker signals.
  const DETECTED_TYPE_CATEGORIES = {
    image: "images",
    images: "images",
    photo: "images",
    picture: "images",
    video: "videos",
    videos: "videos",
    movie: "videos",
    hls: "videos",
    dash: "videos",
    stream: "videos",
    m3u8: "videos",
    mpd: "videos",
    audio: "audio",
    sound: "audio",
    music: "audio",
    voice: "audio",
    archive: "compressed",
    compressed: "compressed",
    zip: "compressed",
    document: "documents",
    documents: "documents",
    ebook: "documents",
    text: "documents",
    subtitle: "documents",
    other: "other",
    // Software/torrent/webpage have no category of their own; they are "other"
    // categories, but the app vocabulary keeps the finer label for the Detected
    // page's Type column.
    software: "other",
    torrent: "other",
    webpage: "other",
    // NOTE: "file" and "unknown" are deliberately absent. They mean "no
    // opinion", so classification must fall through to MIME/extension/context
    // instead of short-circuiting to "other".
  };

  // Application-level ``detected_type`` vocabulary, richer than the six
  // categories. Used for the "other" bucket so the app still shows
  // "App / Software" or "Webpage" instead of a bare "other".
  const APP_TYPES = {
    image: "image",
    video: "video",
    audio: "audio",
    archive: "archive",
    document: "document",
    ebook: "document",
    subtitle: "subtitle",
    font: "font",
    software: "software",
    torrent: "torrent",
    webpage: "webpage",
    other: "other",
  };

  // --- primitives -----------------------------------------------------------

  function normalizeMime(mime) {
    return String(mime || "").split(";", 1)[0].trim().toLowerCase();
  }

  function read(input, ...keys) {
    if (!input) {
      return "";
    }
    for (const key of keys) {
      const value = input[key];
      if (value !== undefined && value !== null && value !== "") {
        return value;
      }
    }
    return "";
  }

  // File extension of a URL, honouring the ``?format=jpg`` convention used by
  // extensionless CDNs (Twitter, Telegram, Cloudinary...).
  function extensionOf(url) {
    const raw = String(url || "");
    if (!raw) {
      return "";
    }
    const clean = raw.split(/[?#]/)[0];
    const match = /\.([a-z0-9]{1,12})$/i.exec(clean);
    if (match) {
      return match[1].toLowerCase();
    }
    const format = /[?&](?:format|ext|f|type)=([a-z0-9]{1,12})/i.exec(raw);
    return format ? format[1].toLowerCase() : "";
  }

  // Extension of a bare filename (e.g. from Content-Disposition). Handles
  // multi-part archives ("archive.tar.gz").
  function filenameExtension(name) {
    const clean = String(name || "").split(/[?#]/)[0].trim();
    if (!clean) {
      return "";
    }
    const base = clean.substring(clean.lastIndexOf("/") + 1);
    const multi = /\.(tar\.(?:gz|bz2|xz|zst))$/i.exec(base);
    if (multi) {
      return multi[1].toLowerCase();
    }
    const match = /\.([a-z0-9]{1,12})$/i.exec(base);
    return match ? match[1].toLowerCase() : "";
  }

  // Basename of a URL path, without inventing an extension.
  function basename(url) {
    const clean = String(url || "").split(/[?#]/)[0];
    if (!clean) {
      return "";
    }
    const name = clean.substring(clean.lastIndexOf("/") + 1);
    if (!name) {
      return "";
    }
    try {
      return decodeURIComponent(name);
    } catch (error) {
      return name;
    }
  }

  function isStreamingMime(mime) {
    return STREAMING_MIMES.indexOf(normalizeMime(mime)) !== -1;
  }

  function isStreamingUrl(url) {
    return STREAMING_EXTS.indexOf(extensionOf(url)) !== -1;
  }

  function categoryForExtension(ext) {
    if (!ext) {
      return "";
    }
    return EXTENSION_CATEGORIES[ext] || "";
  }

  function categoryForMime(mime) {
    const value = normalizeMime(mime);
    if (!value) {
      return "";
    }
    if (isStreamingMime(value)) {
      return "videos";
    }
    const exact = MIME_CATEGORIES[value];
    if (exact) {
      return exact;
    }
    // image/* / video/* / audio/* families.
    if (value.indexOf("image/") === 0) {
      return "images";
    }
    if (value.indexOf("video/") === 0) {
      return "videos";
    }
    if (value.indexOf("audio/") === 0) {
      return "audio";
    }
    return "";
  }

  // Step 6: extensionless resources whose request context reveals the kind.
  function categoryFromRequestContext(url, requestType) {
    const type = String(requestType || "").toLowerCase();
    const path = String(url || "").split(/[?#]/)[0].toLowerCase();
    if (!path) {
      return "";
    }
    const looksLikeAudio = /(^|[\/_.-])(audio|audios|sound|voice|music|track)([\/_.-]|$)/.test(path);
    const looksLikeVideo = /(^|[\/_.-])(video|videos|movie|movies|clip|stream)([\/_.-]|$)/.test(path);
    if (type === "media" || type === "object") {
      if (looksLikeAudio && !looksLikeVideo) {
        return "audio";
      }
      if (looksLikeVideo) {
        return "videos";
      }
      return "";
    }
    if (type === "xmlhttprequest" && (looksLikeAudio || looksLikeVideo)) {
      return looksLikeAudio && !looksLikeVideo ? "audio" : "videos";
    }
    return "";
  }

  /**
   * Classify one detection. Returns a key of ``CATEGORIES`` (never null).
   *
   * @param {{url?: string, filename?: string, mimeType?: string,
   *   mime_type?: string, detectedType?: string, detected_type?: string,
   *   mediaKind?: string, contentDisposition?: string, requestType?: string,
   *   type?: string, width?: number, height?: number}} input
   * @returns {"images"|"videos"|"audio"|"compressed"|"documents"|"other"}
   */
  function classify(input) {
    const url = read(input, "url");
    const filename = read(input, "filename");
    const mime = read(input, "mimeType", "mime_type", "contentType", "content_type");
    const disposition = read(input, "contentDisposition", "content_disposition");
    const requestType = read(input, "requestType", "type", "request_type");

    // 1. explicit detection type / media kind reported by the detector.
    const explicit = String(
      read(input, "detectedType", "detected_type", "mediaKind", "media_kind", "kind")
    )
      .trim()
      .toLowerCase();
    if (explicit) {
      const mapped = DETECTED_TYPE_CATEGORIES[explicit];
      if (mapped) {
        return mapped;
      }
    }

    // 2. HLS/DASH: always a video, never its own category.
    if (isStreamingMime(mime) || isStreamingUrl(url) || isStreamingUrl(filename)) {
      return "videos";
    }

    // 3. MIME type.
    const byMime = categoryForMime(mime);
    if (byMime) {
      return byMime;
    }

    // 4. URL extension.
    const byUrl = categoryForExtension(extensionOf(url));
    if (byUrl) {
      return byUrl;
    }

    // 5. Content-Disposition filename.
    const dispositionName = filename || dispositionFilename(disposition);
    const byName = categoryForExtension(filenameExtension(dispositionName));
    if (byName) {
      return byName;
    }
    const byUrlBase = categoryForExtension(filenameExtension(basename(url)));
    if (byUrlBase) {
      return byUrlBase;
    }

    // 6. request context for extensionless media.
    const byContext = categoryFromRequestContext(url, requestType);
    if (byContext) {
      return byContext;
    }

    // 7. fallback.
    return DEFAULT_CATEGORY;
  }

  function dispositionFilename(disposition) {
    const match = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(String(disposition || ""));
    if (!match) {
      return "";
    }
    const raw = match[1].replace(/^"|"$/g, "");
    try {
      return decodeURIComponent(raw);
    } catch (error) {
      return raw;
    }
  }

  /**
   * Classification plus the derived application-level ``detected_type``.
   * Returns ``{category, detected_type, streaming}``.
   */
  function describe(input) {
    const category = classify(input);
    let type = CATEGORY_TO_TYPE[category] || CATEGORY_TO_TYPE[DEFAULT_CATEGORY];
    if (category === DEFAULT_CATEGORY) {
      // Keep a finer label than "other" when the detector already named one.
      const explicit = String(
        read(input, "detectedType", "detected_type", "mediaKind", "media_kind", "kind")
      )
        .trim()
        .toLowerCase();
      if (APP_TYPES[explicit]) {
        type = APP_TYPES[explicit];
      }
    }
    return {
      category: category,
      detected_type: type,
      streaming:
        category === "videos" &&
        (isStreamingMime(read(input, "mimeType", "mime_type", "contentType")) ||
          isStreamingUrl(read(input, "url"))),
    };
  }

  function typeForCategory(category) {
    return CATEGORY_TO_TYPE[category] || CATEGORY_TO_TYPE[DEFAULT_CATEGORY];
  }

  function categoryForType(detectedType) {
    return TYPE_TO_CATEGORY[String(detectedType || "").toLowerCase()] || DEFAULT_CATEGORY;
  }

  function isCategory(value) {
    return CATEGORIES.indexOf(String(value || "").toLowerCase()) !== -1;
  }

  function normalizeCategory(value) {
    const raw = String(value || "").trim().toLowerCase();
    if (!raw || raw === "all") {
      return "";
    }
    if (isCategory(raw)) {
      return raw;
    }
    // Tolerate singular spellings from any caller ("video" -> "videos").
    if (CATEGORIES.indexOf(raw + "s") !== -1) {
      return raw + "s";
    }
    return TYPE_TO_CATEGORY[raw] || "";
  }

  // --- deduplication --------------------------------------------------------

  /**
   * Deduplication key for a detection URL.
   *
   * Only normalisation that cannot merge two distinct resources is applied:
   * the fragment is dropped, the host is lower-cased and default ports removed.
   * The query string is preserved on purpose — signed/tokenised URLs (HLS
   * manifests, CDN media) differ *only* by their query, so stripping parameters
   * would collapse different files into one queue entry.
   */
  function normalizeUrl(url) {
    const raw = String(url || "").trim();
    if (!raw) {
      return "";
    }
    if (!/^https?:\/\//i.test(raw)) {
      // blob:/data:/extensionless-relative URLs are opaque: compare verbatim.
      return raw.split("#")[0];
    }
    try {
      const parsed = new URL(raw);
      parsed.hash = "";
      parsed.hostname = parsed.hostname.toLowerCase();
      if (
        (parsed.protocol === "http:" && parsed.port === "80") ||
        (parsed.protocol === "https:" && parsed.port === "443")
      ) {
        parsed.port = "";
      }
      return parsed.toString();
    } catch (error) {
      return raw.split("#")[0];
    }
  }

  // --- image noise ----------------------------------------------------------

  // Tracking pixels, spacers, favicons and decorative chrome are technically
  // downloadable but are never what a user means by "download this image", and
  // they would swamp the Images count. The filter is deliberately narrow: real
  // photos, screenshots and artwork keep very different names/paths.
  const IMAGE_NOISE_PATTERNS = [
    /\/(?:pixel|beacon|track|tracking|analytics|measure|spacer|blank|1x1|clear)\b/i,
    /\/(?:favicon|apple-touch-icon|site-webmanifest|manifest)\b/i,
    /\b(?:pixel|beacon|spacer|blank|transparent|loader|placeholder|1x1|dot|clear)\b/i,
    /[?&](?:width|height|w|h)=([1-9]|[1-9]\d)(?:[^0-9]|$)/i,
  ];
  const IMAGE_NOISE_NAMES = /^(?:favicon|spacer|blank|transparent|pixel|px|1x1|clear|loader|placeholder|dot|bullet|ui|track|beacon)[-_.]/i;

  /**
   * True when an image detection is almost certainly page chrome rather than a
   * file the user wants. ``width``/``height`` come from the DOM when known.
   */
  function isImageNoise(input) {
    const url = read(input, "url");
    const filename = read(input, "filename");
    const name = filename || basename(url);
    if (IMAGE_NOISE_NAMES.test(name)) {
      return true;
    }
    for (const pattern of IMAGE_NOISE_PATTERNS) {
      if (pattern.test(url) || (filename && pattern.test(filename))) {
        return true;
      }
    }
    // Explicitly tiny images: 1x1 trackers, 2px sprites, rule lines.
    const width = Number(read(input, "width"));
    const height = Number(read(input, "height"));
    const sized =
      Number.isFinite(width) && width > 0 && Number.isFinite(height) && height > 0;
    if (sized && (width <= 8 || height <= 8)) {
      return true;
    }
    if (Number.isFinite(width) && width > 0 && width <= 2) {
      return true;
    }
    if (Number.isFinite(height) && height > 0 && height <= 2) {
      return true;
    }
    return false;
  }

  // Site chrome disguised as media: the browser can *hear* these, but they are
  // UI furniture, not content the user asked to download. YouTube boots audio
  // files for its search field sounds; they show up as media captures and would
  // otherwise pollute both the queue and the player-overlay hints.
  const UI_CHROME_PATTERNS = [
    /\/s\/search\/audio\//i,
  ];

  function isUiChromeUrl(input) {
    const url = read(input, "url");
    for (const pattern of UI_CHROME_PATTERNS) {
      if (pattern.test(url)) {
        return true;
      }
    }
    return false;
  }

  // --- queue entries --------------------------------------------------------

  let sequence = 0;

  function makeId(timestamp) {
    sequence = (sequence + 1) % 0xffffff;
    return "d" + Number(timestamp || 0).toString(36) + sequence.toString(36);
  }

  /**
   * Build a persistent queue entry from a raw detection. The entry shape is the
   * one MagnetoClip stores, so it can be synced to the app unchanged.
   */
  function buildDetection(input, now) {
    const source = input || {};
    const url = read(source, "url");
    const timestamp = Number.isFinite(now) ? now : Date.now();
    const described = describe(source);
    const entry = {
      id: source.id ? String(source.id) : makeId(timestamp),
      url: url,
      filename: read(source, "filename") || basename(url),
      category: described.category,
      detected_type: described.detected_type,
      mime_type: normalizeMime(read(source, "mimeType", "mime_type")),
      page_url: read(source, "pageUrl", "page_url"),
      page_title: read(source, "pageTitle", "page_title", "title"),
      source: read(source, "source") || "network_capture",
      timestamp: timestamp,
      request_context: source.request_context || read(source, "requestContext") || null,
    };
    const cookies = read(source, "cookies");
    if (cookies) {
      entry.cookies = cookies;
    }
    const headers = source.headers || read(source, "request_headers");
    if (headers && typeof headers === "object" && Object.keys(headers).length) {
      entry.headers = headers;
    }
    const referrer = read(source, "referrer");
    if (referrer) {
      entry.referrer = referrer;
    }
    const frameUrl = read(source, "frameUrl", "frame_url");
    if (frameUrl) {
      entry.frame_url = frameUrl;
    }
    const userAgent = read(source, "userAgent", "user_agent");
    if (userAgent) {
      entry.user_agent = userAgent;
    }
    const dataBase64 = read(source, "dataBase64", "data_base64");
    if (dataBase64) {
      entry.data_base64 = dataBase64;
    }
    const width = Number(read(source, "width"));
    const height = Number(read(source, "height"));
    if (Number.isFinite(width) && width > 0) {
      entry.width = width;
    }
    if (Number.isFinite(height) && height > 0) {
      entry.height = height;
    }
    return entry;
  }

  /** Per-category counts for a list of queue entries. */
  function countsByCategory(entries) {
    const counts = {};
    for (const category of CATEGORIES) {
      counts[category] = 0;
    }
    for (const entry of entries || []) {
      if (!entry) {
        continue;
      }
      const category = isCategory(entry.category)
        ? entry.category
        : categoryForType(entry.detected_type);
      counts[category] = (counts[category] || 0) + 1;
    }
    return counts;
  }

  return {
    CATEGORIES: CATEGORIES,
    CATEGORY_LABELS: CATEGORY_LABELS,
    CATEGORY_TO_TYPE: CATEGORY_TO_TYPE,
    TYPE_TO_CATEGORY: TYPE_TO_CATEGORY,
    DEFAULT_CATEGORY: DEFAULT_CATEGORY,
    IMAGE_EXTS: IMAGE_EXTS,
    VIDEO_EXTS: VIDEO_EXTS,
    AUDIO_EXTS: AUDIO_EXTS,
    COMPRESSED_EXTS: COMPRESSED_EXTS,
    DOCUMENT_EXTS: DOCUMENT_EXTS,
    STREAMING_EXTS: STREAMING_EXTS,
    classify: classify,
    describe: describe,
    typeForCategory: typeForCategory,
    categoryForType: categoryForType,
    isCategory: isCategory,
    normalizeCategory: normalizeCategory,
    normalizeMime: normalizeMime,
    normalizeUrl: normalizeUrl,
    extensionOf: extensionOf,
    filenameExtension: filenameExtension,
    basename: basename,
    dispositionFilename: dispositionFilename,
    isStreamingMime: isStreamingMime,
    isStreamingUrl: isStreamingUrl,
    isImageNoise: isImageNoise,
    isUiChromeUrl: isUiChromeUrl,
    buildDetection: buildDetection,
    countsByCategory: countsByCategory,
  };
})();

if (typeof globalThis !== "undefined") {
  globalThis.MagnetoclipCategories = MagnetoclipCategories;
}
