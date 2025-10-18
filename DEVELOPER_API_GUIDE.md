EduAssist API for Desktop Clients — Developer Guide

Version: 1.0
Updated: 2025-10-10 22:23 (local)

Overview
- This API is a proxy to Azure AI Speech (STT/TTS) and Azure Language (CLU). Desktop apps call this API, not Azure directly.
- Authentication: API key via X-API-Key header (HTTP) or api_key query param (WebSocket).
- Base URL examples:
  - Local: http://localhost:8080
  - Production: http(s)://<your-host>
- Supported endpoints:
  - POST /v1/speech/stt — synchronous speech-to-text for small audio files.
  - WebSocket /v1/speech/stt/stream — streaming speech-to-text with low latency and partial results.
  - POST /v1/speech/tts — text-to-speech, returns audio.
  - POST /v1/clu/predict — CLU intent/entity prediction.
  - GET /health — health probe.

Authentication
- HTTP endpoints: provide header X-API-Key: <key>.
- WebSocket endpoint: provide api_key as query parameter (or X-API-Key header if your WS client supports custom headers).
- Error if missing/invalid: 401 JSON {"detail":"Invalid or missing API key"}.

Common HTTP Status Codes
- 200/201: Success.
- 400: Bad request (missing fields, invalid format).
- 401: Invalid or missing API key.
- 422: Unprocessable (e.g., no speech recognized).
- 429: Too Many Requests (recommend retry with backoff).
- 500: Server configuration issue (e.g., missing Azure keys) or internal error.
- 502: Upstream Azure failure (cancellation, synthesis error, etc.).

Audio Assumptions
- Sample rate 16 kHz, 16-bit little-endian PCM, mono.
- For streaming, send binary frames of 20–40 ms (e.g., 320–640 samples; 640–1280 bytes). Larger frames work but increase latency.

1) Streaming STT — WebSocket
URL
- ws://<host>/v1/speech/stt/stream?language=<locale>&api_key=<key>&endpoint_id=<id>&normalize=<0|1>
  - language (optional): BCP-47 locale, e.g., ru-RU. If omitted, Azure default applies.
  - endpoint_id (optional): Azure Custom Speech endpoint ID to use your custom model.
  - normalize (optional): if 1/true, server applies light peak normalization per chunk.

Protocol
- Client to server:
  - Binary frames: raw PCM16 mono @16kHz.
  - Text message {"event":"stop"} to explicitly end current utterance (optional; server also performs automatic end-of-utterance via Azure endpointing). The server keeps WS open for next utterances.
- Server to client (JSON):
  - {"type":"ready"} — recognizer ready to receive audio.
  - {"type":"partial", "text": "..."} — interim hypothesis (low latency, not final).
  - {"type":"final", "text": "...", "raw": {...}} — final segment of the utterance with optional raw Azure payload.
  - {"type":"session", "event": "started"|"stopped"} — Azure recognizer session lifecycle info.
  - {"type":"info", "event": "stop_ack"} — acknowledgment of a client stop event. Connection stays open.
  - {"type":"error", "reason": "CancellationReason.Error", "error": "..."} — only for real errors. EndOfStream is not treated as an error.

Typical Flow
- Connect WS → receive {"type":"ready"}.
- Send binary audio frames while user speaks.
- Receive {"partial"} messages continuously.
- Receive {"final"} when Azure detects end-of-utterance (end-silence endpointing).
- Optionally, keep the WS open and send more audio for the next utterance (or send {"event":"stop"} to terminate the current one explicitly).
- The server will not close the WS after a single utterance; client may reuse the connection.

Failure Scenarios (WS)
- 401 unauthorized: server sends {"type":"error","error":"unauthorized"} then closes with code 4401.
- Upstream Azure cancellation (real error): {"type":"error","reason":"CancellationReason.Error","error":"details"}. Client should consider retry.
- Transient network hiccup: connection closed (1006/1001). Client should reconnect with exponential backoff (e.g., 0.5s, 1s, 2s, max 10s).
- EndOfStream: handled internally; not surfaced as an error message.

Minimal WS Client Pseudocode
- Connect ws://host/v1/speech/stt/stream?language=ru-RU&api_key=KEY
- On open wait for {"type":"ready"}
- For each audio frame (20–40 ms) → send binary.
- Render {"partial"} inline; on {"final"} commit text.
- If you need to cancel current utterance: send {"event":"stop"}.
- Keep connection for future utterances; reconnect on close.

2) STT (sync, file upload)
Endpoint
- POST /v1/speech/stt
Headers
- X-API-Key: <key>
Content-Type
- multipart/form-data
Form fields
- audio: file (WAV/PCM/MP3/OGG/FLAC). For raw PCM, prefer WS.
- language (optional): e.g., ru-RU
- endpoint_id (optional): Azure Custom Speech endpoint ID
Responses
- 200 JSON: { "text": "...", "raw": { ...Azure JSON... } }
- 400 JSON: {"detail":"..."} (missing file)
- 401 JSON: {"detail":"Invalid or missing API key"}
- 422 JSON: {"detail":"No speech recognized"}
- 502 JSON: {"detail":"STT canceled: <reason> <error_details>"}

Example (curl)
- curl -X POST "http://localhost:8080/v1/speech/stt" \
  -H "X-API-Key: YOUR_KEY" \
  -F "audio=@sample.wav" \
  -F "language=ru-RU"

3) TTS (text-to-speech)
Endpoint
- POST /v1/speech/tts
Headers
- X-API-Key: <key>
Body (JSON)
- {
    "text": "Привет!",
    "voiceName": "ru-RU-DmitryNeural" (optional; default from server .env),
    "format": "riff-16khz-16bit-mono-pcm" (optional),
    "deploymentId": "<custom-voice-deployment-id>" (optional for Custom Neural Voice)
  }
Response
- 200 audio stream, media type "audio/wav" (for riff/wav) or application/octet-stream for other formats.
- 400 JSON: {"detail":"Missing 'text'"}
- 401 JSON: {"detail":"Invalid or missing API key"}
- 502 JSON: {"detail":"TTS canceled: <reason> <error_details>"}

Example (curl)
- curl -X POST "http://localhost:8080/v1/speech/tts" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_KEY" \
  -d "{ \"text\": \"Проверка синтеза\", \"voiceName\": \"ru-RU-DmitryNeural\" }" \
  --output out.wav

4) CLU (intent/entities)
Endpoint
- POST /v1/clu/predict
Headers
- X-API-Key: <key>
Body (JSON)
- {
    "text": "Включи таймер на 10 минут",
    "projectName": "<overrides default>",
    "deploymentName": "<overrides default>",
    "locale": "ru-RU"
  }
Response
- 200 JSON: {
    "topIntent": "...",
    "intents": [...],
    "entities": [...],
    "raw": { ...full Azure response... }
  }
- 400 JSON: {"detail":"Missing 'text'"}
- 401 JSON: {"detail":"Invalid or missing API key"}
- 500 JSON: {"detail":"CLU config missing"}

Example (curl)
- curl -X POST "http://localhost:8080/v1/clu/predict" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_KEY" \
  -d "{ \"text\": \"Включи свет в классе\", \"locale\": \"ru-RU\" }"

5) Health
- GET /health → 200 {"status":"ok"}

Server Behaviors and Timeouts (Speech)
- Automatic end-of-utterance is enabled via Azure endpointing.
  - EndSilenceTimeoutMs default: 800 ms (configurable via END_SILENCE_TIMEOUT_MS env var)
  - InitialSilenceTimeoutMs default: 4000 ms (configurable via INITIAL_SILENCE_TIMEOUT_MS)
- Word-level timestamps requested by default.

Error Handling Matrix (Summary)
- Auth missing/invalid → 401 (HTTP) or WS {"type":"error","error":"unauthorized"} + close 4401.
- STT file: no speech → 422; Azure cancel/error → 502.
- TTS: missing text → 400; Azure cancel/error → 502.
- CLU: missing text → 400; misconfigured env → 500.
- Azure unavailable (network/DNS/5xx): expect 502 in HTTP routes or WS {"type":"error"}; client should retry with exponential backoff.
- Payload too large: streaming recommended; consider limiting frame sizes to avoid buffering.

Client Recommendations
- Use persistent WebSocket for repeated commands to minimize handshake overhead.
- Send audio frames continuously during speech; do not wait to accumulate long buffers.
- Render partial results inline for responsive UI; treat final as commit.
- For cancellation (barge-in), send {"event":"stop"}.
- Reconnect on WS close with backoff. Consider jitter.

Security Notes
- Never expose Azure keys to clients; only the API key is shared with the desktop app.
- Use TLS in production (wss/https). Validate certificates.
- Do not log raw audio or PII.

Environment Variables (server)
- API_KEY — API key for clients.
- SPEECH_KEY, SPEECH_REGION — Azure Speech credentials.
- AZURE_SPEECH_ENDPOINT_ID — default Custom Speech model (optional).
- AZURE_TTS_VOICE — default TTS voice (optional).
- AZURE_CONVERSATIONS_ENDPOINT, AZURE_CONVERSATIONS_KEY — CLU endpoint and key.
- AZURE_CONVERSATIONS_PROJECT_NAME, AZURE_CONVERSATIONS_DEPLOYMENT_NAME — defaults for CLU.
- END_SILENCE_TIMEOUT_MS, INITIAL_SILENCE_TIMEOUT_MS — endpointing tuning.

Known Limits
- This API is optimized for short commands. For long audio transcription, use batch jobs or a different streaming approach.
- WebSocket message types are limited to those listed; unknown types are ignored.

Change Log
- 2025-10-10: Initial developer guide added.
