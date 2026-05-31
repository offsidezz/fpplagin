from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

import atexit
import base64
import email
import email.header
import hashlib
import imaplib
import json
import logging
import os
import queue
import re
import signal
import time
import uuid as uuid_lib
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from threading import Thread, Timer, Lock

from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
from FunPayAPI.common.enums import MessageTypes
from tg_bot import CBT
from telebot.types import (
    InlineKeyboardMarkup as K,
    InlineKeyboardButton as B,
    Message,
    CallbackQuery,
)

NAME = "AutoCode"
VERSION = "5.2.0"
UUID = str(uuid_lib.UUID("b7e21f3a-4c8d-4e2b-9a1f-3c5d6e7f8b9a"))
DESCRIPTION = (
    "Авто-выдача кодов с IMAP-почт по команде !cd / code.\n"
    "v5.2.0: очередь IMAP, мультиязычность, health check, авто-отзыв, "
    "winback, шифрование, graceful shutdown.\n"
    "Управление: /autocode"
)
CREDITS = "@offsidezq"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

logger = logging.getLogger("FPC.AutoCode")

# ── Callback constants ──────────────────────────────────────────────────────
AC_MAIN          = "ac_main"
AC_LIST          = "ac_list"
AC_ADD           = "ac_add"
AC_EDIT          = "ac_edit"
AC_DEL_ASK       = "ac_del_ask"
AC_DEL_OK        = "ac_del_ok"
AC_LOTS          = "ac_lots"
AC_LOT_ADD       = "ac_lot_add"
AC_LOT_DEL       = "ac_lot_del"
AC_WINDOW        = "ac_window"
AC_MAX_AGE       = "ac_max_age"
AC_LEN           = "ac_len"
AC_TYPE          = "ac_type"
AC_SPACES        = "ac_spaces"
AC_FROM          = "ac_from"
AC_SUBJ          = "ac_subj"
AC_BODY          = "ac_body"
AC_TEST          = "ac_test"
AC_LOG           = "ac_log"
AC_RENTALS       = "ac_rentals"
AC_RENT_DEL      = "ac_rent_del"
AC_RENT_DEL_OK   = "ac_rent_del_ok"
AC_RENT_EXT      = "ac_rent_ext"
AC_RENT_EXT_DO   = "ac_rent_ext_do"
AC_STATS         = "ac_stats"
AC_STATS_24H     = "ac_stats_24h"
AC_STATS_48H     = "ac_stats_48h"
AC_STATS_7D      = "ac_stats_7d"
AC_STATS_ALL     = "ac_stats_all"
AC_STATS_RANGE   = "ac_stats_range"
AC_BROADCAST     = "ac_broadcast"
AC_BCAST_SEND    = "ac_bcast_send"
AC_BCAST_RETRY   = "ac_bcast_retry"
AC_BCAST_TPLS    = "ac_bcast_tpls"
AC_BCAST_TPL_USE = "ac_bcast_tpl_use"
AC_BCAST_TPL_DEL = "ac_bcast_tpl_del"
AC_BCAST_TPL_ADD = "ac_bcast_tpl_add"
AC_BCAST_HIST    = "ac_bcast_hist"
AC_BCAST_SCHED   = "ac_bcast_sched"
AC_IMAP          = "ac_imap"
AC_HEALTH        = "ac_health"
AC_REVIEW        = "ac_review"
AC_WINBACK       = "ac_winback"
AC_WB_RUN        = "ac_wb_run"
AC_WB_TPL        = "ac_wb_tpl"

# ── Storage paths ───────────────────────────────────────────────────────────
DATA_DIR        = "storage/autocode"
BACKUP_DIR      = os.path.join(DATA_DIR, "backups")
ACCOUNTS_FILE   = os.path.join(DATA_DIR, "accounts.json")
LOG_FILE        = os.path.join(DATA_DIR, "log.json")
USED_FILE       = os.path.join(DATA_DIR, "used_codes.json")
RENTALS_FILE    = os.path.join(DATA_DIR, "rentals.json")
TEMPLATES_FILE  = os.path.join(DATA_DIR, "templates.json")
BCAST_HIST_FILE = os.path.join(DATA_DIR, "broadcast_history.json")
WARNED_FILE     = os.path.join(DATA_DIR, "warned_state.json")
HEALTH_FILE     = os.path.join(DATA_DIR, "imap_health.json")
WINBACK_FILE    = os.path.join(DATA_DIR, "winback_sent.json")
SETTINGS_FILE   = os.path.join(DATA_DIR, "settings.json")

# Encrypted files (содержат чувствительные данные)
ENCRYPTED_FILES = {RENTALS_FILE, USED_FILE, ACCOUNTS_FILE}

# ── IMAP auto-detect map ────────────────────────────────────────────────────
IMAP_HOSTS = {
    "gmail.com":      "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com":    "imap-mail.outlook.com",
    "hotmail.com":    "imap-mail.outlook.com",
    "live.com":       "imap-mail.outlook.com",
    "yandex.ru":      "imap.yandex.ru",
    "yandex.com":     "imap.yandex.ru",
    "ya.ru":          "imap.yandex.ru",
    "mail.ru":        "imap.mail.ru",
    "inbox.ru":       "imap.mail.ru",
    "list.ru":        "imap.mail.ru",
    "bk.ru":          "imap.mail.ru",
}

IMAP_TIMEOUT        = 15
CODE_CD             = 30
MAX_CODES_HOUR      = 10
WARN_BEFORE_H       = 4
PRE_WINDOW          = 2 * 3600
USED_CODE_TTL_SEC   = 3 * 3600
WEEKLY_REPORT_DOW   = 6
WEEKLY_REPORT_HOUR  = 9
RENTAL_GRACE_SEC    = 300
HEALTH_CHECK_INTERVAL = 3600  # раз в час
REVIEW_DELAY_SEC    = 30 * 60  # 30 минут
WINBACK_AFTER_DAYS  = 3
WINBACK_MAX_DAYS    = 30  # не трогать аренды старше 30 дней

# ── XOR password encryption ─────────────────────────────────────────────────
_SECRET_KEY = (os.environ.get("AC_SECRET") or "ac_fp_secret_2025").encode()
_ENC_MARKER = "ACENC1:"  # маркер зашифрованного JSON-файла

def _xor_bytes(data: bytes) -> bytes:
    key = _SECRET_KEY
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

def _xor_crypt(data: str) -> str:
    return base64.b64encode(_xor_bytes(data.encode("utf-8"))).decode()

def _encrypt_password(plain: str) -> str:
    return _xor_crypt(plain)

def _decrypt_password(enc: str) -> str:
    try:
        raw = base64.b64decode(enc.encode())
        return _xor_bytes(raw).decode("utf-8")
    except Exception:
        return enc

def _encrypt_file_content(plain_json: str) -> str:
    """Шифрует JSON-строку для записи на диск."""
    encoded = base64.b64encode(_xor_bytes(plain_json.encode("utf-8"))).decode()
    return _ENC_MARKER + encoded

def _decrypt_file_content(raw: str) -> str:
    """Расшифровывает содержимое файла. Если маркера нет — возвращает как есть (миграция)."""
    if not raw.startswith(_ENC_MARKER):
        return raw
    try:
        encoded = raw[len(_ENC_MARKER):]
        return _xor_bytes(base64.b64decode(encoded.encode())).decode("utf-8")
    except Exception as e:
        logger.error(f"AutoCode: ошибка расшифровки файла: {e}")
        return raw

_cardinal_ref = None
_shutdown_flag = {"stop": False}

# ── Storage helpers ──────────────────────────────────────────────────────────
def _ensure():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)

def _load(path, default):
    _ensure()
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        if path in ENCRYPTED_FILES:
            raw = _decrypt_file_content(raw)
        return json.loads(raw) if raw.strip() else default
    except Exception as e:
        logger.warning(f"AutoCode: не удалось прочитать {path}: {e}")
        return default

def _save(path, data):
    _ensure()
    try:
        json_str = json.dumps(data, ensure_ascii=False, indent=2)
        if path in ENCRYPTED_FILES:
            json_str = _encrypt_file_content(json_str)
        # атомарная запись через временный файл
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json_str)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"AutoCode: не удалось сохранить {path}: {e}")

def accounts():            return _load(ACCOUNTS_FILE, [])
def save_accs(d):          _save(ACCOUNTS_FILE, d)
def used_codes():          return _load(USED_FILE, {})
def save_used(d):          _save(USED_FILE, d)
def log_entries():         return _load(LOG_FILE, [])
def rentals():             return _load(RENTALS_FILE, {})
def save_rentals(d):       _save(RENTALS_FILE, d)
def templates():           return _load(TEMPLATES_FILE, [])
def save_templates(d):     _save(TEMPLATES_FILE, d)
def bcast_history():       return _load(BCAST_HIST_FILE, [])
def save_bcast_history(d): _save(BCAST_HIST_FILE, d)
def warned_state():        return _load(WARNED_FILE, {"warned_4h": [], "warned_30m": []})
def save_warned(d):        _save(WARNED_FILE, d)
def health_state():        return _load(HEALTH_FILE, {})
def save_health(d):        _save(HEALTH_FILE, d)
def winback_sent():        return _load(WINBACK_FILE, {})
def save_winback(d):       _save(WINBACK_FILE, d)
def app_settings():        return _load(SETTINGS_FILE, {
    "review_enabled":      True,
    "review_template":     "Если код подошёл — буду благодарен за отзыв 🙏 Это очень помогает!",
    "winback_enabled":     True,
    "winback_template":    "👋 Скучаем! Возвращайся — даём промокод RETURN25 на скидку 25% на следующую аренду. Просто напиши в чат, когда соберёшься заказывать.",
    "winback_discount":    25,
    "queue_pause_sec":     2,
})
def save_settings(d):      _save(SETTINGS_FILE, d)

def add_log(email_addr, lot_id, buyer, code, chat_id=None):
    entries = log_entries()
    entries.append({
        "time":    time.time(),
        "email":   email_addr,
        "lot_id":  lot_id,
        "buyer":   buyer,
        "code":    code,
        "chat_id": chat_id,
    })
    _save(LOG_FILE, entries[-500:])

def _fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")

def _fmt_remaining(ts):
    left = max(0, int(ts - time.time()))
    h, m = divmod(left // 60, 60)
    return f"{h}ч {m:02d}мин"

# ── IMAP host auto-detect ────────────────────────────────────────────────────
def detect_imap_host(email_addr: str) -> str:
    domain = email_addr.split("@")[-1].lower()
    return IMAP_HOSTS.get(domain, f"imap.{domain}")

# ── Language detection ──────────────────────────────────────────────────────
_RU_RE = re.compile(r"[а-яА-ЯёЁ]")

def detect_lang(text: str) -> str:
    """Возвращает 'ru' или 'en'. Базовая эвристика по кириллице."""
    if not text:
        return "ru"
    ru_chars = len(_RU_RE.findall(text))
    total    = max(1, len(re.findall(r"[a-zA-Zа-яА-ЯёЁ]", text)))
    return "ru" if (ru_chars / total) >= 0.3 else "en"

# Locale strings
L = {
    "ru": {
        "rental_activated":   "🌸 | Аренда активирована, напиши команду в чат: !cd или code",
        "rental_renewed":     "✅ Аренда продлена на {h}ч!\nНовое время окончания: {t}",
        "code_msg":           "🔑 Ваш код: {code}\n📅 Получен: {dt}",
        "no_rental":          "❌ У вас нет активной аренды. Пожалуйста, оформите заказ.",
        "no_active":          "❌ У вас нет активной аренды.",
        "remaining":          "⏳ Осталось: {rem}\n📅 Окончание: {end}",
        "code_not_found":     "❌ Код не найден. Попробуйте через минуту или обратитесь к продавцу.",
        "rate_wait":          "⏳ Подождите {s} сек. перед повторным запросом.",
        "rate_hour_limit":    "❌ Превышен лимит ({n} запросов/час). Обратитесь к продавцу.",
        "mailbox_missing":    "⚠️ Почтовый аккаунт не настроен. Обратитесь к продавцу.",
        "warn_4h":            "⏰ До окончания аренды осталось {h} часа. Хотите продлить? Просто сделайте новый заказ 😊",
        "warn_30m":           "⏰ До окончания аренды осталось 30 минут!",
        "rental_ended":       "✅ Ваша аренда завершена, спасибо за покупку! Хотите продлить? Просто сделайте новый заказ 😊",
        "queue_position":     "⏳ Ваш запрос в очереди (позиция {pos}). Подождите немного...",
    },
    "en": {
        "rental_activated":   "🌸 | Rental activated! Send !cd or code in this chat to receive your code.",
        "rental_renewed":     "✅ Rental extended by {h}h!\nNew expiry: {t}",
        "code_msg":           "🔑 Your code: {code}\n📅 Received: {dt}",
        "no_rental":          "❌ You have no active rental. Please place an order first.",
        "no_active":          "❌ You have no active rental.",
        "remaining":          "⏳ Time left: {rem}\n📅 Expires: {end}",
        "code_not_found":     "❌ Code not found. Please try again in a minute or contact the seller.",
        "rate_wait":          "⏳ Please wait {s} sec before the next request.",
        "rate_hour_limit":    "❌ Hourly limit exceeded ({n}/hour). Contact the seller.",
        "mailbox_missing":    "⚠️ Mailbox is not configured. Please contact the seller.",
        "warn_4h":            "⏰ {h} hours left on your rental. Want to extend? Just place a new order 😊",
        "warn_30m":           "⏰ Only 30 minutes left on your rental!",
        "rental_ended":       "✅ Your rental has ended. Thank you for your purchase! Want to extend? Just place a new order 😊",
        "queue_position":     "⏳ Your request is queued (position {pos}). Hang tight...",
    },
}

def t(lang: str, key: str, **kwargs) -> str:
    """Локализованная строка."""
    bundle = L.get(lang, L["ru"])
    text = bundle.get(key, L["ru"].get(key, key))
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

def get_buyer_lang(buyer: str, fallback_text: str = "") -> str:
    """Берёт сохранённый язык покупателя или определяет из текста."""
    rents = rentals()
    for r in rents.values():
        if r.get("buyer") == buyer and r.get("lang"):
            return r["lang"]
    return detect_lang(fallback_text)

# ── Email parsing ────────────────────────────────────────────────────────────
def _decode_str(value: str) -> str:
    parts = email.header.decode_header(value)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)

def _html_to_text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)

def _get_text(msg) -> tuple[str, str]:
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                plain += part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
            elif ct == "text/html":
                html += part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            plain = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return plain, html

def _find_code(plain, html, subj, acc) -> str | None:
    code_type    = acc.get("code_type", "alnum")
    code_len     = acc.get("code_len", 0)
    allow_spaces = acc.get("allow_spaces", False)

    patterns = {
        "digits":     r"\d",
        "alpha":      r"[A-Za-z]",
        "alnum":      r"[A-Za-z0-9]",
        "alnum-dash": r"[A-Za-z0-9\-]",
    }
    char_cls = patterns.get(code_type, r"[A-Za-z0-9]")
    if allow_spaces:
        char_cls = f"(?:{char_cls}| )"

    if code_len:
        pat = re.compile(rf"\b{char_cls}{{{max(1, code_len - 2)},{code_len + 10}}}\b")
    else:
        pat = re.compile(rf"{char_cls}{{4,64}}")

    text = plain or _html_to_text(html)

    for line in text.splitlines():
        line = line.strip()
        if pat.fullmatch(line):
            return line

    keywords = ["код", "code", "ключ", "key", "enter", "активац"]
    for kw in keywords:
        idx = text.lower().find(kw)
        if idx != -1:
            snippet = text[idx:idx + 120]
            m = pat.search(snippet)
            if m:
                return m.group()

    m = pat.search(text)
    return m.group() if m else None

# ── IMAP fetch ───────────────────────────────────────────────────────────────
def fetch_code(acc, used, not_before_ts=None) -> tuple[str | None, str | None]:
    email_addr  = acc.get("email", "")
    password    = _decrypt_password(acc.get("password", ""))
    imap_host   = acc.get("imap_host") or detect_imap_host(email_addr)
    max_age     = acc.get("max_age_min", 60)
    filter_from = acc.get("filter_from", "")
    filter_subj = acc.get("filter_subj", "")

    raw_used = used.get(email_addr, [])
    used_set = set()
    for entry in raw_used:
        if isinstance(entry, dict):
            used_set.add(entry.get("code", ""))
        else:
            used_set.add(entry)

    try:
        mail = imaplib.IMAP4_SSL(imap_host, timeout=IMAP_TIMEOUT)
        mail.login(email_addr, password)
        mail.select("INBOX")

        if filter_from:
            _, data = mail.search(None, "FROM", filter_from)
        else:
            _, data = mail.search(None, "ALL")

        msg_ids = data[0].split()
        if not msg_ids:
            mail.logout()
            return None, "Входящих писем нет."

        cutoff = time.time() - max_age * 60
        if not_before_ts:
            cutoff = min(cutoff, not_before_ts - PRE_WINDOW)

        for mid in reversed(msg_ids[-50:]):
            try:
                _, raw = mail.fetch(mid, "(RFC822)")
                msg = email.message_from_bytes(raw[0][1])
                date_str = msg.get("Date", "")
                try:
                    msg_ts = parsedate_to_datetime(date_str).timestamp()
                except Exception:
                    continue
                if msg_ts < cutoff:
                    continue
                subj = _decode_str(msg.get("Subject", ""))
                if filter_subj and filter_subj.lower() not in subj.lower():
                    continue
                plain, html = _get_text(msg)
                code = _find_code(plain, html, subj, acc)
                if code and code not in used_set:
                    mail.logout()
                    return code, None
            except Exception:
                continue

        mail.logout()
        return None, "Подходящий код не найден в письмах."

    except imaplib.IMAP4.error as e:
        return None, f"IMAP ошибка: {e}"
    except OSError as e:
        return None, f"Сервер недоступен (таймаут {IMAP_TIMEOUT}с): {e}"
    except Exception as e:
        return None, f"Ошибка: {e}"

def test_imap(acc) -> str:
    host     = acc.get("imap_host") or detect_imap_host(acc.get("email", ""))
    password = _decrypt_password(acc.get("password", ""))
    try:
        mail = imaplib.IMAP4_SSL(host, timeout=IMAP_TIMEOUT)
        mail.login(acc["email"], password)
        mail.select("INBOX")
        mail.logout()
        return f"✅ Подключение успешно ({host})"
    except Exception as e:
        return f"❌ Ошибка: {e}"

# ── IMAP Queue (per-mailbox serialization) ──────────────────────────────────
class IMAPQueue:
    """
    Один воркер на каждую почту. Запросы fetch_code сериализуются по email,
    чтобы IMAP не получал несколько параллельных коннектов.
    """
    def __init__(self):
        self.queues: dict[str, queue.Queue] = {}
        self.workers: dict[str, Thread] = {}
        self.lock = Lock()

    def submit(self, email_addr: str, task_fn, callback, *args, **kwargs):
        with self.lock:
            if email_addr not in self.queues:
                self.queues[email_addr] = queue.Queue()
                worker = Thread(target=self._worker, args=(email_addr,), daemon=True)
                self.workers[email_addr] = worker
                worker.start()
            q = self.queues[email_addr]
            position = q.qsize() + 1
            q.put((task_fn, callback, args, kwargs))
            return position

    def _worker(self, email_addr: str):
        q = self.queues[email_addr]
        settings = app_settings()
        pause = settings.get("queue_pause_sec", 2)
        while not _shutdown_flag["stop"]:
            try:
                task_fn, callback, args, kwargs = q.get(timeout=5)
            except queue.Empty:
                continue
            try:
                result = task_fn(*args, **kwargs)
                try:
                    callback(result)
                except Exception as cb_err:
                    logger.error(f"AutoCode queue callback error ({email_addr}): {cb_err}")
            except Exception as e:
                logger.error(f"AutoCode queue task error ({email_addr}): {e}")
                try:
                    callback((None, f"Внутренняя ошибка: {e}"))
                except Exception:
                    pass
            time.sleep(pause)

    def queue_size(self, email_addr: str) -> int:
        q = self.queues.get(email_addr)
        return q.qsize() if q else 0

_imap_queue = IMAPQueue()

# ── Rental helpers ───────────────────────────────────────────────────────────
def get_active_rentals() -> list:
    now = time.time()
    return [r for r in rentals().values() if r.get("expires_at", 0) > now]

def get_expiring_rentals(within_hours: float) -> list:
    now = time.time()
    cutoff = now + within_hours * 3600
    return [
        r for r in rentals().values()
        if now < r.get("expires_at", 0) <= cutoff
    ]

# ── Rate-limit helpers ───────────────────────────────────────────────────────
_code_requests: dict[str, list[float]] = {}
_last_code_ts:  dict[str, float]       = {}

def _check_rate(buyer: str, lang: str = "ru") -> tuple[bool, str]:
    now = time.time()
    last = _last_code_ts.get(buyer, 0)
    if now - last < CODE_CD:
        wait = int(CODE_CD - (now - last))
        return False, t(lang, "rate_wait", s=wait)
    hour_ago = now - 3600
    reqs = [x for x in _code_requests.get(buyer, []) if x > hour_ago]
    _code_requests[buyer] = reqs
    if len(reqs) >= MAX_CODES_HOUR:
        return False, t(lang, "rate_hour_limit", n=MAX_CODES_HOUR)
    return True, ""

def _record_request(buyer: str):
    now = time.time()
    _last_code_ts[buyer] = now
    _code_requests.setdefault(buyer, []).append(now)

# ── Keyboards ────────────────────────────────────────────────────────────────
def kb_main():
    return K(keyboard=[
        [B("📬 Список почт",     callback_data=f"{AC_LIST}:0")],
        [B("🏠 Активные аренды", callback_data=AC_RENTALS),
         B("📜 Лог выдач",       callback_data=AC_LOG)],
        [B("📊 Статистика",      callback_data=AC_STATS),
         B("📢 Рассылка",        callback_data=AC_BROADCAST)],
        [B("🩺 Health check",    callback_data=AC_HEALTH),
         B("⚙️ Настройки",       callback_data=AC_REVIEW)],
        [B("🔄 Win-back",        callback_data=AC_WINBACK)],
    ])

def kb_stats_period():
    return K(keyboard=[
        [B("⏱ 24 часа",     callback_data=AC_STATS_24H),
         B("⏱ 48 часов",    callback_data=AC_STATS_48H)],
        [B("📅 7 дней",      callback_data=AC_STATS_7D),
         B("📋 Всё время",   callback_data=AC_STATS_ALL)],
        [B("🗓 Свой период", callback_data=AC_STATS_RANGE)],
        [B("◀ Назад",        callback_data=AC_MAIN)],
    ])

def kb_broadcast_menu():
    return K(keyboard=[
        [B("✍️ Новое сообщение",  callback_data=AC_BCAST_SEND)],
        [B("📁 Шаблоны",          callback_data=AC_BCAST_TPLS)],
        [B("📋 История рассылок", callback_data=AC_BCAST_HIST)],
        [B("⏰ Отложенная",       callback_data=AC_BCAST_SCHED)],
        [B("◀ Назад",             callback_data=AC_MAIN)],
    ])

def kb_templates(tpls):
    rows = []
    for i, tpl in enumerate(tpls):
        rows.append([
            B(f"📄 {tpl['name']}", callback_data=f"{AC_BCAST_TPL_USE}:{i}"),
            B("🗑",                 callback_data=f"{AC_BCAST_TPL_DEL}:{i}"),
        ])
    rows.append([B("➕ Добавить шаблон", callback_data=AC_BCAST_TPL_ADD)])
    rows.append([B("◀ Назад",           callback_data=AC_BROADCAST)])
    return K(keyboard=rows)

# ── Stats helpers ────────────────────────────────────────────────────────────
def _filter_log_by_hours(hours):
    entries = log_entries()
    if hours is None:
        return entries
    cutoff = time.time() - hours * 3600
    return [e for e in entries if e.get("time", 0) >= cutoff]

def _filter_log_by_range(date_from, date_to):
    entries = log_entries()
    return [
        e for e in entries
        if date_from.timestamp() <= e.get("time", 0) <= date_to.timestamp()
    ]

def _stats_text(entries, label: str) -> str:
    total    = len(entries)
    by_email = Counter(e.get("email") for e in entries)
    by_buyer = Counter(e.get("buyer") for e in entries)

    hours_list = [
        datetime.fromtimestamp(e["time"]).hour
        for e in entries if "time" in e
    ]
    peak = Counter(hours_list).most_common(3)
    peak_str = ", ".join(f"{h:02d}:00 ({c})" for h, c in peak) or "—"

    all_rents     = _load(RENTALS_FILE, {})
    rent_by_buyer = Counter(r.get("buyer") for r in all_rents.values())
    top_renters   = "\n".join(
        f"  {i+1}. {b}: {c} аренд"
        for i, (b, c) in enumerate(rent_by_buyer.most_common(3))
    ) or "  —"

    top_emails = "\n".join(
        f"  {i+1}. {em}: {cnt}"
        for i, (em, cnt) in enumerate(by_email.most_common(5))
    ) or "  —"
    top_buyers = "\n".join(
        f"  {i+1}. {b}: {cnt}"
        for i, (b, cnt) in enumerate(by_buyer.most_common(3))
    ) or "  —"

    active = len(get_active_rentals())

    return (
        f"📊 Статистика — {label}\n\n"
        f"Выдано кодов: {total}\n"
        f"Активных аренд сейчас: {active}\n\n"
        f"⏰ Пиковые часы:\n  {peak_str}\n\n"
        f"Топ аккаунтов:\n{top_emails}\n\n"
        f"Топ покупателей (коды):\n{top_buyers}\n\n"
        f"Топ покупателей (аренды):\n{top_renters}"
    )

# ── TG notifications ─────────────────────────────────────────────────────────
def _notify_tg(cardinal, text: str):
    try:
        bot = cardinal.telegram.bot
        for uid in cardinal.telegram.authorized_users:
            bot.send_message(uid, text)
    except Exception as e:
        logger.warning(f"TG notify failed: {e}")

def _notify_tg_code_not_found(cardinal, buyer, email_addr, reason):
    _notify_tg(cardinal,
        f"⚠️ Код не найден\nПокупатель: {buyer}\nПочта: {email_addr}\nПричина: {reason}")

def _notify_tg_rate_limit(cardinal, buyer, count):
    _notify_tg(cardinal,
        f"🚨 Лимит запросов\nПокупатель {buyer} сделал {count} запросов за час.")

# ── IMAP startup test ────────────────────────────────────────────────────────
def _startup_imap_test(cardinal):
    time.sleep(5)
    accs = accounts()
    if not accs:
        return
    broken = []
    health = {}
    for acc in accs:
        result = test_imap(acc)
        health[acc["email"]] = {
            "last_check": time.time(),
            "ok":         result.startswith("✅"),
            "message":    result,
        }
        if result.startswith("❌"):
            broken.append(f"📧 {acc['email']}\n   {result}")
    save_health(health)
    if broken:
        msg = "🚨 AutoCode — проблемы с IMAP при старте:\n\n" + "\n\n".join(broken)
        _notify_tg(cardinal, msg)
        logger.warning(f"AutoCode startup IMAP errors: {broken}")
    else:
        logger.info(f"AutoCode: все {len(accs)} IMAP-аккаунтов прошли проверку при старте.")

# ── IMAP Health Check (hourly) ──────────────────────────────────────────────
def _imap_health_worker(cardinal):
    """Раз в час проверяет все почты, шлёт алерт если что-то сломалось."""
    time.sleep(HEALTH_CHECK_INTERVAL)
    while not _shutdown_flag["stop"]:
        try:
            accs = accounts()
            prev_health = health_state()
            new_health = {}
            newly_broken = []
            recovered = []

            for acc in accs:
                em = acc["email"]
                result = test_imap(acc)
                ok = result.startswith("✅")
                new_health[em] = {
                    "last_check": time.time(),
                    "ok":         ok,
                    "message":    result,
                }
                was_ok = prev_health.get(em, {}).get("ok", True)
                if was_ok and not ok:
                    newly_broken.append(f"📧 {em}\n   {result}")
                elif not was_ok and ok:
                    recovered.append(f"📧 {em}")

            save_health(new_health)

            if newly_broken:
                _notify_tg(cardinal,
                    "🚨 AutoCode Health Check — почта перестала работать:\n\n"
                    + "\n\n".join(newly_broken)
                )
            if recovered:
                _notify_tg(cardinal,
                    "✅ AutoCode Health Check — почта восстановилась:\n\n"
                    + "\n".join(recovered)
                )

            logger.info(f"AutoCode health check: {len(accs)} почт проверено.")
        except Exception as e:
            logger.error(f"Health check error: {e}")

        # спим час с проверкой shutdown
        for _ in range(HEALTH_CHECK_INTERVAL):
            if _shutdown_flag["stop"]:
                return
            time.sleep(1)

# ── Startup sales scan ──────────────────────────────────────────────────────
def _startup_sales_scan(cardinal):
    time.sleep(10)
    try:
        acc_obj = cardinal.account
        now     = time.time()
        cutoff  = now - 30 * 24 * 3600

        logger.info("AutoCode: сканирование продаж за 30 дней...")

        all_shortcuts = []
        start_from    = None
        pages         = 0

        while pages < 50:
            try:
                result = acc_obj.get_sales(
                    start_from=start_from,
                    include_paid=True,
                    include_closed=True,
                    include_refunded=False,
                )
                if isinstance(result, tuple):
                    next_id   = result[0]
                    shortcuts = result[1] if len(result) > 1 else []
                else:
                    break

                if not shortcuts:
                    break

                all_shortcuts.extend(shortcuts)
                pages += 1

                last    = shortcuts[-1]
                last_ts = None
                if hasattr(last, "date_ts"):
                    last_ts = last.date_ts
                elif hasattr(last, "date") and last.date:
                    try:
                        last_ts = last.date.timestamp()
                    except Exception:
                        pass

                if last_ts and last_ts < cutoff:
                    break

                if not next_id:
                    break
                start_from = next_id

            except Exception as e:
                logger.warning(f"AutoCode: ошибка при получении страницы продаж: {e}")
                break

        logger.info(f"AutoCode: получено {len(all_shortcuts)} заказов за 30 дней.")

        if not all_shortcuts:
            return

        rents    = rentals()
        accs     = accounts()
        restored = 0
        skipped  = 0
        no_chat  = 0

        existing_order_ids = {r.get("order_id") for r in rents.values()}

        for shortcut in all_shortcuts:
            try:
                order_id  = str(getattr(shortcut, "id", "") or getattr(shortcut, "order_id", ""))
                lot_name  = str(getattr(shortcut, "description", "") or getattr(shortcut, "lot_name", "") or "")
                buyer     = str(getattr(shortcut, "buyer_username", "") or getattr(shortcut, "buyer", "") or "")
                lot_id    = str(getattr(shortcut, "lot_id", "") or "")

                chat_id = None
                for attr in ("chat_id", "buyer_id", "node_id", "user_id"):
                    val = getattr(shortcut, attr, None)
                    if val:
                        try:
                            chat_id = int(val)
                            break
                        except (TypeError, ValueError):
                            pass

                purchase_ts = None
                if hasattr(shortcut, "date_ts"):
                    purchase_ts = shortcut.date_ts
                elif hasattr(shortcut, "date") and shortcut.date:
                    try:
                        purchase_ts = shortcut.date.timestamp()
                    except Exception:
                        pass

                if not purchase_ts:
                    skipped += 1
                    continue

                if purchase_ts < cutoff:
                    skipped += 1
                    continue

                if order_id and order_id in existing_order_ids:
                    skipped += 1
                    continue

                hours = _parse_hours(lot_name)
                if not hours:
                    skipped += 1
                    continue

                expires_at = purchase_ts + hours * 3600

                if expires_at <= now:
                    skipped += 1
                    continue

                acc_email = None
                for a in accs:
                    if lot_id in a.get("lot_ids", []) or not a.get("lot_ids"):
                        acc_email = a["email"]
                        break

                if not acc_email:
                    skipped += 1
                    continue

                chat_name = buyer
                if not chat_id:
                    try:
                        chat = acc_obj.get_chat_by_name(buyer, True)
                        if chat:
                            chat_id   = chat.id
                            chat_name = getattr(chat, "name", buyer)
                    except Exception:
                        pass

                if not chat_id:
                    no_chat += 1

                key = order_id or str(uuid_lib.uuid4())
                rents[key] = {
                    "order_key":   key,
                    "buyer":       buyer,
                    "chat_id":     chat_id,
                    "chat_name":   chat_name,
                    "email":       acc_email,
                    "lot_id":      lot_id,
                    "order_id":    order_id,
                    "purchase_ts": purchase_ts,
                    "expires_at":  expires_at,
                    "hours":       hours,
                    "restored":    True,
                    "lang":        "ru",
                }
                existing_order_ids.add(order_id)
                restored += 1

            except Exception as e:
                logger.warning(f"AutoCode: ошибка обработки заказа: {e}")
                continue

        if restored > 0:
            save_rentals(rents)

        logger.info(
            f"AutoCode: сканирование завершено. "
            f"Восстановлено: {restored} (без chat_id: {no_chat}), пропущено: {skipped}."
        )

        if restored > 0:
            extra = f"\n⚠️ Без chat_id: {no_chat}" if no_chat else ""
            _notify_tg(cardinal,
                f"✅ AutoCode: восстановлено {restored} активных аренд "
                f"из истории продаж за 30 дней.{extra}"
            )

    except Exception as e:
        logger.error(f"AutoCode: ошибка сканирования продаж при старте: {e}")

# ── Used-code cleanup ────────────────────────────────────────────────────────
def _used_code_cleanup_worker():
    while not _shutdown_flag["stop"]:
        for _ in range(3600):
            if _shutdown_flag["stop"]:
                return
            time.sleep(1)
        try:
            now  = time.time()
            used = used_codes()
            changed = False
            for em, entries in used.items():
                new_entries = []
                for entry in entries:
                    if isinstance(entry, dict):
                        if now - entry.get("used_at", 0) < USED_CODE_TTL_SEC:
                            new_entries.append(entry)
                        else:
                            changed = True
                    else:
                        new_entries.append({"code": entry, "used_at": now})
                        changed = True
                used[em] = new_entries
            if changed:
                save_used(used)
                logger.info("AutoCode: очистка использованных кодов выполнена.")
        except Exception as e:
            logger.error(f"Used-code cleanup error: {e}")

# ── Weekly report ────────────────────────────────────────────────────────────
def _weekly_report_worker(cardinal):
    while not _shutdown_flag["stop"]:
        now = datetime.now()
        days_ahead = (WEEKLY_REPORT_DOW - now.weekday()) % 7
        if days_ahead == 0 and now.hour >= WEEKLY_REPORT_HOUR:
            days_ahead = 7
        next_run = now.replace(hour=WEEKLY_REPORT_HOUR, minute=0, second=0, microsecond=0) \
                   + timedelta(days=days_ahead)
        sleep_sec = (next_run - datetime.now()).total_seconds()
        # дробим сон
        end = time.time() + max(sleep_sec, 60)
        while time.time() < end:
            if _shutdown_flag["stop"]:
                return
            time.sleep(min(30, end - time.time()))
        try:
            entries_7d = _filter_log_by_hours(168)
            text = (
                f"📅 Еженедельный отчёт AutoCode\n"
                f"{(datetime.now() - timedelta(days=7)).strftime('%d.%m')} — "
                f"{datetime.now().strftime('%d.%m.%Y')}\n\n"
            ) + _stats_text(entries_7d, "7 дней")
            _notify_tg(cardinal, text)
        except Exception as e:
            logger.error(f"Weekly report error: {e}")

# ── Expiry watcher (FIXED: персистентный warned + правильное окно 4ч) ───────
def _expiry_watcher(cardinal):
    """
    Исправления:
    1. warned_4h / warned_30m теперь хранятся в файле — не теряются при перезапуске.
    2. Окно отправки 4ч-напоминания сужено: только когда осталось 3:50 — 4:00ч
       (раньше срабатывало при любом времени <= 4ч, что давало повторные алерты).
    3. Окно для 30-минутного: 25-30 минут.
    """
    startup_ts = time.time()
    state = warned_state()
    warned_4h = set(state.get("warned_4h", []))
    warned_30m = set(state.get("warned_30m", []))
    state_dirty = False

    def _persist_state():
        save_warned({
            "warned_4h":  list(warned_4h),
            "warned_30m": list(warned_30m),
        })

    while not _shutdown_flag["stop"]:
        time.sleep(60)
        if _shutdown_flag["stop"]:
            break
        try:
            rents   = rentals()
            now     = time.time()
            changed = False
            state_dirty = False

            # GC: убираем из warned те ключи, которых уже нет в rentals
            for s in (warned_4h, warned_30m):
                stale = [k for k in s if k not in rents]
                for k in stale:
                    s.discard(k)
                    state_dirty = True

            for key, r in list(rents.items()):
                exp         = r.get("expires_at", 0)
                cid         = r.get("chat_id")
                cname       = r.get("chat_name", "")
                purchase_ts = r.get("purchase_ts", 0)
                lang        = r.get("lang", "ru")

                if purchase_ts and (now - purchase_ts) < RENTAL_GRACE_SEC:
                    continue

                if (now - startup_ts) < RENTAL_GRACE_SEC:
                    continue

                left = exp - now

                if not cid:
                    if now >= exp + 60:
                        del rents[key]
                        warned_4h.discard(key)
                        warned_30m.discard(key)
                        changed = True
                        state_dirty = True
                        logger.info(f"AutoCode: аренда {key} ({r.get('buyer')}) завершена (без chat_id).")
                    continue

                # 4ч предупреждение — только в узком окне 3:50 — 4:00 ч до окончания
                # И только если ещё не предупреждали
                four_h = WARN_BEFORE_H * 3600
                if (key not in warned_4h
                        and (four_h - 600) <= left <= four_h):
                    warned_4h.add(key)
                    state_dirty = True
                    Thread(
                        target=cardinal.send_message,
                        args=(cid, t(lang, "warn_4h", h=WARN_BEFORE_H), cname),
                        daemon=True,
                    ).start()
                    logger.info(f"AutoCode: 4h warn sent для {r.get('buyer')} (left={int(left)}s)")

                # 30-минутное — узкое окно 25-30 минут
                if (key not in warned_30m
                        and 1500 <= left <= 1800):
                    warned_30m.add(key)
                    state_dirty = True
                    Thread(
                        target=cardinal.send_message,
                        args=(cid, t(lang, "warn_30m"), cname),
                        daemon=True,
                    ).start()
                    logger.info(f"AutoCode: 30m warn sent для {r.get('buyer')} (left={int(left)}s)")

                # завершение
                if now >= exp + 60:
                    Thread(
                        target=cardinal.send_message,
                        args=(cid, t(lang, "rental_ended"), cname),
                        daemon=True,
                    ).start()
                    del rents[key]
                    warned_4h.discard(key)
                    warned_30m.discard(key)
                    changed = True
                    state_dirty = True
                    logger.info(f"AutoCode: аренда {key} ({r.get('buyer')}) завершена.")

            if changed:
                save_rentals(rents)
            if state_dirty:
                _persist_state()

        except Exception as e:
            logger.error(f"Expiry watcher error: {e}")

# ── Win-back worker ─────────────────────────────────────────────────────────
def _winback_worker(cardinal):
    """
    Раз в день проверяет: есть ли покупатели, у которых аренда закончилась
    3-30 дней назад, и мы им ещё не отправляли winback. Если есть — шлём.
    """
    time.sleep(120)  # подождать пока стартап завершится
    while not _shutdown_flag["stop"]:
        try:
            settings = app_settings()
            if not settings.get("winback_enabled", True):
                pass
            else:
                # ищем по логу выдач: покупатели, которым последняя выдача была
                # WINBACK_AFTER_DAYS - WINBACK_MAX_DAYS дней назад
                entries = log_entries()
                now = time.time()
                last_by_buyer = {}  # buyer -> (last_ts, chat_id)
                for e in entries:
                    b = e.get("buyer")
                    if not b:
                        continue
                    ts = e.get("time", 0)
                    cid = e.get("chat_id")
                    if b not in last_by_buyer or ts > last_by_buyer[b][0]:
                        last_by_buyer[b] = (ts, cid)

                active_buyers = {r.get("buyer") for r in get_active_rentals()}
                sent_log = winback_sent()
                new_sends = 0

                for buyer, (last_ts, cid) in last_by_buyer.items():
                    if buyer in active_buyers:
                        continue  # уже активный — не трогаем
                    days_ago = (now - last_ts) / 86400
                    if days_ago < WINBACK_AFTER_DAYS or days_ago > WINBACK_MAX_DAYS:
                        continue
                    if buyer in sent_log:
                        continue
                    if not cid:
                        # ищем chat_id из rentals (даже истёкших)
                        for r in rentals().values():
                            if r.get("buyer") == buyer and r.get("chat_id"):
                                cid = r["chat_id"]
                                break
                    if not cid:
                        continue

                    text = settings.get("winback_template", "")
                    try:
                        cardinal.send_message(cid, text, buyer)
                        sent_log[buyer] = {
                            "sent_at": now,
                            "chat_id": cid,
                            "last_rental_ts": last_ts,
                        }
                        new_sends += 1
                        logger.info(f"AutoCode winback sent: {buyer}")
                        time.sleep(3)
                    except Exception as ex:
                        logger.warning(f"Winback send fail {buyer}: {ex}")

                if new_sends:
                    save_winback(sent_log)
                    _notify_tg(cardinal,
                        f"🔄 AutoCode Win-back: отправлено {new_sends} сообщений неактивным покупателям.")

        except Exception as e:
            logger.error(f"Winback worker error: {e}")

        # спим 24 часа
        for _ in range(86400):
            if _shutdown_flag["stop"]:
                return
            time.sleep(1)

# ── Review request (scheduled per code) ─────────────────────────────────────
def _schedule_review_request(cardinal, chat_id, chat_name, buyer, lang):
    """Через REVIEW_DELAY_SEC проверяем что аренда всё ещё активна и шлём просьбу отзыва."""
    def _send():
        if _shutdown_flag["stop"]:
            return
        try:
            settings = app_settings()
            if not settings.get("review_enabled", True):
                return
            # проверка: покупатель ещё активен (не вернул деньги, не закрыл сделку)
            still_active = any(
                r.get("buyer") == buyer and r.get("expires_at", 0) > time.time()
                for r in rentals().values()
            )
            if not still_active:
                return
            text = settings.get("review_template", "")
            cardinal.send_message(chat_id, text, chat_name)
            logger.info(f"AutoCode review request sent: {buyer}")
        except Exception as e:
            logger.warning(f"Review request fail {buyer}: {e}")

    t_timer = Timer(REVIEW_DELAY_SEC, _send)
    t_timer.daemon = True
    t_timer.start()

# ── Backup worker ───────────────────────────────────────────────────────────
def _backup_worker():
    """Раз в день делает копию rentals.json и log.json в backups/."""
    while not _shutdown_flag["stop"]:
        for _ in range(86400):
            if _shutdown_flag["stop"]:
                return
            time.sleep(1)
        try:
            _ensure()
            stamp = datetime.now().strftime("%Y%m%d_%H%M")
            for src in (RENTALS_FILE, LOG_FILE):
                if os.path.exists(src):
                    name = os.path.basename(src).replace(".json", f"_{stamp}.json")
                    dst = os.path.join(BACKUP_DIR, name)
                    with open(src, "rb") as f1, open(dst, "wb") as f2:
                        f2.write(f1.read())
            # чистим старые бекапы (>14 дней)
            cutoff = time.time() - 14 * 86400
            for fname in os.listdir(BACKUP_DIR):
                full = os.path.join(BACKUP_DIR, fname)
                try:
                    if os.path.getmtime(full) < cutoff:
                        os.remove(full)
                except Exception:
                    pass
            logger.info("AutoCode: бекапы созданы.")
        except Exception as e:
            logger.error(f"Backup worker error: {e}")

# ── Parse hours from lot name ────────────────────────────────────────────────
def _parse_hours(text: str) -> int | None:
    m = re.search(r"(\d+)\s*ч(?:ас(?:а|ов)?)?", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*д(?:ень|ня|ней|ен)?", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 24
    m = re.search(r"(\d+)\s*h(?:our|ours)?", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*d(?:ay|ays)?", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 24
    return None

# ── Graceful shutdown ────────────────────────────────────────────────────────
def _graceful_shutdown(*args):
    if _shutdown_flag["stop"]:
        return
    _shutdown_flag["stop"] = True
    logger.info("AutoCode: graceful shutdown — сохраняю состояние...")
    try:
        # warned_state уже сохраняется внутри watcher'а, но на всякий случай —
        # тут можно дописать другие финальные действия
        # rentals/used/log сохраняются после каждого изменения, так что они в порядке
        logger.info("AutoCode: завершено корректно.")
    except Exception as e:
        logger.error(f"Shutdown error: {e}")

atexit.register(_graceful_shutdown)
try:
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)
except Exception:
    pass  # на Windows SIGTERM не всегда работает

# ── on_new_order ─────────────────────────────────────────────────────────────
def on_new_order(c, e: NewOrderEvent):
    global _cardinal_ref
    _cardinal_ref = c

    desc     = getattr(e.order, "description", "") or ""
    lot_name = getattr(e.order, "lot_name", "") or desc
    hours    = _parse_hours(lot_name)
    if not hours:
        logger.debug(f"AutoCode: не удалось определить часы из названия лота: {lot_name!r}")
        return

    buyer     = getattr(e.order, "buyer_username", "") or ""
    chat_id   = getattr(e.order, "chat_id", None) or getattr(e.order, "buyer_id", None)
    chat_name = getattr(e.order, "chat_name", "") or buyer
    order_id  = str(getattr(e.order, "id", uuid_lib.uuid4()))
    lot_id    = str(getattr(e.order, "lot_id", ""))

    accs      = accounts()
    acc_email = None
    for acc in accs:
        if lot_id in acc.get("lot_ids", []) or not acc.get("lot_ids"):
            acc_email = acc["email"]
            break
    if not acc_email:
        logger.warning(f"AutoCode: нет аккаунта для лота {lot_id}")
        return

    lang = detect_lang(desc + " " + lot_name)

    now   = time.time()
    rents = rentals()

    for k, r in rents.items():
        if r.get("buyer") == buyer and r.get("expires_at", 0) > now:
            rents[k]["expires_at"] += hours * 3600
            rents[k]["hours"]      = rents[k].get("hours", 0) + hours
            save_rentals(rents)
            # сбрасываем warned для этой аренды чтобы предупреждение пришло заново
            state = warned_state()
            for s_key in ("warned_4h", "warned_30m"):
                if k in state.get(s_key, []):
                    state[s_key].remove(k)
            save_warned(state)

            cur_lang = r.get("lang", lang)
            Thread(
                target=c.send_message,
                args=(chat_id,
                      t(cur_lang, "rental_renewed", h=hours, t=_fmt_time(rents[k]['expires_at'])),
                      chat_name),
                daemon=True,
            ).start()
            logger.info(f"AutoCode: продление {buyer} +{hours}ч")
            return

    expires_at = now + hours * 3600
    rents[order_id] = {
        "order_key":   order_id,
        "buyer":       buyer,
        "chat_id":     chat_id,
        "chat_name":   chat_name,
        "email":       acc_email,
        "lot_id":      lot_id,
        "order_id":    order_id,
        "purchase_ts": now,
        "expires_at":  expires_at,
        "hours":       hours,
        "lang":        lang,
    }
    save_rentals(rents)
    logger.info(
        f"AutoCode: новая аренда {buyer} | {acc_email} | {hours}ч | "
        f"до {_fmt_time(expires_at)} | order={order_id} | lang={lang}"
    )

    Thread(
        target=c.send_message,
        args=(chat_id, t(lang, "rental_activated"), chat_name),
        daemon=True,
    ).start()

# ── on_new_message ────────────────────────────────────────────────────────────
def on_new_message(c, e: NewMessageEvent):
    global _cardinal_ref
    _cardinal_ref = c

    if e.message.author_id == c.account.id:
        return

    text  = (e.message.text or "").strip()
    lower = text.lower()

    buyer     = e.message.author
    chat_id   = e.message.chat_id
    chat_name = e.message.chat_name

    lang = get_buyer_lang(buyer, text)

    # update lang from current message if rental exists
    rents = rentals()
    rents_changed = False
    for k, r in rents.items():
        if r.get("buyer") == buyer and r.get("expires_at", 0) > time.time():
            if not r.get("chat_id"):
                rents[k]["chat_id"]   = chat_id
                rents[k]["chat_name"] = chat_name
                rents_changed = True
            if not r.get("lang"):
                rents[k]["lang"] = lang
                rents_changed = True
    if rents_changed:
        save_rentals(rents)

    if lower in ("!time", "time", "!время", "время"):
        now = time.time()
        active = sorted(
            [r for r in rents.values() if r.get("buyer") == buyer and r.get("expires_at", 0) > now],
            key=lambda r: r.get("purchase_ts", 0),
            reverse=True,
        )
        if not active:
            reply = t(lang, "no_active")
        else:
            r = active[0]
            reply = t(lang, "remaining",
                      rem=_fmt_remaining(r['expires_at']),
                      end=_fmt_time(r['expires_at']))
        Thread(target=c.send_message, args=(chat_id, reply, chat_name), daemon=True).start()
        return

    if lower not in ("!cd", "code", "код", "!код"):
        return

    allowed, reason = _check_rate(buyer, lang)
    if not allowed:
        Thread(target=c.send_message, args=(chat_id, reason, chat_name), daemon=True).start()
        reqs = _code_requests.get(buyer, [])
        if len(reqs) >= MAX_CODES_HOUR:
            Thread(target=_notify_tg_rate_limit, args=(c, buyer, len(reqs)), daemon=True).start()
        return

    now = time.time()
    active = sorted(
        [r for r in rents.values() if r.get("buyer") == buyer and r.get("expires_at", 0) > now],
        key=lambda r: r.get("purchase_ts", 0),
        reverse=True,
    )

    if not active:
        Thread(
            target=c.send_message,
            args=(chat_id, t(lang, "no_rental"), chat_name),
            daemon=True,
        ).start()
        return

    rental    = active[0]
    acc_email = rental.get("email")
    accs      = accounts()
    acc       = next((a for a in accs if a["email"] == acc_email), None)
    if not acc:
        Thread(
            target=c.send_message,
            args=(chat_id, t(lang, "mailbox_missing"), chat_name),
            daemon=True,
        ).start()
        return

    _record_request(buyer)

    # очередь: если уже есть запросы в обработке на эту почту, уведомляем
    q_size = _imap_queue.queue_size(acc_email)
    if q_size > 0:
        Thread(
            target=c.send_message,
            args=(chat_id, t(lang, "queue_position", pos=q_size + 1), chat_name),
            daemon=True,
        ).start()

    used = used_codes()
    not_before_ts = rental.get("purchase_ts", 0)

    def _on_result(result):
        code_val, err = result
        if not code_val:
            reason_str = err or "Код не найден."
            Thread(target=_notify_tg_code_not_found, args=(c, buyer, acc_email, reason_str), daemon=True).start()
            Thread(
                target=c.send_message,
                args=(chat_id, t(lang, "code_not_found"), chat_name),
                daemon=True,
            ).start()
            return

        # сохраняем used
        u = used_codes()
        u.setdefault(acc_email, []).append({"code": code_val, "used_at": time.time()})
        save_used(u)
        add_log(acc_email, rental.get("lot_id", "—"), buyer, code_val, chat_id)

        received_dt = datetime.now().strftime("%d.%m.%Y %H:%M")
        msg = t(lang, "code_msg", code=code_val, dt=received_dt)
        Thread(target=c.send_message, args=(chat_id, msg, chat_name), daemon=True).start()

        # запланировать просьбу отзыва через 30 минут
        _schedule_review_request(c, chat_id, chat_name, buyer, lang)

    _imap_queue.submit(acc_email, fetch_code, _on_result, acc, used, not_before_ts=not_before_ts)

# ── Telegram UI ──────────────────────────────────────────────────────────────
def init_autocode_tg(cardinal, *args):
    global _cardinal_ref
    _cardinal_ref = cardinal
    bot = cardinal.telegram.bot

    _bcast = {"text": "", "failed": [], "scheduled_timer": None}

    @bot.message_handler(commands=["autocode"])
    def cmd_autocode(message: Message):
        bot.send_message(message.chat.id,
                         f"⚙️ AutoCode v{VERSION} — выберите раздел:",
                         reply_markup=kb_main())

    @bot.callback_query_handler(func=lambda c: c.data == AC_MAIN)
    def open_main(call: CallbackQuery):
        bot.edit_message_text(f"⚙️ AutoCode v{VERSION} — выберите раздел:",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=kb_main())

    # ── Account list ──
    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_LIST}:"))
    def open_list(call: CallbackQuery):
        accs = accounts()
        health = health_state()
        rows = []
        for i, a in enumerate(accs):
            h = health.get(a["email"], {})
            mark = "✅" if h.get("ok", True) else "❌"
            rows.append([B(f"{mark} 📧 {a['email']}", callback_data=f"{AC_EDIT}:{i}")])
        rows.append([B("➕ Добавить почту", callback_data=AC_ADD)])
        rows.append([B("◀ Назад", callback_data=AC_MAIN)])
        bot.edit_message_text("📬 Почтовые аккаунты:", call.message.chat.id,
                              call.message.message_id, reply_markup=K(keyboard=rows))

    @bot.callback_query_handler(func=lambda c: c.data == AC_ADD)
    def act_add(call: CallbackQuery):
        msg = bot.send_message(call.message.chat.id, "Введите email:")
        bot.register_next_step_handler(msg, _step_email)

    def _step_email(message: Message):
        email_addr = message.text.strip()
        auto_host  = detect_imap_host(email_addr)
        accs = accounts()
        accs.append({
            "email": email_addr, "password": "", "imap_host": auto_host,
            "lot_ids": [], "code_type": "alnum", "code_len": 0,
            "allow_spaces": False, "max_age_min": 60,
            "filter_from": "", "filter_subj": "", "filter_body": "",
        })
        save_accs(accs)
        idx = len(accs) - 1
        msg = bot.send_message(message.chat.id,
            f"✅ Добавлен. IMAP: {auto_host}\nВведите пароль:")
        bot.register_next_step_handler(msg, lambda m: _step_pass(m, idx))

    def _step_pass(message: Message, idx: int):
        accs = accounts()
        plain = message.text.strip()
        accs[idx]["password"] = _encrypt_password(plain)
        save_accs(accs)
        bot.send_message(message.chat.id,
            f"✅ Пароль сохранён (зашифрован).\n"
            f"Используйте /autocode → 📬 Список почт → аккаунт для дополнительных настроек.")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_EDIT}:"))
    def open_edit(call: CallbackQuery):
        idx = int(call.data.split(":")[1])
        accs = accounts()
        if idx >= len(accs):
            bot.answer_callback_query(call.id, "Аккаунт не найден.")
            return
        acc = accs[idx]
        health = health_state().get(acc["email"], {})
        h_str = "✅ ok" if health.get("ok", True) else f"❌ {health.get('message', '?')}"
        last  = health.get("last_check")
        last_str = _fmt_time(last) if last else "—"
        text = (
            f"📧 {acc['email']}\n"
            f"🩺 Health: {h_str} (проверено: {last_str})\n"
            f"🔌 IMAP: {acc.get('imap_host', detect_imap_host(acc['email']))}\n"
            f"📦 Лоты: {', '.join(acc.get('lot_ids', [])) or 'все'}\n"
            f"🔤 Тип: {acc.get('code_type', 'alnum')} | "
            f"Длина: {acc.get('code_len', 0) or 'авто'}\n"
            f"📬 От: {acc.get('filter_from') or '—'} | "
            f"Тема: {acc.get('filter_subj') or '—'}\n"
            f"🔐 Пароль: {'зашифрован ✅' if acc.get('password') else 'не задан ❌'}"
        )
        kb = K(keyboard=[
            [B("🔌 IMAP хост",      callback_data=f"{AC_IMAP}:{idx}"),
             B("📦 Лоты",           callback_data=f"{AC_LOTS}:{idx}")],
            [B("🔢 Длина кода",     callback_data=f"{AC_LEN}:{idx}"),
             B("🔤 Тип кода",       callback_data=f"{AC_TYPE}:{idx}")],
            [B("📬 От (from)",      callback_data=f"{AC_FROM}:{idx}"),
             B("📌 Тема",           callback_data=f"{AC_SUBJ}:{idx}")],
            [B("🔑 Сменить пароль", callback_data=f"ac_chpass:{idx}"),
             B("🔍 Тест IMAP",      callback_data=f"{AC_TEST}:{idx}")],
            [B("🗑 Удалить",        callback_data=f"{AC_DEL_ASK}:{idx}")],
            [B("◀ Назад",           callback_data=f"{AC_LIST}:0")],
        ])
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("ac_chpass:"))
    def act_chpass(call: CallbackQuery):
        idx = int(call.data.split(":")[1])
        msg = bot.send_message(call.message.chat.id, "Введите новый пароль:")
        bot.register_next_step_handler(msg, lambda m: _do_chpass(m, idx))

    def _do_chpass(message: Message, idx: int):
        accs = accounts()
        if idx >= len(accs):
            bot.send_message(message.chat.id, "❌ Аккаунт не найден.")
            return
        accs[idx]["password"] = _encrypt_password(message.text.strip())
        save_accs(accs)
        bot.send_message(message.chat.id, "✅ Пароль обновлён (зашифрован).")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_IMAP}:"))
    def act_imap(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
        if idx >= len(accs):
            bot.answer_callback_query(call.id, "Аккаунт не найден.")
            return
        auto = detect_imap_host(accs[idx]["email"])
        msg  = bot.send_message(call.message.chat.id,
            f"Текущий IMAP: {accs[idx].get('imap_host', auto)}\n"
            f"Авто: {auto}\nВведите новый хост или «авто»:")
        bot.register_next_step_handler(msg, lambda m: _save_imap(m, idx))

    def _save_imap(message: Message, idx: int):
        accs = accounts()
        if idx >= len(accs):
            bot.send_message(message.chat.id, "❌ Аккаунт не найден.")
            return
        val  = message.text.strip()
        accs[idx]["imap_host"] = (
            detect_imap_host(accs[idx]["email"]) if val.lower() == "авто" else val
        )
        save_accs(accs)
        bot.send_message(message.chat.id, f"✅ IMAP: {accs[idx]['imap_host']}")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_TEST}:"))
    def do_test(call: CallbackQuery):
        idx    = int(call.data.split(":")[1])
        accs   = accounts()
        if idx >= len(accs):
            bot.answer_callback_query(call.id, "Аккаунт не найден.", show_alert=True)
            return
        result = test_imap(accs[idx])
        # обновляем health
        h = health_state()
        h[accs[idx]["email"]] = {
            "last_check": time.time(),
            "ok":         result.startswith("✅"),
            "message":    result,
        }
        save_health(h)
        bot.answer_callback_query(call.id, result, show_alert=True)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_DEL_ASK}:"))
    def ask_del(call: CallbackQuery):
        idx = int(call.data.split(":")[1])
        kb  = K(keyboard=[
            [B("✅ Да, удалить", callback_data=f"{AC_DEL_OK}:{idx}"),
             B("❌ Отмена",      callback_data=f"{AC_LIST}:0")],
        ])
        bot.edit_message_text("Удалить аккаунт?", call.message.chat.id,
                              call.message.message_id, reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_DEL_OK}:"))
    def do_del(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
        if idx < len(accs):
            accs.pop(idx)
            save_accs(accs)
        bot.edit_message_text("🗑 Удалён.", call.message.chat.id,
                              call.message.message_id, reply_markup=kb_main())

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_LEN}:"))
    def act_len(call: CallbackQuery):
        idx = int(call.data.split(":")[1])
        msg = bot.send_message(call.message.chat.id, "Введите длину кода (0 = авто):")
        bot.register_next_step_handler(msg, lambda m: _save_int(m, idx, "code_len"))

    def _save_int(message: Message, idx: int, field: str):
        try:
            val = int(message.text.strip())
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите число.")
            return
        accs = accounts()
        if idx >= len(accs):
            bot.send_message(message.chat.id, "❌ Аккаунт не найден.")
            return
        accs[idx][field] = val
        save_accs(accs)
        bot.send_message(message.chat.id, f"✅ {field} = {val}")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_TYPE}:"))
    def act_type(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
        if idx >= len(accs):
            bot.answer_callback_query(call.id, "Аккаунт не найден.")
            return
        types = ["alnum", "digits", "alpha", "alnum-dash"]
        cur   = accs[idx].get("code_type", "alnum")
        nxt   = types[(types.index(cur) + 1) % len(types)] if cur in types else "alnum"
        accs[idx]["code_type"] = nxt
        save_accs(accs)
        bot.answer_callback_query(call.id, f"Тип кода: {nxt}")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_FROM}:"))
    def act_from(call: CallbackQuery):
        idx = int(call.data.split(":")[1])
        msg = bot.send_message(call.message.chat.id, "Фильтр «От» (email отправителя, или пусто):")
        bot.register_next_step_handler(msg, lambda m: _save_str(m, idx, "filter_from"))

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_SUBJ}:"))
    def act_subj(call: CallbackQuery):
        idx = int(call.data.split(":")[1])
        msg = bot.send_message(call.message.chat.id, "Фильтр по теме письма (или пусто):")
        bot.register_next_step_handler(msg, lambda m: _save_str(m, idx, "filter_subj"))

    def _save_str(message: Message, idx: int, field: str):
        accs = accounts()
        if idx >= len(accs):
            bot.send_message(message.chat.id, "❌ Аккаунт не найден.")
            return
        accs[idx][field] = message.text.strip()
        save_accs(accs)
        bot.send_message(message.chat.id, f"✅ {field} сохранён.")

    # ── Lots ──
    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_LOTS}:"))
    def open_lots(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
        if idx >= len(accs):
            bot.answer_callback_query(call.id, "Аккаунт не найден.")
            return
        acc  = accs[idx]
        lots = acc.get("lot_ids", [])
        rows = [[B(f"🗑 {l}", callback_data=f"{AC_LOT_DEL}:{idx}:{l}")] for l in lots]
        rows.append([B("➕ Добавить лот", callback_data=f"{AC_LOT_ADD}:{idx}")])
        rows.append([B("◀ Назад",        callback_data=f"{AC_EDIT}:{idx}")])
        bot.edit_message_text(f"Лоты для {acc['email']}:", call.message.chat.id,
                              call.message.message_id, reply_markup=K(keyboard=rows))

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_LOT_ADD}:"))
    def act_lot_add(call: CallbackQuery):
        idx = int(call.data.split(":")[1])
        msg = bot.send_message(call.message.chat.id, "Введите ID лота:")
        bot.register_next_step_handler(msg, lambda m: _do_lot_add(m, idx))

    def _do_lot_add(message: Message, idx: int):
        accs = accounts()
        if idx >= len(accs):
            bot.send_message(message.chat.id, "❌ Аккаунт не найден.")
            return
        accs[idx].setdefault("lot_ids", []).append(message.text.strip())
        save_accs(accs)
        bot.send_message(message.chat.id, "✅ Лот добавлен.")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_LOT_DEL}:"))
    def act_lot_del(call: CallbackQuery):
        parts = call.data.split(":")
        idx, lot = int(parts[1]), parts[2]
        accs = accounts()
        if idx >= len(accs):
            bot.answer_callback_query(call.id, "Аккаунт не найден.")
            return
        accs[idx]["lot_ids"] = [l for l in accs[idx].get("lot_ids", []) if l != lot]
        save_accs(accs)
        bot.answer_callback_query(call.id, f"Лот {lot} удалён.")

    # ── Log ──
    @bot.callback_query_handler(func=lambda c: c.data == AC_LOG)
    def open_log(call: CallbackQuery):
        entries = log_entries()[-30:]
        if not entries:
            text = "Лог пуст."
        else:
            lines = [
                f"{_fmt_time(e['time'])} | {e['buyer']} | {e['code']}"
                for e in reversed(entries)
            ]
            text = "📜 Последние выдачи:\n\n" + "\n".join(lines)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
            reply_markup=K(keyboard=[[B("◀ Назад", callback_data=AC_MAIN)]]))

    # ── Rentals ──
    @bot.callback_query_handler(func=lambda c: c.data == AC_RENTALS)
    def open_rentals(call: CallbackQuery):
        active = get_active_rentals()
        if not active:
            text = "Нет активных аренд."
            kb   = K(keyboard=[[B("◀ Назад", callback_data=AC_MAIN)]])
        else:
            lines = [
                f"👤 {r['buyer']} | до {_fmt_time(r['expires_at'])} | {r['email']} | {r.get('lang','ru')}"
                + (" ⚠️" if not r.get("chat_id") else "")
                for r in active
            ]
            rows = []
            for r in active:
                rows.append([
                    B(f"🗑 {r['buyer']}", callback_data=f"{AC_RENT_DEL}:{r['order_key']}"),
                    B("➕1ч",            callback_data=f"{AC_RENT_EXT}:{r['order_key']}:1"),
                    B("➕24ч",           callback_data=f"{AC_RENT_EXT}:{r['order_key']}:24"),
                ])
            rows.append([B("◀ Назад", callback_data=AC_MAIN)])
            text = "🏠 Активные аренды:\n\n" + "\n".join(lines)
            text += "\n\n⚠️ = без chat_id (восстановлено, нет уведомлений)"
            kb   = K(keyboard=rows)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_RENT_DEL}:"))
    def do_rent_del(call: CallbackQuery):
        key   = call.data.split(":")[1]
        rents = rentals()
        rents.pop(key, None)
        save_rentals(rents)
        bot.answer_callback_query(call.id, "Аренда удалена.")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_RENT_EXT}:"))
    def do_rent_ext(call: CallbackQuery):
        parts = call.data.split(":")
        key, hours = parts[1], int(parts[2])
        rents = rentals()
        if key in rents:
            rents[key]["expires_at"] += hours * 3600
            rents[key]["hours"] = rents[key].get("hours", 0) + hours
            save_rentals(rents)
            # сброс warned
            state = warned_state()
            for s_key in ("warned_4h", "warned_30m"):
                if key in state.get(s_key, []):
                    state[s_key].remove(key)
            save_warned(state)
        bot.answer_callback_query(call.id, f"Продлено на {hours}ч.")

    # ── Stats ──
    @bot.callback_query_handler(func=lambda c: c.data == AC_STATS)
    def open_stats(call: CallbackQuery):
        bot.edit_message_text("📊 Выберите период:", call.message.chat.id,
                              call.message.message_id, reply_markup=kb_stats_period())

    @bot.callback_query_handler(func=lambda c: c.data in (
        AC_STATS_24H, AC_STATS_48H, AC_STATS_7D, AC_STATS_ALL
    ))
    def open_stats_period(call: CallbackQuery):
        mapping = {
            AC_STATS_24H: (24,   "24 часа"),
            AC_STATS_48H: (48,   "48 часов"),
            AC_STATS_7D:  (168,  "7 дней"),
            AC_STATS_ALL: (None, "Всё время"),
        }
        hours, label = mapping[call.data]
        entries = _filter_log_by_hours(hours)
        bot.edit_message_text(
            _stats_text(entries, label), call.message.chat.id, call.message.message_id,
            reply_markup=K(keyboard=[[B("◀ Назад", callback_data=AC_STATS)]]),
        )

    @bot.callback_query_handler(func=lambda c: c.data == AC_STATS_RANGE)
    def ask_stats_range(call: CallbackQuery):
        msg = bot.send_message(call.message.chat.id,
            "Введите период:\n<code>01.05.2026-31.05.2026</code>", parse_mode="HTML")
        bot.register_next_step_handler(msg, _handle_stats_range)

    def _handle_stats_range(message: Message):
        try:
            a, b = message.text.strip().split("-", 1)
            df = datetime.strptime(a.strip(), "%d.%m.%Y")
            dt = datetime.strptime(b.strip(), "%d.%m.%Y").replace(hour=23, minute=59, second=59)
            entries = _filter_log_by_range(df, dt)
            label   = f"{df.strftime('%d.%m.%Y')} — {dt.strftime('%d.%m.%Y')}"
            bot.send_message(message.chat.id, _stats_text(entries, label),
                reply_markup=K(keyboard=[[B("◀ Назад", callback_data=AC_STATS)]]))
        except Exception:
            bot.send_message(message.chat.id,
                "❌ Неверный формат. Пример: <code>01.05.2026-31.05.2026</code>",
                parse_mode="HTML")

    # ── Health check (вручную) ──
    @bot.callback_query_handler(func=lambda c: c.data == AC_HEALTH)
    def open_health(call: CallbackQuery):
        accs = accounts()
        health = health_state()
        if not accs:
            text = "Нет почтовых аккаунтов."
        else:
            lines = []
            for a in accs:
                h = health.get(a["email"], {})
                mark = "✅" if h.get("ok", True) else "❌"
                last = h.get("last_check")
                last_str = _fmt_time(last) if last else "—"
                msg = h.get("message", "не проверялась")
                lines.append(f"{mark} {a['email']}\n   {msg}\n   ⏱ {last_str}")
            text = "🩺 IMAP Health Check\n\n" + "\n\n".join(lines)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
            reply_markup=K(keyboard=[
                [B("🔄 Проверить сейчас", callback_data="ac_health_now")],
                [B("◀ Назад", callback_data=AC_MAIN)],
            ]))

    @bot.callback_query_handler(func=lambda c: c.data == "ac_health_now")
    def health_now(call: CallbackQuery):
        bot.answer_callback_query(call.id, "Проверяю...")
        accs = accounts()
        h = {}
        for a in accs:
            result = test_imap(a)
            h[a["email"]] = {
                "last_check": time.time(),
                "ok":         result.startswith("✅"),
                "message":    result,
            }
        save_health(h)
        open_health(call)

    # ── Review settings ──
    @bot.callback_query_handler(func=lambda c: c.data == AC_REVIEW)
    def open_review(call: CallbackQuery):
        s = app_settings()
        text = (
            f"⚙️ Настройки\n\n"
            f"📝 Запрос отзыва: {'✅ вкл' if s.get('review_enabled', True) else '❌ выкл'}\n"
            f"   Через 30 мин после выдачи кода.\n"
            f"   Шаблон: {s.get('review_template', '')[:80]}...\n\n"
            f"🔄 Win-back: {'✅ вкл' if s.get('winback_enabled', True) else '❌ выкл'}\n"
            f"   Через {WINBACK_AFTER_DAYS}+ дней простоя, скидка {s.get('winback_discount', 25)}%.\n"
            f"   Шаблон: {s.get('winback_template', '')[:80]}..."
        )
        kb = K(keyboard=[
            [B("📝 Вкл/выкл отзывы",  callback_data="ac_rev_toggle"),
             B("✏️ Шаблон отзыва",    callback_data="ac_rev_tpl")],
            [B("🔄 Вкл/выкл win-back", callback_data="ac_wb_toggle"),
             B("✏️ Шаблон win-back",   callback_data=AC_WB_TPL)],
            [B("◀ Назад", callback_data=AC_MAIN)],
        ])
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data == "ac_rev_toggle")
    def rev_toggle(call: CallbackQuery):
        s = app_settings()
        s["review_enabled"] = not s.get("review_enabled", True)
        save_settings(s)
        bot.answer_callback_query(call.id,
            f"Отзывы: {'вкл' if s['review_enabled'] else 'выкл'}")
        open_review(call)

    @bot.callback_query_handler(func=lambda c: c.data == "ac_rev_tpl")
    def rev_tpl(call: CallbackQuery):
        msg = bot.send_message(call.message.chat.id, "Введите новый шаблон запроса отзыва:")
        bot.register_next_step_handler(msg, _save_review_tpl)

    def _save_review_tpl(message: Message):
        s = app_settings()
        s["review_template"] = message.text.strip()
        save_settings(s)
        bot.send_message(message.chat.id, "✅ Шаблон сохранён.")

    @bot.callback_query_handler(func=lambda c: c.data == "ac_wb_toggle")
    def wb_toggle(call: CallbackQuery):
        s = app_settings()
        s["winback_enabled"] = not s.get("winback_enabled", True)
        save_settings(s)
        bot.answer_callback_query(call.id,
            f"Win-back: {'вкл' if s['winback_enabled'] else 'выкл'}")
        open_review(call)

    @bot.callback_query_handler(func=lambda c: c.data == AC_WB_TPL)
    def wb_tpl(call: CallbackQuery):
        msg = bot.send_message(call.message.chat.id,
            "Введите новый шаблон win-back сообщения:")
        bot.register_next_step_handler(msg, _save_wb_tpl)

    def _save_wb_tpl(message: Message):
        s = app_settings()
        s["winback_template"] = message.text.strip()
        save_settings(s)
        bot.send_message(message.chat.id, "✅ Шаблон win-back сохранён.")

    # ── Winback manual ──
    @bot.callback_query_handler(func=lambda c: c.data == AC_WINBACK)
    def open_winback(call: CallbackQuery):
        sent = winback_sent()
        entries = log_entries()
        now = time.time()
        last_by_buyer = {}
        for e in entries:
            b = e.get("buyer")
            if not b:
                continue
            ts = e.get("time", 0)
            if b not in last_by_buyer or ts > last_by_buyer[b]:
                last_by_buyer[b] = ts
        active_buyers = {r.get("buyer") for r in get_active_rentals()}
        candidates = []
        for b, ts in last_by_buyer.items():
            if b in active_buyers:
                continue
            d = (now - ts) / 86400
            if WINBACK_AFTER_DAYS <= d <= WINBACK_MAX_DAYS and b not in sent:
                candidates.append((b, d))

        text = (
            f"🔄 Win-back\n\n"
            f"Кандидатов (неактивны 3-30 дней): <b>{len(candidates)}</b>\n"
            f"Уже отправлено: <b>{len(sent)}</b>\n\n"
        )
        if candidates:
            text += "Топ-10 кандидатов:\n"
            for b, d in sorted(candidates, key=lambda x: x[1])[:10]:
                text += f"  • {b} — {int(d)} дн.\n"

        kb = K(keyboard=[
            [B(f"📤 Отправить всем ({len(candidates)})", callback_data=AC_WB_RUN)],
            [B("✏️ Шаблон", callback_data=AC_WB_TPL)],
            [B("◀ Назад", callback_data=AC_MAIN)],
        ])
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=kb, parse_mode="HTML")

    @bot.callback_query_handler(func=lambda c: c.data == AC_WB_RUN)
    def wb_run(call: CallbackQuery):
        bot.answer_callback_query(call.id, "Запущено...")
        Thread(target=_winback_manual_run, args=(cardinal,), daemon=True).start()

    # ── Broadcast (без изменений по сути, но фильтр chat_id) ──
    @bot.callback_query_handler(func=lambda c: c.data == AC_BROADCAST)
    def open_broadcast(call: CallbackQuery):
        active = get_active_rentals()
        with_chat = sum(1 for r in active if r.get("chat_id"))
        text = (f"📢 Рассылка\n"
                f"Активных аренд: <b>{len(active)}</b>\n"
                f"Получат рассылку (с chat_id): <b>{with_chat}</b>")
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=kb_broadcast_menu(), parse_mode="HTML")

    @bot.callback_query_handler(func=lambda c: c.data == AC_BCAST_SEND)
    def bcast_new(call: CallbackQuery):
        msg = bot.send_message(call.message.chat.id, "Введите текст рассылки:")
        bot.register_next_step_handler(msg, _handle_bcast_text)

    def _handle_bcast_text(message: Message):
        _bcast["text"] = message.text.strip()
        active = [r for r in get_active_rentals() if r.get("chat_id")]
        kb = K(keyboard=[
            [B(f"✅ Отправить ({len(active)} чатов)", callback_data="ac_bcast_confirm")],
            [B("💾 Сохранить как шаблон", callback_data=AC_BCAST_TPL_ADD)],
            [B("❌ Отмена", callback_data=AC_BROADCAST)],
        ])
        bot.send_message(message.chat.id,
            f"Предпросмотр:\n\n{_bcast['text']}", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data == "ac_bcast_confirm")
    def bcast_confirm(call: CallbackQuery):
        targets = [r for r in get_active_rentals() if r.get("chat_id")]
        _do_broadcast(call, targets)

    @bot.callback_query_handler(func=lambda c: c.data == AC_BCAST_RETRY)
    def bcast_retry(call: CallbackQuery):
        _do_broadcast(call, _bcast.get("failed", []), is_retry=True)

    def _do_broadcast(call, targets, is_retry=False):
        text = _bcast.get("text", "")
        if not text:
            bot.answer_callback_query(call.id, "Текст не задан.")
            return
        if not targets:
            bot.answer_callback_query(call.id, "Нет получателей.")
            return
        bot.answer_callback_query(call.id, "Рассылка запущена...")

        pairs = targets if is_retry else [
            (r["chat_id"], r.get("chat_name", "")) for r in targets
        ]

        def _send_all():
            ok, fail = 0, []
            for chat_id, chat_name in pairs:
                try:
                    cardinal.send_message(chat_id, text, chat_name)
                    ok += 1
                except Exception as ex:
                    logger.warning(f"Broadcast fail {chat_id}: {ex}")
                    fail.append((chat_id, chat_name))
                time.sleep(2)

            _bcast["failed"] = fail

            hist = bcast_history()
            hist.append({
                "time":   time.time(),
                "text":   text,
                "sent":   ok,
                "failed": len(fail),
            })
            save_bcast_history(hist[-50:])

            kb_rows = []
            if fail:
                kb_rows.append([B(f"🔄 Повторить ({len(fail)})", callback_data=AC_BCAST_RETRY)])
            kb_rows.append([B("◀ Меню рассылки", callback_data=AC_BROADCAST)])
            bot.send_message(call.message.chat.id,
                f"📢 Рассылка завершена\n\n✅ Отправлено: {ok}\n❌ Не доставлено: {len(fail)}",
                reply_markup=K(keyboard=kb_rows))

        Thread(target=_send_all, daemon=True).start()

    # ── Templates ──
    @bot.callback_query_handler(func=lambda c: c.data == AC_BCAST_TPLS)
    def open_templates(call: CallbackQuery):
        tpls = templates()
        if not tpls:
            bot.edit_message_text("Шаблонов нет.", call.message.chat.id,
                call.message.message_id,
                reply_markup=K(keyboard=[
                    [B("➕ Добавить", callback_data=AC_BCAST_TPL_ADD)],
                    [B("◀ Назад",    callback_data=AC_BROADCAST)],
                ]))
            return
        bot.edit_message_text("📁 Шаблоны:", call.message.chat.id,
                              call.message.message_id, reply_markup=kb_templates(tpls))

    @bot.callback_query_handler(func=lambda c: c.data == AC_BCAST_TPL_ADD)
    def add_template(call: CallbackQuery):
        msg = bot.send_message(call.message.chat.id, "Введите название шаблона:")
        bot.register_next_step_handler(msg, _step_tpl_name)

    def _step_tpl_name(message: Message):
        name = message.text.strip()
        msg  = bot.send_message(message.chat.id, "Введите текст шаблона:")
        bot.register_next_step_handler(msg, lambda m: _step_tpl_text(m, name))

    def _step_tpl_text(message: Message, name: str):
        tpls = templates()
        tpls.append({"name": name, "text": message.text.strip()})
        save_templates(tpls)
        bot.send_message(message.chat.id, f"✅ Шаблон «{name}» сохранён.")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_BCAST_TPL_USE}:"))
    def use_template(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        tpls = templates()
        if idx >= len(tpls):
            bot.answer_callback_query(call.id, "Шаблон не найден.")
            return
        _bcast["text"] = tpls[idx]["text"]
        active = [r for r in get_active_rentals() if r.get("chat_id")]
        kb = K(keyboard=[
            [B(f"✅ Отправить ({len(active)} чатов)", callback_data="ac_bcast_confirm")],
            [B("❌ Отмена", callback_data=AC_BROADCAST)],
        ])
        bot.send_message(call.message.chat.id,
            f"Предпросмотр:\n\n{_bcast['text']}", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_BCAST_TPL_DEL}:"))
    def del_template(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        tpls = templates()
        if idx < len(tpls):
            tpls.pop(idx)
            save_templates(tpls)
        bot.answer_callback_query(call.id, "Шаблон удалён.")

    # ── Broadcast history ──
    @bot.callback_query_handler(func=lambda c: c.data == AC_BCAST_HIST)
    def open_bcast_hist(call: CallbackQuery):
        hist = bcast_history()[-20:]
        if not hist:
            text = "История рассылок пуста."
        else:
            lines = [
                f"{_fmt_time(h['time'])} | ✅{h['sent']} ❌{h['failed']}\n"
                f"  {h['text'][:60]}{'...' if len(h['text']) > 60 else ''}"
                for h in reversed(hist)
            ]
            text = "📋 История рассылок:\n\n" + "\n\n".join(lines)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
            reply_markup=K(keyboard=[[B("◀ Назад", callback_data=AC_BROADCAST)]]))

    # ── Scheduled broadcast ──
    @bot.callback_query_handler(func=lambda c: c.data == AC_BCAST_SCHED)
    def open_sched(call: CallbackQuery):
        msg = bot.send_message(call.message.chat.id, "Введите текст рассылки:")
        bot.register_next_step_handler(msg, _sched_step_text)

    def _sched_step_text(message: Message):
        _bcast["text"] = message.text.strip()
        msg = bot.send_message(message.chat.id,
            "Введите время отправки:\n<code>31.05.2026 18:00</code>", parse_mode="HTML")
        bot.register_next_step_handler(msg, _sched_step_time)

    def _sched_step_time(message: Message):
        try:
            dt    = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
            delay = (dt - datetime.now()).total_seconds()
            if delay <= 0:
                bot.send_message(message.chat.id, "❌ Время уже прошло.")
                return

            if _bcast.get("scheduled_timer"):
                _bcast["scheduled_timer"].cancel()

            def _fire():
                targets = [r for r in get_active_rentals() if r.get("chat_id")]
                ok, fail = 0, []
                for r in targets:
                    try:
                        cardinal.send_message(r["chat_id"], _bcast["text"], r.get("chat_name", ""))
                        ok += 1
                    except Exception:
                        fail.append((r["chat_id"], r.get("chat_name", "")))
                    time.sleep(2)
                hist = bcast_history()
                hist.append({"time": time.time(), "text": _bcast["text"],
                             "sent": ok, "failed": len(fail)})
                save_bcast_history(hist[-50:])
                try:
                    for uid in cardinal.telegram.authorized_users:
                        bot.send_message(uid,
                            f"📢 Отложенная рассылка выполнена\n✅ {ok} | ❌ {len(fail)}")
                except Exception:
                    pass

            t_timer = Timer(delay, _fire)
            t_timer.daemon = True
            t_timer.start()
            _bcast["scheduled_timer"] = t_timer

            bot.send_message(message.chat.id,
                f"✅ Рассылка запланирована на {dt.strftime('%d.%m.%Y %H:%M')}\n"
                f"Текст: {_bcast['text'][:80]}")
        except ValueError:
            bot.send_message(message.chat.id,
                "❌ Неверный формат. Пример: <code>31.05.2026 18:00</code>", parse_mode="HTML")

    # ── Background workers ──
    Thread(target=_expiry_watcher,           args=(cardinal,), daemon=True).start()
    Thread(target=_startup_imap_test,        args=(cardinal,), daemon=True).start()
    Thread(target=_startup_sales_scan,       args=(cardinal,), daemon=True).start()
    Thread(target=_used_code_cleanup_worker,                   daemon=True).start()
    Thread(target=_weekly_report_worker,     args=(cardinal,), daemon=True).start()
    Thread(target=_imap_health_worker,       args=(cardinal,), daemon=True).start()
    Thread(target=_winback_worker,           args=(cardinal,), daemon=True).start()
    Thread(target=_backup_worker,                              daemon=True).start()
    logger.info(f"AutoCode v{VERSION} инициализирован.")


def _winback_manual_run(cardinal):
    """Manual win-back triggered from TG button."""
    try:
        settings = app_settings()
        entries = log_entries()
        now = time.time()
        last_by_buyer = {}
        for e in entries:
            b = e.get("buyer")
            if not b:
                continue
            ts = e.get("time", 0)
            cid = e.get("chat_id")
            if b not in last_by_buyer or ts > last_by_buyer[b][0]:
                last_by_buyer[b] = (ts, cid)

        active_buyers = {r.get("buyer") for r in get_active_rentals()}
        sent_log = winback_sent()
        new_sends = 0

        for buyer, (last_ts, cid) in last_by_buyer.items():
            if buyer in active_buyers:
                continue
            days_ago = (now - last_ts) / 86400
            if days_ago < WINBACK_AFTER_DAYS or days_ago > WINBACK_MAX_DAYS:
                continue
            if buyer in sent_log:
                continue
            if not cid:
                for r in rentals().values():
                    if r.get("buyer") == buyer and r.get("chat_id"):
                        cid = r["chat_id"]
                        break
            if not cid:
                continue

            text = settings.get("winback_template", "")
            try:
                cardinal.send_message(cid, text, buyer)
                sent_log[buyer] = {
                    "sent_at": now,
                    "chat_id": cid,
                    "last_rental_ts": last_ts,
                }
                new_sends += 1
                time.sleep(3)
            except Exception as ex:
                logger.warning(f"Manual winback fail {buyer}: {ex}")

        if new_sends:
            save_winback(sent_log)
        _notify_tg(cardinal,
            f"🔄 Win-back завершён. Отправлено: {new_sends}.")
    except Exception as e:
        logger.error(f"Manual winback error: {e}")


# ── Plugin hooks ─────────────────────────────────────────────────────────────
BIND_TO_PRE_INIT    = [init_autocode_tg]
BIND_TO_NEW_ORDER   = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
