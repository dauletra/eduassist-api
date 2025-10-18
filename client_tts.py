import os
import sys
import json
import platform
from typing import List, Optional

import requests

# Auto-load environment variables from .env if available
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

API_KEY = os.getenv("API_KEY", "your_api_key_for_clients")
API_HOST = os.getenv("API_HOST", "localhost:8080")  # host:port without scheme
DEFAULT_VOICE = os.getenv("AZURE_TTS_VOICE", "ru-RU-DmitryNeural")
DEFAULT_FORMAT = os.getenv("TTS_FORMAT", "riff-16khz-16bit-mono-pcm")
DEFAULT_OUT = os.getenv("TTS_OUT", "out.wav")
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
AUTO_PLAY = os.getenv("TTS_AUTO_PLAY", "1").lower() in ("1", "true", "yes")

URL = f"http://{API_HOST}/v1/speech/tts"


def print_usage():
    print("Usage:")
    print("  python client_tts.py \"text to synthesize\" [--voice VOICE] [--format FMT] [--out FILE] [--no-play]")
    print("  python client_tts.py --demo")
    print("")
    print("Examples:")
    print("  python client_tts.py \"Привет! Это проверка синтеза речи.\"")
    print("  python client_tts.py --demo")
    print("")
    print("Env:")
    print("  API_HOST=localhost:8080  API_KEY=...  AZURE_TTS_VOICE=ru-RU-DmitryNeural  TTS_FORMAT=riff-16khz-16bit-mono-pcm  TTS_OUT=out.wav  TTS_AUTO_PLAY=1")


def parse_args(argv: List[str]):
    if not argv:
        print_usage()
        sys.exit(1)

    if argv[0] == "--demo":
        return {"demo": True, "play": True}

    # collect flags
    voice: Optional[str] = None
    fmt: Optional[str] = None
    out: Optional[str] = None
    play: Optional[bool] = None

    # extract flags from the tail
    args = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--voice" and i + 1 < len(argv):
            voice = argv[i + 1]
            i += 2
        elif a == "--format" and i + 1 < len(argv):
            fmt = argv[i + 1]
            i += 2
        elif a == "--out" and i + 1 < len(argv):
            out = argv[i + 1]
            i += 2
        elif a == "--no-play":
            play = False
            i += 1
        else:
            args.append(a)
            i += 1

    text = " ".join(args).strip()
    if not text:
        print_usage()
        sys.exit(1)

    return {
        "demo": False,
        "text": text,
        "voice": voice or DEFAULT_VOICE,
        "format": fmt or DEFAULT_FORMAT,
        "out": out or DEFAULT_OUT,
        "play": AUTO_PLAY if play is None else play,
    }


def _play_audio_file(path: str, media_type: str) -> None:
    """Attempt to play the audio file immediately.
    - On Windows and WAV content, use built-in winsound.
    - Otherwise try simpleaudio if installed; if not, print a hint.
    """
    try:
        # If not WAV-like, skip trying to play
        is_wav_like = media_type.startswith("audio/wav") or media_type.startswith("audio/x-wav") or path.lower().endswith(".wav")
        if not is_wav_like:
            print(f"[info] Playback skipped (unsupported media type: {media_type}). File saved: {path}")
            return

        if platform.system().lower().startswith("win"):
            try:
                import winsound  # type: ignore
                print(f"[play] Playing '{os.path.basename(path)}'...")
                winsound.PlaySound(path, winsound.SND_FILENAME)
                return
            except Exception as e:
                print(f"[warn] winsound playback failed: {e}")
        # Cross-platform fallback: simpleaudio (optional dependency)
        try:
            import simpleaudio as sa  # type: ignore
            import wave
            with wave.open(path, 'rb') as wf:
                data = wf.readframes(wf.getnframes())
                play_obj = sa.play_buffer(data, wf.getnchannels(), wf.getsampwidth(), wf.getframerate())
                play_obj.wait_done()
            return
        except Exception as e:
            print(f"[info] Could not auto-play (simpleaudio not available or failed: {e}).")
            print(f"[info] Audio saved to: {path}")
    except Exception as e:
        print(f"[playback error] {e}")


def synthesize(text: str, voice: str, fmt: str, out_file: str, play: bool) -> bool:
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
    }
    payload = {
        "text": text,
        "voiceName": voice,
        "format": fmt,
    }

    try:
        # stream response to file to handle potentially large audio
        with requests.post(URL, headers=headers, json=payload, timeout=TIMEOUT, stream=True) as resp:
            if resp.status_code != 200:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                print(f"[http {resp.status_code}] {detail}")
                return False

            # ensure directory exists
            out_dir = os.path.dirname(os.path.abspath(out_file))
            if out_dir and not os.path.exists(out_dir):
                os.makedirs(out_dir, exist_ok=True)

            # choose extension based on format if user left default filename
            target_path = out_file
            if out_file == DEFAULT_OUT:
                # switch to .wav for riff/wav, or .bin otherwise
                if "riff" in fmt or "wav" in fmt:
                    target_path = out_file if out_file.lower().endswith(".wav") else os.path.splitext(out_file)[0] + ".wav"
                else:
                    target_path = out_file if out_file.lower().endswith(".bin") else os.path.splitext(out_file)[0] + ".bin"

            with open(target_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)

            media_type = resp.headers.get("Content-Type", "audio/wav")
            size = os.path.getsize(target_path)
            print(f"[ok] saved {size} bytes to {target_path} (Content-Type: {media_type})")

            if play:
                _play_audio_file(target_path, media_type)
            return True
    except requests.exceptions.RequestException as e:
        print(f"[network error] {e}")
        return False
    except OSError as e:
        print(f"[file error] {e}")
        return False


def main(argv: List[str]) -> int:
    if not API_KEY or API_KEY == "your_api_key_for_clients":
        print("[warn] API_KEY looks default; set API_KEY in .env for authenticated requests.")

    args = parse_args(argv)
    if args.get("demo"):
        tests = [
            ("Привет! Это проверка синтеза речи.", DEFAULT_VOICE, DEFAULT_FORMAT, DEFAULT_OUT),
            ("Поставь таймер на десять минут.", DEFAULT_VOICE, DEFAULT_FORMAT, DEFAULT_OUT),
        ]
        ok_all = True
        for (text, voice, fmt, out_file) in tests:
            print(f"\n>>> text='{text}' voice='{voice}' format='{fmt}'")
            ok = synthesize(text, voice, fmt, out_file, play=True)
            ok_all = ok_all and ok
        return 0 if ok_all else 2

    text = args["text"]
    voice = args["voice"]
    fmt = args["format"]
    out_file = args["out"]
    play = args.get("play", True)

    print(f">>> text='{text}'\nvoice='{voice}'\nformat='{fmt}'\nout='{out_file}'\nplay={'yes' if play else 'no'}")
    ok = synthesize(text, voice, fmt, out_file, play)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
