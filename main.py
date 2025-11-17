import os
import json
import asyncio
from typing import Optional

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import azure.cognitiveservices.speech as speechsdk
from azure.ai.language.conversations import ConversationAnalysisClient
from azure.core.credentials import AzureKeyCredential

app = FastAPI(title="EduAssist API Proxy (Low Latency)", version="1.0")

# Добавить CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В продакшене указать конкретные origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load environment variables automatically from .env if present
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    # If python-dotenv is not available, simply skip; rely on OS env
    pass

API_KEY = os.getenv("API_KEY")

SPEECH_KEY = os.getenv("SPEECH_KEY") or os.getenv("AZURE_SPEECH_KEY")
SPEECH_REGION = os.getenv("SPEECH_REGION") or os.getenv("AZURE_SPEECH_REGION")
DEFAULT_STT_ENDPOINT_ID = os.getenv("AZURE_SPEECH_ENDPOINT_ID")

DEFAULT_TTS_VOICE = os.getenv("AZURE_TTS_VOICE", "ru-RU-DmitryNeural")

LANG_ENDPOINT = os.getenv("AZURE_CONVERSATIONS_ENDPOINT") or os.getenv("AZURE_LANGUAGE_ENDPOINT")
LANG_KEY = os.getenv("AZURE_CONVERSATIONS_KEY") or os.getenv("AZURE_LANGUAGE_KEY")
CLU_PROJECT = os.getenv("AZURE_CONVERSATIONS_PROJECT_NAME") or os.getenv("AZURE_CLU_PROJECT")
CLU_DEPLOYMENT = os.getenv("AZURE_CONVERSATIONS_DEPLOYMENT_NAME") or os.getenv("AZURE_CLU_DEPLOYMENT")

async def require_api_key(x_api_key: Optional[str]):
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

def make_speech_config(endpoint_id: Optional[str] = None, language: Optional[str] = None):
    if not SPEECH_KEY or not SPEECH_REGION:
        raise HTTPException(500, detail="Speech config missing")
    cfg = speechsdk.SpeechConfig(subscription=SPEECH_KEY, region=SPEECH_REGION)
    if endpoint_id:
        cfg.endpoint_id = endpoint_id
    if language:
        cfg.speech_recognition_language = language
    # Endpointing tuned for assistant use-case (automatic end-of-utterance)
    end_sil_ms = os.getenv("END_SILENCE_TIMEOUT_MS", "800")
    init_sil_ms = os.getenv("INITIAL_SILENCE_TIMEOUT_MS", "4000")
    try:
        cfg.set_property(speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs, str(int(end_sil_ms)))
        cfg.set_property(speechsdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs, str(int(init_sil_ms)))
    except Exception:
        pass
    cfg.request_word_level_timestamps()
    return cfg

@app.websocket("/v1/speech/stt/stream")
async def stt_stream(ws: WebSocket):
    await ws.accept()
    try:
        headers = {k.lower(): v for k, v in ws.headers.items()}
        x_api_key = headers.get("x-api-key") or ws.query_params.get("api_key")
        if not API_KEY or x_api_key != API_KEY:
            await ws.send_json({"type": "error", "error": "unauthorized"})
            await ws.close(code=4401)
            return

        language = ws.query_params.get("language", None)
        endpoint_id = ws.query_params.get("endpoint_id", DEFAULT_STT_ENDPOINT_ID)
        normalize_flag = str(ws.query_params.get("normalize", "false")).lower() in ("1", "true", "yes")

        stream_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=16000,
            bits_per_sample=16,
            channels=1,
        )
        push_stream = speechsdk.audio.PushAudioInputStream(stream_format)
        audio_config = speechsdk.audio.AudioConfig(stream=push_stream)

        speech_config = make_speech_config(endpoint_id=endpoint_id, language=language)

        recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config,
            audio_config=audio_config,
        )

        # ✅ ДОБАВЬТЕ ЗДЕСЬ Phrase List
        phrase_list = speechsdk.PhraseListGrammar.from_recognizer(recognizer)
        phrase_list.addPhrase("сабақта жоқ")
        phrase_list.addPhrase("жоқ")

        # Имена учеников
        phrase_list.addPhrase("Әлібек сабақта жоқ")
        phrase_list.addPhrase("Ақмарал сабақта жоқ")
        phrase_list.addPhrase("Сұлтан сабақта жоқ")
        phrase_list.addPhrase("Алмаз сабақта жоқ")

        recognizing_q: asyncio.Queue = asyncio.Queue()
        recognized_q: asyncio.Queue = asyncio.Queue()
        canceled_q: asyncio.Queue = asyncio.Queue()
        session_started_q: asyncio.Queue = asyncio.Queue()
        session_stopped_q: asyncio.Queue = asyncio.Queue()

        def handle_recognizing(evt: speechsdk.SessionEventArgs):
            try:
                text = evt.result.text
                recognizing_q.put_nowait(text)
            except Exception:
                pass

        def handle_recognized(evt: speechsdk.SessionEventArgs):
            try:
                res = evt.result
                payload = {
                    "text": res.text,
                    "reason": str(res.reason),
                }
                raw = res.properties.get(speechsdk.PropertyId.SpeechServiceResponse_JsonResult)
                if raw:
                    payload["raw"] = json.loads(raw)
                recognized_q.put_nowait(payload)
            except Exception:
                pass

        def handle_canceled(evt: speechsdk.SessionEventArgs):
            try:
                det = evt.result.cancellation_details
                # Only treat actual errors as errors; ignore EndOfStream etc.
                if det.reason == speechsdk.CancellationReason.Error:
                    canceled_q.put_nowait({"reason": str(det.reason), "error": det.error_details or "unknown"})
                else:
                    session_stopped_q.put_nowait(True)
            except Exception:
                pass

        def handle_session_started(evt):
            session_started_q.put_nowait(True)

        def handle_session_stopped(evt):
            session_stopped_q.put_nowait(True)

        recognizer.recognizing.connect(handle_recognizing)
        recognizer.recognized.connect(handle_recognized)
        recognizer.canceled.connect(handle_canceled)
        recognizer.session_started.connect(handle_session_started)
        recognizer.session_stopped.connect(handle_session_stopped)

        await asyncio.get_event_loop().run_in_executor(None, recognizer.start_continuous_recognition)

        await ws.send_json({"type": "ready"})

        async def pump_events():
            async def process_recognizing():
                while True:
                    text = await recognizing_q.get()
                    await ws.send_json({"type": "partial", "text": text})

            async def process_recognized():
                while True:
                    payload = await recognized_q.get()
                    if "error" in payload or payload.get("reason") == "CancellationReason.Error":
                        await ws.send_json({"type": "error", **payload})
                    else:
                        await ws.send_json({"type": "final", **payload})

            async def process_canceled():
                while True:
                    item = await canceled_q.get()
                    await ws.send_json({"type": "error", **item})

            async def process_session():
                while True:
                    await session_started_q.get()
                    await ws.send_json({"type": "session", "event": "started"})

            async def process_session_stop():
                while True:
                    await session_stopped_q.get()
                    await ws.send_json({"type": "session", "event": "stopped"})

            try:
                await asyncio.gather(
                    process_recognizing(),
                    process_recognized(),
                    process_canceled(),
                    process_session(),
                    process_session_stop(),
                )
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        pump_task = asyncio.create_task(pump_events())

        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            if msg["type"] == "websocket.receive" and "text" in msg:
                try:
                    data = json.loads(msg["text"]) if msg.get("text") else {}
                    if data.get("event") == "stop":
                        # Do not close WS; keep session alive. We simply restart endpointing implicitly.
                        await ws.send_json({"type": "info", "event": "stop_ack"})
                        continue
                except Exception:
                    pass

            if msg["type"] == "websocket.receive" and msg.get("bytes"):
                data_bytes = msg["bytes"]
                if normalize_flag:
                    try:
                        arr = np.frombuffer(data_bytes, dtype=np.int16)
                        if arr.size > 0:
                            maxv = int(np.max(np.abs(arr)))
                            if maxv > 0:
                                # Aim for ~30000 peak, cap gain to 3x to avoid artifacts
                                gain = min(3.0, 30000.0 / float(maxv))
                                if gain > 1.0:
                                    arr = np.clip(arr.astype(np.float32) * gain, -32768.0, 32767.0).astype(np.int16)
                                    data_bytes = arr.tobytes()
                    except Exception:
                        # Fallback to raw bytes in case of parsing issues
                        data_bytes = msg["bytes"]
                push_stream.write(data_bytes)

        push_stream.close()
        await asyncio.get_event_loop().run_in_executor(None, recognizer.stop_continuous_recognition)
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass
        await ws.close()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "error": str(e)})
            await ws.close()
        except Exception:
            pass

@app.post("/v1/speech/stt")
async def stt_file(
        audio: UploadFile = File(...),
        language: Optional[str] = None,
        endpoint_id: Optional[str] = None,
        x_api_key: Optional[str] = Header(default=None, alias="X-API-Key", include_in_schema=False),
):
    await require_api_key(x_api_key)

    wav_bytes = await audio.read()
    push_stream = speechsdk.audio.PushAudioInputStream()
    push_stream.write(wav_bytes)
    push_stream.close()

    audio_config = speechsdk.audio.AudioConfig(stream=push_stream)
    cfg = make_speech_config(endpoint_id or DEFAULT_STT_ENDPOINT_ID, language)
    recognizer = speechsdk.SpeechRecognizer(speech_config=cfg, audio_config=audio_config)
    result = recognizer.recognize_once()

    if result.reason == speechsdk.ResultReason.RecognizedSpeech:
        raw = result.properties.get(speechsdk.PropertyId.SpeechServiceResponse_JsonResult)
        return JSONResponse({"text": result.text, "raw": json.loads(raw) if raw else None})
    elif result.reason == speechsdk.ResultReason.NoMatch:
        raise HTTPException(422, detail="No speech recognized")
    else:
        det = result.cancellation_details
        raise HTTPException(502, detail=f"STT canceled: {det.reason} {det.error_details}")

@app.post("/v1/speech/tts")
async def tts(
        payload: dict,
        x_api_key: Optional[str] = Header(default=None, alias="X-API-Key", include_in_schema=False),
):
    await require_api_key(x_api_key)

    text = payload.get("text")
    if not text:
        raise HTTPException(400, detail="Missing 'text'")
    voice = payload.get("voiceName", DEFAULT_TTS_VOICE)
    endpoint_id = payload.get("deploymentId")
    fmt = payload.get("format", "riff-16khz-16bit-mono-pcm")

    cfg = make_speech_config(endpoint_id=endpoint_id)
    cfg.speech_synthesis_voice_name = voice

    enum_name = fmt.replace('-', '_').upper()
    out_fmt = getattr(speechsdk.SpeechSynthesisOutputFormat, enum_name, speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm)
    cfg.set_speech_synthesis_output_format(out_fmt)

    synthesizer = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)
    result = await asyncio.get_event_loop().run_in_executor(None, lambda: synthesizer.speak_text_async(text).get())

    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        audio = result.audio_data
        media = "audio/wav" if "riff" in fmt or "wav" in fmt else "application/octet-stream"
        return StreamingResponse(iter([audio]), media_type=media)
    else:
        det = result.cancellation_details
        raise HTTPException(502, detail=f"TTS canceled: {det.reason} {det.error_details}")

@app.post("/v1/clu/predict")
async def clu_predict(
        payload: dict,
        x_api_key: Optional[str] = Header(default=None, alias="X-API-Key", include_in_schema=False),
):
    await require_api_key(x_api_key)

    text = payload.get("text")
    if not text:
        raise HTTPException(400, detail="Missing 'text'")
    project = payload.get("projectName", CLU_PROJECT)
    deployment = payload.get("deploymentName", CLU_DEPLOYMENT)
    locale = payload.get("locale", "kk-KZ")

    if not all([LANG_ENDPOINT, LANG_KEY, project, deployment]):
        raise HTTPException(500, detail="CLU config missing")

    client = ConversationAnalysisClient(LANG_ENDPOINT, AzureKeyCredential(LANG_KEY))
    with client:
        res = client.analyze_conversation(
            task={
                "kind": "Conversation",
                "analysisInput": {
                    "conversationItem": {
                        "id": "1",
                        "participantId": "user",
                        "text": text,
                        "modality": "text",
                        "language": locale,
                    }
                },
                "parameters": {
                    "projectName": project,
                    "deploymentName": deployment,
                    "verbose": True,
                },
            }
        )
    pred = res["result"]["prediction"]
    return {"topIntent": pred.get("topIntent"), "intents": pred.get("intents", []), "entities": pred.get("entities", []), "raw": res}

@app.get("/health")
async def health():
    return {"status": "ok"}