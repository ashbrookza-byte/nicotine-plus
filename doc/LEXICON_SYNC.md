# Lexicon DJ Sync

Links Nicotine+ Download Lists to [Lexicon DJ](https://www.lexicondj.com/) via its
[Local API](https://www.lexicondj.com/docs/developers/api). Implemented in
`pynicotine/lexiconsync.py`, configured from **Wishlists → Wishlist Settings → Lexicon DJ sync**.

## What it does

1. **Smartlist mirroring** — every download list (including ones auto-created by the
   Spotify playlist watcher) gets a matching smartlist in Lexicon, inside a playlist
   folder (default: `nicotine`), with the rule *file location contains the list's
   download folder*. Lexicon keeps the smartlist's contents current on its own.
2. **Auto-import** — every finished download is added to the Lexicon library
   immediately, so the smartlists actually fill up without a manual import.
3. **Duplicate replacement** — after each import, the library is checked for another
   version of the same song (same artist + title, ignoring `(Extended Mix)`-style
   qualifiers). The preferred version is kept:
   - a **significantly longer** version wins first (extended mix beats radio edit),
   - then **lossless / higher bitrate** wins (FLAC replaces an MP3 of the same song).

   The losing version is removed from the Lexicon *library only* — never deleted from
   disk — after its playlist placements are moved to the winner and its
   rating/energy/color/tags are copied across. Cue points are not copied (they would
   not line up between different-length versions). Ties keep the existing track.

Each behavior has its own toggle in Wishlist Settings.

## Requirements

- Lexicon with the Local API enabled: **Lexicon → Settings → Integrations → Local API**.
- Lexicon does *not* need to be running all the time: pending work (new lists,
  finished downloads) is queued in `lexicon_sync.json` in the Nicotine+ data folder
  and retried every minute, so it lands whenever Lexicon is next open.
- Lists should use **"Save into a subfolder named after the list"** (per list, or the
  default in Wishlist Settings) — otherwise several lists share one folder and their
  location-based smartlists can't tell their files apart.

## Optional: sync while Nicotine+ is closed

A single sync pass can be run without the app:

```bash
python3 -m pynicotine.lexiconsync
```

To have macOS run that automatically every 5 minutes, save this as
`~/Library/LaunchAgents/org.nicotine-plus.lexicon-sync.plist` (adjust the repo path),
then run `launchctl load ~/Library/LaunchAgents/org.nicotine-plus.lexicon-sync.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>org.nicotine-plus.lexicon-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>-m</string>
        <string>pynicotine.lexiconsync</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/ashbrook/Documents/repos/nicotine-plus</string>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
```

This is a safety net, not a requirement — the in-app sync alone already covers
everything as long as Nicotine+ gets opened now and then.
