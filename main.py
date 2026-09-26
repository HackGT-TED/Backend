import requests
from fastapi import FastAPI, HTTPException
import json

from DeepGram import fullDeepGramPipeline

from pydantic import BaseModel

class AudioPayload(BaseModel):
    filename: str
    content_type: str
    audio_base64: str
    size_bytes: int | None = None

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


#this is the endpoint that receives json data and sends it to the fullDeepGramPipeline() inside the DeepGram.py file. 
@app.post("/transcribe")
def transcribe(payload: AudioPayload):
    try:
        transcript = fullDeepGramPipeline(json.dumps(payload.model_dump()))
    except requests.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Gladia error: {e.response.text}")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return transcript




"""

    audio = file.file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="Empty audio file")
    try:
        return fullDeepGramPipeline(audio, file.content_type or "audio/mp4")
    except requests.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Deepgram error: {e.response.text}")
        
"""
