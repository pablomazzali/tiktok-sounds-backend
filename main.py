from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import json
from pydantic import BaseModel
from typing import List

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
