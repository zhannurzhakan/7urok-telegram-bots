"""Telegram-бот «Чек-лист»: вечером принимает задачи на завтра, структурирует и напоминает.

Только стандартная библиотека Python (3.9+). Данные хранятся в tasks.json рядом с файлом.
Токен: CHECKLIST_BOT_TOKEN, а если его нет — BOT_TOKEN (файл .env).
Один токен может слушать только один запущенный бот: погодный bot.py и этот
одновременно на одном токене работать не смогут.

Структурирование без нейросети, по правилам: задачи делятся по частям дня
(утро/день/вечер), по темам (работа, учёба, дом, покупки…) и по важности.
"""
import json
import math
import os
import re
import sys
import time
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bot import MONTHS, WEEKDAYS, http_json, load_env

DATA_FILE = Path(__file__).with_name("tasks.json")
DEFAULT_EVENING = "21:00"  # когда спрашивать про завтра
DEFAULT_MORNING = "08:00"  # когда присылать план дня
REMIND_BEFORE_MIN = 15  # за сколько минут напоминать о задаче со временем
WINDOW = timedelta(hours=3)  # сколько после назначенного времени ещё можно слать напоминание
DAY_SPLIT_HOUR = 12  # до этого часа задачи идут на сегодня, после — на завтра
KEEP_DAYS = 7  # сколько дней истории хранить

HELP = """📝 Чек-лист на завтра

Вечером пришлите задачи на завтра. Можно по одной в строке, через запятую или «;». Я разложу их по порядку:
🔥 Важное: со словом «срочно»/«важно» или знаком «!»
🌅 Утро / ☀️ День / 🌆 Вечер: по времени (14:30) или по слову: «утром», «днём», «вечером»
💼 Работа, 📚 Учёба, 💪 Здоровье и спорт, 🏠 Дом, 🛒 Покупки, 📞 Звонки, 💳 Финансы: остальные задачи по смыслу

Примеры:
Курс по английскому 18:00
прогулка вечером, уборка
Срочно сдать отчёт

Чтобы получить напоминание, допишите время: «Прогулка 18:00» (повторять всю задачу не нужно, она обновится).

Напоминания (по умолчанию):
• в 21:00 спрошу, что запланировано на завтра
• в 08:00 пришлю план дня с кнопками «выполнено»
• за 15 минут до задач со временем

Команды:
/today: план на сегодня
/tomorrow: план на завтра
/del 2 5: удалить задачи по номерам
/clear: очистить список
/evening 21:30: во сколько спрашивать про завтра
/morning 07:45: во сколько присылать план дня

Время по Астане. Задачи, присланные до 12:00, идут на сегодня, после 12:00 — на завтра."""

NOT_TASKS = (
    "Не нашёл здесь задач. Напишите, что нужно сделать, например:\n"
    "«Прогулка вечером»\n«Курс по английскому 18:00»\n«Срочно сдать отчёт»\n\n"
    "Все возможности: /help"
)

BULLET = re.compile(r"^\s*(?:[-•*–—]+\s*|\d+[.)]\s+)")
TIME = re.compile(r"(?:\b[вк]\s+)?(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", re.IGNORECASE)
PART_WORD = re.compile(r"\b(?:с\s+утра|утром|днём|днем|вечером|ночью)\b", re.IGNORECASE)
IMPORTANT = re.compile(r"срочн|важн|!", re.IGNORECASE)
HHMM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
GREETING = re.compile(
    r"^(?:привет\w*|здравствуй\w*|добр\w+\s+(?:день|вечер|утро)|хай|hi|hello|спасибо|ок|окей|ладно|да|нет|тест)\W*$",
    re.IGNORECASE,
)
INFINITIVE_AND = re.compile(r"\s+и\s+(?=[а-яёa-z]+(?:ть|ться|ти|чь)\b)", re.IGNORECASE)

# Темы: порядок важен, побеждает первое совпадение.
CATEGORIES = [
    ("calls", "📞 Звонки", re.compile(r"позвонить|звонок|набрать|ответить|связаться")),
    ("shop", "🛒 Покупки", re.compile(r"купить|покупк|заказать|магазин|продукт|аптек|забрать")),
    ("work", "💼 Работа", re.compile(r"отч[её]т|встреч|созвон|письм|проект|клиент|презентац|договор|совещан|коллег|дедлайн|сдать|работ")),
    ("study", "📚 Учёба", re.compile(r"курс|урок|учить|выучить|лекци|экзамен|домашк|прочита|книг|вебинар|занят|англий|учёб|учеб")),
    ("health", "💪 Здоровье и спорт", re.compile(r"тренир|спорт|зал\b|бег\b|пробежк|йога|прогулк|гулять|врач|таблетк|зарядк|плаван|растяжк|массаж")),
    ("home", "🏠 Дом", re.compile(r"убор|убрать|прибра|постира|помыть|помою|приготов|готовить|мусор|пылесос|погладить|вынести|ужин|обед|завтрак")),
    ("money", "💳 Финансы и дела", re.compile(r"оплатить|оплата|сч[её]т|налог|перевести|банк|квитанц|документ|справк")),
]
PART_TITLES = {"morning": "🌅 Утро", "day": "☀️ День", "evening": "🌆 Вечер"}


# ---------- время ----------

def _make_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Almaty")  # Астана, UTC+5
    except Exception:  # noqa: BLE001  нет zoneinfo/tzdata: в Казахстане нет летнего времени
        return timezone(timedelta(hours=5))


TZ = _make_tz()


def now_local() -> datetime:
    return datetime.now(TZ)


def at(now: datetime, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def target_date(now: datetime) -> date:
    """Дата, на которую сейчас записываются новые задачи."""
    return now.date() if now.hour < DAY_SPLIT_HOUR else now.date() + timedelta(days=1)


# ---------- хранилище ----------

def load_state() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"chats": {}}


def save_state(state: dict) -> None:
    tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


def get_chat(state: dict, chat_id) -> dict:
    return state["chats"].setdefault(
        str(chat_id),
        {"evening": DEFAULT_EVENING, "morning": DEFAULT_MORNING, "next_id": 1, "days": {}, "sent": {}},
    )


# ---------- разбор задач ----------

def classify(text: str) -> str:
    low = text.lower()
    for key, _title, pattern in CATEGORIES:
        if pattern.search(low):
            return key
    return "other"


def part_of(time_str, word_part):
    """Часть дня: по времени, а если его нет — по слову «утром/днём/вечером»."""
    if time_str:
        hour = int(time_str[:2])
        return "morning" if hour < 12 else "day" if hour < 18 else "evening"
    return word_part


def split_line(line: str) -> list:
    """Делит строку на отдельные задачи: по запятым (если куски короткие) и по «и» перед глаголом."""
    pieces = [line]
    by_comma = [p.strip() for p in line.split(",")]
    if len(by_comma) > 1 and all(p and len(p.split()) <= 5 for p in by_comma):
        pieces = by_comma
    out = []
    for piece in pieces:
        out += [x.strip() for x in INFINITIVE_AND.split(piece) if x.strip()]
    return out


def parse_one(piece: str):
    if piece.endswith("?") or GREETING.match(piece.strip()):
        return None  # вопрос или приветствие, а не задача
    task_time = None
    word_part = None
    clean = piece
    m = TIME.search(clean)
    if m:
        task_time = f"{int(m.group(1)):02d}:{m.group(2)}"
        clean = clean[: m.start()] + " " + clean[m.end():]
    w = PART_WORD.search(clean)
    if w:
        word = w.group(0).lower()
        word_part = "morning" if "утр" in word else "day" if word.startswith("дн") or word.startswith("днё") else "evening"
        clean = clean[: w.start()] + " " + clean[w.end():]
    clean = re.sub(r"\s+", " ", clean).strip(" ,.-–—:")
    clean = clean.lstrip("!❗ ").strip() or piece.strip()
    return {
        "text": clean,
        "time": task_time,
        "part": part_of(task_time, word_part),
        "important": bool(IMPORTANT.search(piece)),
        "category": classify(clean),
    }


def parse_tasks(text: str) -> list:
    """Делит сообщение на задачи и достаёт время, часть дня, важность и тему."""
    out = []
    for chunk in re.split(r"[\n;]+", text):
        line = BULLET.sub("", chunk).strip()
        if not line:
            continue
        for piece in split_line(line):
            task = parse_one(piece)
            if task:
                out.append(task)
    return out


def meta(t: dict) -> dict:
    """Дозаполняет тему и часть дня у задач, сохранённых старой версией бота."""
    if "category" not in t:
        t["category"] = classify(t["text"])
    if "part" not in t:
        t["part"] = part_of(t["time"], None)
    return t


def add_tasks(chat: dict, day: date, parsed: list) -> tuple:
    """Добавляет задачи в день. Возвращает (добавлено, обновлено время у существующих)."""
    tasks = chat["days"].setdefault(day.isoformat(), [])
    added = updated = 0
    for p in parsed:
        same = next((t for t in tasks if t["text"].lower() == p["text"].lower()), None)
        if same is not None:
            if p["time"] and not same["time"]:
                same["time"] = p["time"]
                same["part"] = part_of(p["time"], p.get("part"))
                same["reminded"] = False
                updated += 1
            elif p.get("part") and not same["time"] and not meta(same)["part"]:
                same["part"] = p["part"]
                updated += 1
            continue  # такая задача уже есть
        tasks.append(
            {
                "id": chat["next_id"],
                "text": p["text"],
                "time": p["time"],
                "part": p.get("part") or part_of(p["time"], None),
                "important": p["important"],
                "category": p.get("category") or classify(p["text"]),
                "done": False,
                "reminded": False,
            }
        )
        chat["next_id"] += 1
        added += 1
    return added, updated


# ---------- оформление плана ----------

def sections(tasks: list) -> list:
    """Структура плана: [(заголовок, задачи, режим)]. Порядок задач здесь = порядок номеров."""
    for t in tasks:
        meta(t)
    result = []
    urgent = [t for t in tasks if t["important"] and not t["part"]]
    if urgent:
        result.append(("🔥 Важное", urgent, "category"))
    for key, title in PART_TITLES.items():
        items = sorted(
            (t for t in tasks if t["part"] == key),
            key=lambda t: (t["time"] is None, t["time"] or "", t["id"]),
        )
        if items:
            result.append((title, items, "time"))
    rest = [t for t in tasks if not t["part"] and not t["important"]]
    for key, title, _pattern in CATEGORIES:
        items = [t for t in rest if t["category"] == key]
        if items:
            result.append((title, items, None))
    other = [t for t in rest if t["category"] == "other"]
    if other:
        result.append(("📝 Остальное", other, None))
    return result


def flat(tasks: list) -> list:
    return [t for _title, items, _mode in sections(tasks) for t in items]


CATEGORY_EMOJI = {key: title.split(" ", 1)[0] for key, title, _p in CATEGORIES}
CATEGORY_EMOJI["other"] = "📝"


def render_plan(day: date, today: date, tasks: list) -> str:
    if day == today:
        title = f"сегодня ({WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]})"
    elif day == today + timedelta(days=1):
        title = f"завтра ({WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]})"
    else:
        title = f"{day.day} {MONTHS[day.month - 1]} ({WEEKDAYS[day.weekday()]})"
    lines = [f"📋 План на {title}"]
    if not tasks:
        lines += ["", "Задач пока нет. Пришлите их сообщением."]
        return "\n".join(lines)

    n = 0
    for heading, items, mode in sections(tasks):
        lines += ["", heading]
        for t in items:
            n += 1
            done = "✅ " if t["done"] else ""
            if mode == "time":
                when = f"{t['time']} — " if t["time"] else ""
                fire = " 🔥" if t["important"] else ""
                lines.append(f"{n}. {done}{when}{t['text']}{fire}")
            elif mode == "category":
                lines.append(f"{n}. {done}{CATEGORY_EMOJI.get(t['category'], '📝')} {t['text']}")
            else:
                lines.append(f"{n}. {done}{t['text']}")
    done_count = sum(1 for t in tasks if t["done"])
    if done_count:
        lines += ["", f"Выполнено: {done_count} из {len(tasks)}"]
    return "\n".join(lines)


def keyboard(day: date, tasks: list) -> dict:
    rows = []
    for n, t in enumerate(flat(tasks), 1):
        mark = "✅" if t["done"] else "⬜"
        rows.append([{"text": f"{mark} {n}. {t['text'][:30]}", "callback_data": f"t:{day.isoformat()}:{t['id']}"}])
    return {"inline_keyboard": rows}


def carry_markup(day: date) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "↪️ Перенести невыполненное на завтра", "callback_data": f"carry:{day.isoformat()}"}]
        ]
    }


# ---------- Telegram ----------

class Api:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **params):
        try:
            resp = http_json(self.base + method, params, timeout=params.get("timeout", 0) + 15)
        except urllib.error.HTTPError as e:
            resp = json.load(e)
        if not resp.get("ok"):
            wait = resp.get("parameters", {}).get("retry_after")
            if wait:
                time.sleep(wait)
            raise RuntimeError(f"{method}: {resp.get('error_code')} {resp.get('description')}")
        return resp["result"]

    def send(self, chat_id, text: str, markup: dict = None):
        params = {"chat_id": chat_id, "text": text}
        if markup:
            params["reply_markup"] = markup
        return self.call("sendMessage", **params)


# ---------- обработка сообщений ----------

def handle_message(state: dict, msg: dict, now: datetime, api) -> None:
    chat_id = msg["chat"]["id"]
    chat = get_chat(state, chat_id)
    text = (msg.get("text") or "").strip()
    today = now.date()
    target = target_date(now)

    if not text:
        api.send(chat_id, "Пришлите задачи текстом: по одной в строке.")
        return

    if text.startswith("/"):
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd in ("/start", "/help"):
            api.send(chat_id, HELP)
        elif cmd == "/today":
            tasks = chat["days"].get(today.isoformat(), [])
            api.send(chat_id, render_plan(today, today, tasks), keyboard(today, tasks) if tasks else None)
        elif cmd == "/tomorrow":
            tomorrow = today + timedelta(days=1)
            api.send(chat_id, render_plan(tomorrow, today, chat["days"].get(tomorrow.isoformat(), [])))
        elif cmd == "/del":
            tasks = chat["days"].get(target.isoformat(), [])
            order = flat(tasks)
            ids = {order[int(n) - 1]["id"] for n in re.findall(r"\d+", arg) if 1 <= int(n) <= len(order)}
            if not ids:
                api.send(chat_id, "Укажите номера задач из плана: /del 2 5")
                return
            chat["days"][target.isoformat()] = [t for t in tasks if t["id"] not in ids]
            api.send(
                chat_id,
                f"Удалено: {len(ids)}\n\n" + render_plan(target, today, chat["days"][target.isoformat()]),
            )
        elif cmd == "/clear":
            chat["days"][target.isoformat()] = []
            api.send(chat_id, "Список очищен.\n\n" + render_plan(target, today, []))
        elif cmd in ("/evening", "/morning"):
            m = HHMM.fullmatch(arg)
            if not m:
                api.send(chat_id, f"Укажите время в формате ЧЧ:ММ, например {cmd} 21:30")
                return
            value = f"{int(m.group(1)):02d}:{m.group(2)}"
            chat[cmd[1:]] = value
            what = "спрошу про завтра" if cmd == "/evening" else "пришлю план дня"
            api.send(chat_id, f"Готово: буду в {value} (по Астане) — {what}.")
        else:
            api.send(chat_id, "Не знаю такой команды. Список команд: /help")
        return

    parsed = parse_tasks(text)
    if not parsed:
        api.send(chat_id, NOT_TASKS)
        return
    added, updated = add_tasks(chat, target, parsed)
    tasks = chat["days"].get(target.isoformat(), [])
    if added or updated:
        parts = ([f"Добавил задач: {added}"] if added else []) + ([f"Уточнил время: {updated}"] if updated else [])
        note = ", ".join(parts)
    else:
        note = "Эти задачи уже есть в плане."
    reply = f"{note}\n\n{render_plan(target, today, tasks)}"
    unscheduled = [t for t in tasks if not meta(t)["part"] and not t["done"]]
    if added and unscheduled:
        reply += f"\n\n💡 Чтобы я напомнил, допишите время: «{unscheduled[0]['text']} 18:00»"
    api.send(chat_id, reply)


def handle_callback(state: dict, cq: dict, now: datetime, api) -> None:
    answer = {}
    try:
        msg = cq.get("message")
        data = cq.get("data") or ""
        if not msg:
            return
        chat_id = msg["chat"]["id"]
        chat = get_chat(state, chat_id)
        today = now.date()

        if data.startswith("t:"):
            _, ds, tid = data.split(":", 2)
            day = date.fromisoformat(ds)
            tasks = chat["days"].get(ds, [])
            for t in tasks:
                if str(t["id"]) == tid:
                    t["done"] = not t["done"]
            try:
                api.call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=msg["message_id"],
                    text=render_plan(day, today, tasks),
                    reply_markup=keyboard(day, tasks),
                )
            except RuntimeError as e:
                if "not modified" not in str(e):
                    raise
        elif data.startswith("carry:"):
            src = date.fromisoformat(data.split(":", 1)[1])
            dst = src + timedelta(days=1)
            undone = [meta(t) for t in chat["days"].get(src.isoformat(), []) if not t["done"]]
            added, _updated = add_tasks(chat, dst, undone)
            answer["text"] = f"Перенесено задач: {added}"
            api.send(chat_id, render_plan(dst, today, chat["days"].get(dst.isoformat(), [])))
    finally:
        api.call("answerCallbackQuery", callback_query_id=cq["id"], **answer)


def handle_update(state: dict, update: dict, now: datetime, api) -> None:
    if "message" in update:
        if update["message"]["chat"]["type"] == "private":
            handle_message(state, update["message"], now, api)
    elif "callback_query" in update:
        handle_callback(state, update["callback_query"], now, api)


# ---------- напоминания ----------

def tick(state: dict, now: datetime, api) -> bool:
    """Проверяет, не пора ли что-то отправить. Возвращает True, если состояние изменилось."""
    changed = False
    today = now.date()
    tomorrow = today + timedelta(days=1)
    oldest = (today - timedelta(days=KEEP_DAYS)).isoformat()

    def safe_send(chat_id, text, markup=None):
        try:
            api.send(chat_id, text, markup)
        except Exception as e:  # noqa: BLE001
            print("send error:", e, flush=True)

    for cid, chat in state["chats"].items():
        chat_id = int(cid)

        for key in [k for k in chat["days"] if k < oldest]:
            del chat["days"][key]
            changed = True

        # вечер: спросить задачи на завтра
        start = at(now, chat["evening"])
        if start <= now < start + WINDOW and chat["sent"].get("evening") != today.isoformat():
            chat["sent"]["evening"] = today.isoformat()
            changed = True
            left = [t for t in chat["days"].get(today.isoformat(), []) if not t["done"]]
            planned = len(chat["days"].get(tomorrow.isoformat(), []))
            text = "🌙 Добрый вечер! Что планируете на завтра?\nПришлите задачи: по одной в строке или через запятую. Время пишите так: 14:30, важное отмечайте «!»."
            if planned:
                text += f"\n\nУже записано на завтра: {planned} (посмотреть: /tomorrow)."
            if left:
                text += f"\nНе выполнено сегодня: {len(left)}."
            safe_send(chat_id, text, carry_markup(today) if left else None)

        # утро: план дня
        start = at(now, chat["morning"])
        if start <= now < start + WINDOW and chat["sent"].get("morning") != today.isoformat():
            chat["sent"]["morning"] = today.isoformat()
            changed = True
            tasks = chat["days"].get(today.isoformat(), [])
            if tasks:
                safe_send(chat_id, "☀️ Доброе утро!\n\n" + render_plan(today, today, tasks), keyboard(today, tasks))

        # за 15 минут до задач со временем
        for t in chat["days"].get(today.isoformat(), []):
            if t["time"] and not t["done"] and not t.get("reminded"):
                when = at(now, t["time"])
                if when - timedelta(minutes=REMIND_BEFORE_MIN) <= now < when:
                    t["reminded"] = True
                    changed = True
                    mins = max(1, math.ceil((when - now).total_seconds() / 60))
                    safe_send(chat_id, f"⏰ Через {mins} мин ({t['time']}): {t['text']}")
    return changed


# ---------- запуск ----------

def main() -> None:
    load_env(Path(__file__).with_name(".env"))
    token = os.environ.get("CHECKLIST_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    if not token:
        sys.exit("Токен не задан (CHECKLIST_BOT_TOKEN или BOT_TOKEN в .env)")
    api = Api(token)

    me = api.call("getMe")
    print(f"Бот-чеклист @{me['username']} запущен. Ctrl+C для остановки.", flush=True)
    # сообщения, пришедшие пока бот был офлайн, игнорируем
    api.call("deleteWebhook", drop_pending_updates=True)

    state = load_state()
    offset = None
    while True:
        try:
            params = {"timeout": 20, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            for update in api.call("getUpdates", **params):
                offset = update["update_id"] + 1
                try:
                    handle_update(state, update, now_local(), api)
                    save_state(state)
                except Exception as e:  # noqa: BLE001
                    print("update error:", e, flush=True)
            if tick(state, now_local(), api):
                save_state(state)
        except KeyboardInterrupt:
            print("Остановлен.")
            return
        except Exception as e:  # noqa: BLE001
            print("error:", e, flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
