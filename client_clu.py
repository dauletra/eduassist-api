import os
import sys
import json
from typing import Optional, List

import requests

# Auto-load environment variables from .env if available
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

API_KEY = os.getenv("API_KEY", "your_api_key_for_clients")
API_HOST = os.getenv("API_HOST", "localhost:8080")  # host:port without scheme
LANGUAGE = os.getenv("LANGUAGE", "kk-KZ")
# CLU configuration is server-side; the client does not need any Azure keys or project names.

URL = f"http://{API_HOST}/v1/clu/predict"
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))


def print_usage():
    print("Usage:")
    print("  python client_clu.py [""text of the phrase"" | --demo]")
    print("")
    print("Examples:")
    print("  python client_clu.py \"открой журнал 9 в класса\"")
    print("  python client_clu.py \"подели учеников на 3 групп\"")
    print("  python client_clu.py --demo   # send two demo phrases in a row")
    print("")
    print("Env:")
    print("  API_HOST=localhost:8080  API_KEY=...  LANGUAGE=ru-RU")


def build_payload(text: str) -> dict:
    payload = {
        "text": text,
        "locale": LANGUAGE,
    }
    return payload


def pretty_print_result(rjson: dict):
    top = rjson.get("topIntent")
    print("\n=== CLU Result ===")
    print("topIntent:", top)

    intents = rjson.get("intents") or []
    if intents:
        print("intents:")
        for it in intents:
            name = it.get("category") or it.get("intent") or it.get("name")
            score = it.get("confidenceScore") or it.get("confidence")
            print(f"  - {name}: {score}")
    else:
        print("intents: []")

    entities = rjson.get("entities") or []
    if entities:
        print("entities:")
        for en in entities:
            cat = en.get("category")
            text = en.get("text")
            val = en.get("resolutions") or en.get("extraInformation")
            print(f"  - {cat}: '{text}' {val if val is not None else ''}")
    else:
        print("entities: []")


def send_phrase(text: str) -> bool:
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
    }
    payload = build_payload(text)

    try:
        resp = requests.post(URL, headers=headers, json=payload, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
        print(f"[network error] {e}")
        return False

    if resp.status_code != 200:
        # Try to show server-provided detail if any
        try:
            err = resp.json()
        except Exception:
            err = resp.text
        print(f"[http {resp.status_code}] {err}")
        return False

    try:
        data = resp.json()
    except Exception as e:
        print("[error] Failed to parse JSON:", e)
        print(resp.text[:500])
        return False

    pretty_print_result(data)
    return True


def main(argv: List[str]) -> int:
    if not API_KEY or API_KEY == "your_api_key_for_clients":
        print("[warn] API_KEY looks default; set API_KEY in .env for authenticated requests.")
    if len(argv) == 0:
        print_usage()
        return 1

    if argv[0] == "--demo":
        phrases = [
            "9 в екінші топтың журналын аш",
            "презентацияны аш",
            "видеоны жап",
            "оқушыларды екі топқа бөл",
            "журналды жап",
            "тапсырманы басып шығар",
            "5 минутқа таймер қой"
        ]
        ok_all = True
        for p in phrases:
            print("\n>>>", p)
            ok = send_phrase(p)
            ok_all = ok_all and ok
        return 0 if ok_all else 2

    text = " ".join(argv)
    print(">>>", text)
    ok = send_phrase(text)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
