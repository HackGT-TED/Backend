# Project Spec: Bedtime Story Teddy Bear (HackGT)

## Concept

A grandparent records a bedtime story on their phone, with nothing to learn beyond pressing record and talking. Across the country, the grandkid hugs a teddy bear that plays the story with matching sound effects and haptics: rain patter when it rains in the story, a rumble when the dragon roars, a heartbeat during the scary part.

**Why AI is essential:** the grandparent improvises rather than reading a script, so only a language model can follow free-form narration and choose fitting effects. To keep it reliable, the LLM picks from a **fixed library of predefined effects** and returns structured JSON. It does not generate audio.

**Themes:** elderly-friendly (the grandparent just talks), family connection, and a keepsake storybook of past stories (fits Meta's "turn family moments into a shared story" example).

> Note: the original idea was a *live* phone call with real-time effects. The current plan is **record, then process, then play back**. That removes the one-second latency requirement, so streaming STT and a keyword fallback are no longer needed on the critical path.

## Architecture / Data Flow

```
[Grandparent phone: Expo / React Native app]
   │  1. Record story audio in the app
   │  2. Upload audio file (HTTP POST)
   ▼
[Python backend: FastAPI on Render]
   │  3. Send audio to Deepgram → transcript JSON (words + timestamps)
   │  4. Send transcript JSON + effect library to chat model → effects_json
   │  5. For each effect: fetch sound from Freesound API
   │  6. Build effects.mp3 (silence + effects placed at timestamps)
   │  7. Mix story.mp3 + effects.mp3 → combined.mp3
   │  8. Save all three MP3s (+ transcript, effects_json) as one DB row
   ▼
[Database / storage]
   ▼
[Kid/parent phone: Expo / React Native app]
   │  9. Fetch the story's MP3s + effects_json and store them locally
   │ 10. Send audio to the bear over Bluetooth (see §9 for the transport)
   ▼
[Teddy bear: ESP32 + speaker + haptic motor]
     11. Play audio; trigger haptics
```

## Components

### 1. Grandparent app (recorder): Expo / React Native
- Very simple UI: one big record/stop button, then send.
- Records with `expo-audio`, which produces `.m4a` (AAC), not MP3. Deepgram accepts AAC directly.
- Uploads the audio to the backend as a multipart POST.

### 2. Backend (Python, FastAPI, hosted on Render)
Location: `Backend/`
- Deployed as a Render **Web Service** and run with `uvicorn main:app --host 0.0.0.0 --port $PORT`.
- ffmpeg is needed for `pydub`. The most reliable way to get it is a Docker deploy (`apt-get install -y ffmpeg`). Alternatively, use the `imageio-ffmpeg` pip package, which bundles a static binary.
- API keys are set as Render environment variables.
- The filesystem is ephemeral, so write temporary audio to `/tmp` and persist final files to object storage.
- `main.py`: FastAPI app and endpoints. Currently empty.
- `DeepGram.py`: `DeepGram(audio)` sends audio to the Deepgram `/v1/listen` REST endpoint and returns the transcript JSON as a dict. Currently a stub.
- Planned modules:
  - **Effect selection:** LLM call. Input is the transcript with word timestamps plus the effect library. Output is `effects_json`.
  - **Effect audio builder:** Freesound lookup, then builds `effects.mp3` with correct offsets.
  - **Mixer:** overlays the effects onto the story to make `combined.mp3`.
  - **Persistence:** writes the story row to the database.

### 3. Deepgram (speech-to-text)
- Pre-recorded transcription with word-level timestamps (`results.channels[0].alternatives[0].words[]`, each with `word`, `start`, `end`).
- API key in the `DEEPGRAM_API_KEY` environment variable.

### 4. Effect selection (chat model)
- Given the transcript and the fixed effect library, return the best-matching effects with timestamps.
- Output must be strict JSON validated against the library. Reject or ignore unknown effect names.
- Output shape (draft):

```json
{
  "effects": [
    {
      "name": "thunder",
      "description": "Dragon roars as it lands",
      "timestamp": 12.4,
      "intensity": 0.8
    }
  ]
}
```

- `timestamp` is in seconds from the start of the story, taken from Deepgram word `start` times.
- `intensity` (0–1) is optional. It can scale the effect's volume and haptic strength.

### 5. Effect library (fixed, about 10 effects)
Draft list, to be finalized: `rain`, `thunder`, `heartbeat`, `footsteps`, `wind`, `roar`, `door_creak`, `birds`, `ocean_waves`, `snoring`/`sleep`, `magic_sparkle`, `laugh`.
- Each entry has a name, a description (for the LLM prompt), a Freesound query or a pinned Freesound sound ID, and a haptic pattern.
- **Recommendation:** pin specific Freesound IDs, or cache the sounds in the repo, rather than searching at runtime. This keeps results consistent and fast.

### 6. Audio assembly
- `effects.mp3` is a silent track the length of the story, with each effect overlaid at its `timestamp`.
- `combined.mp3` is the story overlaid with the effects, with the effects ducked to lower volume.
- Likely library: `pydub`, which requires ffmpeg.

### 7. Database / storage
One row per story:

| field | notes |
|---|---|
| id | |
| created_at | |
| title / grandparent / kid | optional metadata for the storybook |
| story_mp3 | original recording |
| effects_mp3 | effects only |
| combined_mp3 | story + effects |
| transcript | Deepgram JSON or plain text (for the storybook) |
| effects_json | LLM output (also drives the haptics) |

Recommendation: keep the MP3 files in object storage (for example Supabase Storage or S3) and store their **URLs** in the row, not the raw bytes.

### 8. Kid/parent app (player): Expo / React Native
- Lists stories (the storybook), downloads and caches the MP3s and `effects_json`, and starts playback on the bear.
- The grandparent app and this app may be the same app with two modes.
- BLE requires a native module (`react-native-ble-plx`), so the app needs an **Expo development build** (`npx expo run:ios` / `run:android`, or EAS). It won't run in Expo Go.

### 9. Teddy bear hardware and Bluetooth transport
- **Board:** an original **ESP32** (for example ESP32-WROOM-32 or ESP32 DevKitC), which supports Classic Bluetooth. The ESP32-S3, C3, and C6 support BLE only and can't act as an A2DP speaker.
- A speaker driven by an I2S amp (for example the MAX98357A), plus a haptic motor (vibration motor or DRV2605 haptic driver).
- **Audio (recommended): Classic Bluetooth A2DP.** The ESP32 acts as a Bluetooth speaker, using the `ESP32-A2DP` Arduino library as an A2DP sink that outputs over I2S. The phone pairs with it once in system Bluetooth settings, and the app then plays `combined.mp3` normally with `expo-audio`, which the OS routes to the bear. The phone handles decoding and streaming, and there's no MP3 decoding or chunking on the ESP32.
- **Not recommended: streaming MP3 bytes over BLE.** BLE throughput from phones is limited and variable (iOS in particular). It would need custom chunking, flow control, and buffering, plus MP3 decoding on the ESP32. A whole story (several MB) won't fit in RAM.
- **Haptics:** these need timing information separate from the audio. Options:
  - (a) **BLE command channel:** the app sends "haptic X, intensity Y" over BLE at each `effects_json` timestamp during playback. The ESP32 runs A2DP and BLE simultaneously (dual mode). This is RAM-heavy, so test it early.
  - (b) **Stereo trick (no second channel):** the backend encodes `combined.mp3` as stereo, with the left channel carrying the full mix and the right channel carrying the effects only. The ESP32 plays the left channel on the speaker and drives the motor from the right channel's loudness envelope. This keeps haptics in perfect sync with the audio.
- A2DP adds roughly 100–300 ms of latency. With option (a), fire haptic commands slightly early to compensate.
- Stretch goals from the original idea: NeoPixel glow, a force sensor squeeze that notifies the grandparent ("still listening"), and a goodnight hug.

## Known Risks / Open Questions

1. **Recordings are not MP3.** `expo-audio` produces `.m4a` (AAC). Deepgram and pydub/ffmpeg accept it, so convert to MP3 on the backend only for the outputs.
2. **Render free-tier cold starts.** Free web services spin down after about 15 minutes idle, and the first request afterward can take roughly a minute. Before a demo, warm the service up by hitting a `/health` endpoint, or use a paid instance. Processing a story (Deepgram, the LLM, Freesound, and mixing) may take tens of seconds, so the upload endpoint should either return a story ID immediately and process in the background, or the front end should show a loading state.
3. **ESP32 model.** Confirm the board is an original ESP32 with Classic Bluetooth. If it's an S3, C3, or C6, A2DP is impossible and the audio plan must change.
4. **A2DP plus BLE at the same time.** Dual-mode Bluetooth on the ESP32 works but uses a lot of RAM. Prototype early. If it fails, fall back to the stereo-channel haptic trick (§9, option b).
5. **Haptic sync.** Haptics need `effects_json` or an effects signal in addition to the story audio (see §9).
6. **iOS/Android background limits.** If the phone locks or the app goes to the background, JavaScript timers and BLE writes can be throttled, which would break option (a) haptics. Keep the screen awake during playback (`expo-keep-awake`) and enable the audio background mode. A2DP audio itself keeps playing in the background.
7. **Bluetooth permissions.** iOS needs `NSBluetoothAlwaysUsageDescription`. Android 12+ needs the `BLUETOOTH_SCAN` and `BLUETOOTH_CONNECT` permissions. Configure these through the `react-native-ble-plx` Expo config plugin.
8. **Freesound.** Requires an API key. Previews (`previews.preview-hq-mp3`) can be downloaded with a token; full-quality original files require OAuth. Previews are fine for this project.
9. **LLM reliability.** Use JSON mode or structured output, validate effect names against the library, and fall back to keyword matching if the call fails.

## Conventions
- Python backend (FastAPI on Render). Mobile apps built with Expo / React Native (development build, not Expo Go). ESP32 firmware in Arduino/C++.
- Secrets live in environment variables (`DEEPGRAM_API_KEY`, `FREESOUND_API_KEY`, the LLM key, and database credentials) and are never committed.
