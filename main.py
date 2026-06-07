from fastapi import FastAPI, File, UploadFile, BackgroundTasks, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import yt_dlp
import json
from pydantic import BaseModel
from typing import List, Iterator, Tuple
import os
import tempfile
import mimetypes

app = FastAPI()

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
    
    thumbnail_url = info.get('thumbnail', '')

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
    videos = []
    
    # Parse TikTok export JSON format
    if 'Video' in data:
        for video in data['Video']:
            videos.append(ImportedVideo(
                url=video.get('Link', ''),
                title=video.get('Desc', '')
            ))
    
    return videos

@app.get("/stream")
async def stream_audio(url: str, request: Request, background_tasks: BackgroundTasks):
    """Download TikTok audio to a temp file and stream it back to the client"""
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
            file_path = ydl.prepare_filename(info)

        if not os.path.exists(file_path):
            temp_dir.cleanup()
            return {"error": "Failed to download audio from TikTok"}

        file_size = os.path.getsize(file_path)
        content_type = get_audio_content_type(file_path)

        try:
            start, end, is_partial = parse_range_header(
                request.headers.get("range"),
                file_size,
            )
        except ValueError:
            background_tasks.add_task(os.remove, file_path)
            background_tasks.add_task(temp_dir.cleanup)
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

        background_tasks.add_task(os.remove, file_path)
        background_tasks.add_task(temp_dir.cleanup)

        return StreamingResponse(
            iter_file_range(file_path, start, end),
            status_code=206 if is_partial else 200,
            media_type=content_type,
            headers=headers,
            background=background_tasks,
        )
    except Exception as e:
        temp_dir.cleanup()
        return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
