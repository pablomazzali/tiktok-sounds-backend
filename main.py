from fastapi import FastAPI, File, UploadFile, BackgroundTasks, Request, Response, HTTPException
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
import requests
from urllib.parse import urlparse

app = FastAPI()

CACHE_TTL_SECONDS = 60 * 15
AUDIO_CACHE_DIR = os.path.join(tempfile.gettempdir(), "mitok-audio-cache")
AUDIO_FORMAT_SELECTOR = "bestaudio[ext=m4a]/bestaudio/best[ext=mp4]/best"
SOUND_PAGE_ERROR = "TikTok sound links are catalog pages, not directly playable audio. Import a TikTok video/post link that uses this sound."

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

TIKTOK_URL_PATTERN = re.compile(
    r"https?://(?:[\w.-]+\.)?(?:tiktok\.com|tiktokv\.com)/[^\s\"'<>]+",
    re.IGNORECASE,
)
SAVED_SOUND_MARKERS = ("favorite", "favourite", "saved", "bookmark")
SOUND_MARKERS = ("sound", "sounds", "music", "audio")
EXCLUDED_IMPORT_MARKERS = (
    "comment",
    "comments",
    "history",
    "watchhistory",
    "search",
    "sharehistory",
    "message",
    "messages",
    "directmessage",
    "following",
    "follower",
)
EXCLUDED_LIKE_SEGMENTS = ("like", "likes", "liked", "likelist", "likedlist")


def clean_tiktok_url(url: str) -> str:
    return url.strip().rstrip(").,;]}")


def normalize_import_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def is_sound_url(url: str) -> bool:
    normalized_url = url.lower()
    return any(
        marker in normalized_url
        for marker in ("/music/", "/sound/", "/share/music/", "/share/sound/", "/h5/share/music/")
    )


def is_tiktok_short_url(url: str) -> bool:
    hostname = urlparse(url).netloc.lower()
    return hostname in {"vm.tiktok.com", "vt.tiktok.com"}


def resolve_tiktok_url(url: str) -> str:
    if is_sound_url(url):
        raise ValueError(SOUND_PAGE_ERROR)

    if not is_tiktok_short_url(url):
        return url

    response = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
        },
        allow_redirects=True,
        timeout=12,
    )
    response.raise_for_status()

    resolved_url = response.url
    if is_sound_url(resolved_url):
        raise ValueError(SOUND_PAGE_ERROR)

    return resolved_url


def is_video_url(url: str) -> bool:
    normalized_url = url.lower()
    return any(marker in normalized_url for marker in ("/video/", "/photo/", "/share/video/"))


def is_saved_sound_path(path: tuple[str, ...]) -> bool:
    normalized_path = tuple(normalize_import_label(part) for part in path)
    joined_path = "".join(normalized_path)

    if any(marker in joined_path for marker in EXCLUDED_IMPORT_MARKERS):
        return False

    for segment in normalized_path:
        if segment in EXCLUDED_LIKE_SEGMENTS or segment.startswith("liked"):
            return False
        if segment.startswith("like") and "favorite" not in segment and "favourite" not in segment:
            return False

    has_saved_marker = any(marker in joined_path for marker in SAVED_SOUND_MARKERS)
    has_sound_marker = any(marker in joined_path for marker in SOUND_MARKERS)

    return has_saved_marker and has_sound_marker


def format_import_path(path: tuple[str, ...]) -> str:
    return " > ".join(part for part in path if part)


def collect_import_diagnostics(data: Any) -> str:
    candidates: dict[tuple[str, ...], dict[str, int]] = {}

    def register(path: tuple[str, ...], url: str) -> None:
        parent_path = path[:-1] if path and normalize_import_label(path[-1]) in {"link", "url", "videolink", "shareurl"} else path
        bucket = candidates.setdefault(parent_path, {"sound": 0, "video": 0, "other": 0})

        if is_sound_url(url):
            bucket["sound"] += 1
        elif is_video_url(url):
            bucket["video"] += 1
        else:
            bucket["other"] += 1

    def walk(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                walk(nested, (*path, str(key)))
            return

        if isinstance(value, list):
            for item in value:
                walk(item, path)
            return

        if isinstance(value, str):
            for match in TIKTOK_URL_PATTERN.findall(value):
                register(path, clean_tiktok_url(match))

    walk(data)

    interesting = [
        (path, counts)
        for path, counts in candidates.items()
        if counts["sound"] or any(marker in normalize_import_label(format_import_path(path)) for marker in SOUND_MARKERS)
    ]

    if not interesting:
        total_links = sum(sum(counts.values()) for counts in candidates.values())
        return f"No sound/music sections were found. TikTok links found elsewhere: {total_links}."

    interesting.sort(
        key=lambda item: (item[1]["sound"], item[1]["video"], item[1]["other"]),
        reverse=True,
    )

    lines = []
    for path, counts in interesting[:5]:
        parts = []
        if counts["sound"]:
            parts.append(f"{counts['sound']} sound links")
        if counts["video"]:
            parts.append(f"{counts['video']} video links")
        if counts["other"]:
            parts.append(f"{counts['other']} other TikTok links")

        lines.append(f"{format_import_path(path) or 'root'} ({', '.join(parts)})")

    return "Possible sections found: " + "; ".join(lines)


def get_import_title(item: dict[str, Any]) -> str:
    for key in ("Desc", "desc", "Description", "description", "Title", "title", "name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def collect_imported_videos(data: Any) -> List[ImportedVideo]:
    videos: List[ImportedVideo] = []
    seen: set[str] = set()

    def add_video(url: str, title: str = "", path: tuple[str, ...] = ()) -> None:
        cleaned_url = clean_tiktok_url(url)
        if not cleaned_url or cleaned_url in seen:
            return

        if not is_saved_sound_path(path) or not is_sound_url(cleaned_url):
            return

        seen.add(cleaned_url)
        videos.append(ImportedVideo(url=cleaned_url, title=title.strip()))

    def walk(value: Any, title_hint: str = "", path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            title = get_import_title(value) or title_hint

            for key in ("Link", "link", "Url", "URL", "url", "VideoLink", "videoLink", "shareUrl", "share_url"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    for match in TIKTOK_URL_PATTERN.findall(candidate):
                        add_video(match, title, (*path, key))

            for key, nested in value.items():
                walk(nested, title, (*path, str(key)))
            return

        if isinstance(value, list):
            for item in value:
                walk(item, title_hint, path)
            return

        if isinstance(value, str):
            for match in TIKTOK_URL_PATTERN.findall(value):
                add_video(match, title_hint, path)

    walk(data)
    return videos


def get_cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def find_downloaded_file(info: dict[str, Any], temp_dir: str, expected_path: str) -> str | None:
    requested_downloads = info.get("requested_downloads") or []
    for download in requested_downloads:
        file_path = download.get("filepath")
        if isinstance(file_path, str) and os.path.exists(file_path):
            return file_path

    if os.path.exists(expected_path):
        return expected_path

    candidates: list[str] = []
    for root, _, files in os.walk(temp_dir):
        for file_name in files:
            file_path = os.path.join(root, file_name)
            if os.path.isfile(file_path):
                candidates.append(file_path)

    if not candidates:
        return None

    return max(candidates, key=lambda path: os.path.getsize(path))


def extract_media_info(url: str, download: bool = False) -> dict[str, Any]:
    url = resolve_tiktok_url(url)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": AUDIO_FORMAT_SELECTOR,
        "noplaylist": True,
        "ignore_no_formats_error": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=download)


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
    url = resolve_tiktok_url(url)
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
            'format': AUDIO_FORMAT_SELECTOR,
            'noplaylist': True,
            'ignore_no_formats_error': True,
            'outtmpl': os.path.join(temp_dir.name, '%(id)s.%(ext)s')
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            expected_path = ydl.prepare_filename(info)
            downloaded_path = find_downloaded_file(info, temp_dir.name, expected_path)

        if not downloaded_path or not os.path.exists(downloaded_path):
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
    try:
        info = extract_media_info(request.url, download=False)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=422,
            detail=f"Could not read this TikTok link. Try sharing the original post link instead. ({str(e)})",
        ) from e

    if not info:
        raise HTTPException(
            status_code=422,
            detail="Could not read this TikTok link. Try sharing the original post link instead.",
        )
    
    thumbnail_url = get_thumbnail_url(info)

    return VideoInfo(
        title=info.get('title', 'Unknown'),
        creator=info.get('uploader', 'Unknown'),
        duration=info.get('duration') or 0,
        audioUrl=info.get('url', ''),
        thumbnailUrl=thumbnail_url,
        coverArt=thumbnail_url
    )

@app.post("/import-json", response_model=List[ImportedVideo])
async def import_json(file: UploadFile = File(...)):
    """Import TikTok videos from JSON export"""
    try:
        content = await file.read()
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="Invalid JSON file.") from e

    videos = collect_imported_videos(data)
    diagnostics = collect_import_diagnostics(data)
    if videos:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Found {len(videos)} saved sound links, but TikTok exports saved sounds as sound pages, "
                "not playable audio. Import TikTok video/post links with Bulk import instead. "
                f"{diagnostics}"
            ),
        )

    raise HTTPException(
        status_code=422,
        detail=f"No playable TikTok sound links were imported. {diagnostics}",
    )



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
        return Response(
            content=json.dumps({"error": str(e)}),
            status_code=422,
            media_type="application/json",
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
