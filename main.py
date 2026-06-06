from fastapi import FastAPI, File, UploadFile, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import yt_dlp
import json
from pydantic import BaseModel
from typing import List
import os
import tempfile
import mimetypes

app = FastAPI()

# Enable CORS for all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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

class ImportedVideo(BaseModel):
    url: str
    title: str

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
    
    return VideoInfo(
        title=info.get('title', 'Unknown'),
        creator=info.get('uploader', 'Unknown'),
        duration=info.get('duration', 0),
        audioUrl=info.get('url', ''),
        thumbnailUrl=info.get('thumbnail', '')
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
async def stream_audio(url: str, background_tasks: BackgroundTasks):
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

        content_type, _ = mimetypes.guess_type(file_path)
        if not content_type:
            content_type = 'audio/mp4'

        file_handle = open(file_path, 'rb')
        background_tasks.add_task(file_handle.close)
        background_tasks.add_task(os.remove, file_path)
        background_tasks.add_task(temp_dir.cleanup)

        return StreamingResponse(
            file_handle,
            media_type=content_type,
            headers={"Content-Disposition": "inline"}
        )
    except Exception as e:
        temp_dir.cleanup()
        return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
