# Bundled ffmpeg

## Why the app ships its own ffmpeg

The video preview falls back to transcoding when the machine cannot decode a
codec. The common case is AV1: Qt's bundled FFmpeg (`avcodec-61.dll`, used by
`QMediaPlayer`) exposes the native `av1` decoder only behind a D3D11 hardware
path, so on a machine with no hardware AV1 support the file yields audio and no
frames. See `tests/media/test_preview_proxy.py`.

That fallback needs ffmpeg. Requiring users to install ffmpeg themselves would
mean previews silently failing on most machines, so the app vendors one.

## Why the LGPL build

`pyproject.toml` declares `license = { text = "Proprietary" }`. The obvious
Windows builds — including the one on a typical developer machine — are
**GPL** builds, configured `--enable-gpl --enable-libx264`. Shipping a GPL
binary inside a proprietary distribution imposes GPL on the combined work, so
those builds are not an option.

The vendored build is BtbN/FFmpeg-Builds `ffmpeg-n8.1-latest-win64-lgpl-8.1.zip`:

- LGPL 2.1+, no `--enable-gpl`, no `--enable-nonfree` (verified at fetch time by
  the absence of those flags in `ffmpeg -version`).
- Decodes AV1 via `libdav1d` (BSD-2-Clause), so the AV1 fallback works.
- Encodes H.264 via `libopenh264` (BSD-2-Clause, from Cisco).
- Has no `libx264`, which is exactly what keeps it out of GPL.

MagnetoClip invokes ffmpeg as a separate executable over `subprocess` and never
links against it, which is the arrangement LGPL explicitly permits. The practical
obligations for redistributing an LGPL binary this way:

1. Ship the ffmpeg license text with the app. `scripts/fetch_ffmpeg.py`
   downloads it to `docs/FFMPEG_LICENSE.md`; it is included in `resources/docs`,
   which the spec already packages.
2. Keep the LGPL components replaceable: ffmpeg is a standalone binary the user
   can delete or point elsewhere, and the `streaming.ffmpeg_path` setting plus
   `FFmpegLocator` already let a user override it. Nothing links into it, so
   swapping it does not require relinking MagnetoClip.
3. Note that the GPL path is still available to users who want it: a machine
   with a GPL ffmpeg on `PATH` keeps using it, because `FFmpegLocator` resolves
   configured path → bundled → `PATH` and the encoder selection picks up
   `libx264` automatically. Note the order is *bundled before PATH*, so the
   bundled copy wins by default; a user wanting the system one sets
   `streaming.ffmpeg_path`.

**This is an engineering assessment, not legal advice.** If the licensing terms
matter commercially, have counsel confirm the LGPL obligations before release.

## Only `ffmpeg.exe` is bundled

`ffprobe.exe` is another ~128 MB and is deliberately left out. Codec detection
has two independent paths:

1. `ffprobe`, when present, identifies the codec before playback.
2. The no-frames watchdog in `PreviewDialog`, which fires when a video track
   produces no frames shortly after playback starts.

The watchdog is the fallback, so previews still work with `ffprobe` absent —
the file plays, yields no frames, and the proxy is built. Dropping `ffprobe`
roughly halves the bundled payload.

## Encoder selection

`preview_encoders()` reads `ffmpeg -encoders` once per binary path and picks the
best encoder that build actually has, so the GPL encoder is never attempted on a
build that lacks it (and vice versa):

| encoder | license | quality | used by |
|---|---|---|---|
| `libx264` | GPL | best | user-supplied GPL ffmpeg |
| `libopenh264` | BSD | good | the bundled LGPL build |
| `mpeg4` | native | adequate | last resort, present in every build |

## Size

`ffmpeg.exe` is ~128 MB raw and ~38 MB LZMA-compressed, so the installer grows
from roughly 18 MB to roughly 60 MB. A future option is trimming a custom
ffmpeg build, which would need an MSYS2/mingw toolchain; that is not wired up
here.

## Reproducing

```powershell
python scripts/fetch_ffmpeg.py
python scripts/build.py --no-zip
```

The binary is not committed (see `.gitignore`); the fetch script is pinned to a
fixed URL and verified against a SHA-256 digest, so a swapped or truncated
artifact fails the build rather than shipping.
