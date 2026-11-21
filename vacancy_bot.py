"""Telegram-бот «Вакансии РК»: раз в день ищет вакансии по ключевым словам и присылает новые.

Только стандартная библиотека Python (3.9+). Данные (подписчики, что уже отправлено) лежат в vacancies.json.
Токен: VACANCY_BOT_TOKEN, а если его нет — BOT_TOKEN (файл .env). Один токен = один запущенный бот.

Источники — классы с методами name и search(query, days). Сейчас подключён hh.kz: страница поиска
открыта без входа и не закрыта в robots.txt, данные берутся из JSON, встроенного в страницу.
Запросы делаются бережно: пауза между ними, не больше нескольких десятков в сутки.
Чтобы добавить площадку, напишите класс с тем же интерфейсом и добавьте его в SOURCES.
"""
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import http_json, load_env

DATA_FILE = Path(__file__).with_name("vacancies.json")
DEFAULT_TIME = "09:00"  # когда слать ежедневную подборку (по Астане)
SEND_WINDOW = timedelta(hours=3)  # если бот был выключен дольше, подборку за этот день пропускаем
DAILY_DAYS = 3  # за сколько дней искать при ежедневной рассылке (повторы отсекает список отправленного)
BACKLOG_DAYS = 7  # за сколько дней показывать при /start, /now, /week
REQUEST_DELAY = 2.0  # секунд между запросами к площадке
MAX_PAGES = 3  # страниц по 50 вакансий на один запрос
CACHE_TTL = 1800  # результаты поиска кэшируются на 30 минут
SEEN_KEEP_DAYS = 90
USER_AGENT = "Mozilla/5.0 (compatible; personal-vacancy-digest/1.0)"

# Ключевые слова. queries: что вводим в поиск по названию вакансии, match: что должно быть в названии.
# Строгий match нужен, потому что поиск hh «нечёткий» (например, «C&B» находит «B2B»).
GROUPS = [
    {
        "name": "C&B manager",
        "queries": ["C&B", "C & B", "Compensation Benefits", "Compensation and Benefits", "Total Rewards", "Rewards Manager"],
        "match": r"(?<![a-zа-я0-9])c\s*(?:&|and|энд)\s*b(?![a-zа-я0-9])|compensation|benefits|total\s+rewards|rewards?\s+(?:manager|specialist|analyst|partner|lead)",
    },
    {
        "name": "Менеджер по компенсациям и льготам",
        "queries": ["компенсациям и льготам", "компенсации и льготы", "льготам и компенсациям"],
        "match": r"компенсац\w*[^;()]{0,50}льгот|льгот\w*[^;()]{0,50}компенсац",
    },
    {
        "name": "HR аналитика",
        "queries": ["HR аналитик", "HR-аналитик", "HR аналитика", "HR analytics", "People analytics", "аналитик по персоналу"],
        "match": r"hr[\s\-]*(?:аналит|analyt|data)|people\s+analyt|^(?=.*(?:\bhr\b|персонал|кадр))(?=.*(?:аналит|analyst))",
    },
    {
        "name": "Компенсации и льготы",
        "queries": ["компенсации", "льготы"],
        "match": r"компенсац(?:ия|ии|иям|иями|ий)\b|льгот(?:ы|ам|ами|ах)\b",
    },
]
for _g in GROUPS:
    _g["regex"] = re.compile(_g["match"], re.IGNORECASE)

CURRENCY = {"KZT": "₸", "RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€"}


class SourceError(Exception):
    """Площадка недоступна или изменила вёрстку."""


# ---------- время ----------

def _make_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Almaty")  # Астана, UTC+5
    except Exception:  # noqa: BLE001  нет zoneinfo/tzdata: в Казахстане нет летнего времени
        return timezone(timedelta(hours=5))


TZ = _make_tz()
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def now_local() -> datetime:
    return datetime.now(TZ)


def at(now: datetime, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


# ---------- площадка hh.kz ----------

INIT_STATE = re.compile(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)


def fetch(url: str) -> str:
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ru"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise SourceError(f"{last}")


def parse_hh_page(page: str) -> dict:
    m = INIT_STATE.search(page)
    if not m:
        raise SourceError("в ответе нет данных поиска (изменилась вёрстка или показана проверка на робота)")
    try:
        return json.loads(html.unescape(m.group(1)))["vacancySearchResult"]
    except (ValueError, KeyError) as e:
        raise SourceError(f"не удалось разобрать данные поиска: {e}") from e


def format_salary(c) -> str:
    if not c or c.get("noCompensation") is not None or not (c.get("from") or c.get("to")):
        return "не указана"
    cur = CURRENCY.get(c.get("currencyCode"), c.get("currencyCode") or "")
    lo, hi = c.get("from"), c.get("to")
    if lo and hi and lo != hi:
        text = f"{lo:,} – {hi:,}"
    elif lo and hi:
        text = f"{lo:,}"
    elif lo:
        text = f"от {lo:,}"
    else:
        text = f"до {hi:,}"
    text = f"{text} {cur}".replace(",", " ")
    return text + (" (до вычета налогов)" if c.get("gross") else "")


EXPERIENCE = {
    "noExperience": "без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "более 6 лет",
}
YEARS = re.compile(
    r"(?:не\s+менее\s+|минимум\s+|от\s+|более\s+|свыше\s+)?\d{1,2}(?:\s*[-–—]\s*\d{1,2}|\+|\s+до\s+\d{1,2})?\s*(?:лет|года|год)\b",
    re.IGNORECASE,
)
ENG_WORD = re.compile(r"английск\w*|english", re.IGNORECASE)
CEFR = re.compile(r"\b[abc][12]\+?(?![a-z0-9])", re.IGNORECASE)
ENG_LEVEL_WORD = re.compile(
    r"upper[\s-]?intermediate|pre[\s-]?intermediate|intermediate|advanced|elementary|fluent|свободн\w*|разговорн\w*|продвинут\w*|базов\w*",
    re.IGNORECASE,
)


def structured_english(languages):
    """Уровень английского из анкеты вакансии (поле languages), например «B2»."""
    for item in languages or []:
        group = item.get("language") if isinstance(item, dict) and "language" in item else [item]
        for lang in group or []:
            if isinstance(lang, dict) and lang.get("isoCode") == "eng" and lang.get("degree"):
                return str(lang["degree"]).upper()
    return None


def find_english(text: str):
    """Ищет в описании вакансии упоминание английского и его уровень."""
    mentions = list(ENG_WORD.finditer(text))
    if not mentions:
        return None
    for m in mentions:
        window = text[max(0, m.start() - 50): m.end() + 50]
        level = CEFR.search(window)
        if level:
            return level.group(0).upper() + " (в описании)"
        word = ENG_LEVEL_WORD.search(window)
        if word:
            return word.group(0).lower() + " (в описании)"
    return "упомянут, уровень не указан"


def find_years(text: str):
    """Требуемый опыт из текста, например «не менее 3 лет»: берём только фразы рядом со словом «опыт»/«стаж»."""
    for m in YEARS.finditer(text):
        around = text[max(0, m.start() - 60): m.end() + 60].lower()
        phrase = re.sub(r"\s+", " ", m.group(0)).strip().lower()
        near_experience = "опыт" in around or "стаж" in around or "experience" in around
        # «Не менее 3 лет в управлении…» без слова «опыт», но с оговоркой «не менее/от/минимум»
        qualified = re.match(r"(?:не менее|минимум|от|более|свыше)\b", phrase) and not re.search(
            r"на рынке|основан|существу|работаем|гарантия|отпуск", around
        )
        if near_experience or qualified:
            return phrase
    return None


class HhKz:
    name = "hh.kz"

    def __init__(self):
        self._last = 0.0

    def _get(self, url: str) -> str:
        wait = REQUEST_DELAY - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            return fetch(url)
        finally:
            self._last = time.time()

    @staticmethod
    def _vacancy(v: dict):
        vid = v.get("vacancyId")
        if not vid:
            return None
        ts = (v.get("publicationTime") or {}).get("@timestamp")
        return {
            "id": f"hh.kz:{vid}",
            "source": "hh.kz",
            "title": v.get("name") or "",
            "company": (v.get("company") or {}).get("name") or "",
            "area": (v.get("area") or {}).get("name") or "",
            "salary": format_salary(v.get("compensation")),
            "url": f"https://hh.kz/vacancy/{vid}",
            "published": datetime.fromtimestamp(ts, TZ) if ts else None,
            "experience": EXPERIENCE.get(v.get("workExperience")),
            "experience_text": None,
            "english": structured_english(v.get("languages")),
            "details_done": False,
        }

    def details(self, v: dict) -> None:
        """Открывает страницу вакансии: достаёт опыт из текста и уровень английского."""
        page = self._get("https://hh.kz/vacancy/" + v["id"].split(":", 1)[1])
        m = INIT_STATE.search(page)
        if not m:
            raise SourceError("на странице вакансии нет данных")
        try:
            vac = json.loads(html.unescape(m.group(1)))["vacancyView"]["vacancyFull"]["vacancy"]
        except (ValueError, KeyError) as e:
            raise SourceError(f"не удалось разобрать страницу вакансии: {e}") from e
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", vac.get("description") or "")))
        v["experience"] = v.get("experience") or EXPERIENCE.get(vac.get("workExperience"))
        v["experience_text"] = find_years(text)
        v["english"] = v.get("english") or structured_english(vac.get("languages")) or find_english(text)

    def search(self, query: str, days: int) -> list:
        """Вакансии по всей РК (area=40), у которых запрос найден в названии, за последние days дней."""
        out = []
        for page in range(MAX_PAGES):
            params = {
                "text": query,
                "area": 40,
                "search_field": "name",
                "search_period": days,
                "order_by": "publication_time",
                "items_on_page": 50,
                "page": page,
            }
            result = parse_hh_page(self._get("https://hh.kz/search/vacancy?" + urllib.parse.urlencode(params)))
            out += [x for x in map(self._vacancy, result["vacancies"]) if x]
            if (page + 1) * 50 >= result.get("totalResults", 0):
                break
        return out


SOURCES = [HhKz()]


# ---------- поиск и отбор ----------

def group_of(title: str, groups: list = None):
    """Номер первой ключевой группы, под которую подходит название, или None."""
    for i, g in enumerate(GROUPS if groups is None else groups):
        if g["regex"].search(title):
            return i
    return None


_details_cache = {}  # id вакансии -> опыт и английский, чтобы не открывать одну страницу дважды


def enrich(items: list, sources: list, log=print) -> None:
    """Для найденных вакансий открывает страницу и уточняет опыт и уровень английского."""
    by_name = {s.name: s for s in sources}
    for v in items:
        if v["id"] in _details_cache:
            v.update(_details_cache[v["id"]])
            continue
        src = by_name.get(v["source"])
        if not hasattr(src, "details"):
            continue
        try:
            src.details(v)
        except SourceError as e:
            log(f"{v['id']}: не удалось открыть страницу вакансии: {e}")
            continue
        v["details_done"] = True
        _details_cache[v["id"]] = {k: v.get(k) for k in ("experience", "experience_text", "english", "details_done")}


def collect(sources: list, days: int, log=print, groups: list = None) -> tuple:
    """Возвращает (вакансии, ошибки). Вакансии отсортированы по группе и дате (новые выше)."""
    groups = GROUPS if groups is None else groups
    found, errors = {}, []
    for src in sources:
        asked = set()
        for group in groups:
            for query in group["queries"]:
                if query.lower() in asked:
                    continue
                asked.add(query.lower())
                try:
                    vacancies = src.search(query, days)
                except SourceError as e:
                    errors.append(f"{src.name}: {e}")
                    log(f"{src.name} error ({query!r}): {e}")
                    break
                else:
                    for v in vacancies:
                        gi = group_of(v["title"], groups)
                        if gi is not None and v["id"] not in found:
                            found[v["id"]] = {**v, "group": gi}
                    continue
                break  # источник сломался, остальные его запросы пропускаем
            else:
                continue
            break
    enrich(list(found.values()), sources, log)
    items = sorted(
        found.values(),
        key=lambda v: (v["group"], -(v["published"].timestamp() if v["published"] else 0)),
    )
    return items, errors


# ---------- оформление ----------

def format_vacancy(n: int, v: dict) -> str:
    when = ""
    if v["published"]:
        when = f"\n   🕒 {v['published'].day} {MONTHS[v['published'].month - 1]}"
    place = f" · 📍 {html.escape(v['area'])}" if v["area"] else ""
    experience = v.get("experience") or "не указан"
    if v.get("experience_text"):
        experience += f" (в описании: {v['experience_text']})"
    english = v.get("english") or ("не указан" if v.get("details_done") else "не удалось проверить")
    return (
        f"{n}. <a href=\"{html.escape(v['url'])}\">{html.escape(v['title'])}</a>\n"
        f"   🏢 {html.escape(v['company'] or 'компания не указана')}{place}\n"
        f"   💰 {html.escape(v['salary'])}\n"
        f"   🎓 Опыт: {html.escape(experience)}\n"
        f"   🇬🇧 Английский: {html.escape(english)}{when}"
    )


def build_messages(items: list, header: str, groups: list = None) -> list:
    """Делит подборку на сообщения до ~3800 символов, группируя по ключевым словам."""
    limit = 3800  # лимит Telegram 4096, с запасом на разметку
    messages, current, cur_group = [], header, None
    n = 0
    for gi, group in enumerate(GROUPS if groups is None else groups):
        title = f"🔎 <b>{html.escape(group['name'])}</b>"
        first = True
        for v in (x for x in items if x["group"] == gi):
            n += 1
            vac = format_vacancy(n, v)
            add = vac if cur_group == gi else f"{title}\n\n{vac}"
            if len(current) + len(add) + 2 > limit:
                messages.append(current)
                head = title if first else title.replace("</b>", " (продолжение)</b>")
                current = f"{head}\n\n{vac}"
            else:
                current += "\n\n" + add
            cur_group = gi
            first = False
    messages.append(current)
    return messages


HELP = """💼 Вакансии по Казахстану

Каждый день в {time} (по Астане) присылаю новые вакансии по ключевым словам:
{keywords}

Источники: {sources}. Ищу по названию вакансии, по всей РК.

Всё делается кнопками внизу экрана, писать ничего не нужно. Команды тоже работают:
/now — новые вакансии прямо сейчас
/day, /3days, /week — все подходящие за сутки, 3 дня, неделю
/keywords — добавить, изменить или убрать ключевые слова
/time 09:30 — во сколько присылать ежедневную подборку
/stop — отписаться
/status — подписка и источники"""


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

    def send(self, chat_id, text: str, html_mode: bool = False, markup: dict = None):
        params = {"chat_id": chat_id, "text": text}
        if html_mode:
            params["parse_mode"] = "HTML"
            params["link_preview_options"] = {"is_disabled": True}
        if markup:
            params["reply_markup"] = markup
        return self.call("sendMessage", **params)


# ---------- кнопки ----------

BTN_NOW = "🔎 Новые вакансии"
BTN_DAY = "📅 За сутки"
BTN_3DAYS = "📅 За 3 дня"
BTN_WEEK = "📅 За неделю"
BTN_KEYWORDS = "🔑 Ключевые слова"
BTN_TIME = "⏰ Время рассылки"
BTN_STATUS = "ℹ️ Статус"
BTN_STOP = "🔕 Отписаться"
BTN_START = "▶️ Подписаться"
BUTTON_COMMANDS = {
    BTN_NOW: "/now", BTN_DAY: "/day", BTN_3DAYS: "/3days", BTN_WEEK: "/week", BTN_KEYWORDS: "/keywords", BTN_TIME: "/timemenu",
    BTN_STATUS: "/status", BTN_STOP: "/stop", BTN_START: "/start",
}
PERIODS = {"/day": (1, "за сутки"), "/3days": (3, "за 3 дня"), "/week": (7, "за неделю")}
TIME_CHOICES = (("07:00", "08:00", "09:00"), ("10:00", "12:00", "18:00"))
MENU_VERSION = 2  # при изменении набора кнопок увеличить: бот один раз пришлёт подписчикам обновлённое меню


def main_keyboard(subscribed: bool) -> dict:
    """Постоянная клавиатура внизу экрана: писать ничего не нужно."""
    rows = [
        [BTN_NOW],
        [BTN_DAY, BTN_3DAYS, BTN_WEEK],
        [BTN_KEYWORDS, BTN_TIME],
        [BTN_STATUS, BTN_STOP if subscribed else BTN_START],
    ]
    return {
        "keyboard": [[{"text": t} for t in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": True,
    }


class KeyboardApi:
    """Обёртка: каждое отправленное сообщение сопровождается клавиатурой с кнопками."""

    def __init__(self, api, markup_fn):
        self._api, self._markup_fn = api, markup_fn

    def send(self, chat_id, text: str, html_mode: bool = False, markup: dict = None):
        return self._api.send(chat_id, text, html_mode, markup or self._markup_fn())

    def __getattr__(self, name):
        return getattr(self._api, name)


# ---------- состояние ----------

def load_state() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"time": DEFAULT_TIME, "last_run": None, "subscribers": {}}


def save_state(state: dict) -> None:
    tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


_cache = {}


def cached_collect(sources: list, days: int, groups: list = None, now_ts: float = None) -> tuple:
    now_ts = time.time() if now_ts is None else now_ts
    key = (days, tuple(g["name"] for g in (GROUPS if groups is None else groups)))
    hit = _cache.get(key)
    if hit and now_ts - hit[0] < CACHE_TTL:
        return hit[1], hit[2]
    items, errors = collect(sources, days, groups=groups)
    if not errors:  # неполные результаты не кэшируем
        _cache[key] = (now_ts, items, errors)
    return items, errors


def recent(items: list, now: datetime, days: int) -> list:
    """Вакансии, опубликованные не раньше чем days суток назад."""
    return [v for v in items if v["published"] and now - v["published"] <= timedelta(days=days)]


def deliver(api, chat_id, sub: dict, items: list, errors: list, today: str, title: str, mark_seen: bool, only_new: bool,
            empty: str = "🔎 Новых вакансий по вашим ключевым словам нет.", groups: list = None) -> None:
    fresh = [v for v in items if v["id"] not in sub["seen"]] if only_new else items
    if fresh:
        header = f"💼 {title}: {len(fresh)}"
        for text in build_messages(fresh, header, groups):
            api.send(chat_id, text, html_mode=True)
        if mark_seen:
            for v in fresh:
                sub["seen"][v["id"]] = today
    elif errors and not items:
        api.send(chat_id, "⚠️ Не удалось получить вакансии: " + "; ".join(errors) + "\nПопробую позже.")
    else:
        api.send(chat_id, empty)
    if errors and (fresh or items):
        api.send(chat_id, "⚠️ Часть данных получить не удалось: " + "; ".join(errors))
    for key in [k for k, d in sub["seen"].items() if d < (datetime.fromisoformat(today) - timedelta(days=SEEN_KEEP_DAYS)).date().isoformat()]:
        del sub["seen"][key]


# ---------- ключевые слова (их можно менять кнопками) ----------

MAX_KEYWORDS = 12  # каждое слово — отдельный запрос к площадке, поэтому список ограничен
BUILTIN_BY_NAME = {g["name"].lower(): g for g in GROUPS}


def new_keyword(state: dict, name: str) -> dict:
    state["kw_counter"] = state.get("kw_counter", 100) + 1  # id не повторяются, старые кнопки не заденут чужие слова
    return {"id": state["kw_counter"], "name": name}


def get_keywords(state: dict) -> list:
    """Список слов хранится в состоянии; при первом обращении заполняется стандартными."""
    if "keywords" not in state:
        state["keywords"] = [new_keyword(state, g["name"]) for g in GROUPS]
    return state["keywords"]


def make_group(phrase: str) -> dict:
    """Группа для слова, добавленного вручную: все слова фразы должны встречаться в названии вакансии."""
    parts = []
    for token in re.findall(r"[\w&+#.]+", phrase.lower()):
        short = len(token) <= 4
        stem = token if short else token[: max(4, len(token) - 2)]  # грубо отсекаем окончание
        parts.append(r"(?=.*(?<!\w)" + re.escape(stem) + (r"(?!\w)" if short else "") + ")")
    return {"name": phrase, "queries": [phrase], "regex": re.compile("^" + "".join(parts), re.IGNORECASE)}


def active_groups(state: dict) -> list:
    """Стандартные слова используют настроенные правила, добавленные вручную — обычный поиск по словам."""
    return [BUILTIN_BY_NAME.get(kw["name"].lower()) or make_group(kw["name"]) for kw in get_keywords(state)]


def check_keyword(state: dict, phrase: str, ignore_id=None):
    """Текст ошибки, если слово не подходит, иначе None."""
    if not (2 <= len(phrase) <= 60):
        return "Слово должно быть длиной от 2 до 60 символов."
    if not re.search(r"\w", phrase):
        return "В слове должна быть хотя бы одна буква или цифра."
    for kw in get_keywords(state):
        if kw["id"] != ignore_id and kw["name"].lower() == phrase.lower():
            return "Такое слово уже есть."
    return None


def keywords_view(state: dict) -> tuple:
    """Текст и кнопки списка ключевых слов."""
    kws = get_keywords(state)
    if kws:
        lines = ["🔑 Ключевые слова. Ищу вакансии, в названии которых они есть:"]
        lines += [f"{i}. {kw['name']}" for i, kw in enumerate(kws, 1)]
        lines += ["", "✏️ изменить слово, ❌ убрать, ➕ добавить новое."]
    else:
        lines = ["🔑 Список ключевых слов пуст, искать нечего. Добавьте слово."]
    rows = [
        [
            {"text": f"✏️ {kw['name'][:26]}", "callback_data": f"kw:edit:{kw['id']}"},
            {"text": "❌", "callback_data": f"kw:del:{kw['id']}"},
        ]
        for kw in kws
    ]
    rows.append(
        [{"text": "➕ Добавить", "callback_data": "kw:add"}, {"text": "↩️ Стандартные", "callback_data": "kw:reset"}]
    )
    return "\n".join(lines), {"inline_keyboard": rows}


CANCEL_MARKUP = {"inline_keyboard": [[{"text": "✖️ Отмена", "callback_data": "kw:cancel"}]]}


def apply_keyword_input(state: dict, sub: dict, phrase: str, api, chat_id) -> None:
    """Пользователь прислал текст после кнопки «Добавить» или «Изменить»."""
    awaiting = sub.pop("awaiting")
    phrase = re.sub(r"\s+", " ", phrase).strip()
    error = check_keyword(state, phrase, awaiting.get("id"))
    if error:
        sub["awaiting"] = awaiting  # продолжаем ждать нормальное слово
        api.send(chat_id, f"{error} Напишите другое слово или нажмите «Отмена».", markup=CANCEL_MARKUP)
        return
    kws = get_keywords(state)
    target = next((kw for kw in kws if kw["id"] == awaiting.get("id")), None) if awaiting["mode"] == "edit" else None
    if target is not None:
        target["name"] = phrase
    else:
        kws.append(new_keyword(state, phrase))
    _cache.clear()
    text, markup = keywords_view(state)
    api.send(chat_id, "✅ Готово. Новое слово учтётся в следующей подборке и в «🔎 Новые вакансии».\n\n" + text, markup=markup)


def handle_keyword_callback(state: dict, cq: dict, api) -> None:
    msg = cq.get("message")
    data = cq.get("data") or ""
    answer = None
    try:
        if not msg:
            return
        chat_id = msg["chat"]["id"]
        sub = state["subscribers"].get(str(chat_id))
        kws = get_keywords(state)

        def refresh():
            text, markup = keywords_view(state)
            try:
                api.call("editMessageText", chat_id=chat_id, message_id=msg["message_id"], text=text, reply_markup=markup)
            except RuntimeError as e:
                if "not modified" not in str(e):
                    raise

        if data in ("kw:add",) or data.startswith("kw:edit:"):
            if sub is None:
                answer = "Сначала подпишитесь: /start"
            elif data == "kw:add" and len(kws) >= MAX_KEYWORDS:
                answer = f"Максимум {MAX_KEYWORDS} слов. Уберите ненужные."
            elif data == "kw:add":
                sub["awaiting"] = {"mode": "add"}
                api.call("sendMessage", chat_id=chat_id, reply_markup=CANCEL_MARKUP,
                         text="Напишите новое ключевое слово или фразу, например: Payroll")
            else:
                target = next((kw for kw in kws if str(kw["id"]) == data.split(":")[2]), None)
                if target is None:
                    answer = "Этого слова уже нет"
                    refresh()
                else:
                    sub["awaiting"] = {"mode": "edit", "id": target["id"]}
                    api.call("sendMessage", chat_id=chat_id, reply_markup=CANCEL_MARKUP,
                             text=f"Напишите, на что заменить «{target['name']}»")
        elif data.startswith("kw:del:"):
            before = len(kws)
            state["keywords"] = [kw for kw in kws if str(kw["id"]) != data.split(":")[2]]
            if len(state["keywords"]) != before:
                _cache.clear()
                answer = "Убрал"
            refresh()
        elif data == "kw:reset":
            state["keywords"] = [new_keyword(state, g["name"]) for g in GROUPS]
            _cache.clear()
            answer = "Вернул стандартные слова"
            refresh()
        elif data == "kw:cancel":
            if sub:
                sub.pop("awaiting", None)
            answer = "Отменено"
            try:
                api.call("editMessageText", chat_id=chat_id, message_id=msg["message_id"], text="Отменено.")
            except RuntimeError:
                pass
    finally:
        api.call("answerCallbackQuery", callback_query_id=cq["id"], **({"text": answer} if answer else {}))


# ---------- команды ----------

def help_text(state: dict, sources: list) -> str:
    return HELP.format(
        time=state["time"],
        keywords="\n".join(f"• {g['name']}" for g in active_groups(state)) or "(список пуст, добавьте слова кнопкой 🔑)",
        sources=", ".join(s.name for s in sources),
    )


def handle_message(state: dict, msg: dict, now: datetime, api, sources: list) -> None:
    chat_id = msg["chat"]["id"]
    cid = str(chat_id)
    text = (msg.get("text") or "").strip()
    today = now.date().isoformat()
    api = KeyboardApi(api, lambda: main_keyboard(cid in state["subscribers"]))
    aliases = {"start": "/start", "старт": "/start", "начать": "/start", "help": "/help", "помощь": "/help",
               "stop": "/stop", "стоп": "/stop"}
    aliases.update({label.lower(): command for label, command in BUTTON_COMMANDS.items()})
    sub = state["subscribers"].get(cid)
    is_command = text.lower() in aliases or text.startswith("/")
    if sub is not None and sub.get("awaiting"):
        if not is_command:  # это слово для списка ключевых слов
            apply_keyword_input(state, sub, text, api, chat_id)
            return
        sub.pop("awaiting")  # нажали другую кнопку: ввод слова отменяется
    if text.lower() in aliases:  # нажали кнопку или написали слово без «/»
        text = aliases[text.lower()]
    elif not text.startswith("/"):
        api.send(chat_id, "Я присылаю вакансии по Казахстану. Пользуйтесь кнопками внизу экрана или нажмите /help.")
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    groups = active_groups(state)

    if cmd == "/start":
        if sub is None:
            sub = state["subscribers"][cid] = {"seen": {}}
        sub["menu"] = MENU_VERSION  # клавиатура уже показана вместе с этим сообщением
        api.send(chat_id, help_text(state, sources))
        if not groups:
            api.send(chat_id, "Список ключевых слов пуст. Добавьте слова кнопкой «🔑 Ключевые слова».")
            return
        api.send(chat_id, f"Ищу вакансии за последние {BACKLOG_DAYS} дней, это займёт около минуты…")
        items, errors = cached_collect(sources, BACKLOG_DAYS, groups)
        deliver(api, chat_id, sub, items, errors, today, "Подходящие вакансии за неделю", mark_seen=True, only_new=True, groups=groups)
    elif cmd in ("/help", ""):
        api.send(chat_id, help_text(state, sources))
    elif cmd == "/stop":
        state["subscribers"].pop(cid, None)
        api.send(chat_id, "Отписал. Чтобы подписаться снова, нажмите /start.")
    elif cmd == "/now" or cmd in PERIODS:
        if sub is None:
            api.send(chat_id, "Сначала подпишитесь: /start")
            return
        if not groups:
            api.send(chat_id, "Список ключевых слов пуст. Добавьте слова кнопкой «🔑 Ключевые слова».")
            return
        api.send(chat_id, "Ищу, это займёт около минуты…")
        items, errors = cached_collect(sources, BACKLOG_DAYS, groups)
        if cmd == "/now":
            deliver(api, chat_id, sub, items, errors, today, "Новые вакансии", mark_seen=True, only_new=True, groups=groups)
        else:
            days, label = PERIODS[cmd]
            deliver(
                api, chat_id, sub, recent(items, now, days), errors, today, f"Подходящие вакансии {label}",
                mark_seen=False, only_new=False, empty=f"🔎 {label.capitalize()} подходящих вакансий нет.", groups=groups,
            )
    elif cmd == "/keywords":
        text, markup = keywords_view(state)
        api.send(chat_id, text, markup=markup)
    elif cmd == "/time":
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", arg)
        if not m:
            api.send(chat_id, "Укажите время в формате ЧЧ:ММ, например /time 09:30")
            return
        state["time"] = f"{int(m.group(1)):02d}:{m.group(2)}"
        api.send(chat_id, f"Готово: ежедневная подборка в {state['time']} (по Астане).")
    elif cmd == "/timemenu":
        rows = [[{"text": t, "callback_data": f"time:{t}"} for t in row] for row in TIME_CHOICES]
        api.call(
            "sendMessage",
            chat_id=chat_id,
            text=f"Сейчас подборка приходит в {state['time']} (по Астане). Выберите новое время:",
            reply_markup={"inline_keyboard": rows},
        )
    elif cmd == "/status":
        status = "подписаны" if sub is not None else "не подписаны (нажмите /start)"
        api.send(
            chat_id,
            f"Вы {status}.\nПодборка каждый день в {state['time']} (по Астане).\n"
            f"Источники: {', '.join(s.name for s in sources)}.\nКлючевых слов: {len(groups)}.",
        )
    else:
        api.send(chat_id, "Не знаю такой команды. Список команд: /help")


def handle_callback(state: dict, cq: dict, api) -> None:
    """Нажатие инлайн-кнопки: сейчас только выбор времени ежедневной подборки."""
    if (cq.get("data") or "").startswith("kw:"):
        handle_keyword_callback(state, cq, api)
        return
    msg = cq.get("message")
    answer = None
    try:
        m = re.fullmatch(r"time:([01]\d|2[0-3]):([0-5]\d)", cq.get("data") or "")
        if m and msg:
            state["time"] = f"{m.group(1)}:{m.group(2)}"
            answer = f"Готово: подборка каждый день в {state['time']} (по Астане)."
            try:
                api.call("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"], text=answer)
            except RuntimeError as e:
                if "not modified" not in str(e):
                    raise
    finally:
        api.call("answerCallbackQuery", callback_query_id=cq["id"], **({"text": answer} if answer else {}))


# ---------- ежедневная рассылка ----------

_next_try = {"ts": 0.0}


def tick(state: dict, now: datetime, api, sources: list) -> bool:
    """Раз в день в назначенное время ищет вакансии и рассылает новые. True, если состояние изменилось."""
    api = KeyboardApi(api, lambda: main_keyboard(True))
    today = now.date().isoformat()
    start = at(now, state["time"])
    if not state["subscribers"] or state.get("last_run") == today:
        return False
    if not (start <= now < start + SEND_WINDOW) or time.time() < _next_try["ts"]:
        return False
    groups = active_groups(state)
    if not groups:  # ключевых слов нет, искать нечего
        return False

    items, errors = collect(sources, DAILY_DAYS, groups=groups)
    if errors and not items:
        _next_try["ts"] = time.time() + 1800  # источник недоступен, повторим через 30 минут
        print("daily run failed, retry later:", errors, flush=True)
        return False
    for cid, sub in state["subscribers"].items():
        try:
            deliver(api, int(cid), sub, items, errors, today, "Новые вакансии", mark_seen=True, only_new=True, groups=groups)
        except Exception as e:  # noqa: BLE001
            print("send error:", e, flush=True)
    state["last_run"] = today
    return True


# ---------- запуск ----------

def main() -> None:
    load_env(Path(__file__).with_name(".env"))
    token = os.environ.get("VACANCY_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    if not token:
        sys.exit("Токен не задан (VACANCY_BOT_TOKEN или BOT_TOKEN в .env)")
    api = Api(token)

    me = api.call("getMe")
    print(f"Бот вакансий @{me['username']} запущен. Ctrl+C для остановки.", flush=True)
    api.call("deleteWebhook", drop_pending_updates=True)  # сообщения за время офлайна игнорируем

    state = load_state()
    # подписчикам, которые ещё не видели меню, один раз показываем кнопки
    for cid, sub in state["subscribers"].items():
        if sub.get("menu") != MENU_VERSION:
            try:
                api.send(
                    int(cid),
                    "👇 Бот управляется кнопками внизу экрана, писать ничего не нужно.\n\n"
                    "🔎 Новые вакансии: найти прямо сейчас то, что вы ещё не видели\n"
                    "📅 За сутки / За 3 дня / За неделю: все подходящие за выбранный период\n"
                    "🔑 Ключевые слова: добавить, изменить или убрать слова для поиска\n"
                    f"⏰ Время рассылки: сейчас {state['time']} (по Астане)\n"
                    "ℹ️ Статус: проверить подписку\n"
                    "🔕 Отписаться: больше не присылать",
                    markup=main_keyboard(True),
                )
                sub["menu"] = MENU_VERSION
            except Exception as e:  # noqa: BLE001
                print("menu send error:", e, flush=True)
    save_state(state)

    offset = None
    while True:
        try:
            params = {"timeout": 20, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            for update in api.call("getUpdates", **params):
                offset = update["update_id"] + 1
                try:
                    if "callback_query" in update:
                        handle_callback(state, update["callback_query"], api)
                    else:
                        msg = update.get("message")
                        if not msg or msg["chat"]["type"] != "private" or not msg.get("text"):
                            continue
                        handle_message(state, msg, now_local(), api, SOURCES)
                    save_state(state)
                except Exception as e:  # noqa: BLE001
                    print("update error:", e, flush=True)
            if tick(state, now_local(), api, SOURCES):
                save_state(state)
        except KeyboardInterrupt:
            print("Остановлен.")
            return
        except Exception as e:  # noqa: BLE001
            print("error:", e, flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
