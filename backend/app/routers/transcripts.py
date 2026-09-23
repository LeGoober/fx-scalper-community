"""Transcripts: pull from YouTube (playlist/channel/video), import third-party exports, browse and search."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app import db, jobs
from app.services.transcripts import extract

router = APIRouter(prefix="/api/transcripts", tags=["transcripts"])

FREE_FALLBACKS = [
    {"name": "youtube-transcript.io", "url": "https://www.youtube-transcript.io/bulk",
     "notes": "Bulk playlist/channel export (TXT, SRT, JSON)."},
    {"name": "youtubetranscript.pro", "url": "https://youtubetranscript.pro/",
     "notes": "Free video/playlist/channel export in TXT or JSON."},
    {"name": "gettranscript.app", "url": "https://gettranscript.app/", "notes": "TXT/SRT/VTT, no signup."},
    {"name": "ScrapeCreators free tool", "url": "https://scrapecreators.com/free-tools/get-youtube-channel-transcripts",
     "notes": "Up to 100 channel videos per run."},
]


class PullRequest(BaseModel):
    url: str = Field(extract.DEFAULT_PLAYLIST, description="Playlist, channel (@handle) or video URL / id")
    limit: int | None = Field(None, ge=1, le=5000)
    delay_s: float = Field(1.5, ge=0, le=30, description="Pause between videos (be polite to YouTube)")
    force: bool = Field(False, description="Re-fetch videos already stored")


class ImportRequest(BaseModel):
    video_id: str
    format: str = Field("srt", pattern="^(srt|vtt|json|txt)$")
    content: str = Field(min_length=1)
    title: str | None = None
    channel: str | None = None
    source: str = "import"


@router.get("/sources", summary="Default ICT playlist and free third-party fallbacks")
def sources() -> dict:
    return {"default_url": extract.DEFAULT_PLAYLIST, "default_title": "2022 ICT Mentorship (The Inner Circle Trader)",
            "fallbacks": FREE_FALLBACKS}


@router.post("/preview", summary="List the videos a URL resolves to (no transcripts fetched)")
async def preview(body: PullRequest) -> dict:
    try:
        listing = await asyncio.to_thread(extract.enumerate_url, body.url)
    except Exception as exc:
        raise HTTPException(400, f"Could not read that URL: {exc}") from exc
    status = await asyncio.to_thread(extract.stored_status)
    for v in listing["videos"]:
        v["stored"] = status.get(v["video_id"])
    return listing


@router.post("/jobs", summary="Start pulling transcripts in the background (progress on /api/stream)")
def start_pull(body: PullRequest) -> dict:
    async def run(job: jobs.Job) -> dict:
        return await asyncio.to_thread(extract.pull, body.url, limit=body.limit, delay=body.delay_s,
                                       force=body.force, progress=lambda s: job.update(**s))
    return jobs.start("transcripts", body.model_dump(), run).to_dict()


@router.get("/jobs", summary="Transcript pull jobs (this server session)")
def list_pull_jobs() -> dict:
    return {"jobs": jobs.list_jobs("transcripts")}


@router.post("/import", summary="Import a transcript exported from a third-party tool")
def import_transcript(body: ImportRequest) -> dict:
    try:
        return extract.import_transcript(body.video_id, body.content, body.format, title=body.title,
                                         channel=body.channel, source=body.source)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/videos", summary="Stored videos with status and segment/tag counts")
def list_videos(status: str | None = None, q: str | None = None) -> dict:
    sql = ("SELECT v.*, (SELECT COUNT(*) FROM segment_tags t WHERE t.video_id = v.video_id) AS tagged_windows "
           "FROM videos v WHERE 1=1")
    params: list = []
    if status:
        sql += " AND v.status = ?"
        params.append(status)
    if q:
        sql += " AND v.title LIKE ?"
        params.append(f"%{q}%")
    with db.connect() as conn:
        items = db.rows(conn, sql + " ORDER BY v.title", tuple(params))
        totals = db.row(conn, "SELECT COUNT(*) AS videos, SUM(status='ok') AS ok, SUM(status='failed') AS failed, "
                              "SUM(status='blocked') AS blocked, COALESCE(SUM(segment_count),0) AS segments "
                              "FROM videos")
    return {"videos": items, "totals": totals}


@router.get("/videos/{video_id}", summary="One video with its timestamped segments")
def get_video(video_id: str, offset: int = 0, limit: int = Query(2000, le=20000)) -> dict:
    with db.connect() as conn:
        video = db.row(conn, "SELECT * FROM videos WHERE video_id = ?", (video_id,))
        if video is None:
            raise HTTPException(404, "Unknown video.")
        segments = db.rows(conn, "SELECT idx, start, duration, text FROM transcript_segments WHERE video_id = ? "
                                 "ORDER BY idx LIMIT ? OFFSET ?", (video_id, limit, offset))
    for s in segments:
        s["url"] = extract.video_url(video_id, s["start"])
    return {"video": video, "segments": segments}


@router.get("/search", summary="Search transcript text; hits link to the exact timestamp")
def search(q: str = Query(min_length=2), limit: int = Query(50, le=500)) -> dict:
    with db.connect() as conn:
        hits = db.rows(conn, "SELECT s.video_id, s.start, s.text, v.title FROM transcript_segments s "
                             "JOIN videos v USING (video_id) WHERE s.text LIKE ? ORDER BY v.title, s.start LIMIT ?",
                       (f"%{q}%", limit))
    for h in hits:
        h["url"] = extract.video_url(h["video_id"], h["start"])
    return {"q": q, "hits": hits}
