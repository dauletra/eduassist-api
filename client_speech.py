import os
import sys
import asyncio
import json
from typing import Optional

import numpy as np
import websockets
import sounddevice as sd

RATE = 16000  # 16 kHz mono

# Try to load API key and other settings from .env if available
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

API_KEY = os.getenv("API_KEY", "your_api_key_for_clients")
LANGUAGE = os.getenv("LANGUAGE", "kk-KZ")
HOST = os.getenv("API_HOST", "localhost:8080")  # without scheme
NORMALIZE = os.getenv("NORMALIZE", "0").lower() in ("1", "true", "yes")

# End-of-utterance (silence) detector
ENERGY_THRESHOLD = int(os.getenv("ENERGY_THRESHOLD", "500"))  # int16 avg abs threshold
END_SIL_MS = int(os.getenv("END_SIL_MS", "800"))  # required trailing silence to stop, ms


def print_instructions(ws_url: str, frame_len: int):
    print("================ Voice Assistant Client ==================")
    print("WebSocket:", ws_url)
    print("Mode: Press SPACE to start recording → stream one command → server detects end-of-utterance")
    print(f"Audio: 16 kHz, 16-bit, mono, blocksize={frame_len} samples")
    print("Controls:")
    print("  SPACE   — start recording")
    print("  Q or Esc — quit")
    print("  X        — cancel current recording and send {\"event\":\"stop\"}")
    print("==========================================================")


async def run():
    frame_len = 512  # reasonable default frame size
    chunk_ms = int(1000 * frame_len / RATE)

    normalize_param = "1" if NORMALIZE else "0"
    uri = f"ws://{HOST}/v1/speech/stt/stream?language={LANGUAGE}&api_key={API_KEY}&normalize={normalize_param}"

    async with websockets.connect(uri, max_size=2**22, ping_interval=15) as ws:
        print_instructions(uri, frame_len)
        ready = await ws.recv()
        print("server:", ready)

        # Queues and state
        audio_q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=20)
        send_q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=20)
        stream: Optional[sd.InputStream] = None
        state = "idle"  # "idle" | "recording"
        stop_recording_evt = asyncio.Event()

        def sd_callback(indata, frames, time_info, status):
            try:
                # indata is NumPy array int16
                audio_q.put_nowait(indata.copy().tobytes())
            except asyncio.QueueFull:
                pass

        async def sender_task():
            try:
                while True:
                    chunk = await send_q.get()
                    if chunk is None:
                        break
                    await ws.send(chunk)
            except Exception as e:
                print("[sender] error:", e)

        def print_partial(text: str):
            sys.stdout.write("\r[partial] " + text[:160].ljust(160))
            sys.stdout.flush()

        def print_final(text: str):
            sys.stdout.write("\r" + " " * 180 + "\r")
            sys.stdout.write("[final]  " + text + "\n")
            sys.stdout.flush()
            # After one final result, end this utterance; stop sending more frames
            try:
                stop_recording_evt.set()
            except Exception:
                pass

        async def receiver_task():
            try:
                while True:
                    msg = await ws.recv()
                    try:
                        data = json.loads(msg) if isinstance(msg, str) else None
                    except Exception:
                        data = None
                    if isinstance(data, dict):
                        t = data.get("type")
                        if t == "partial":
                            print_partial(data.get("text", ""))
                        elif t == "final":
                            text = data.get("text") or data.get("raw", {}).get("DisplayText") or ""
                            print_final(text)
                        elif t == "error":
                            sys.stdout.write("\n[error] " + str(data) + "\n")
                            sys.stdout.flush()
                        elif t == "ready":
                            sys.stdout.write("[client] server ready\n")
                            sys.stdout.flush()
                    else:
                        print(msg)
            except Exception as e:
                print("[receiver] stopped:", e)

        async def processor_task():
            nonlocal state
            leftover = b""
            while True:
                data = await audio_q.get()
                if data is None:
                    break
                buf = leftover + data
                # process in frames (frame_len samples -> *2 bytes)
                bytes_per_frame = frame_len * 2
                offset = 0
                while offset + bytes_per_frame <= len(buf):
                    frame_bytes = buf[offset:offset + bytes_per_frame]
                    offset += bytes_per_frame

                    # When recording, forward audio; rely on server endpointing
                    if state == "recording":
                        # Forward frame to server
                        try:
                            send_q.put_nowait(frame_bytes)
                        except asyncio.QueueFull:
                            pass
                        # End this utterance when receiver signals final
                        if stop_recording_evt.is_set():
                            state = "idle"
                leftover = buf[offset:]

        async def start_stream():
            nonlocal stream
            if stream is None:
                stream = sd.InputStream(samplerate=RATE, channels=1, dtype='int16', blocksize=frame_len, callback=sd_callback)
                stream.start()
                print("[client] microphone active — press SPACE to start recording...")

        async def stop_stream(send_stop_event: bool = True):
            nonlocal stream, state
            if stream is not None:
                stream.stop()
                stream.close()
                stream = None
                print("\n[client] microphone stopped")
            state = "idle"
            if send_stop_event:
                try:
                    await ws.send(json.dumps({"event": "stop"}))
                except Exception:
                    pass

        # Start background tasks
        send_task = asyncio.create_task(sender_task())
        recv_task = asyncio.create_task(receiver_task())
        proc_task = asyncio.create_task(processor_task())

        # Start mic in idle mode
        await start_stream()

        async def key_listener():
            nonlocal state  # Добавлено nonlocal для доступа к переменной state
            # SPACE to start recording, Q/Esc quit; X cancel current
            if sys.platform.startswith("win"):
                import msvcrt
                try:
                    while True:
                        if msvcrt.kbhit():
                            ch = msvcrt.getwch()
                            if ch in ('q', 'Q', '\u001b'):
                                await stop_stream(send_stop_event=True)
                                break
                            elif ch in ('x', 'X'):
                                # cancel current recording (if any)
                                try:
                                    await ws.send(json.dumps({"event": "stop"}))
                                except Exception:
                                    pass
                                state = "idle"
                                stop_recording_evt.set()
                                print("\n[client] recording cancelled")
                            elif ch == ' ':
                                # SPACE to start recording
                                if state == "idle":
                                    state = "recording"
                                    stop_recording_evt.clear()
                                    print("\n[client] recording started — speak now...")
                        await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    pass
            else:
                try:
                    while True:
                        line = await asyncio.to_thread(input, "(space=record, x=cancel, q=quit)> ")
                        ch = (line or "").strip()[:1]
                        if ch in ('q', 'Q'):
                            await stop_stream(send_stop_event=True)
                            break
                        elif ch in ('x', 'X'):
                            try:
                                await ws.send(json.dumps({"event": "stop"}))
                            except Exception:
                                pass
                            state = "idle"
                            stop_recording_evt.set()
                            print("\n[client] recording cancelled")
                        elif ch == '':
                            # SPACE (empty input when pressing just Enter)
                            if state == "idle":
                                state = "recording"
                                stop_recording_evt.clear()
                                print("\n[client] recording started — speak now...")
                except asyncio.CancelledError:
                    pass

        key_task = asyncio.create_task(key_listener())

        try:
            await key_task
        except KeyboardInterrupt:
            await stop_stream(send_stop_event=True)
        finally:
            try:
                audio_q.put_nowait(None)
                send_q.put_nowait(None)
            except Exception:
                pass
            await asyncio.sleep(0)
            for t in (send_task, recv_task, proc_task):
                t.cancel()
            try:
                await ws.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(run())