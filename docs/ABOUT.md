# About MagnetoClip

**Capture the Web.**

MagnetoClip is an advanced download manager for Windows that turns the chaos of
web downloads into an organized, one-click experience. Files, videos, music,
documents, and torrents — MagnetoClip detects them as you browse, captures them
reliably, and files them into smart, auto-sorted categories. It is developed
and maintained by **Devallix**.

This document is the source text for the "About" page of the MagnetoClip
website.

---

## What is MagnetoClip?

MagnetoClip is a Windows 10/11 desktop application that does three things
especially well:

1. **It finds downloadable content for you.** A companion browser extension
   watches the pages you visit and flags downloadable files, video and audio
   streams, and media you may not have noticed.
2. **It downloads anything, reliably.** Whether it is a single file, a
   segmented multi-connection download, a live HLS/DASH stream, or a torrent,
   MagnetoClip fetches it with parallel connections, resumes interrupted
   transfers, and verifies finished files.
3. **It keeps you organized.** Every finished download is auto-sorted into a
   category — Image, Video, Audio, Document, Compressed, App/Software, Torrent,
   or Other — so your download folder stays tidy without any effort.

Its tagline, *"Capture the Web"*, sums up the idea in three words: whatever is
on the web, MagnetoClip can bring it home.

---

## Features

### Downloads that just work

- **Multi-connection engine** — each file is split into parts fetched in
  parallel (up to 64 connections per download).
- **Resume & retry** — interrupted jobs pick up where they stopped
  (`.mclip` sidecar files) and transient failures retry with backoff.
- **Integrity checking** — a finished file is verified before it is marked
  *Completed*.
- **Speed control** — global and per-download bandwidth caps, plus a live speed
  monitor with an animated performance dashboard.
- **Pause with auto-resume** — one switch pauses every running and waiting
  download instantly; an optional countdown timer resumes them automatically.
- **Built-in speed test** — the Speed page measures download, upload and
  latency, keeps a history, identifies your ISP and flags throttling.

### Browser companion extension

- **Works with Chrome, Edge, Firefox, Brave, Vivaldi, and Chromium.**
- **Detects** files, streamed media, and other downloadables as you browse.
- **Resolves `blob:` URLs** — grabs the real bytes of embedded videos and
  images straight from the open tab.
- **Captures streaming media** from dozens of popular video and music sites,
  plus raw HLS (`.m3u8`) and DASH (`.mpd`) streams, merged into a single
  playable file with bundled FFmpeg tooling.
- **Can become your default downloader**, intercepting every browser download
  and routing it through MagnetoClip instead.

### Full torrent manager

- **Magnet links and `.torrent` files** — add either, or register MagnetoClip
  to open them system-wide.
- **Modern engine** — DHT, PEX, protocol encryption, per-torrent and global
  throttles, configurable listen port and limits.
- **Sequential download** for near-instant preview playback, and an optional
  auto-seed mode.
- **Live status** — progress, speed, peers/seeds, ETA, and a detailed file
  list with a built-in torrent preview (info hash, trackers, files, sizes).

### Everything else you'd expect

- **Duplicate detection** — MagnetoClip indexes finished files and warns you
  before you download the same thing twice.
- **File preview** — images, video, audio, PDFs, text, and torrents right from
  the Downloads page.
- **Archiver** — a built-in tool for archiving web content.
- **Remote dashboard** — watch and control downloads from your phone or another
  computer on your local network (protected by a pairing token; it never
  exposes your filesystem).
- **Proxy profiles** — route individual downloads through `http`, `https`, or
  `socks5` proxies, per-file or as a default.
- **System tray** — pause/resume everything, open the remote dashboard, and
  jump to finished files without opening the main window.

---

## Built with privacy in mind

MagnetoClip is a **local-first** application:

- Your download history, analytics, and settings live in a local database on
  your own computer.
- **No telemetry.** Nothing about what you download is uploaded anywhere.
- Your serial key and proxy passwords are stored in the **Windows Credential
  Manager**, never in plain files.
- The **Analytics** dashboard is computed locally from your own history.

See the Privacy Policy for full details.

---

## About the developer

**Devallix** is the developer behind MagnetoClip — a self-taught software
developer with two years of hands-on experience building desktop applications,
download technology, and tools that people use every day.

MagnetoClip is the product of that journey: a single, focused tool designed
around one conviction — that managing web downloads should be fast, reliable,
and entirely under your control. Every feature in MagnetoClip was built and
tested with the same care that goes into software the developer uses
personally.

Devallix personally maintains MagnetoClip, plans its roadmap, and responds to
user feedback.

---

## A note on usage

MagnetoClip is a general-purpose download tool. It is designed to help you save
content you are legally entitled to download — your own files, public-domain
material, licensed content you own, and media offered for downloading by their
owners. As with any download tool, the responsibility for the content you
download rests with you. Please respect copyright and applicable laws, and see
the Terms of Service for acceptable use.

---

*Developed by Devallix · "Capture the Web" · MagnetoClip v0.2.7*

*See also: [Terms of Service](TERMS_OF_SERVICE.md) · [Privacy Policy](PRIVACY_POLICY.md) · [End User License Agreement](EULA.md)*