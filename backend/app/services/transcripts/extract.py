"""YouTube transcript extraction: enumerate with yt-dlp, fetch with youtube-transcript-api.

- Works for playlist, channel (@handle or /channel/...) and single-video URLs.
- Resumable: videos already stored with status 'ok' are skipped.
- Polite: a delay between videos; stops early if YouTube blocks the IP (usually
  cloud/VPN IPs; home connections are normally fine).
- Stores timestamped segments in SQLite and exports {video_id}.md / .json to
  backend/data/transcripts/ (gitignored: the creator's copyrighted content,
  for personal research only).
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable

from app import config, db

DEFAULT_PLAYLIST = "https://www.youtube.com/playlist?list=PLVgHx4Z63paYiFGQ56PjTF1PGePL3r69s"  # 2022 ICT Mentorship
LANGUAGES = ["en", "en-US", "en-GB"]
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class TranscriptBlocked(RuntimeError):
    """YouTube is refusing this IP; continuing would only fail every video."""


def video_url(video_id: str, t: float | None = None) -> str:
    return f"https://www.youtube.com/watch?v={video_id}" + (f"&t={int(t)}s" if t else "")


def _normalise_url(url: str) -> str:
    url = url.strip()
    if VIDEO_ID.match(url):
        return video_url(url)
    # A bare channel URL lists tabs; the videos tab lists uploads.
    if re.search(r"youtube\.com/(@[^/?#]+|channel/[^/?#]+|c/[^/?#]+)/?$", url):
        return url.rstrip("/") + "/videos"
    return url


def enumerate_url(url: str) -> dict:
    import yt_dlp
    opts = {"extract_flat": "in_playlist", "quiet": True, "skip_download": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(_normalise_url(url), download=False)
    if info.get("_type") in {"playlist", "multi_video"}:
        entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
        kind = "playlist" if "list=" in url else "channel"
    else:
        entries, kind = [info], "video"
    return {
        "kind": kind, "id": info.get("id"), "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "videos": [{"video_id": e["id"], "title": e.get("title"), "duration_s": e.get("duration"),
                    "channel": e.get("channel") or e.get("uploader") or info.get("channel"),
                    "upload_date": e.get("upload_date")} for e in entries],
    }


def _save_video(meta: dict, playlist_id: str | None, status: str, *, error: str | None = None,
                language: str | None = None, segments: list[dict] | None = None, source: str = "youtube") -> None:
    with db.connect() as conn, db.tx(conn):
        conn.execute(
            "INSERT INTO videos(video_id, title, channel, upload_date, duration_s, playlist_id, url, status, error, "
            "language, segment_count, fetched_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(video_id) DO UPDATE SET title=COALESCE(excluded.title, videos.title), "
            "channel=COALESCE(excluded.channel, videos.channel), playlist_id=COALESCE(excluded.playlist_id, "
            "videos.playlist_id), status=excluded.status, error=excluded.error, "
            "language=COALESCE(excluded.language, videos.language), segment_count=excluded.segment_count, "
            "fetched_at=excluded.fetched_at, source=excluded.source",
            (meta["video_id"], meta.get("title"), meta.get("channel"), meta.get("upload_date"), meta.get("duration_s"),
             playlist_id, video_url(meta["video_id"]), status, error, language, len(segments or []), db.utc_now(),
             source))
        if segments is not None:
            conn.execute("DELETE FROM transcript_segments WHERE video_id = ?", (meta["video_id"],))
            conn.executemany(
                "INSERT INTO transcript_segments(video_id, idx, start, duration, text) VALUES (?,?,?,?,?)",
                [(meta["video_id"], i, s["start"], s["duration"], s["text"]) for i, s in enumerate(segments)])
    if segments:
        _export(meta, segments)


def _export(meta: dict, segments: list[dict]) -> None:
    folder = config.transcripts_dir()
    vid = meta["video_id"]
    (folder / f"{vid}.json").write_text(json.dumps({**meta, "url": video_url(vid), "segments": segments},
                                                   ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# {meta.get('title') or vid}", "", f"Source: {video_url(vid)}  ", f"Channel: {meta.get('channel')}", ""]
    minute = -1
    for s in segments:
        m = int(s["start"] // 60)
        if m != minute:  # a timestamp heading per minute keeps the file skimmable
            minute = m
            lines += ["", f"[**{m // 60:d}:{m % 60:02d}:00**]({video_url(vid, s['start'])})"]
        lines.append(s["text"])
    (folder / f"{vid}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def fetch_transcript(video_id: str, languages: list[str] | None = None) -> tuple[str, list[dict]]:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import IpBlocked, RequestBlocked
    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=languages or LANGUAGES)
    except (RequestBlocked, IpBlocked) as exc:
        raise TranscriptBlocked(str(exc).splitlines()[0]) from exc
    segments = [{"start": round(float(s.start), 2), "duration": round(float(s.duration), 2),
                 "text": " ".join(s.text.split())} for s in fetched.snippets if s.text.strip()]
    return fetched.language_code + (" (auto)" if fetched.is_generated else ""), segments


def stored_status() -> dict[str, str]:
    with db.connect() as conn:
        return {r["video_id"]: r["status"] for r in db.rows(conn, "SELECT video_id, status FROM videos")}


def pull(url: str, *, limit: int | None = None, delay: float = 1.5, languages: list[str] | None = None,
         force: bool = False, progress: Callable[[dict], None] | None = None) -> dict:
    listing = enumerate_url(url)
    playlist_id = listing["id"] if listing["kind"] == "playlist" else None
    videos = listing["videos"][:limit] if limit else listing["videos"]
    already = stored_status()
    stats = {"source": {k: listing[k] for k in ("kind", "id", "title", "channel")}, "total": len(videos),
             "done": 0, "ok": 0, "skipped": 0, "failed": 0, "blocked": False, "failures": []}
    for meta in videos:
        vid = meta["video_id"]
        if not force and already.get(vid) == "ok":
            stats["skipped"] += 1
        else:
            try:
                language, segments = fetch_transcript(vid, languages)
                _save_video(meta, playlist_id, "ok", language=language, segments=segments)
                stats["ok"] += 1
            except TranscriptBlocked as exc:
                _save_video(meta, playlist_id, "blocked", error=str(exc))
                stats.update(blocked=True, failed=stats["failed"] + 1)
                stats["failures"].append({"video_id": vid, "error": "blocked by YouTube: " + str(exc)})
                stats["done"] += 1
                if progress:
                    progress(stats)
                break
            except Exception as exc:  # TranscriptsDisabled, NoTranscriptFound, VideoUnavailable, network
                reason = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
                _save_video(meta, playlist_id, "failed", error=reason[:500])
                stats["failed"] += 1
                stats["failures"].append({"video_id": vid, "error": reason[:200]})
            if delay:
                time.sleep(delay)
        stats["done"] += 1
        if progress:
            progress(stats)
    return stats


# ------------------------------------------------------------------ imports
_SRT_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def _t(value: str) -> float:
    h, m, s, ms = _SRT_TIME.match(value.strip()).groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_import(content: str, fmt: str) -> list[dict]:
    """Parse a third-party export (srt | vtt | json | txt) into segments."""
    fmt = fmt.lower()
    if fmt == "json":
        data = json.loads(content)
        items = data.get("segments", data) if isinstance(data, dict) else data
        return [{"start": float(i.get("start", 0)), "duration": float(i.get("duration", i.get("dur", 0))),
                 "text": " ".join(str(i.get("text", "")).split())} for i in items if str(i.get("text", "")).strip()]
    if fmt in {"srt", "vtt"}:
        segments = []
        for block in re.split(r"\n\s*\n", content.replace("\r", "")):
            lines = [line for line in block.strip().splitlines() if line.strip()]
            timing = next((line for line in lines if "-->" in line), None)
            if not timing:
                continue
            start, end = (part.split(" ")[0] for part in timing.split("-->"))
            start_s, end_s = _t(start if start.count(":") == 2 else "00:" + start), \
                _t(end.strip() if end.strip().count(":") == 2 else "00:" + end.strip())
            text = " ".join(line for line in lines[lines.index(timing) + 1:])
            text = re.sub(r"<[^>]+>", "", text).strip()
            if text:
                segments.append({"start": round(start_s, 2), "duration": round(end_s - start_s, 2), "text": text})
        return segments
    # txt: no timestamps; ~12 words per segment, 4 s spacing so ordering and windows still work
    words = content.split()
    return [{"start": float(i // 12 * 4), "duration": 4.0, "text": " ".join(words[i:i + 12])}
            for i in range(0, len(words), 12)]


def import_transcript(video_id: str, content: str, fmt: str, *, title: str | None = None,
                      channel: str | None = None, source: str = "import") -> dict:
    if not VIDEO_ID.match(video_id):
        raise ValueError("video_id must be an 11-character YouTube id")
    segments = parse_import(content, fmt)
    if not segments:
        raise ValueError("No transcript text found in the upload.")
    _save_video({"video_id": video_id, "title": title, "channel": channel}, None, "ok", language=f"import:{fmt}",
                segments=segments, source=source)
    return {"video_id": video_id, "segments": len(segments)}
