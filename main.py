from fastapi import FastAPI, File, UploadFile, BackgroundTasks, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import yt_dlp
import json
import re
from pydantic import BaseModel
from typing import Any, List, Iterator, Tuple
import os
import tempfile
import mimetypes
import hashlib
import glob
import time
import shutil

app = FastAPI()

CACHE_TTL_SECONDS = 60 * 15
AUDIO_CACHE_DIR = os.path.join(tempfile.gettempdir(), "mitok-audio-cache")

# Enable CORS for all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Accept-Ranges", "Content-Length", "Content-Range", "Cache-Control"],
)

# Request/Response Models
class URLRequest(BaseModel):
    url: str

class VideoInfo(BaseModel):
    title: str
    creator: str
    duration: int
    audioUrl: str
    thumbnailUrl: str
    coverArt: str

class ImportedVideo(BaseModel):
    url: str
    title: str

TIKTOK_URL_PATTERN = re.compile(r"https?://(?:[\w.-]+\.)?tiktok\.com/[^\s\"'<>]+", re.IGNORECASE)


def clean_tiktok_url(url: str) -> str:
    return url.strip().rstrip(").,;]}")


def get_import_title(item: dict[str, Any]) -> str:
    for key in ("Desc", "desc", "Description", "description", "Title", "title", "name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def collect_imported_videos(data: Any) -> List[ImportedVideo]:
    videos: List[ImportedVideo] = []
    seen: set[str] = set()

    def add_video(url: str, title: str = "") -> None:
        cleaned_url = clean_tiktok_url(url)
        if not cleaned_url or cleaned_url in seen:
            return

        seen.add(cleaned_url)
        videos.append(ImportedVideo(url=cleaned_url, title=title.strip()))

    def walk(value: Any, title_hint: str = "") -> None:
        if isinstance(value, dict):
            title = get_import_title(value) or title_hint

            for key in ("Link", "link", "Url", "URL", "url", "VideoLink", "videoLink", "shareUrl", "share_url"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    for match in TIKTOK_URL_PATTERN.findall(candidate):
                        add_video(match, title)

            for nested in value.values():
                walk(nested, title)
            return

        if isinstance(value, list):
            for item in value:
                walk(item, title_hint)
            return

        if isinstance(value, str):
            for match in TIKTOK_URL_PATTERN.findall(value):
                add_video(match, title_hint)

    walk(data)
    return videos


def get_cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def cleanup_audio_cache() -> None:
    os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)
    now = time.time()

    for file_path in glob.glob(os.path.join(AUDIO_CACHE_DIR, "*")):
        try:
            if os.path.isfile(file_path) and now - os.path.getmtime(file_path) > CACHE_TTL_SECONDS:
                os.remove(file_path)
        except OSError:
            pass


def get_cached_audio_path(url: str) -> str | None:
    cleanup_audio_cache()
    cache_key = get_cache_key(url)
    matches = glob.glob(os.path.join(AUDIO_CACHE_DIR, f"{cache_key}.*"))

    for file_path in matches:
        if os.path.isfile(file_path):
            os.utime(file_path, None)
            return file_path

    return None


def download_audio_to_cache(url: str) -> str:
    cached_path = get_cached_audio_path(url)
    if cached_path:
        return cached_path

    os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)
    cache_key = get_cache_key(url)
    temp_dir = tempfile.TemporaryDirectory()

    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': os.path.join(temp_dir.name, '%(id)s.%(ext)s')
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_path = ydl.prepare_filename(info)

        if not os.path.exists(downloaded_path):
            raise FileNotFoundError("Failed to download audio from TikTok")

        extension = os.path.splitext(downloaded_path)[1] or ".mp4"
        cached_path = os.path.join(AUDIO_CACHE_DIR, f"{cache_key}{extension}")
        existing_path = get_cached_audio_path(url)
        if existing_path:
            return existing_path

        shutil.move(downloaded_path, cached_path)
        return cached_path
    finally:
        temp_dir.cleanup()


def parse_range_header(range_header: str | None, file_size: int) -> Tuple[int, int, bool]:
    if not range_header:
        return 0, file_size - 1, False

    if not range_header.startswith("bytes="):
        raise ValueError("Invalid range header")

    range_value = range_header.replace("bytes=", "", 1).split(",", 1)[0].strip()
    start_raw, separator, end_raw = range_value.partition("-")
    if not separator:
        raise ValueError("Invalid range header")

    if start_raw == "":
        suffix_length = int(end_raw)
        if suffix_length <= 0:
            raise ValueError("Invalid range header")
        start = max(file_size - suffix_length, 0)
        end = file_size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1
        end = min(end, file_size - 1)

    if start < 0 or end < start or start >= file_size:
        raise ValueError("Invalid range header")

    return start, end, True


def iter_file_range(file_path: str, start: int, end: int) -> Iterator[bytes]:
    with open(file_path, "rb") as file:
        file.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = file.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def get_audio_content_type(file_path: str) -> str:
    extension = os.path.splitext(file_path)[1].lower()
    if extension in {".m4a", ".mp4"}:
        return "audio/mp4"

    content_type, _ = mimetypes.guess_type(file_path)
    return content_type or "audio/mp4"


def get_thumbnail_url(info: dict) -> str:
    if info.get('thumbnail'):
        return info.get('thumbnail', '')

    thumbnails = info.get('thumbnails') or []
    if not thumbnails:
        return ''

    sorted_thumbnails = sorted(
        thumbnails,
        key=lambda item: item.get('width', 0) * item.get('height', 0),
        reverse=True,
    )

    return sorted_thumbnails[0].get('url', '')

# Endpoints
@app.post("/extract", response_model=VideoInfo)
async def extract_tiktok(request: URLRequest):
    """Extract TikTok metadata from URL"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'best[ext=mp4]'
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(request.url, download=False)
    
    thumbnail_url = get_thumbnail_url(info)

    return VideoInfo(
        title=info.get('title', 'Unknown'),
        creator=info.get('uploader', 'Unknown'),
        duration=info.get('duration', 0),
        audioUrl=info.get('url', ''),
        thumbnailUrl=thumbnail_url,
        coverArt=thumbnail_url
    )

@app.post("/import-json", response_model=List[ImportedVideo])
async def import_json(file: UploadFile = File(...)):
    """Import TikTok videos from JSON export"""
    content = await file.read()
    data = json.loads(content)
    return collect_imported_videos(data)


@app.get("/prepare")
async def prepare_audio(url: str):
    """Warm the short-lived server cache so playback starts faster later."""
    try:
        file_path = download_audio_to_cache(url)
        return {
            "ready": True,
            "contentType": get_audio_content_type(file_path),
            "cacheTtlSeconds": CACHE_TTL_SECONDS,
        }
    except Exception as e:
        return {"ready": False, "error": str(e)}

@app.get("/stream")
async def stream_audio(url: str, request: Request, background_tasks: BackgroundTasks):
    """Download TikTok audio to a temp file and stream it back to the client"""
    try:
        file_path = download_audio_to_cache(url)

        file_size = os.path.getsize(file_path)
        content_type = get_audio_content_type(file_path)

        try:
            start, end, is_partial = parse_range_header(
                request.headers.get("range"),
                file_size,
            )
        except ValueError:
            return Response(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{file_size}",
                    "Accept-Ranges": "bytes",
                },
                background=background_tasks,
            )

        content_length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Disposition": "inline",
            "Cache-Control": "private, max-age=900",
        }

        if is_partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

        return StreamingResponse(
            iter_file_range(file_path, start, end),
            status_code=206 if is_partial else 200,
            media_type=content_type,
            headers=headers,
            background=background_tasks,
        )
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
