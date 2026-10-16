"""
Versión para GitHub Actions del bot de Vinted.

En cada ejecución (que lanza GitHub cada ~5 min) el script:
  1. lee los comandos que le hayas mandado al bot (/add, /list, /remove),
  2. revisa tus filtros cada CHECK_EVERY_SECONDS durante RUN_SECONDS,
  3. guarda el estado en state.json (el workflow lo conserva entre ejecuciones).
"""
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qsl

from curl_cffi import requests as cffi_requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "270"))
CHECK_EVERY = int(os.getenv("CHECK_EVERY_SECONDS", "60"))
FEE_FIXED = float(os.getenv("FEE_FIXED", "0.70"))
FEE_PERCENT = float(os.getenv("FEE_PERCENT", "5"))
SHIPPING_FROM = float(os.getenv("SHIPPING_FROM", "2.50"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
MAX_SEEN = 500

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vinted-once")

PARAM_MAP = {
    "catalog": "catalog_ids",
    "catalog_ids": "catalog_ids",
    "brand_ids": "brand_ids",
    "size_ids": "size_ids",
    "status_ids": "status_ids",
    "color_ids": "color_ids",
    "material_ids": "material_ids",
    "search_text": "search_text",
    "price_from": "price_from",
    "price_to": "price_to",
    "currency": "currency",
}

HELP = (
    "Pega /add seguido de la URL de Vinted con tus filtros.\n\n"
    "/add <url> — añadir filtro\n"
    "/list — ver filtros\n"
    "/remove <n> — borrar filtro\n\n"
    "Los comandos se responden en un máximo de ~1 minuto."
)

sessions: dict = {}


# ---------------- Estado ----------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state.json corrupto, empiezo de cero")
    return {"offset": 0, "chats": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# ---------------- Telegram ----------------
def tg(method: str, **data) -> dict:
    r = cffi_requests.post(f"{API}/{method}", json=data, timeout=40)
    r.raise_for_status()
    return r.json()


def send(chat, text: str) -> None:
    try:
        tg("sendMessage", chat_id=chat, text=text, disable_web_page_preview=True)
    except Exception as e:
        log.warning("No se pudo enviar mensaje: %s", e)


# ---------------- Vinted ----------------
def url_to_api(url: str):
    p = urlparse(url)
    if "vinted." not in p.netloc:
        raise ValueError("Eso no parece una URL de Vinted.")
    params: dict = {}
    for key, value in parse_qsl(p.query):
        key = key.replace("[]", "")
        if key in PARAM_MAP:
            params.setdefault(PARAM_MAP[key], []).append(value)
    api_params = {k: ",".join(v) for k, v in params.items()}
    api_params["order"] = "newest_first"
    api_params["per_page"] = "30"
    return p.netloc, api_params


def get_session(domain: str):
    s = sessions.get(domain)
    if s is None:
        s = cffi_requests.Session(impersonate="chrome")
        s.get(f"https://{domain}/", timeout=20)
        sessions[domain] = s
    return s


def fetch_items(domain: str, params: dict) -> list:
    for _ in range(2):
        s = get_session(domain)
        r = s.get(f"https://{domain}/api/v2/catalog/items", params=params, timeout=20)
        if r.status_code in (401, 403):
            sessions.pop(domain, None)
            continue
        r.raise_for_status()
        return r.json().get("items", [])
    raise RuntimeError("Vinted ha bloqueado la petición (401/403).")


def to_float(v) -> float:
    if isinstance(v, dict):
        v = v.get("amount")
    return float(v)


def format_item(item: dict) -> str:
    price = to_float(item["price"])
    with_fee = price + FEE_FIXED + price * FEE_PERCENT / 100
    with_ship = with_fee + SHIPPING_FROM
    return "\n".join([
        f"🆕 {item.get('title', '')}",
        f"🏷️ {item.get('brand_title') or ''}  |  📏 Talla: {item.get('size_title') or '—'}",
        f"💶 Precio: {price:.2f} €",
        f"🛡️ Con tasa: {with_fee:.2f} €",
        f"📦 Con tasa + envío (desde {SHIPPING_FROM:.2f} €): {with_ship:.2f} €",
        f"🔗 {item.get('url', '')}",
    ])


# ---------------- Comandos ----------------
def handle_command(state: dict, chat: str, text: str) -> None:
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    filters = state["chats"].setdefault(chat, [])

    if cmd in ("/start", "/help"):
        send(chat, "Hola 👋\n" + HELP)
    elif cmd == "/add":
        if not args:
            send(chat, "Uso: /add <url de Vinted>")
            return
        try:
            domain, params = url_to_api(args[0])
            items = fetch_items(domain, params)
        except Exception as e:
            send(chat, f"No he podido usar esa URL: {e}")
            return
        filters.append({"url": args[0], "seen": [i["id"] for i in items][:MAX_SEEN]})
        send(chat, f"✅ Filtro #{len(filters)} añadido. Te aviso de lo nuevo que se suba.")
    elif cmd == "/list":
        if not filters:
            send(chat, "No tienes filtros. Usa /add <url>.")
        else:
            send(chat, "\n\n".join(f"#{n}: {f['url']}" for n, f in enumerate(filters, 1)))
    elif cmd == "/remove":
        try:
            removed = filters.pop(int(args[0]) - 1)
        except (IndexError, ValueError):
            send(chat, "Uso: /remove <número> (mira /list).")
            return
        send(chat, f"🗑️ Borrado: {removed['url']}")


def process_updates(state: dict) -> None:
    try:
        res = tg("getUpdates", offset=state["offset"], timeout=0, allowed_updates=["message"])
    except Exception as e:
        log.warning("getUpdates falló: %s", e)
        return
    for upd in res.get("result", []):
        state["offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        text = msg.get("text")
        if text and text.startswith("/"):
            handle_command(state, str(msg["chat"]["id"]), text)


# ---------------- Vigilancia ----------------
def check_filters(state: dict) -> None:
    for chat, filters in state["chats"].items():
        for f in filters:
            try:
                domain, params = url_to_api(f["url"])
                items = fetch_items(domain, params)
            except Exception as e:
                log.warning("Error en filtro %s: %s", f["url"], e)
                continue
            seen = set(f["seen"])
            for item in reversed([i for i in items if i["id"] not in seen]):
                send(chat, format_item(item))
                f["seen"].append(item["id"])
            f["seen"] = f["seen"][-MAX_SEEN:]


def main() -> None:
    state = load_state()
    end = time.time() + RUN_SECONDS
    while True:
        process_updates(state)
        check_filters(state)
        save_state(state)
        if time.time() + CHECK_EVERY > end:
            break
        time.sleep(CHECK_EVERY)
    log.info("Ejecución terminada")


if __name__ == "__main__":
    main()
