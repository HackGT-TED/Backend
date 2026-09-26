import json
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()


def gladia_api_key() -> str:
    """Read the Gladia key when a request is made, not when this module is imported."""

    load_dotenv()
    key = os.environ.get("GLADIA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GLADIA_API_KEY is not set. Add it to .env. No request was sent.")
    return key



url = "https://api.gladia.io/v2/pre-recorded"










def InitiateTranscriptionJob(audio_url):
    payload = {
    "audio_url": audio_url,
    "custom_vocabulary": False,
    "callback_url": "https://callback.example",
    "callback": False,
    "subtitles": False,
    "subtitles_config": {
        "formats": ["srt"],
        "minimum_duration": 1,
        "maximum_duration": 15.5,
        "maximum_characters_per_row": 2,
        "maximum_rows_per_caption": 3,
        "style": "default"
    },
    "diarization": False,
    "diarization_config": {
        "number_of_speakers": 3,
        "min_speakers": 1,
        "max_speakers": 2
    },
    "translation": False,
    "summarization": False,
    "summarization_config": { "type": "general" },
    "named_entity_recognition": False,
    "custom_spelling": False,
    "sentiment_analysis": False,
    "audio_to_llm": False,
    "pii_redaction": False,
    "pii_redaction_config": {
        "entity_types": ["GDPR", "HEALTH_INFORMATION", "HIPAA_SAFE_HARBOR", "QUEBEC_PRIVACY_ACT", "EMAIL_ADDRESS", "NAME", "PHONE_NUMBER"],
        "processed_text_type": "MARKER"
    },
    "custom_metadata": { "user": "John Doe" },
    "sentences": False,
    "punctuation_enhanced": False,
    "language_config": {
        "languages": [],
        "code_switching": False
    },
    "model": "solaria-1"
    }

    headers = {
    "x-gladia-key": gladia_api_key(),
    "Content-Type": "application/json"
    }

    response = requests.post(url, json=payload, headers=headers)
    data = response.json()
    result_url = data["result_url"]
    
    return result_url

def getTranscriptionResult(result_url):
    headers = {"x-gladia-key": gladia_api_key()}
    #data = response.json()
    while True:
        response = requests.get(result_url, headers=headers)
        data = response.json()
        if data["status"] == "done":
            #print(data["result"]["transcription"]["full_transcript"])
            #return data["result"]
            return (data["result"]["transcription"]["utterances"])
            break
        elif data["status"] == "error":
            #print("Transcription Failed:", data)
            raise RuntimeError(f"Transcription failed: {data}")
            break
        time.sleep(1)


def uploadJsonAudio(payload_json):
    """Upload audio that arrived as JSON (see encodeToJson) to Gladia; return its audio_url."""
    import base64

    payload = json.loads(payload_json)
    audio_bytes = base64.b64decode(payload["audio_base64"])  # back to the original file bytes

    response = requests.post(
        "https://api.gladia.io/v2/upload",
        headers={"x-gladia-key": gladia_api_key()},
        # multipart field "audio": (filename, bytes, content type)
        files={"audio": (payload["filename"], audio_bytes, payload["content_type"])},
    )
    response.raise_for_status()
    return response.json()["audio_url"]




#------------------purely testing functions: -----------------------------
def encodeToJson(local_audio_url):
    """Package a local audio file as a JSON string, like a client would send to an API endpoint.

    Audio is binary and JSON only holds text, so the bytes are base64-encoded.
    Decode on the receiving side with base64.b64decode(payload["audio_base64"]).
    """
    import base64
    import json
    import mimetypes

    with open(local_audio_url, "rb") as f:
        audio_bytes = f.read()

    payload = {
        "filename": os.path.basename(local_audio_url),
        "content_type": mimetypes.guess_type(local_audio_url)[0] or "application/octet-stream",
        "size_bytes": len(audio_bytes),
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
    }
    return json.dumps(payload)



def fullDeepGramPipeline(newRecordedAudio):
    uploadedUrl = uploadJsonAudio(newRecordedAudio)
    result = getTranscriptionResult(InitiateTranscriptionJob(uploadedUrl))
    return json.dumps(result)

if __name__ == "__main__":
    # Quick manual test: python DeepGram.py path/to/story.m4a
    import json
    import sys

    file_path = sys.argv[1]
    payload_json = encodeToJson(file_path)
    uploadedUrl = uploadJsonAudio(payload_json)
    getTranscriptionResult(InitiateTranscriptionJob(uploadedUrl))

    
    

"""
    with open(sys.argv[1], "rb") as f:
        #print(json.dumps(DeepGram(f.read()), indent=2))
        print(getTranscriptionResult(InitiateTranscriptionJob(f.read())))"""
