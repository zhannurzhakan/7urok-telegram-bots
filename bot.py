"""Telegram-бот: на любое сообщение отвечает временем, погодой в Астане и курсом биткойна.

Только стандартная библиотека Python (3.9+). Погода: Open-Meteo, курс: CoinGecko
(ключи не нужны).
Токен берётся из переменной BOT_TOKEN (файл .env рядом с bot.py).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

LAT, LON = 51.1694, 71.4491  # Астана
CACHE_TTL = 600  # секунд; не дёргаем Open-Meteo на каждое сообщение
BTC_CACHE_TTL = 60  # курс меняется быстро, но у CoinGecko есть лимиты

WEATHER_CODES = {
    0: "☀️ Ясно",
    1: "🌤 Преимущественно ясно",
    2: "⛅️ Переменная облачность",
    3: "☁️ Пасмурно",
    45: "🌫 Туман",
    48: "🌫 Изморозь",
    51: "🌦 Лёгкая морось",
    53: "🌦 Морось",
    55: "🌦 Сильная морось",
    56: "🌧 Ледяная морось",
    57: "🌧 Сильная ледяная морось",
    61: "🌧 Небольшой дождь",
    63: "🌧 Дождь",
    65: "🌧 Сильный дождь",
    66: "🌧 Ледяной дождь",
    67: "🌧 Сильный ледяной дождь",
    71: "🌨 Небольшой снег",
    73: "🌨 Снег",
    75: "❄️ Сильный снег",
    77: "🌨 Снежные зёрна",
    80: "🌦 Небольшой ливень",
    81: "🌧 Ливень",
    82: "⛈ Сильный ливень",
    85: "🌨 Снегопад",
    86: "❄️ Сильный снегопад",
    95: "⛈ Гроза",
    96: "⛈ Гроза с градом",
    99: "⛈ Сильная гроза с градом",
}


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def http_json(url: str, data: dict = None, timeout: int = 40) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"} if body else {}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def get_time_text() -> str:
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Almaty")  # Астана, UTC+5
    except Exception:  # noqa: BLE001  нет zoneinfo/tzdata: в Казахстане нет перехода на летнее время
        tz = timezone(timedelta(hours=5))
    now = datetime.now(tz)
    return (
        f"🕒 {now:%H:%M}, {WEEKDAYS[now.weekday()]}\n"
        f"📅 {now.day} {MONTHS[now.month - 1]} {now.year}"
    )


_cache = {"ts": 0.0, "text": ""}
_btc_cache = {"ts": 0.0, "text": ""}


def get_btc_text() -> str:
    if _btc_cache["text"] and time.time() - _btc_cache["ts"] < BTC_CACHE_TTL:
        return _btc_cache["text"]

    params = urllib.parse.urlencode(
        {"ids": "bitcoin", "vs_currencies": "usd,kzt", "include_24hr_change": "true"}
    )
    btc = http_json("https://api.coingecko.com/api/v3/simple/price?" + params, timeout=15)["bitcoin"]

    arrow = "📈" if btc["usd_24h_change"] >= 0 else "📉"
    text = (
        f"₿ Курс биткойна\n"
        f"💵 ${btc['usd']:,.0f}\n"
        f"🇰🇿 {btc['kzt']:,.0f} ₸\n"
        f"{arrow} {btc['usd_24h_change']:+.1f}% за 24 ч"
    ).replace(",", " ")
    _btc_cache.update(ts=time.time(), text=text)
    return text


def get_weather_text() -> str:
    if _cache["text"] and time.time() - _cache["ts"] < CACHE_TTL:
        return _cache["text"]

    params = urllib.parse.urlencode(
        {
            "latitude": LAT,
            "longitude": LON,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "surface_pressure,wind_speed_10m,weather_code",
            "wind_speed_unit": "ms",
            "timezone": "Asia/Almaty",
        }
    )
    cur = http_json("https://api.open-meteo.com/v1/forecast?" + params, timeout=15)["current"]

    desc = WEATHER_CODES.get(cur["weather_code"], "Погода")
    text = (
        f"Погода в Астане сейчас\n"
        f"{desc}\n"
        f"🌡 {cur['temperature_2m']:+.0f}°C (ощущается как {cur['apparent_temperature']:+.0f}°C)\n"
        f"💨 Ветер {cur['wind_speed_10m']:.0f} м/с\n"
        f"💧 Влажность {cur['relative_humidity_2m']}%\n"
        f"🧭 Давление {cur['surface_pressure'] * 0.750062:.0f} мм рт. ст."
    )
    _cache.update(ts=time.time(), text=text)
    return text


def main() -> None:
    load_env(Path(__file__).with_name(".env"))
    token = os.environ.get("BOT_TOKEN")
    if not token:
        sys.exit("BOT_TOKEN не задан (добавьте его в .env)")
    api = f"https://api.telegram.org/bot{token}/"

    def call(method: str, **params) -> dict:
        try:
            resp = http_json(api + method, params, timeout=params.get("timeout", 0) + 15)
        except urllib.error.HTTPError as e:
            resp = json.load(e)
        if not resp.get("ok"):
            wait = resp.get("parameters", {}).get("retry_after")
            if wait:
                time.sleep(wait)
            raise RuntimeError(f"{method}: {resp.get('error_code')} {resp.get('description')}")
        return resp["result"]

    me = call("getMe")
    print(f"Бот @{me['username']} запущен. Ctrl+C для остановки.", flush=True)
    # polling и webhook несовместимы; drop_pending_updates отбрасывает сообщения,
    # накопившиеся, пока бот был офлайн, чтобы не отвечать на старые запросы
    call("deleteWebhook", drop_pending_updates=True)

    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            for update in call("getUpdates", **params):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg or msg["chat"]["type"] != "private":
                    continue
                parts = []
                for getter, err in (
                    (get_time_text, "Не удалось определить время."),
                    (get_weather_text, "Не удалось получить погоду."),
                    (get_btc_text, "Не удалось получить курс биткойна."),
                ):
                    try:
                        parts.append(getter())
                    except Exception as e:  # noqa: BLE001
                        print(f"{getter.__name__} error:", e, flush=True)
                        parts.append(err)
                call("sendMessage", chat_id=msg["chat"]["id"], text="\n\n".join(parts))
        except KeyboardInterrupt:
            print("Остановлен.")
            return
        except Exception as e:  # noqa: BLE001
            print("error:", e, flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
