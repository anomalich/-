"""
Мониторинг Minecraft-сервера через игрока-бота.
Каждые CHECK_INTERVAL секунд проверяет, есть ли ник бота в списке игроков.
При смене состояния отправляет сообщение в Telegram.

Установка:  pip install mcstatus requests
Запуск:     python mc_monitor.py
"""

import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
from mcstatus import JavaServer

# ========== НАСТРОЙКИ ==========
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]   # задаётся в настройках Render
CHAT_ID = os.environ["CHAT_ID"]                 # задаётся в настройках Render

SERVER_ADDRESS = "on3daysmp.aternos.me:40856"   # адрес сервера
BOT_NICKNAME = "bratok"                 # ник игрока-бота на сервере

CHECK_INTERVAL = 30    # как часто проверять (сек)
FAILS_TO_OFFLINE = 3   # сколько проверок подряд бот должен отсутствовать,
                       # прежде чем считать сервер отключенным (защита от ложных срабатываний)

# Если Telegram заблокирован у провайдера и нет VPN, укажите прокси, например:
# "socks5://127.0.0.1:1080" или "http://127.0.0.1:8080". Иначе оставьте пустым.
PROXY = os.environ.get("PROXY", "")   # на Render прокси не нужен

MSG_ONLINE = "Сервер работает ✅"
MSG_OFFLINE = "Сервер отключен ❌"
# ===============================


def send_telegram(text: str, chat_id: str = None) -> bool:
    """Возвращает True, если сообщение доставлено."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    try:
        r = requests.post(url, data={"chat_id": chat_id or CHAT_ID, "text": text},
                          timeout=15, proxies=proxies)
        if not r.ok:
            print(f"[Telegram] Ошибка {r.status_code}: {r.text}")
        return r.ok
    except requests.RequestException as e:
        print(f"[Telegram] Не удалось отправить, повторю позже: {type(e).__name__}")
        return False


def check_via_api(prefix: str = ""):
    """Запасной способ: статус сервера спрашиваем у api.mcsrvstat.us."""
    url = f"https://api.mcsrvstat.us/3/{SERVER_ADDRESS}"
    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    try:
        r = requests.get(url, timeout=20, proxies=proxies,
                         headers={"User-Agent": "mc-status-telegram-bot"})
        data = r.json()
    except Exception as e:
        return False, f"{prefix}Через mcsrvstat.us тоже не вышло: {type(e).__name__}: {e}"

    players = data.get("players") or {}
    raw = players.get("list") or []
    names = [p.get("name", "") if isinstance(p, dict) else str(p) for p in raw]
    try:
        count = int(players.get("online") or 0)
    except (TypeError, ValueError):
        count = 0
    # сервер считается работающим, если на нём есть бот ИЛИ хотя бы один игрок
    found = any(n.lower() == BOT_NICKNAME.lower() for n in names) or count >= 1
    info = (f"{prefix}Через mcsrvstat.us: online={data.get('online')}, "
            f"игроков {players.get('online', '?')}/{players.get('max', '?')}, "
            f"список имён: {names if names else 'пусто'}")
    return found, info


def check_server(server: JavaServer):
    """Возвращает (бот_онлайн, подробности для диагностики)."""
    try:
        status = server.status()
    except Exception as e:
        # Прямое подключение не удалось (например, Aternos не пускает с IP Render).
        # Пробуем спросить через внешний сервис mcsrvstat.us
        return check_via_api(f"Прямое подключение не удалось ({type(e).__name__}). ")

    sample = status.players.sample or []
    names = [p.name for p in sample]
    try:
        motd = status.motd.to_plain()
    except Exception:
        motd = "?"
    info = (f"Сервер ответил. Игроков: {status.players.online}/{status.players.max}. "
            f"Видимый список имён: {names if names else 'пусто'}. "
            f"Версия: {status.version.name}. MOTD: {motd}")
    # сервер считается работающим, если на нём есть бот ИЛИ хотя бы один игрок
    found = any(n.lower() == BOT_NICKNAME.lower() for n in names) or status.players.online >= 1
    return found, info


_last_info = None


def bot_is_online(server: JavaServer) -> bool:
    global _last_info
    found, info = check_server(server)
    if info != _last_info:      # пишем в лог только когда ответ изменился
        print(f"[Проверка] {info}")
        _last_info = info
    return found


def tg_api(method: str, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    r = requests.post(url, data=params, timeout=45, proxies=proxies)
    return r.json()


def commands_loop() -> None:
    """Слушает команды /status от участников чата и отвечает актуальным статусом."""
    server = JavaServer.lookup(SERVER_ADDRESS, timeout=10)
    offset = 0

    try:
        tg_api("setMyCommands",
               commands='[{"command":"status","description":"Проверить, работает ли сервер"}]')
    except Exception:
        pass

    while True:
        try:
            data = tg_api("getUpdates", offset=offset, timeout=30,
                          allowed_updates='["message"]')
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip().lower()
                if not text.startswith("/"):
                    continue
                cmd = text.split()[0].split("@")[0]   # "/status@botname" -> "/status"
                if cmd == "/debug":
                    _, info = check_server(server)
                    send_telegram(info, chat_id=msg["chat"]["id"])
                elif cmd in ("/status", "/start"):
                    answer = MSG_ONLINE if bot_is_online(server) else MSG_OFFLINE
                    send_telegram(answer, chat_id=msg["chat"]["id"])
        except Exception as e:
            print(f"[Команды] Ошибка: {type(e).__name__}, повтор через 5 сек")
            time.sleep(5)


class _Health(BaseHTTPRequestHandler):
    """Мини-веб-сервер: Render требует, чтобы сервис отвечал на HTTP-запросы."""
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self._ok()

    def log_message(self, *args):
        pass  # не засорять логи


def run_health_server() -> None:
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()


def main() -> None:
    server = JavaServer.lookup(SERVER_ADDRESS, timeout=10)
    state = None      # реальное состояние: None / True / False
    reported = None   # последнее состояние, которое УСПЕШНО отправили в Telegram
    fails = 0

    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=commands_loop, daemon=True).start()
    print("Мониторинг запущен...")
    while True:
        online = bot_is_online(server)

        if online:
            fails = 0
            if state is not True:
                state = True
                print("-> онлайн")
        else:
            fails += 1
            if state is not False and (fails >= FAILS_TO_OFFLINE or state is None):
                state = False
                print("-> офлайн")

        # если состояние не доставлено в Telegram, пробуем снова при каждой проверке
        if state is not None and state != reported:
            if send_telegram(MSG_ONLINE if state else MSG_OFFLINE):
                reported = state
                print("   отправлено в Telegram")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
