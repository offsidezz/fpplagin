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
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import threading
from threading import Thread, Timer, Lock

from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent, OrderStatusChangedEvent
from FunPayAPI.common.enums import MessageTypes
from tg_bot import CBT
from telebot.types import (
    InlineKeyboardMarkup as K,
    InlineKeyboardButton as B,
    Message,
    CallbackQuery,
)

NAME = "AutoCode"
VERSION = "5.7.0"
UUID = str(uuid_lib.UUID("b7e21f3a-4c8d-4e2b-9a1f-3c5d6e7f8b9a"))
DESCRIPTION = (
    "Авто-выдача кодов с IMAP-почт по команде !cd / code.\n"
    "v5.5.0: только RU, все тексты редактируемы в TG, команды для всех, подтверждение заказа, фикс IMAP.\n"
    "Управление: /autocode"
)
CREDITS = "@offsidezq"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

logger = logging.getLogger("FPC.AutoCode")

# ── Callback constants ──
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
AC_RENT_PAGE     = "ac_rent_p"
AC_RENT_INFO     = "ac_rent_i"
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
AC_CHECK_ALL     = "ac_check_all"

CODE_TYPE_RU = {
    "alnum":      "буквы + цифры",
    "digits":     "только цифры",
    "alpha":      "только буквы",
    "alnum-dash": "буквы + цифры + дефис",
}

# ── Storage paths ──
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
SETTINGS_FILE   = os.path.join(DATA_DIR, "settings.json")
LANG_CACHE_FILE = os.path.join(DATA_DIR, "lang_cache.json")

ENCRYPTED_FILES = {RENTALS_FILE, USED_FILE, ACCOUNTS_FILE}

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

IMAP_TIMEOUT          = 15
CODE_CD               = 30
MAX_CODES_HOUR        = 10
WARN_BEFORE_H         = 12
PRE_WINDOW            = 2 * 3600
USED_CODE_TTL_SEC     = 3 * 3600
WEEKLY_REPORT_DOW     = 6
WEEKLY_REPORT_HOUR    = 9
RENTAL_GRACE_SEC      = 300
LOYALTY_WINDOW_DAYS   = 7          # repurchase within N days → bonus
LOYALTY_BONUS_DAYS    = 7          # bonus days for loyal buyers
HEALTH_CHECK_INTERVAL = 3600
REVIEW_DELAY_SEC      = 30 * 60
ORDER_CONFIRM_DELAY   = 5 * 60   # 5 min after code → ask buyer to confirm order
RENTALS_PAGE_SIZE     = 8
BROADCAST_COOLDOWN_SEC = 3
BROADCAST_RETRY_MAX    = 3

COMMAND_MESSAGES = {
    "!cd", "cd", "code", "!code", "код", "!код",
    "!time", "time", "!время", "время",
    "!help", "help", "помощь", "!помощь",
    "!продлить", "!extend", "продлить",
    "!faq", "faq", "!команды", "команды",
}

# ── Problem detection keywords (buyer messages → seller TG notification) ──
_PROBLEM_KEYWORDS = (
    "не работает", "не могу войти", "не заходит", "не получается",
    "помогите", "проблема", "ошибка", "не подходит", "не тот",
    "верните деньги", "возврат", "обман", "не приходит",
    "doesn't work", "can't login", "can't log in", "not working",
    "help me", "problem", "error", "wrong", "refund",
)
_PROBLEM_COOLDOWN: dict[str, float] = {}  # buyer -> last_notify_ts
_PROBLEM_COOLDOWN_SEC = 600  # don't spam seller: 1 notification per buyer per 10 min

_ENC_MARKER = "ACENC1:"
_FERNET_MARKER = "ACFERNET:"

# ── Crypto: Fernet (preferred) with XOR fallback for migration ──
try:
    from cryptography.fernet import Fernet as _Fernet, InvalidToken as _InvalidToken
    _FERNET_KEY = os.environ.get("AC_FERNET_KEY", "")
    if not _FERNET_KEY:
        _key_file = os.path.join("storage", "autocode", ".fernet_key")
        if os.path.exists(_key_file):
            with open(_key_file, "r") as _f:
                _FERNET_KEY = _f.read().strip()
        else:
            _FERNET_KEY = _Fernet.generate_key().decode()
            os.makedirs(os.path.dirname(_key_file), exist_ok=True)
            with open(_key_file, "w") as _f:
                _f.write(_FERNET_KEY)
            logger.info("AutoCode: сгенерирован новый Fernet-ключ в .fernet_key")
    _cipher = _Fernet(_FERNET_KEY.encode() if isinstance(_FERNET_KEY, str) else _FERNET_KEY)
    _HAS_FERNET = True
except ImportError:
    _HAS_FERNET = False
    logger.warning("AutoCode: cryptography не установлена, используется XOR (небезопасно!). pip install cryptography")

# XOR fallback for reading old data
_SECRET_KEY = (os.environ.get("AC_SECRET") or "ac_fp_secret_2025").encode()

def _xor_bytes(data: bytes) -> bytes:
    key = _SECRET_KEY
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

def _encrypt_password(plain: str) -> str:
    if _HAS_FERNET:
        return _FERNET_MARKER + _cipher.encrypt(plain.encode("utf-8")).decode()
    return base64.b64encode(_xor_bytes(plain.encode("utf-8"))).decode()

def _decrypt_password(enc: str) -> str:
    if enc.startswith(_FERNET_MARKER) and _HAS_FERNET:
        try:
            return _cipher.decrypt(enc[len(_FERNET_MARKER):].encode()).decode("utf-8")
        except Exception:
            return enc
    # XOR fallback for old passwords
    try:
        raw = base64.b64decode(enc.encode())
        return _xor_bytes(raw).decode("utf-8")
    except Exception:
        return enc

def _encrypt_file_content(plain_json: str) -> str:
    if _HAS_FERNET:
        return _FERNET_MARKER + _cipher.encrypt(plain_json.encode("utf-8")).decode()
    encoded = base64.b64encode(_xor_bytes(plain_json.encode("utf-8"))).decode()
    return _ENC_MARKER + encoded

def _decrypt_file_content(raw: str) -> str:
    if raw.startswith(_FERNET_MARKER) and _HAS_FERNET:
        try:
            return _cipher.decrypt(raw[len(_FERNET_MARKER):].encode()).decode("utf-8")
        except Exception as e:
            logger.error(f"AutoCode: ошибка Fernet-расшифровки: {e}")
            return raw
    if raw.startswith(_ENC_MARKER):
        try:
            encoded = raw[len(_ENC_MARKER):]
            return _xor_bytes(base64.b64decode(encoded.encode())).decode("utf-8")
        except Exception as e:
            logger.error(f"AutoCode: ошибка XOR-расшифровки: {e}")
            return raw
    return raw

_cardinal_ref = None
_shutdown_event = threading.Event()
# Legacy compat wrapper
_shutdown_flag = type("_ShutdownProxy", (), {
    "__getitem__": lambda self, k: _shutdown_event.is_set(),
    "__setitem__": lambda self, k, v: _shutdown_event.set() if v else None,
    "get": lambda self, k, d=None: _shutdown_event.is_set(),
})()

def _ensure():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)

# ── Data-loss protection ──
# Tracks files that failed to decrypt — prevents overwriting encrypted data
# with an empty default.
_decrypt_failed: set[str] = set()

def _load(path, default):
    _ensure()
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        if path in ENCRYPTED_FILES:
            decrypted = _decrypt_file_content(raw)
            # If decryption returned the raw blob unchanged, it failed
            if decrypted == raw and raw.startswith((_FERNET_MARKER, _ENC_MARKER)):
                _decrypt_failed.add(path)
                logger.critical(
                    f"AutoCode: ❌ РАСШИФРОВКА ПРОВАЛЕНА для {path}! "
                    f"Данные на диске защищены, запись заблокирована. "
                    f"Проверьте .fernet_key!"
                )
                return default
            raw = decrypted
        result = json.loads(raw) if raw.strip() else default
        _decrypt_failed.discard(path)
        return result
    except Exception as e:
        logger.warning(f"AutoCode: не удалось прочитать {path}: {e}")
        return default

def _save(path, data):
    _ensure()
    # ── Guard: block saves if decryption previously failed ──
    if path in _decrypt_failed:
        logger.critical(
            f"AutoCode: ❌ ЗАПИСЬ В {path} ЗАБЛОКИРОВАНА — расшифровка была "
            f"неудачной, данные на диске сохранены. Восстановите .fernet_key "
            f"и перезагрузите плагин."
        )
        return
    try:
        # ── Guard: pre-save backup if data shrinks significantly ──
        if path in ENCRYPTED_FILES and os.path.exists(path):
            try:
                file_size = os.path.getsize(path)
                new_json = json.dumps(data, ensure_ascii=False)
                # If the file on disk is 2x+ bigger than what we're about to
                # write, make an emergency backup before overwriting.
                if file_size > 500 and len(new_json) < file_size // 2:
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    name = os.path.basename(path).replace(
                        ".json", f"_emergency_{stamp}.json"
                    )
                    import shutil
                    shutil.copy2(path, os.path.join(BACKUP_DIR, name))
                    logger.warning(
                        f"AutoCode: ⚠️ Экстренный бекап {path} → {name} "
                        f"(было {file_size}B, пишем {len(new_json)}B)"
                    )
            except Exception as be:
                logger.warning(f"AutoCode: ошибка экстренного бекапа: {be}")

        json_str = json.dumps(data, ensure_ascii=False, indent=2)
        if path in ENCRYPTED_FILES:
            json_str = _encrypt_file_content(json_str)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json_str)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"AutoCode: не удалось сохранить {path}: {e}")

def _cid_norm(value) -> str | None:
    """Normalize a chat_id to a comparable string.

    chat_id is stored inconsistently across the codebase: restored rentals
    keep it as a str ("12345" / "users-X-Y"), order events as str-or-None, and
    `e.message.chat_id` arrives as an int. Comparing str == int silently fails,
    so an active (restored) rental would look like "no active rental". Always
    compare via this normalizer.
    """
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _name_norm(value) -> str:
    """Normalize a buyer/author name for tolerant comparison."""
    return (value or "").strip().casefold()


def _bid_norm(value) -> str | None:
    """Normalize a buyer/author *user-id* to a comparable string.

    The order/restore API exposes the buyer's user-id (a small number, ~1-20M)
    which is stable across display-name changes. We match on it as a robust
    alternative to the buyer name. Note: this is NOT the chat_id (~264M).
    """
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _buyer_id_from_chat(raw, seller_id=None) -> str | None:
    """Extract the *buyer's* numeric user-id from a chat identifier.

    FunPay exposes chat identifiers as a composite string
    ``users-{A}-{B}`` where one of A/B is the seller's user-id and the other
    is the buyer's. The seller can be in *either* position
    (e.g. ``users-7028500-16710870`` and ``users-3223981-7028500``), so we
    pick the segment that is NOT the seller. The buyer user-id equals the
    author_id of the buyer's incoming messages, which makes it a robust
    matching key. A plain numeric id is returned as-is.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = re.match(r"^users-(\d+)-(\d+)$", s)
    if m:
        a, b = m.group(1), m.group(2)
        sid = _bid_norm(seller_id)
        if sid and a == sid:
            return b
        if sid and b == sid:
            return a
        return b  # fallback: trailing segment is usually the buyer
    return s  # already a plain user-id


def _stack_b2(orders):
    """Stack a buyer's rental orders into one window (model B2).

    B2 = sequential extension: orders are applied in chronological order and
    each one extends the window from the LATER of (current expiry, its own
    purchase moment). Nothing is ever lost — if a window expires before the
    next purchase, that purchase simply restarts the clock from its own moment;
    if purchases overlap, their durations add up. Returns
    ``(earliest_purchase_ts, total_hours, expires_at, order_ids)``.
    """
    orders = sorted(orders, key=lambda o: _safe_ts(o.get("purchase_ts", 0)))
    exp         = 0.0
    total_hours = 0
    order_ids   = []
    for o in orders:
        pts   = _safe_ts(o.get("purchase_ts", 0))
        hours = int(o.get("hours", 0) or 0)
        exp = max(exp, pts) + hours * 3600
        total_hours += hours
        oid = o.get("order_id")
        if oid:
            order_ids.append(str(oid))
    earliest = _safe_ts(orders[0].get("purchase_ts", 0)) if orders else 0.0
    return earliest, total_hours, exp, order_ids


def _parse_review_bonus(text: str) -> int:
    """Hours auto-added when the buyer leaves a review, parsed from a lot
    title like '... 720ч • +12ч ЗА ОТЗЫВ'. Returns 0 if not present."""
    if not text:
        return 0
    s = text.lower()
    m = re.search(r"[+➕]\s*(\d+)\s*ч[а-я.]*\s*[•|/\-–]?\s*за\s+отзыв", s)
    if m:
        return int(m.group(1))
    if "отзыв" in s:                       # looser: any +Nч near 'отзыв'
        m = re.search(r"[+➕]\s*(\d+)\s*ч", s)
        if m:
            return int(m.group(1))
    return 0


def _apply_refund_to_rental(r: dict, order_id, hours: int = 0) -> bool:
    """Subtract a refunded order's contribution from a rental (idempotent via
    ``refunded_ids``). Returns True if the rental still has remaining orders,
    False if every order was refunded (caller should drop it)."""
    oid  = str(order_id)
    done = {str(x) for x in r.get("refunded_ids", [])}
    if oid in done:
        return bool(r.get("order_ids") or r.get("orders"))
    h = int(hours or 0)
    for od in r.get("orders", []):
        if str(od.get("order_id")) == oid:
            h = int(od.get("hours", h) or h)
            break
    if h:
        r["expires_at"] = _safe_ts(r.get("expires_at", 0)) - h * 3600
        r["hours"]      = max(0, int(r.get("hours", 0) or 0) - h)
    r["orders"]    = [od for od in r.get("orders", []) if str(od.get("order_id")) != oid]
    r["order_ids"] = [x for x in r.get("order_ids", []) if str(x) != oid]
    done.add(oid)
    r["refunded_ids"] = sorted(done)
    return bool(r.get("order_ids") or r.get("orders"))


def _order_review_exists(cardinal, order_id) -> bool:
    """True if the FunPay order already carries a buyer review. Defensive: any
    failure (API error / older Cardinal) returns False so the review request
    still goes out rather than being silently swallowed."""
    if not order_id:
        return False
    try:
        acc = getattr(cardinal, "account", None)
        if acc is None or not hasattr(acc, "get_order"):
            return False
        order  = acc.get_order(str(order_id))
        return getattr(order, "review", None) is not None
    except Exception as e:
        logger.warning(f"AutoCode: get_order({order_id}) review-check failed: {e}")
        return False


def _safe_ts(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return 0.0
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0.0
        try:
            return float(s)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                return datetime.strptime(s, fmt).timestamp()
            except Exception:
                continue
        try:
            return parsedate_to_datetime(s).timestamp()
        except Exception:
            return 0.0
    return 0.0

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
def warned_state():        return _load(WARNED_FILE, {"warned_12h": []})
def save_warned(d):        _save(WARNED_FILE, d)
def health_state():        return _load(HEALTH_FILE, {})
def save_health(d):        _save(HEALTH_FILE, d)
def lang_cache():          return _load(LANG_CACHE_FILE, {})
def save_lang_cache(d):    _save(LANG_CACHE_FILE, d)
def app_settings():        return _load(SETTINGS_FILE, {
    "review_enabled":   True,
    "review_template":  "Если код подошёл — буду благодарен за отзыв 🙏 Это очень помогает!",
    "confirm_enabled":  True,
    "queue_pause_sec":  2,
    "faq_custom_ru":      "",
    "texts":              {},
})
def save_settings(d):      _save(SETTINGS_FILE, d)


# ── Bonus hours for review ──
_lang_cache_mem: dict[str, str] = {}
_lang_cache_lock = Lock()

def _load_lang_cache():
    global _lang_cache_mem
    _lang_cache_mem = lang_cache() or {}

def _set_buyer_lang(buyer: str, lang: str):
    if not buyer or lang not in ("ru", "en"):
        return
    with _lang_cache_lock:
        if _lang_cache_mem.get(buyer) == lang:
            return
        _lang_cache_mem[buyer] = lang
        try:
            save_lang_cache(dict(_lang_cache_mem))
        except Exception:
            pass

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
    return datetime.fromtimestamp(_safe_ts(ts)).strftime("%d.%m.%Y %H:%M")

def _fmt_time_short(ts):
    return datetime.fromtimestamp(_safe_ts(ts)).strftime("%d.%m %H:%M")

def _fmt_remaining(ts):
    left = max(0, int(_safe_ts(ts) - time.time()))
    h, m = divmod(left // 60, 60)
    return f"{h}ч {m:02d}м"

def _fmt_remaining_compact(ts):
    left = max(0, int(_safe_ts(ts) - time.time()))
    h, m = divmod(left // 60, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}д{h:02d}ч"
    return f"{h}ч{m:02d}м"

def detect_imap_host(email_addr: str) -> str:
    domain = email_addr.split("@")[-1].lower()
    return IMAP_HOSTS.get(domain, f"imap.{domain}")

_RU_RE = re.compile(r"[а-яА-ЯёЁ]")
_EN_RE = re.compile(r"[a-zA-Z]")

def detect_lang(text: str) -> str | None:
    if not text:
        return None
    ru_chars = len(_RU_RE.findall(text))
    en_chars = len(_EN_RE.findall(text))
    total = ru_chars + en_chars
    if total < 2:
        return None
    if ru_chars >= en_chars:
        return "ru"
    return "en"

_INVISIBLE_RE = re.compile(r'[\u200b\u200c\u200d\u200e\u200f\u00a0\ufeff\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068\u2069\u202a\u202b\u202c\u202d\u202e]')

def _strip_invisible(text: str) -> str:
    """Remove zero-width and invisible Unicode characters."""
    return _INVISIBLE_RE.sub('', text).strip()

def _is_command_message(text: str) -> bool:
    if not text:
        return False
    return _strip_invisible(text).lower() in COMMAND_MESSAGES

def get_buyer_lang(buyer: str, current_text: str = "") -> str:
    with _lang_cache_lock:
        cached = _lang_cache_mem.get(buyer)
    if cached in ("ru", "en"):
        return cached

    if not _is_command_message(current_text):
        lang = detect_lang(current_text)
        if lang:
            return lang

    rents = rentals()
    candidates = [
        (r.get("purchase_ts", 0), r.get("lang"))
        for r in rents.values()
        if r.get("buyer") == buyer and r.get("lang") in ("ru", "en")
    ]
    if candidates:
        candidates.sort(key=lambda x: _safe_ts(x[0]), reverse=True)
        return candidates[0][1]

    return "ru"

L_DEFAULTS = {
    "loyalty_bonus":    "🎁 Бонус за повторную покупку: +{days} дн!",
    "rental_activated":   "🌸 | Аренда активирована, команды для входа можешь узнать по команде !faq",
    "rental_renewed":     "✅ Аренда продлена на {h}ч!\nНовое время окончания: {t}",
    "code_msg":           "🔑 Ваш код: {code}\n📅 Получен: {dt}",
    "no_rental":          "❌ У вас нет активной аренды. Пожалуйста, оформите заказ.",
    "no_active":          "❌ У вас нет активной аренды.",
    "remaining":          "⏳ Осталось: {rem}\n📅 Окончание: {end}",
    "code_not_found":     "❌ Код не найден. Попробуйте через минуту или обратитесь к продавцу.",
    "rate_wait":          "⏳ Подождите {s} сек. перед повторным запросом.",
    "rate_hour_limit":    "❌ Превышен лимит ({n} запросов/час). Обратитесь к продавцу.",
    "mailbox_missing":    "⚠️ Почтовый аккаунт не настроен. Обратитесь к продавцу.",
    "warn_12h":           "⏰ До окончания аренды осталось {h} часов. Хотите продлить? Просто сделайте новый заказ 😊",
    "rental_ended":       "✅ Ваша аренда завершена, спасибо за покупку! Хотите продлить? Просто сделайте новый заказ 😊",
    "queue_position":     "⏳ Ваш запрос в очереди (позиция {pos}). Подождите немного...",
    "faq":                "📋 Доступные команды:\n\n"
                          "• !cd / code / код — получить код\n"
                          "• !time / время — сколько осталось аренды\n"
                          "• !продлить — ссылка для продления\n"
                          "• !faq / команды — эта справка\n\n"
                          "{custom}",
    "extend_link":        "🔄 Для продления оформите новый заказ по ссылке:\nhttps://funpay.com/lots/offer?id={lot_id}",
    "extend_no_rental":   "❌ У вас нет активной аренды для продления.",
    "order_confirm":      "✅ Если всё работает — пожалуйста, оставьте отзыв 🙏\nhttps://funpay.com/orders/{order_id}/",
    "review_bonus_added": "🎁 Спасибо за отзыв! Аренда продлена на +{h}ч.\n📅 Новое окончание: {t}",
}

# Editable text keys shown in TG settings (display_name → L_DEFAULTS key)
_EDITABLE_TEXTS = {
    "Активация аренды":      "rental_activated",
    "Продление аренды":      "rental_renewed",
    "Выдача кода":           "code_msg",
    "Нет аренды":            "no_rental",
    "Нет активной аренды":   "no_active",
    "Остаток времени":       "remaining",
    "Код не найден":         "code_not_found",
    "Подождите (КД)":        "rate_wait",
    "Лимит запросов":        "rate_hour_limit",
    "Почта не настроена":    "mailbox_missing",
    "Предупр. 12ч":          "warn_12h",
    "Аренда завершена":      "rental_ended",
    "Очередь":               "queue_position",
    "FAQ":                   "faq",
    "Ссылка продления":      "extend_link",
    "Нет аренды (продл.)":   "extend_no_rental",
    "Запрос отзыва (заказ)":  "order_confirm",
    "Бонус за отзыв":         "review_bonus_added",
}

def t(key: str, **kwargs) -> str:
    """Get message text: first check settings overrides, then L_DEFAULTS."""
    s = app_settings()
    overrides = s.get("texts", {})
    text = overrides.get(key) or L_DEFAULTS.get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

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
                # FIX: get_payload(decode=True) can return None (e.g. empty
                # part / message/* subtype) → guard to avoid AttributeError,
                # which would skip the whole email and lose a valid code.
                payload = part.get_payload(decode=True)
                if payload:
                    plain += payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
            elif ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html += payload.decode(
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
        # STRICT: exact length match — if code_len=4 only match exactly 4 chars
        pat = re.compile(rf"(?<!\w){char_cls}{{{code_len}}}(?!\w)")
    else:
        # Auto: at least 4 chars, require digit for alnum types.
        # FIX: the lookahead must require the digit *inside the matched token*,
        # not merely somewhere later in the email. The old `(?=.*\d)` allowed a
        # pure-letter word (e.g. "verification") to match as long as any digit
        # appeared anywhere downstream.
        if code_type in ("alnum", "alnum-dash"):
            pat = re.compile(rf"(?={char_cls}*\d){char_cls}{{4,64}}")
        elif code_type == "digits":
            pat = re.compile(rf"\d{{4,64}}")
        else:
            pat = re.compile(rf"{char_cls}{{4,64}}")

    text = plain or _html_to_text(html)

    # Validate match — for strict code_len, ensure it's actually the right length
    def _validate(match_str: str) -> bool:
        clean = match_str.strip()
        if code_len and len(clean) != code_len:
            return False
        if code_type == "digits" and not clean.isdigit():
            return False
        return True

    # 1. Check each line as a standalone code
    for line in text.splitlines():
        line = line.strip()
        m = pat.fullmatch(line)
        if m and _validate(m.group()):
            return m.group()

    # 2. Search near code-related keywords
    keywords = ["код", "code", "ключ", "key", "enter", "активац", "verification", "pin"]
    for kw in keywords:
        idx = text.lower().find(kw)
        if idx != -1:
            snippet = text[max(0, idx - 20):idx + 120]
            m = pat.search(snippet)
            if m and _validate(m.group()):
                return m.group()

    # 3. Global search
    for m in pat.finditer(text):
        if _validate(m.group()):
            return m.group()
    return None

def fetch_code(acc, used, not_before_ts=None, dry_run=False) -> tuple[str | None, str | None]:
    email_addr  = acc.get("email", "")
    password    = _decrypt_password(acc.get("password", ""))
    imap_host   = acc.get("imap_host") or detect_imap_host(email_addr)
    max_age     = acc.get("max_age_min", 60)
    filter_from = acc.get("filter_from", "")
    filter_subj = acc.get("filter_subj", "")

    # FIX (double-delivery race): always read the *current* used-codes state
    # instead of relying on the snapshot captured before the IMAP queue ran.
    # Tasks for the same mailbox are serialized by IMAPQueue, so a fresh read
    # here is guaranteed to include codes marked used by the previous task.
    # The `used` argument is kept only for backward compatibility / dry_run.
    if not dry_run:
        used = used_codes()

    raw_used = used.get(email_addr, [])
    used_set = set()
    for entry in raw_used:
        if isinstance(entry, dict):
            used_set.add(entry.get("code", ""))
        else:
            used_set.add(entry)

    mail = None
    try:
        mail = imaplib.IMAP4_SSL(imap_host, timeout=IMAP_TIMEOUT)
        mail.login(email_addr, password)
        # FIX: Explicit error handling for INBOX select
        status, _ = mail.select("INBOX")
        if status != "OK":
            return None, f"INBOX недоступен: статус {status}"

        if filter_from:
            _, data = mail.search(None, "FROM", filter_from)
        else:
            _, data = mail.search(None, "ALL")

        msg_ids = data[0].split()
        if not msg_ids:
            return None, "Входящих писем нет."

        # FIX: Strict max_age_min window — only look at recent emails.
        # Previously widened by not_before_ts which made max_age meaningless.
        cutoff = time.time() - max_age * 60

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
                if code and (dry_run or code not in used_set):
                    return code, None
            except Exception:
                continue

        return None, "Подходящий код не найден в письмах."

    except imaplib.IMAP4.error as e:
        return None, f"IMAP ошибка: {e}"
    except OSError as e:
        return None, f"Сервер недоступен (таймаут {IMAP_TIMEOUT}с): {e}"
    except Exception as e:
        return None, f"Ошибка: {e}"
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass

def test_imap(acc) -> str:
    host     = acc.get("imap_host") or detect_imap_host(acc.get("email", ""))
    password = _decrypt_password(acc.get("password", ""))
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(host, timeout=IMAP_TIMEOUT)
        mail.login(acc["email"], password)
        mail.select("INBOX")
        return f"✅ Подключение успешно ({host})"
    except Exception as e:
        return f"❌ Ошибка: {e}"
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass

def full_check_account(acc) -> dict:
    result = {
        "email":      acc.get("email", "?"),
        "imap_ok":    False,
        "imap_msg":   "",
        "code_ok":    False,
        "code_msg":   "",
        "code_found": None,
    }
    imap_res = test_imap(acc)
    result["imap_ok"]  = imap_res.startswith("✅")
    result["imap_msg"] = imap_res
    if not result["imap_ok"]:
        result["code_msg"] = "Пропущено — нет IMAP."
        return result
    code, err = fetch_code(acc, used_codes(), dry_run=True)
    if code:
        result["code_ok"]    = True
        result["code_msg"]   = "Тестовый код найден"
        result["code_found"] = code[:20] + ("…" if len(code) > 20 else "")
    else:
        result["code_ok"]  = False
        result["code_msg"] = err or "Код не найден"
    return result

class IMAPQueue:
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
        while not _shutdown_flag["stop"]:
            try:
                task_fn, callback, args, kwargs = q.get(timeout=5)
            except queue.Empty:
                continue
            if _shutdown_flag["stop"]:
                try:
                    callback((None, "Перезапуск плагина, попробуй ещё раз."))
                except Exception:
                    pass
                break
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
            # FIX: read pause each iteration so changes to queue_pause_sec in
            # the TG settings take effect without restarting the plugin.
            time.sleep(app_settings().get("queue_pause_sec", 2))

    def queue_size(self, email_addr: str) -> int:
        q = self.queues.get(email_addr)
        return q.qsize() if q else 0

_imap_queue = IMAPQueue()

def get_active_rentals() -> list:
    now = time.time()
    all_rents = rentals()
    active = [r for r in all_rents.values() if _safe_ts(r.get("expires_at", 0)) > now]
    logger.debug(f"AutoCode: get_active_rentals: total={len(all_rents)}, active={len(active)}")
    return active

def get_expiring_rentals(within_hours: float) -> list:
    now = time.time()
    cutoff = now + within_hours * 3600
    return [
        r for r in rentals().values()
        if now < _safe_ts(r.get("expires_at", 0)) <= cutoff
    ]

_code_requests: dict[str, list[float]] = {}
_last_code_ts:  dict[str, float]       = {}
_rate_lock = Lock()
_used_lock = Lock()
_rentals_lock = Lock()

def _check_rate(buyer: str) -> tuple[bool, str]:
    with _rate_lock:
        now = time.time()
        last = _last_code_ts.get(buyer, 0)
        if now - last < CODE_CD:
            wait = int(CODE_CD - (now - last))
            return False, t("rate_wait", s=wait)
        hour_ago = now - 3600
        reqs = [x for x in _code_requests.get(buyer, []) if x > hour_ago]
        # FIX: Clean up stale buyer entries to prevent memory leak
        if not reqs:
            _code_requests.pop(buyer, None)
        else:
            _code_requests[buyer] = reqs
        if len(reqs) >= MAX_CODES_HOUR:
            return False, t("rate_hour_limit", n=MAX_CODES_HOUR)
        return True, ""

def _record_request(buyer: str):
    with _rate_lock:
        now = time.time()
        _last_code_ts[buyer] = now
        _code_requests.setdefault(buyer, []).append(now)

def kb_main():
    return K(keyboard=[
        [B("📬 Список почт",     callback_data=f"{AC_LIST}:0"),
         B("🔬 Проверить все",   callback_data=AC_CHECK_ALL)],
        [B("🏠 Активные аренды", callback_data=f"{AC_RENT_PAGE}:0"),
         B("📜 Лог выдач",       callback_data=AC_LOG)],
        [B("📊 Статистика",      callback_data=AC_STATS),
         B("📢 Рассылка",        callback_data=AC_BROADCAST)],
        [B("🩺 Проверка почт",   callback_data=AC_HEALTH),
         B("⚙️ Настройки",       callback_data=AC_REVIEW)],
    ])

def kb_stats_period():
    return K(keyboard=[
        [B("⏱ 24ч",    callback_data=AC_STATS_24H),
         B("⏱ 48ч",    callback_data=AC_STATS_48H)],
        [B("📅 7д",     callback_data=AC_STATS_7D),
         B("📋 Всё",    callback_data=AC_STATS_ALL)],
        [B("🗓 Период", callback_data=AC_STATS_RANGE)],
        [B("◀ Назад",   callback_data=AC_MAIN)],
    ])

def kb_broadcast_menu():
    return K(keyboard=[
        [B("✍️ Новое сообщение",  callback_data=AC_BCAST_SEND)],
        [B("📁 Шаблоны",          callback_data=AC_BCAST_TPLS)],
        [B("📋 История",          callback_data=AC_BCAST_HIST)],
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

def _filter_log_by_hours(hours):
    entries = log_entries()
    if hours is None:
        return [e for e in entries if _safe_ts(e.get("time", 0)) > 0]
    cutoff = time.time() - hours * 3600
    return [e for e in entries if _safe_ts(e.get("time", 0)) >= cutoff]

def _filter_log_by_range(date_from, date_to):
    entries = log_entries()
    df_ts = date_from.timestamp()
    dt_ts = date_to.timestamp()
    out = []
    for e in entries:
        ts = _safe_ts(e.get("time", 0))
        if df_ts <= ts <= dt_ts:
            out.append(e)
    return out

def _stats_text(entries, label: str) -> str:
    total    = len(entries)
    by_email = Counter(e.get("email") for e in entries if e.get("email"))
    by_buyer = Counter(e.get("buyer") for e in entries if e.get("buyer"))

    hours_list = []
    for e in entries:
        ts = _safe_ts(e.get("time", 0))
        if ts > 0:
            try:
                hours_list.append(datetime.fromtimestamp(ts).hour)
            except Exception:
                continue
    peak = Counter(hours_list).most_common(3)
    peak_str = ", ".join(f"{h:02d}:00 ({c})" for h, c in peak) or "—"

    all_rents     = _load(RENTALS_FILE, {})
    rent_by_buyer = Counter(r.get("buyer") for r in all_rents.values() if r.get("buyer"))
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

def _safe_edit(bot, call, text, kb, parse_mode=""):
    # parse_mode="" (not None): telebot treats None as "use the bot's default
    # parse_mode", and the host bot defaults to HTML. That made plain-text panels
    # containing literal "<"/">" (e.g. the legend "🔴 <2ч") fail with
    # "can't parse entities: Unsupported start tag". An empty string forces
    # plain text. Callers that genuinely need HTML pass parse_mode="HTML".
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode=parse_mode,
        )
    except Exception as e:
        msg = str(e).lower()
        if "message is not modified" in msg or "not modified" in msg:
            try:
                bot.answer_callback_query(call.id, "Уже актуально")
            except Exception:
                pass
            return
        logger.warning(f"AutoCode edit failed: {e}")
        try:
            bot.send_message(call.message.chat.id, text,
                             reply_markup=kb, parse_mode=parse_mode)
        except Exception:
            pass

def _safe_answer(bot, call, text="", show_alert=False):
    try:
        bot.answer_callback_query(call.id, text, show_alert=show_alert)
    except Exception:
        pass

def _send_with_retry(cardinal, chat_id, text, chat_name="") -> tuple[bool, str]:
    last_err = ""
    for attempt in range(1, BROADCAST_RETRY_MAX + 1):
        try:
            cardinal.send_message(chat_id, text, chat_name)
            return True, ""
        except Exception as ex:
            last_err = str(ex)
            logger.warning(f"send_with_retry attempt {attempt} fail to {chat_id}: {ex}")
            if attempt < BROADCAST_RETRY_MAX:
                time.sleep(2)
    return False, last_err

def _lookup_real_chat_id(cardinal, buyer: str):
    """Resolve the *real* FunPay chat_id (e.g. 264909029) for a buyer name.

    The order/restore API exposes the buyer's user-id (a small number) which is
    NOT a valid send target — sending to it raises "Доступ запрещён". The real
    chat_id only comes from an incoming Message or get_chat_by_name(). Returns a
    str chat_id, or None on failure.
    """
    if not buyer:
        return None
    try:
        acc = getattr(cardinal, "account", None)
        if acc is None or not hasattr(acc, "get_chat_by_name"):
            return None
        chat = acc.get_chat_by_name(buyer, True)
        cid = getattr(chat, "id", None) if chat else None
        return str(cid) if cid else None
    except Exception as ex:
        logger.warning(f"AutoCode: _lookup_real_chat_id({buyer!r}) failed: {ex}")
        return None

def _send_to_buyer(cardinal, rental: dict, text: str) -> bool:
    """Send a message to a rental's buyer, repairing a stale chat_id.

    Tries the stored chat_id first; on failure (commonly because the stored
    value is the buyer's user-id, not a real chat_id) it resolves the real
    chat_id via get_chat_by_name(), persists it back onto the rental, and
    retries. This is what stops the "Доступ запрещён" outbound failures.
    """
    cid   = rental.get("chat_id")
    cname = rental.get("chat_name") or rental.get("buyer") or ""
    buyer = rental.get("buyer") or ""
    key   = rental.get("order_key") or rental.get("order_id")

    if cid:
        try:
            cardinal.send_message(cid, text, cname)
            return True
        except Exception as ex:
            logger.warning(
                f"AutoCode: send to chat_id={cid} (buyer={buyer}) failed: {ex}; "
                f"resolving real chat_id…")

    real = _lookup_real_chat_id(cardinal, buyer)
    if real and _cid_norm(real) != _cid_norm(cid):
        # Persist the healed chat_id onto the live rental record (if still present).
        try:
            with _rentals_lock:
                rents = rentals()
                if key in rents:
                    rents[key]["chat_id"] = real
                    save_rentals(rents)
        except Exception as ex:
            logger.warning(f"AutoCode: persist repaired chat_id failed: {ex}")
        rental["chat_id"] = real
        logger.info(f"AutoCode: repaired chat_id for {buyer}: {cid} → {real}")
        try:
            cardinal.send_message(real, text, cname)
            return True
        except Exception as ex:
            logger.warning(
                f"AutoCode: send to repaired chat_id={real} (buyer={buyer}) failed: {ex}")
    return False

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
        logger.info(f"AutoCode: все {len(accs)} IMAP-аккаунтов прошли проверку.")

def _imap_health_worker(cardinal):
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

        for _ in range(HEALTH_CHECK_INTERVAL):
            if _shutdown_flag["stop"]:
                return
            time.sleep(1)

def _startup_sales_scan(cardinal):
    time.sleep(10)
    try:
        acc_obj = cardinal.account
        # FIX: Check that get_sales exists before calling
        if not hasattr(acc_obj, 'get_sales'):
            logger.warning("AutoCode: get_sales недоступен в текущей версии Cardinal")
            return
        now     = time.time()
        cutoff  = now - 31 * 24 * 3600

        logger.info("AutoCode: сканирование продаж за 31 день...")

        all_shortcuts = []
        start_from    = None
        pages         = 0

        while pages < 50:
            try:
                result = acc_obj.get_sales(
                    start_from=start_from,
                    include_paid=True,
                    include_closed=True,
                    include_refunded=True,   # needed to subtract refunds
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
                    last_ts = _safe_ts(last.date_ts)
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

        logger.info(f"AutoCode: получено {len(all_shortcuts)} заказов за 31 день.")

        if not all_shortcuts:
            return

        rents    = rentals()
        accs     = accounts()
        restored = 0
        created  = 0
        updated  = 0
        skipped  = 0
        skip_no_hours    = 0
        skip_no_acc      = 0
        skip_expired     = 0
        skip_existing    = 0
        skip_no_purchase = 0
        no_chat  = 0

        # Log first shortcut's attributes to help debug lot_name detection
        if all_shortcuts:
            s0 = all_shortcuts[0]
            attr_dump = {k: repr(getattr(s0, k, "?"))[:80]
                         for k in dir(s0)
                         if not k.startswith("_") and isinstance(getattr(s0, k, None), str)}
            logger.info(f"AutoCode: shortcut attrs sample: {attr_dump}")

        seller_id = getattr(acc_obj, "id", None)

        # ── Phase 1: extract normalized rental orders within the window ──
        orders         = []
        refunded_pairs = []   # (order_id, hours) of refunded orders to subtract
        for shortcut in all_shortcuts:
            try:
                order_id = str(getattr(shortcut, "id", "") or getattr(shortcut, "order_id", ""))
                _st      = getattr(shortcut, "status", None)
                is_refunded = "REFUND" in (getattr(_st, "name", "") or str(_st or "")).upper()

                # Try multiple attributes to find the lot title carrying duration
                _lot_candidates = []
                for _attr in ("description", "lot_name", "title", "lot_title",
                              "short_description", "subcategory_name", "name"):
                    _val = getattr(shortcut, _attr, None)
                    if _val and isinstance(_val, str):
                        _lot_candidates.append(_val)
                try:
                    _str_val = str(shortcut)
                    if _str_val and len(_str_val) > 5:
                        _lot_candidates.append(_str_val)
                except Exception:
                    pass
                lot_name = ""
                for _cand in _lot_candidates:
                    if _parse_hours(_cand):
                        lot_name = _cand
                        break
                if not lot_name:
                    lot_name = _lot_candidates[0] if _lot_candidates else ""

                buyer  = str(getattr(shortcut, "buyer_username", "") or getattr(shortcut, "buyer", "") or "")
                lot_id = str(getattr(shortcut, "lot_id", "") or "")
                _raw_bid = (getattr(shortcut, "buyer_id", None)
                            or getattr(shortcut, "chat_id", None))
                buyer_id = _buyer_id_from_chat(_raw_bid, seller_id)

                purchase_ts = None
                if hasattr(shortcut, "date_ts"):
                    purchase_ts = _safe_ts(shortcut.date_ts)
                elif hasattr(shortcut, "date") and shortcut.date:
                    try:
                        purchase_ts = shortcut.date.timestamp()
                    except Exception:
                        pass
                if not purchase_ts:
                    skipped += 1
                    skip_no_purchase += 1
                    continue
                if purchase_ts < cutoff:
                    skipped += 1
                    continue

                hours = _parse_hours(lot_name)
                if not hours:
                    skipped += 1
                    skip_no_hours += 1
                    if skip_no_hours <= 5:
                        logger.warning(
                            f"AutoCode: scan skip (no hours) #{skip_no_hours}: "
                            f"lot_name={lot_name!r:.120}, order_id={order_id}")
                    continue

                acc_email = None
                for a in accs:
                    if lot_id in a.get("lot_ids", []) or not a.get("lot_ids"):
                        acc_email = a["email"]
                        break
                if not acc_email:
                    skipped += 1
                    skip_no_acc += 1
                    continue

                if is_refunded:
                    # Don't count refunded orders into any stack; record them so
                    # their hours are subtracted from existing rentals (Phase 3).
                    refunded_pairs.append((order_id, hours))
                    continue

                orders.append({
                    "order_id": order_id, "buyer": buyer, "buyer_id": buyer_id,
                    "lot_id": lot_id, "email": acc_email, "lot_name": lot_name,
                    "purchase_ts": purchase_ts, "hours": hours,
                })
            except Exception as e:
                logger.warning(f"AutoCode: ошибка обработки заказа: {e}")
                continue

        # ── Phase 2: group by (buyer, email), stack durations (B2) and
        #    reconcile against existing rentals (backfill buyer_id, repair
        #    chat_id, merge duplicate per-order rentals, never shorten). ──
        groups = {}
        for o in orders:
            groups.setdefault((_name_norm(o["buyer"]), o["email"]), []).append(o)

        existing_by_group = {}
        for k, r in rents.items():
            existing_by_group.setdefault(
                (_name_norm(r.get("buyer")), r.get("email")), []).append(k)

        for gkey, glist in groups.items():
            glist.sort(key=lambda o: o["purchase_ts"])
            earliest, total_hours, computed_exp, order_ids = _stack_b2(glist)
            buyer_disp = glist[-1]["buyer"] or glist[0]["buyer"]
            buyer_id   = next((o["buyer_id"] for o in reversed(glist) if o["buyer_id"]), None)
            email      = gkey[1]
            lot_id     = glist[-1]["lot_id"]
            lot_name   = glist[-1].get("lot_name", "")
            review_bonus = _parse_review_bonus(lot_name)
            order_detail = [{"order_id": o["order_id"], "purchase_ts": o["purchase_ts"],
                             "hours": o["hours"]} for o in glist if o["order_id"]]

            ex_keys = existing_by_group.get(gkey, [])
            if ex_keys:
                keep = ex_keys[0]
                r = rents[keep]
                # Never shorten: keep the longer of stored (maybe manually
                # extended) and the recomputed stacked expiry.
                r["expires_at"] = max(_safe_ts(r.get("expires_at", 0)), computed_exp)
                if buyer_id and not r.get("buyer_id"):
                    r["buyer_id"] = buyer_id
                # Repair a chat_id that is actually the buyer's user-id or a
                # composite "users-..." string so it re-heals cleanly.
                _ex_cid = r.get("chat_id")
                if (isinstance(_ex_cid, str) and _ex_cid.startswith("users-")) or \
                   (_ex_cid is not None and buyer_id and _cid_norm(_ex_cid) == _bid_norm(buyer_id)):
                    r["chat_id"] = None
                merged_ids = set(order_ids)
                if r.get("order_id"):
                    merged_ids.add(str(r.get("order_id")))
                merged_ids.update(str(x) for x in r.get("order_ids", []))
                r["order_ids"]   = sorted(i for i in merged_ids if i)
                # Merge per-order detail (backfill for accurate refund subtraction)
                _have = {str(o.get("order_id")) for o in r.get("orders", [])}
                r.setdefault("orders", [])
                for od in order_detail:
                    if str(od["order_id"]) not in _have:
                        r["orders"].append(od)
                r["lot_name"]     = r.get("lot_name") or lot_name
                if review_bonus:
                    r["review_bonus"] = review_bonus
                r["hours"]       = max(int(r.get("hours", 0) or 0), total_hours)
                r["purchase_ts"] = min(_safe_ts(r.get("purchase_ts", 0)) or earliest, earliest)
                rents[keep] = r
                # Collapse duplicate per-order rentals for this buyer+email.
                for dup in ex_keys[1:]:
                    rents.pop(dup, None)
                    skip_existing += 1
                updated += 1
                if not r.get("chat_id"):
                    no_chat += 1
            else:
                if computed_exp <= now:
                    skipped += 1
                    skip_expired += 1
                    continue
                key = order_ids[0] if order_ids else str(uuid_lib.uuid4())
                rents[key] = {
                    "order_key":    key,
                    "buyer":        buyer_disp,
                    "buyer_id":     buyer_id,
                    "chat_id":      None,
                    "chat_name":    buyer_disp,
                    "email":        email,
                    "lot_id":       lot_id,
                    "lot_name":     lot_name,
                    "order_id":     order_ids[0] if order_ids else key,
                    "order_ids":    order_ids,
                    "orders":       order_detail,
                    "review_bonus": review_bonus,
                    "purchase_ts":  earliest,
                    "expires_at":   computed_exp,
                    "hours":        total_hours,
                    "restored":     True,
                    "lang":         "ru",
                }
                created += 1
                no_chat += 1

        # ── Phase 3: subtract refunded orders from any rental that counted
        #    them (idempotent via per-rental refunded_ids). Newly-built stacks
        #    already excluded refunds, so this only touches pre-existing data. ──
        refund_adjusted = 0
        refund_removed  = 0
        for oid, rh in refunded_pairs:
            soid = str(oid)
            for k in list(rents.keys()):
                r = rents[k]
                referenced = (str(r.get("order_id")) == soid
                              or soid in [str(x) for x in r.get("order_ids", [])]
                              or any(str(o.get("order_id")) == soid for o in r.get("orders", [])))
                if not referenced:
                    continue
                if soid in {str(x) for x in r.get("refunded_ids", [])}:
                    break
                keep_it = _apply_refund_to_rental(r, soid, rh)
                if not keep_it:
                    rents.pop(k, None)
                    refund_removed += 1
                else:
                    refund_adjusted += 1
                break

        restored = created + updated
        save_rentals(rents)

        # Count total active rentals in file (including previously saved)
        active_now = [r for r in rents.values()
                      if _safe_ts(r.get("expires_at", 0)) > now]
        active_count = len(active_now)

        diag = (
            f"AutoCode: сканирование завершено. "
            f"Создано: {created}, обновлено: {updated} (без chat_id: {no_chat}), "
            f"возвраты: -{refund_adjusted}/удалено {refund_removed}, пропущено: {skipped} "
            f"[нет часов: {skip_no_hours}, нет акк: {skip_no_acc}, "
            f"истёк: {skip_expired}, дублей слито: {skip_existing}, нет даты: {skip_no_purchase}]. "
            f"Всего в файле: {len(rents)}, из них активных: {active_count}."
        )
        logger.info(diag)

        # Always notify with active count
        extra = f"\n⚠️ Без chat_id: {no_chat}" if no_chat else ""
        refund_line = (f"\n↩️ Возвраты: {refund_adjusted + refund_removed}"
                       if (refund_adjusted or refund_removed) else "")
        _notify_tg(cardinal,
            f"📊 AutoCode: сканирование продаж за 31 день.\n"
            f"Новых аренд: {created}, обновлено: {updated}\n"
            f"🏠 Активных аренд: {active_count}{refund_line}\n"
            f"В файле всего: {len(rents)}{extra}"
        )

    except Exception as e:
        logger.error(f"AutoCode: ошибка сканирования продаж: {e}")

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
                        if now - _safe_ts(entry.get("used_at", 0)) < USED_CODE_TTL_SEC:
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

def _weekly_report_worker(cardinal):
    while not _shutdown_flag["stop"]:
        now = datetime.now()
        days_ahead = (WEEKLY_REPORT_DOW - now.weekday()) % 7
        if days_ahead == 0 and now.hour >= WEEKLY_REPORT_HOUR:
            days_ahead = 7
        next_run = now.replace(hour=WEEKLY_REPORT_HOUR, minute=0, second=0, microsecond=0) \
                   + timedelta(days=days_ahead)
        sleep_sec = (next_run - datetime.now()).total_seconds()
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

def _expiry_watcher(cardinal):
    startup_ts = time.time()
    state = warned_state()
    warned_12h = set(state.get("warned_12h", []))

    def _persist_state():
        save_warned({
            "warned_12h": list(warned_12h),
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

            stale = [k for k in warned_12h if k not in rents]
            for k in stale:
                warned_12h.discard(k)
                state_dirty = True

            for key, r in list(rents.items()):
                exp         = _safe_ts(r.get("expires_at", 0))
                cid         = r.get("chat_id")
                cname       = r.get("chat_name", "")
                purchase_ts = _safe_ts(r.get("purchase_ts", 0))
                lang        = r.get("lang", "ru")

                if purchase_ts and (now - purchase_ts) < RENTAL_GRACE_SEC:
                    continue

                if (now - startup_ts) < RENTAL_GRACE_SEC:
                    continue

                left = exp - now

                warn_sec = WARN_BEFORE_H * 3600
                if (key not in warned_12h
                        and (warn_sec - 600) <= left <= warn_sec):
                    warned_12h.add(key)
                    state_dirty = True
                    # _send_to_buyer resolves/repairs the real chat_id if the
                    # stored one is a user-id (or missing) → no "Доступ запрещён".
                    Thread(
                        target=_send_to_buyer,
                        args=(cardinal, r, t("warn_12h", h=WARN_BEFORE_H)),
                        daemon=True,
                    ).start()

                if now >= exp + 60:
                    # FIX: Lock rental deletion to prevent race with on_new_order
                    snapshot = dict(r)  # keep buyer/chat_id for the farewell send
                    with _rentals_lock:
                        del rents[key]
                        warned_12h.discard(key)
                        changed = True
                        state_dirty = True
                        save_rentals(rents)
                    _persist_state()
                    Thread(
                        target=_send_to_buyer,
                        args=(cardinal, snapshot, t("rental_ended")),
                        daemon=True,
                    ).start()

            if changed:
                save_rentals(rents)
            if state_dirty:
                _persist_state()

        except Exception as e:
            logger.error(f"Expiry watcher error: {e}")



def _schedule_review_request(cardinal, chat_id, chat_name, buyer, order_id=None):
    def _send():
        if _shutdown_flag["stop"]:
            return
        try:
            settings = app_settings()
            if not settings.get("review_enabled", True):
                return
            # If the buyer already left a review for this order, stay silent —
            # only the code itself should have been sent.
            if order_id and _order_review_exists(cardinal, order_id):
                logger.info(f"AutoCode: review already exists for {order_id}, skip nudge")
                return
            has_rental = any(
                r.get("buyer") == buyer for r in rentals().values()
            )
            if not has_rental:
                return
            tpl = settings.get("review_template", "")
            if not tpl:
                return
            cardinal.send_message(chat_id, tpl, chat_name)
            logger.info(f"AutoCode review request sent: {buyer}")
        except Exception as e:
            logger.warning(f"Review request fail {buyer}: {e}")

    t_timer = Timer(REVIEW_DELAY_SEC, _send)
    t_timer.daemon = True
    t_timer.start()


def _schedule_order_confirm(cardinal, chat_id, chat_name, order_id):
    """Send order confirmation request 5 min after code delivery."""
    if not order_id:
        return
    def _send():
        if _shutdown_flag["stop"]:
            return
        try:
            settings = app_settings()
            if not settings.get("confirm_enabled", True):
                return
            # Skip the "leave a review" nudge if the order already has one.
            if _order_review_exists(cardinal, order_id):
                logger.info(f"AutoCode: review already exists for {order_id}, skip review request")
                return
            msg = t("order_confirm", order_id=order_id)
            cardinal.send_message(chat_id, msg, chat_name)
            logger.info(f"AutoCode review request (order link) sent: {order_id}")
        except Exception as e:
            logger.warning(f"Order confirm fail {order_id}: {e}")

    t_timer = Timer(ORDER_CONFIRM_DELAY, _send)
    t_timer.daemon = True
    t_timer.start()

def _backup_worker():
    while not _shutdown_flag["stop"]:
        for _ in range(86400):
            if _shutdown_flag["stop"]:
                return
            time.sleep(1)
        try:
            _ensure()
            stamp = datetime.now().strftime("%Y%m%d_%H%M")
            # FIX: Also backup ACCOUNTS_FILE (credentials)
            for src in (RENTALS_FILE, LOG_FILE, ACCOUNTS_FILE):
                if os.path.exists(src):
                    name = os.path.basename(src).replace(".json", f"_{stamp}.json")
                    dst = os.path.join(BACKUP_DIR, name)
                    with open(src, "rb") as f1, open(dst, "wb") as f2:
                        f2.write(f1.read())
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

def _parse_hours(text: str) -> int | None:
    if not text:
        return None
    s = text.lower()

    m = re.search(r"(\d+)\s*(?:год|года|лет|y|year|years)\b", s)
    if m:
        return int(m.group(1)) * 24 * 365

    m = re.search(r"(\d+)\s*(?:месяц|месяца|месяцев|мес\.?|month|months|mo)\b", s)
    if m:
        return int(m.group(1)) * 24 * 30
    if re.search(r"\bмесяц\b", s) and not re.search(r"\d+\s*месяц", s):
        return 24 * 30

    # FIX: Split multi-byte ➕ into separate lookbehind for Python compat
    m = re.search(r"(?<!\+)(?<!➕)(\d+)\s*(?:неделя|недели|недель|нед\.?|week|weeks)\b", s)
    if m:
        return int(m.group(1)) * 24 * 7
    if re.search(r"\bнеделя\b", s) and not re.search(r"\d+\s*недел", s):
        return 24 * 7

    m = re.search(r"(\d+)\s*(?:день|дня|дней|сутки|суток|д\.?|d|day|days)\b", s)
    if m:
        return int(m.group(1)) * 24
    m = re.search(r"(?<!\+)(?<!➕)(\d+)\s*д(?:ень|ня|ней|ен)?(?:\b|[^а-яё])", s)
    if m:
        return int(m.group(1)) * 24
    if re.search(r"\bсутки\b", s) and not re.search(r"\d+\s*сут", s):
        return 24

    m = re.search(r"(?<!\+)(?<!➕)(\d+)\s*(?:час|часа|часов|ч\.?|h|hour|hours|hr)\b", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(?<!\+)(?<!➕)(\d+)\s*ч(?:ас(?:а|ов)?)?(?:\b|[^а-яё])", s)
    if m:
        return int(m.group(1))

    return None

def _graceful_shutdown(*args):
    if _shutdown_event.is_set():
        return
    _shutdown_event.set()
    logger.info("AutoCode: graceful shutdown.")

atexit.register(_graceful_shutdown)
try:
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)
except Exception:
    pass

def on_new_order(c, e: NewOrderEvent):
    global _cardinal_ref
    _cardinal_ref = c

    desc     = getattr(e.order, "description", "") or ""
    lot_name = getattr(e.order, "lot_name", "") or desc

    # Try multiple attributes to find text with duration
    hours = _parse_hours(lot_name) or _parse_hours(desc)
    if not hours:
        for _attr in ("title", "lot_title", "short_description",
                       "subcategory_name", "name"):
            _val = getattr(e.order, _attr, None)
            if _val and isinstance(_val, str):
                hours = _parse_hours(_val)
                if hours:
                    lot_name = _val
                    break
        if not hours:
            try:
                _str_val = str(e.order)
                hours = _parse_hours(_str_val)
                if hours:
                    lot_name = _str_val
            except Exception:
                pass

    if not hours:
        # Log all string attrs for debugging
        _attrs = {k: repr(getattr(e.order, k, "?"))[:60]
                  for k in dir(e.order)
                  if not k.startswith("_") and isinstance(getattr(e.order, k, None), str)}
        logger.warning(f"AutoCode: ⚠️ не удалось определить часы из названия лота: {lot_name!r} / {desc!r}. "
                       f"Аренда НЕ создана. Добавьте длительность в название (напр. '24ч', '30 дней'). "
                       f"Attrs: {_attrs}")
        return

    buyer     = getattr(e.order, "buyer_username", "") or ""
    # NOTE: e.order.chat_id is frequently None and, when present, is a composite
    # "users-{A}-{B}" string (not a real chat_id ~264M). Extract the buyer's
    # user-id for robust matching and leave chat_id None — it is healed when the
    # buyer first writes (and repaired before any outbound send). This avoids the
    # "Доступ запрещён" failures from sending to a non-chat id.
    _raw_bid  = getattr(e.order, "buyer_id", None) or getattr(e.order, "chat_id", None)
    buyer_id  = _buyer_id_from_chat(_raw_bid, getattr(getattr(c, "account", None), "id", None))
    chat_id   = None
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

    lang = get_buyer_lang(buyer, desc + " " + lot_name)

    now   = time.time()
    rents = rentals()

    # FIX: Match by lot_id first, then fallback to buyer-only match
    for k, r in rents.items():
        if r.get("buyer") == buyer \
           and r.get("lot_id") == lot_id \
           and _safe_ts(r.get("expires_at", 0)) > now:
            with _rentals_lock:
                rents[k]["expires_at"] = _safe_ts(rents[k].get("expires_at", 0)) + hours * 3600
                rents[k]["hours"]      = rents[k].get("hours", 0) + hours
                if buyer_id and not rents[k].get("buyer_id"):
                    rents[k]["buyer_id"] = buyer_id
                # Track per-order detail (for accurate refund subtraction) and
                # the lot name / review bonus (for "+Nч за отзыв" auto-extend).
                _ords = rents[k].setdefault("orders", [])
                if not any(str(o.get("order_id")) == order_id for o in _ords):
                    _ords.append({"order_id": order_id, "purchase_ts": now, "hours": hours})
                _oids = rents[k].setdefault("order_ids", [])
                if order_id and order_id not in _oids:
                    _oids.append(order_id)
                rents[k]["lot_name"]     = lot_name
                rents[k]["review_bonus"] = _parse_review_bonus(lot_name) or rents[k].get("review_bonus", 0)
                cur_lang = get_buyer_lang(buyer, desc + " " + lot_name) or lang
                rents[k]["lang"] = cur_lang
                save_rentals(rents)
            state = warned_state()
            for s_key in ("warned_12h",):
                if k in state.get(s_key, []):
                    state[s_key].remove(k)
            save_warned(state)

            # FIX: prefer the chat_id stored on the rental (it was healed when
            # the buyer first wrote in the order chat). The chat_id on a
            # NewOrderEvent is frequently None, so using it here silently drops
            # the "rental renewed" confirmation.
            target_chat_id   = rents[k].get("chat_id") or chat_id
            target_chat_name = rents[k].get("chat_name") or chat_name
            if target_chat_id:
                Thread(
                    target=c.send_message,
                    args=(target_chat_id,
                          t("rental_renewed", h=hours, t=_fmt_time(rents[k]['expires_at'])),
                          target_chat_name),
                    daemon=True,
                ).start()
            else:
                logger.info(
                    f"AutoCode: продление {buyer} — chat_id ещё неизвестен, "
                    f"подтверждение уйдёт после первого сообщения в чате."
                )
            return

    # ── Loyalty: +LOYALTY_BONUS_DAYS if buyer repurchases within LOYALTY_WINDOW_DAYS ──
    loyalty_bonus_sec = 0
    window = LOYALTY_WINDOW_DAYS * 86400
    for r in rents.values():
        if r.get("buyer") == buyer:
            exp = _safe_ts(r.get("expires_at", 0))
            # Rental expired, but within the loyalty window
            if exp <= now and (now - exp) <= window:
                loyalty_bonus_sec = LOYALTY_BONUS_DAYS * 86400
                logger.info(
                    f"AutoCode loyalty: {buyer} repurchased within "
                    f"{LOYALTY_WINDOW_DAYS}d of expiry → +{LOYALTY_BONUS_DAYS}d bonus")
                break

    expires_at = now + hours * 3600 + loyalty_bonus_sec
    with _rentals_lock:
        rents[order_id] = {
            "order_key":    order_id,
            "buyer":        buyer,
            "buyer_id":     buyer_id,
            "chat_id":      chat_id,
            "chat_name":    chat_name,
            "email":        acc_email,
            "lot_id":       lot_id,
            "lot_name":     lot_name,
            "order_id":     order_id,
            "order_ids":    [order_id],
            "orders":       [{"order_id": order_id, "purchase_ts": now, "hours": hours}],
            "review_bonus": _parse_review_bonus(lot_name),
            "purchase_ts":  now,
            "expires_at":   expires_at,
            "hours":        hours,
            "lang":         lang,
        }
        save_rentals(rents)
    logger.info(
        f"AutoCode: новая аренда {buyer} | {acc_email} | {hours}ч | "
        f"до {_fmt_time(expires_at)} | order={order_id} | lang={lang}"
    )

    # FIX: chat_id from a NewOrderEvent is often None. Only send now if we
    # actually have a chat to send to; otherwise on_new_message heals the
    # chat_id when the buyer first writes and the buyer can use !cd right away.
    if chat_id:
        Thread(
            target=c.send_message,
            args=(chat_id, t("rental_activated"), chat_name),
            daemon=True,
        ).start()

        if loyalty_bonus_sec > 0:
            Thread(
                target=c.send_message,
                args=(chat_id,
                      t("loyalty_bonus", days=LOYALTY_BONUS_DAYS),
                      chat_name),
                daemon=True,
            ).start()
    else:
        logger.info(
            f"AutoCode: активация {buyer} — chat_id ещё неизвестен, "
            f"приветствие будет пропущено (покупатель сразу может использовать !cd)."
        )

    # Schedule order confirmation after first code delivery (5 min)
    # (actual sending happens in _on_result inside on_new_message)

def on_order_status_changed(c, e: OrderStatusChangedEvent):
    """When an order is refunded, subtract its contribution from the buyer's
    rental (idempotent via refunded_ids). Removes the rental if nothing remains."""
    global _cardinal_ref
    _cardinal_ref = c
    try:
        order  = getattr(e, "order", None)
        status = getattr(order, "status", None)
        sname  = (getattr(status, "name", "") or str(status or "")).upper()
        if "REFUND" not in sname:
            return
        oid = str(getattr(order, "id", "") or "")
        if not oid:
            return
        h = 0
        for _attr in ("description", "lot_name", "title", "short_description"):
            _v = getattr(order, _attr, None)
            if _v and isinstance(_v, str):
                h = _parse_hours(_v) or 0
                if h:
                    break
        rents = rentals()
        for k in list(rents.keys()):
            r = rents[k]
            referenced = (str(r.get("order_id")) == oid
                          or oid in [str(x) for x in r.get("order_ids", [])]
                          or any(str(o.get("order_id")) == oid for o in r.get("orders", [])))
            if not referenced:
                continue
            if oid in {str(x) for x in r.get("refunded_ids", [])}:
                return
            keep_it = _apply_refund_to_rental(r, oid, h)
            if not keep_it:
                rents.pop(k, None)
            save_rentals(rents)
            logger.info(f"AutoCode: refund applied for order {oid} (-{h}ч), kept={keep_it}")
            _notify_tg(c, (f"↩️ Возврат заказа #{oid}: "
                           f"{'аренда удалена' if not keep_it else f'аренда сокращена на {h}ч'}."))
            return
    except Exception as ex:
        logger.warning(f"AutoCode: on_order_status_changed error: {ex}")


def on_new_message(c, e: NewMessageEvent):
    global _cardinal_ref
    _cardinal_ref = c

    text  = _strip_invisible((e.message.text or ""))
    lower = text.lower()

    # Skip own (bot-sent) messages, but allow commands from the seller
    if e.message.author_id == c.account.id:
        if not _is_command_message(text):
            return

    author    = e.message.author       # whoever sent this message
    chat_id   = e.message.chat_id      # the order chat
    chat_name = e.message.chat_name

    # ── Find active rental for this chat (by chat_id, not by author) ──
    rents = rentals()
    now = time.time()

    cid_norm    = _cid_norm(chat_id)
    author_norm = _name_norm(author)
    author_id   = _bid_norm(getattr(e.message, "author_id", None))
    seller_id   = _bid_norm(getattr(getattr(c, "account", None), "id", None))
    is_seller   = bool(author_id) and author_id == seller_id

    def _heal(k, reason):
        """Stamp the real incoming chat_id onto a matched rental and persist."""
        old_cid = rents[k].get("chat_id")
        rents[k]["chat_id"]   = cid_norm
        rents[k]["chat_name"] = chat_name
        save_rentals(rents)
        logger.info(f"AutoCode: healed chat_id for rental {k} via {reason} "
                    f"(buyer={rents[k].get('buyer')}, {old_cid} → {cid_norm})")
        return [rents[k]]

    def _find_active_rentals_for_chat():
        """Find active rentals for this chat. Tiers, in order:
        1) real chat_id match, 2) buyer user-id match, 3) buyer-name match,
        4) seller testing → resolve the chat's interlocutor and match by name.
        Each fallback heals chat_id so subsequent lookups/sends hit tier 1."""
        # 1. chat_id match — normalized to string so restored rentals (str)
        #    match an int e.message.chat_id (previously failed silently).
        results = [r for r in rents.values()
                   if cid_norm is not None
                   and _cid_norm(r.get("chat_id")) == cid_norm
                   and _safe_ts(r.get("expires_at", 0)) > now]
        if results:
            return sorted(results,
                          key=lambda r: _safe_ts(r.get("purchase_ts", 0)),
                          reverse=True)

        # 2. buyer user-id match (stable across name changes). Only when the
        #    message is from the buyer (not the seller). Most robust signal.
        if author_id and not is_seller:
            id_cands = [
                (k, r) for k, r in rents.items()
                if _bid_norm(r.get("buyer_id")) == author_id
                and _safe_ts(r.get("expires_at", 0)) > now
                and _cid_norm(r.get("chat_id")) != cid_norm
            ]
            if id_cands:
                id_cands.sort(key=lambda x: -_safe_ts(x[1].get("purchase_ts", 0)))
                return _heal(id_cands[0][0], "buyer_id")

        # 3. Fallback: match by buyer name (normalized), heal the most recent
        #    unmatched rental only (to avoid clobbering other chats).
        candidates = [
            (k, r) for k, r in rents.items()
            if _name_norm(r.get("buyer")) == author_norm
            and author_norm
            and _safe_ts(r.get("expires_at", 0)) > now
            and _cid_norm(r.get("chat_id")) != cid_norm
        ]
        if candidates:
            # Prefer rentals with no chat_id (=None), then most recent
            candidates.sort(
                key=lambda x: (0 if not x[1].get("chat_id") else 1,
                               -_safe_ts(x[1].get("purchase_ts", 0))))
            return _heal(candidates[0][0], "buyer-name")

        # 4. Seller testing the command inside a buyer's chat: the author is the
        #    seller, so name/id can't match. Resolve the chat's interlocutor
        #    (the actual buyer) and match the rental by that name.
        if is_seller and cid_norm is not None:
            interloc = ""
            try:
                acc = getattr(c, "account", None)
                chat = None
                if acc is not None and hasattr(acc, "get_chat"):
                    _arg = int(chat_id) if str(chat_id).isdigit() else chat_id
                    chat = acc.get_chat(_arg)
                if chat is not None:
                    interloc = _name_norm(getattr(chat, "name", "")
                                          or getattr(chat, "interlocutor", ""))
            except Exception as ex:
                logger.warning(f"AutoCode: get_chat({chat_id}) failed: {ex}")
            if interloc:
                seller_cands = [
                    (k, r) for k, r in rents.items()
                    if _name_norm(r.get("buyer")) == interloc
                    and _safe_ts(r.get("expires_at", 0)) > now
                ]
                if seller_cands:
                    seller_cands.sort(key=lambda x: -_safe_ts(x[1].get("purchase_ts", 0)))
                    return _heal(seller_cands[0][0], f"seller-test/{interloc}")

        return []

    # Update chat_id on rentals by buyer name if not set
    rents_changed = False
    for k, r in rents.items():
        if _name_norm(r.get("buyer")) == author_norm and author_norm \
           and _safe_ts(r.get("expires_at", 0)) > now:
            if not r.get("chat_id"):
                rents[k]["chat_id"]   = cid_norm
                rents[k]["chat_name"] = chat_name
                rents_changed = True
    if rents_changed:
        save_rentals(rents)

    # ── Auto-extend on review: lots tagged "+Nч за отзыв" grant bonus hours
    #    the first time the buyer leaves a review in this chat. ──
    _mtype  = getattr(e.message, "type", None)
    _mtname = (getattr(_mtype, "name", "") or "").upper()
    _type_review = "FEEDBACK" in _mtname        # host-provided signal (trusted)
    _text_review = (any(p in lower for p in ("написал отзыв", "оставил отзыв",
                                             "изменил отзыв"))
                    and ("к заказу" in lower or "#" in lower))
    if _type_review or _text_review:
        for r in _find_active_rentals_for_chat():
            bonus = int(r.get("review_bonus", 0) or 0)
            if bonus <= 0 or r.get("review_bonus_given"):
                continue
            # If we only have a text hint (no message type), confirm a real
            # review exists via the API so nobody can game it by chatting.
            if not _type_review:
                _oids = [r.get("order_id"), *[x for x in r.get("order_ids", [])]]
                if not any(_order_review_exists(c, o) for o in _oids if o):
                    continue
            r["expires_at"] = _safe_ts(r.get("expires_at", 0)) + bonus * 3600
            r["hours"]      = int(r.get("hours", 0) or 0) + bonus
            r["review_bonus_given"] = True
            save_rentals(rents)
            Thread(
                target=c.send_message,
                args=(chat_id,
                      t("review_bonus_added", h=bonus, t=_fmt_time(r["expires_at"])),
                      chat_name),
                daemon=True,
            ).start()
            logger.info(f"AutoCode: review bonus +{bonus}ч for {r.get('buyer')}")
            break
        return

    if lower in ("!time", "time", "!время", "время"):
        active = _find_active_rentals_for_chat()
        if not active:
            reply = t("no_active")
        else:
            r = active[0]
            reply = t("remaining",
                         rem=_fmt_remaining(r['expires_at']),
                         end=_fmt_time(r['expires_at']))
        Thread(target=c.send_message, args=(chat_id, reply, chat_name), daemon=True).start()
        return

    # ── !продлить / !extend — send lot link for re-purchase ──
    if lower in ("!продлить", "!extend", "продлить"):
        active = _find_active_rentals_for_chat()
        if not active:
            reply = t("extend_no_rental")
        else:
            lot_id = active[0].get("lot_id", "")
            reply = t("extend_link", lot_id=lot_id)
        Thread(target=c.send_message, args=(chat_id, reply, chat_name), daemon=True).start()
        return

    # ── !faq / !команды — show available commands ──
    if lower in ("!faq", "faq", "!команды", "команды"):
        settings = app_settings()
        custom = settings.get("faq_custom_ru", "")
        reply = t("faq", custom=custom)
        Thread(target=c.send_message, args=(chat_id, reply, chat_name), daemon=True).start()
        return

    # ── Problem detection → notify seller in TG ──
    if not _is_command_message(text) and any(kw in lower for kw in _PROBLEM_KEYWORDS):
        now_p = time.time()
        last_notify = _PROBLEM_COOLDOWN.get(author, 0)
        if now_p - last_notify > _PROBLEM_COOLDOWN_SEC:
            _PROBLEM_COOLDOWN[author] = now_p
            active_r = _find_active_rentals_for_chat()
            oid_tag = ""
            if active_r:
                oid = active_r[0].get("order_id", "")
                lot_info = active_r[0].get("lot_id", "—")
                remain   = _fmt_remaining(active_r[0]["expires_at"])
                if oid:
                    oid_tag = f"\n🏷 Заказ: #{oid} (https://funpay.com/orders/{oid}/)"
            else:
                lot_info = "—"
                remain   = "—"
            alert = (
                f"🚨 Проблема у покупателя!\n\n"
                f"👤 Отправитель: {author}\n"
                f"📦 Лот: {lot_info}\n"
                f"⏳ Осталось: {remain}\n"
                f"💬 Сообщение: {text[:200]}"
                f"{oid_tag}"
            )
            Thread(target=_notify_tg, args=(c, alert), daemon=True).start()

    if lower not in ("!cd", "code", "код", "!код"):
        return

    allowed, reason = _check_rate(author)
    if not allowed:
        Thread(target=c.send_message, args=(chat_id, reason, chat_name), daemon=True).start()
        reqs = _code_requests.get(author, [])
        if len(reqs) >= MAX_CODES_HOUR:
            Thread(target=_notify_tg_rate_limit, args=(c, author, len(reqs)), daemon=True).start()
        return

    active = _find_active_rentals_for_chat()

    if not active:
        # Diagnostic: dump what active rentals exist so a real "no rental"
        # can be told apart from a matching bug (buyer/chat_id mismatch).
        _active_dump = [
            {"buyer": r.get("buyer"), "buyer_id": r.get("buyer_id"),
             "chat_id": r.get("chat_id"), "lot_id": r.get("lot_id"),
             "left_min": int((_safe_ts(r.get("expires_at", 0)) - now) / 60)}
            for r in rents.values()
            if _safe_ts(r.get("expires_at", 0)) > now
        ]
        logger.warning(
            f"AutoCode: !cd без аренды — author={author!r} (norm={author_norm!r}, "
            f"id={author_id!r}, seller={is_seller}), "
            f"chat_id={chat_id!r} (norm={cid_norm!r}). "
            f"Активных аренд в файле: {len(_active_dump)} → {_active_dump[:10]}"
        )
        Thread(
            target=c.send_message,
            args=(chat_id, t("no_rental"), chat_name),
            daemon=True,
        ).start()
        return

    rental    = active[0]
    acc_email = rental.get("email")
    order_id  = rental.get("order_id", "")
    accs      = accounts()
    acc       = next((a for a in accs if a["email"] == acc_email), None)
    if not acc:
        Thread(
            target=c.send_message,
            args=(chat_id, t("mailbox_missing"), chat_name),
            daemon=True,
        ).start()
        return

    _record_request(author)

    q_size = _imap_queue.queue_size(acc_email)
    if q_size > 0:
        Thread(
            target=c.send_message,
            args=(chat_id, t("queue_position", pos=q_size + 1), chat_name),
            daemon=True,
        ).start()

    used = used_codes()
    not_before_ts = _safe_ts(rental.get("purchase_ts", 0))

    def _on_result(result):
        code_val, err = result
        if not code_val:
            # Don't notify seller in TG — just tell buyer
            Thread(
                target=c.send_message,
                args=(chat_id, t("code_not_found"), chat_name),
                daemon=True,
            ).start()
            return

        # FIX: Lock used_codes read-modify-write to prevent race condition / double delivery
        with _used_lock:
            u = used_codes()
            u.setdefault(acc_email, []).append({"code": code_val, "used_at": time.time()})
            save_used(u)
        buyer_name = rental.get("buyer", author)
        add_log(acc_email, rental.get("lot_id", "—"), buyer_name, code_val, chat_id)

        received_dt = datetime.now().strftime("%d.%m.%Y %H:%M")
        msg = t("code_msg", code=code_val, dt=received_dt)
        Thread(target=c.send_message, args=(chat_id, msg, chat_name), daemon=True).start()

        _schedule_review_request(c, chat_id, chat_name, buyer_name, order_id)

        # Ask buyer to leave a review (skipped automatically if one already
        # exists — then only the code above was sent).
        _schedule_order_confirm(c, chat_id, chat_name, order_id)

    _imap_queue.submit(acc_email, fetch_code, _on_result, acc, used, not_before_ts=not_before_ts)



def init_autocode_tg(cardinal, *args):
    global _cardinal_ref
    _cardinal_ref = cardinal
    bot = cardinal.telegram.bot

    _load_lang_cache()


    _bcast = {"text": "", "failed": [], "retry_text": "", "scheduled_timer": None}

    @bot.message_handler(commands=["autocode"])
    def cmd_autocode(message: Message):
        bot.send_message(message.chat.id,
                         f"⚙️ AutoCode v{VERSION} — выберите раздел:",
                         reply_markup=kb_main())

    @bot.callback_query_handler(func=lambda c: c.data == AC_MAIN)
    def open_main(call: CallbackQuery):
        _safe_edit(bot, call, f"⚙️ AutoCode v{VERSION} — выберите раздел:", kb_main())

    @bot.callback_query_handler(func=lambda c: c.data == AC_CHECK_ALL)
    def check_all_mailboxes(call: CallbackQuery):
        accs = accounts()
        if not accs:
            _safe_answer(bot, call, "Нет почт.", show_alert=True)
            return
        _safe_answer(bot, call, f"Проверяю {len(accs)} почт...")
        bot.send_message(call.message.chat.id,
            f"🔬 Запущена полная проверка {len(accs)} почт (IMAP + поиск кода)...")

        def _run():
            results = []
            health = health_state()
            for acc in accs:
                try:
                    r = full_check_account(acc)
                except Exception as ex:
                    r = {
                        "email":    acc.get("email", "?"),
                        "imap_ok":  False, "imap_msg": f"Exception: {ex}",
                        "code_ok":  False, "code_msg": "",
                        "code_found": None,
                    }
                results.append(r)
                health[r["email"]] = {
                    "last_check": time.time(),
                    "ok":         r["imap_ok"],
                    "message":    r["imap_msg"],
                }
            save_health(health)

            ok_imap  = sum(1 for r in results if r["imap_ok"])
            ok_code  = sum(1 for r in results if r["code_ok"])
            lines = [
                f"🔬 Результаты проверки ({len(results)} почт)",
                f"✅ IMAP: {ok_imap}/{len(results)} | ✅ Код: {ok_code}/{len(results)}",
                "",
            ]
            for r in results:
                em = r["email"]
                imap_mark = "✅" if r["imap_ok"] else "❌"
                code_mark = "✅" if r["code_ok"] else ("⏭" if not r["imap_ok"] else "❌")
                lines.append(f"📧 {em}")
                lines.append(f"   {imap_mark} IMAP: {r['imap_msg']}")
                lines.append(f"   {code_mark} Код: {r['code_msg']}"
                             + (f" ({r['code_found']})" if r.get("code_found") else ""))
                lines.append("")
            full_text = "\n".join(lines).strip()

            chunks, cur = [], ""
            for line in full_text.split("\n"):
                if len(cur) + len(line) + 1 > 3800:
                    chunks.append(cur)
                    cur = line
                else:
                    cur = cur + "\n" + line if cur else line
            if cur:
                chunks.append(cur)
            for i, ch in enumerate(chunks):
                kb = K(keyboard=[[B("◀ Назад", callback_data=AC_MAIN)]]) if i == len(chunks) - 1 else None
                bot.send_message(call.message.chat.id, ch, reply_markup=kb)

        Thread(target=_run, daemon=True).start()

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
        _safe_edit(bot, call, "📬 Почтовые аккаунты:", K(keyboard=rows))

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
        bot.register_next_step_handler(msg, lambda m, _i=idx: _step_pass(m, _i))

    def _step_pass(message: Message, idx: int):
        accs = accounts()
        if idx >= len(accs):
            bot.send_message(message.chat.id, "❌ Аккаунт не найден.")
            return
        plain = message.text.strip()
        accs[idx]["password"] = _encrypt_password(plain)
        save_accs(accs)
        _send_acc_card(message.chat.id, idx,
                       prefix="✅ Аккаунт добавлен, пароль зашифрован.")

    # ── helpers: build account card & lots card ──

    def _acc_card(idx):
        """Build (text, keyboard) for account edit view."""
        accs = accounts()
        if idx >= len(accs):
            return None, None
        acc = accs[idx]
        h = health_state().get(acc["email"], {})
        h_ok   = h.get("ok", True)
        h_str  = "✅ ok" if h_ok else f"❌ {h.get('message', '?')}"
        last   = h.get("last_check")
        l_str  = _fmt_time(last) if last else "—"
        lots   = acc.get("lot_ids", [])
        ctype  = acc.get("code_type", "alnum")
        clen   = acc.get("code_len", 0)
        ihost  = acc.get("imap_host") or detect_imap_host(acc["email"])

        text = (
            f"📧 {acc['email']}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🩺 Health: {h_str}\n"
            f"    проверено: {l_str}\n"
            f"🔌 IMAP: {ihost}\n"
            f"📦 Лоты: {', '.join(lots) if lots else 'все'}\n"
            f"🔤 Тип кода: {CODE_TYPE_RU.get(ctype, ctype)}\n"
            f"🔢 Длина кода: {clen or 'авто'}\n"
            f"📬 От: {acc.get('filter_from') or '—'}\n"
            f"📌 Тема: {acc.get('filter_subj') or '—'}\n"
            f"🔐 Пароль: {'зашифрован ✅' if acc.get('password') else 'не задан ❌'}"
        )
        kb = K(keyboard=[
            [B("🔌 IMAP хост",      callback_data=f"{AC_IMAP}:{idx}"),
             B("📦 Лоты",           callback_data=f"{AC_LOTS}:{idx}")],
            [B(f"🔢 Длина: {clen or 'авто'}", callback_data=f"{AC_LEN}:{idx}"),
             B(f"🔤 {CODE_TYPE_RU.get(ctype, ctype)}", callback_data=f"{AC_TYPE}:{idx}")],
            [B("📬 От (from)",      callback_data=f"{AC_FROM}:{idx}"),
             B("📌 Тема",           callback_data=f"{AC_SUBJ}:{idx}")],
            [B("🔑 Пароль",         callback_data=f"ac_chpass:{idx}"),
             B("🔍 Тест IMAP",      callback_data=f"{AC_TEST}:{idx}")],
            [B("🗑 Удалить",        callback_data=f"{AC_DEL_ASK}:{idx}")],
            [B("◀ Назад",           callback_data=f"{AC_LIST}:0")],
        ])
        return text, kb

    def _lots_card(idx):
        """Build (text, keyboard) for lots view."""
        accs = accounts()
        if idx >= len(accs):
            return None, None
        acc  = accs[idx]
        lots = acc.get("lot_ids", [])
        if lots:
            lines = [f"📦 Лоты для {acc['email']}:"]
            for i, l in enumerate(lots, 1):
                lines.append(f"  {i}. {l}")
        else:
            lines = [f"📦 Лоты для {acc['email']}:\n  (все лоты — без фильтра)"]
        rows = [[B(f"🗑 {l}", callback_data=f"{AC_LOT_DEL}:{idx}:{l}")] for l in lots]
        rows.append([B("➕ Добавить лот", callback_data=f"{AC_LOT_ADD}:{idx}")])
        rows.append([B("◀ Назад",        callback_data=f"{AC_EDIT}:{idx}")])
        return "\n".join(lines), K(keyboard=rows)

    def _send_acc_card(chat_id, idx, prefix=""):
        """Send account card as a new message (after text-input saves)."""
        text, kb = _acc_card(idx)
        if text is None:
            bot.send_message(chat_id, "❌ Аккаунт не найден.")
            return
        bot.send_message(chat_id, (prefix + "\n\n" + text) if prefix else text,
                         reply_markup=kb)

    # ── account edit view ──

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_EDIT}:"))
    def open_edit(call: CallbackQuery):
        idx = int(call.data.split(":")[1])
        text, kb = _acc_card(idx)
        if text is None:
            _safe_answer(bot, call, "Аккаунт не найден.")
            return
        _safe_edit(bot, call, text, kb)

    # ── password ──

    @bot.callback_query_handler(func=lambda c: c.data.startswith("ac_chpass:"))
    def act_chpass(call: CallbackQuery):
        idx = int(call.data.split(":")[1])
        msg = bot.send_message(call.message.chat.id, "🔑 Введите новый пароль:")
        bot.register_next_step_handler(msg, lambda m, _i=idx: _do_chpass(m, _i))

    def _do_chpass(message: Message, idx: int):
        accs = accounts()
        if idx >= len(accs):
            bot.send_message(message.chat.id, "❌ Аккаунт не найден.")
            return
        accs[idx]["password"] = _encrypt_password(message.text.strip())
        save_accs(accs)
        _send_acc_card(message.chat.id, idx, prefix="✅ Пароль обновлён (зашифрован).")

    # ── IMAP host ──

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_IMAP}:"))
    def act_imap(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
        if idx >= len(accs):
            _safe_answer(bot, call, "Аккаунт не найден.")
            return
        auto = detect_imap_host(accs[idx]["email"])
        cur  = accs[idx].get("imap_host", auto)
        msg  = bot.send_message(call.message.chat.id,
            f"🔌 Текущий IMAP: {cur}\n"
            f"Авто-определение: {auto}\n\n"
            f"Введите новый хост или «авто»:")
        bot.register_next_step_handler(msg, lambda m, _i=idx: _save_imap(m, _i))

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
        _send_acc_card(message.chat.id, idx,
                       prefix=f"✅ IMAP хост: {accs[idx]['imap_host']}")

    # ── IMAP test ──

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_TEST}:"))
    def do_test(call: CallbackQuery):
        idx    = int(call.data.split(":")[1])
        accs   = accounts()
        if idx >= len(accs):
            _safe_answer(bot, call, "Аккаунт не найден.", show_alert=True)
            return
        _safe_answer(bot, call, "⏳ Проверяю IMAP...")

        def _run_test():
            result = test_imap(accs[idx])
            h = health_state()
            h[accs[idx]["email"]] = {
                "last_check": time.time(),
                "ok":         result.startswith("✅"),
                "message":    result,
            }
            save_health(h)
            # Refresh the card with updated health
            text, kb = _acc_card(idx)
            if text:
                bot.send_message(call.message.chat.id,
                    f"🔍 Результат: {result}\n\n{text}", reply_markup=kb)

        Thread(target=_run_test, daemon=True).start()

    # ── delete ──

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_DEL_ASK}:"))
    def ask_del(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
        email = accs[idx]["email"] if idx < len(accs) else "?"
        kb  = K(keyboard=[
            [B("✅ Да, удалить", callback_data=f"{AC_DEL_OK}:{idx}"),
             B("❌ Отмена",      callback_data=f"{AC_EDIT}:{idx}")],
        ])
        _safe_edit(bot, call, f"🗑 Удалить аккаунт {email}?", kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_DEL_OK}:"))
    def do_del(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
        if idx < len(accs):
            email = accs[idx]["email"]
            accs.pop(idx)
            save_accs(accs)
            _safe_edit(bot, call, f"🗑 Аккаунт {email} удалён.", kb_main())
        else:
            _safe_edit(bot, call, "🗑 Удалён.", kb_main())

    # ── code length ──

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_LEN}:"))
    def act_len(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
        cur  = accs[idx].get("code_len", 0) if idx < len(accs) else 0
        msg  = bot.send_message(call.message.chat.id,
            f"🔢 Текущая длина кода: {cur or 'авто'}\n"
            f"Введите новую длину (0 = авто):")
        bot.register_next_step_handler(msg, lambda m, _i=idx: _save_code_len(m, _i))

    def _save_code_len(message: Message, idx: int):
        try:
            val = int(message.text.strip())
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите число.")
            return
        accs = accounts()
        if idx >= len(accs):
            bot.send_message(message.chat.id, "❌ Аккаунт не найден.")
            return
        accs[idx]["code_len"] = val
        save_accs(accs)
        _send_acc_card(message.chat.id, idx,
                       prefix=f"✅ Длина кода: {val or 'авто'}")

    # ── code type (inline toggle) ──

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_TYPE}:"))
    def act_type(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
        if idx >= len(accs):
            _safe_answer(bot, call, "Аккаунт не найден.")
            return
        types = ["alnum", "digits", "alpha", "alnum-dash"]
        cur   = accs[idx].get("code_type", "alnum")
        nxt   = types[(types.index(cur) + 1) % len(types)] if cur in types else "alnum"
        accs[idx]["code_type"] = nxt
        save_accs(accs)
        # Refresh card inline so the change is visible immediately
        text, kb = _acc_card(idx)
        if text:
            _safe_edit(bot, call, text, kb)
        _safe_answer(bot, call, f"Тип кода → {CODE_TYPE_RU.get(nxt, nxt)}")

    # ── filter: from ──

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_FROM}:"))
    def act_from(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
        cur  = accs[idx].get("filter_from", "") if idx < len(accs) else ""
        msg  = bot.send_message(call.message.chat.id,
            f"📬 Текущий фильтр «От»: {cur or '—'}\n"
            f"Введите email отправителя (или «—» для сброса):")
        bot.register_next_step_handler(msg, lambda m, _i=idx: _save_from(m, _i))

    def _save_from(message: Message, idx: int):
        accs = accounts()
        if idx >= len(accs):
            bot.send_message(message.chat.id, "❌ Аккаунт не найден.")
            return
        val = message.text.strip()
        accs[idx]["filter_from"] = "" if val in ("—", "-", "") else val
        save_accs(accs)
        _send_acc_card(message.chat.id, idx,
                       prefix=f"✅ Фильтр «От»: {accs[idx]['filter_from'] or '—'}")

    # ── filter: subject ──

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_SUBJ}:"))
    def act_subj(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
        cur  = accs[idx].get("filter_subj", "") if idx < len(accs) else ""
        msg  = bot.send_message(call.message.chat.id,
            f"📌 Текущий фильтр по теме: {cur or '—'}\n"
            f"Введите тему (или «—» для сброса):")
        bot.register_next_step_handler(msg, lambda m, _i=idx: _save_subj(m, _i))

    def _save_subj(message: Message, idx: int):
        accs = accounts()
        if idx >= len(accs):
            bot.send_message(message.chat.id, "❌ Аккаунт не найден.")
            return
        val = message.text.strip()
        accs[idx]["filter_subj"] = "" if val in ("—", "-", "") else val
        save_accs(accs)
        _send_acc_card(message.chat.id, idx,
                       prefix=f"✅ Фильтр по теме: {accs[idx]['filter_subj'] or '—'}")

    # ── lots management ──

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_LOTS}:"))
    def open_lots(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        text, kb = _lots_card(idx)
        if text is None:
            _safe_answer(bot, call, "Аккаунт не найден.")
            return
        _safe_edit(bot, call, text, kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_LOT_ADD}:"))
    def act_lot_add(call: CallbackQuery):
        idx = int(call.data.split(":")[1])
        msg = bot.send_message(call.message.chat.id, "📦 Введите ID лота:")
        bot.register_next_step_handler(msg, lambda m, _i=idx: _do_lot_add(m, _i))

    def _do_lot_add(message: Message, idx: int):
        accs = accounts()
        if idx >= len(accs):
            bot.send_message(message.chat.id, "❌ Аккаунт не найден.")
            return
        lot_id = message.text.strip()
        accs[idx].setdefault("lot_ids", []).append(lot_id)
        save_accs(accs)
        # Send refreshed lots card
        text, kb = _lots_card(idx)
        bot.send_message(message.chat.id,
            f"✅ Лот {lot_id} добавлен.\n\n{text}", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_LOT_DEL}:"))
    def act_lot_del(call: CallbackQuery):
        parts = call.data.split(":")
        idx, lot = int(parts[1]), parts[2]
        accs = accounts()
        if idx >= len(accs):
            _safe_answer(bot, call, "Аккаунт не найден.")
            return
        accs[idx]["lot_ids"] = [l for l in accs[idx].get("lot_ids", []) if l != lot]
        save_accs(accs)
        # Refresh lots list inline
        text, kb = _lots_card(idx)
        if text:
            _safe_edit(bot, call, text, kb)
        _safe_answer(bot, call, f"🗑 Лот {lot} удалён.")

    @bot.callback_query_handler(func=lambda c: c.data == AC_LOG)
    def open_log(call: CallbackQuery):
        entries = log_entries()[-30:]
        if not entries:
            text = "Лог пуст."
        else:
            lines = []
            for e in reversed(entries):
                ts = _safe_ts(e.get("time", 0))
                lines.append(f"{_fmt_time(ts)} | {e.get('buyer', '?')} | {e.get('code', '?')}")
            text = "📜 Последние выдачи:\n\n" + "\n".join(lines)
        _safe_edit(bot, call, text,
            K(keyboard=[[B("◀ Назад", callback_data=AC_MAIN)]]))

    @bot.callback_query_handler(func=lambda c: c.data == AC_RENTALS or c.data.startswith(f"{AC_RENT_PAGE}:"))
    def open_rentals(call: CallbackQuery):
        try:
            page = 0
            try:
                page = int(call.data.split(":")[1]) if ":" in call.data else 0
            except Exception:
                pass

            active = sorted(get_active_rentals(), key=lambda r: _safe_ts(r.get("expires_at", 0)))
            total  = len(active)

            if total == 0:
                _safe_edit(bot, call, "🏠 Активные аренды\n\nНет активных аренд.",
                    K(keyboard=[[B("🔄 Обновить", callback_data=f"{AC_RENT_PAGE}:0"),
                                 B("◀ Назад",   callback_data=AC_MAIN)]]))
                return

            pages_total = (total + RENTALS_PAGE_SIZE - 1) // RENTALS_PAGE_SIZE
            page = max(0, min(page, pages_total - 1))
            start = page * RENTALS_PAGE_SIZE
            chunk = active[start:start + RENTALS_PAGE_SIZE]

            now_ts = time.time()
            lines = [f"🏠 Активные аренды — стр. {page + 1}/{pages_total}",
                     f"Всего: {total}\n"]
            rows  = []
            for r in chunk:
                buyer    = (r.get("buyer") or "—")[:18]
                remain   = _fmt_remaining_compact(r.get("expires_at", 0))
                warn     = "⚠️" if not r.get("chat_id") else ""
                left_h = (_safe_ts(r.get("expires_at", 0)) - now_ts) / 3600
                if left_h > 24:
                    color = "🟢"
                elif left_h > 12:
                    color = "🟡"
                elif left_h > 2:
                    color = "🟠"
                else:
                    color = "🔴"
                lines.append(f"{color} {buyer} • ⏳{remain} {warn}")
                order_key = r.get("order_key") or "?"
                rows.append([B(f"{color} {buyer}", callback_data=f"{AC_RENT_INFO}:{order_key}")])

            nav = []
            if page > 0:
                nav.append(B("⏮", callback_data=f"{AC_RENT_PAGE}:0"))
                nav.append(B("◀", callback_data=f"{AC_RENT_PAGE}:{page - 1}"))
            nav.append(B(f"🔄 {page + 1}/{pages_total}", callback_data=f"{AC_RENT_PAGE}:{page}"))
            if page < pages_total - 1:
                nav.append(B("▶", callback_data=f"{AC_RENT_PAGE}:{page + 1}"))
                nav.append(B("⏭", callback_data=f"{AC_RENT_PAGE}:{pages_total - 1}"))
            if nav:
                rows.append(nav)

            rows.append([B("◀ Назад в меню", callback_data=AC_MAIN)])

            lines.append("\n🟢 >24ч  🟡 12-24ч  🟠 2-12ч  🔴 <2ч")
            lines.append("⚠️ = без chat_id")
            _safe_edit(bot, call, "\n".join(lines), K(keyboard=rows))
        except Exception as exc:
            logger.error(f"AutoCode open_rentals CRASH: {exc}", exc_info=True)
            try:
                _safe_edit(bot, call, f"⚠️ Ошибка загрузки аренд:\n{exc}",
                    K(keyboard=[[B("🔄 Обновить", callback_data=f"{AC_RENT_PAGE}:0"),
                                 B("◀ Назад", callback_data=AC_MAIN)]]))
            except Exception:
                pass

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_RENT_INFO}:"))
    def rent_info(call: CallbackQuery):
        try:
            key = call.data.split(":", 1)[1]
            rents = rentals()
            r = rents.get(key)
            if not r:
                _safe_answer(bot, call, "Аренда не найдена.")
                return
            text = (
                f"🏠 Аренда {r.get('buyer', '—')}\n\n"
                f"📧 Почта: {r.get('email', '—')}\n"
                f"📦 Лот: {r.get('lot_id', '—')}\n"
                f"🌐 Язык: {r.get('lang', 'ru')}\n"
                f"💬 chat_id: {r.get('chat_id') or '⚠️ нет'}\n"
                f"🕐 Куплено: {_fmt_time(r.get('purchase_ts', 0))}\n"
                f"⏰ Окончание: {_fmt_time(r.get('expires_at', 0))}\n"
                f"⏳ Осталось: {_fmt_remaining(r.get('expires_at', 0))}\n"
            )
            kb = K(keyboard=[
                [B("➕ 1ч",  callback_data=f"{AC_RENT_EXT}:{key}:1"),
                 B("➕ 6ч",  callback_data=f"{AC_RENT_EXT}:{key}:6"),
                 B("➕ 24ч", callback_data=f"{AC_RENT_EXT}:{key}:24")],
                [B("🗑 Удалить",  callback_data=f"{AC_RENT_DEL}:{key}")],
                [B("◀ К списку", callback_data=f"{AC_RENT_PAGE}:0")],
            ])
            _safe_edit(bot, call, text, kb)
        except Exception as exc:
            logger.error(f"AutoCode rent_info CRASH: {exc}", exc_info=True)
            _safe_answer(bot, call, f"Ошибка: {exc}", show_alert=True)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_RENT_DEL}:"))
    def do_rent_del(call: CallbackQuery):
        try:
            key   = call.data.split(":", 1)[1]
            rents = rentals()
            rents.pop(key, None)
            save_rentals(rents)
            _safe_answer(bot, call, "Аренда удалена.")
            call.data = f"{AC_RENT_PAGE}:0"
            open_rentals(call)
        except Exception as exc:
            logger.error(f"AutoCode do_rent_del CRASH: {exc}", exc_info=True)
            _safe_answer(bot, call, f"Ошибка: {exc}", show_alert=True)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_RENT_EXT}:"))
    def do_rent_ext(call: CallbackQuery):
        try:
            parts = call.data.split(":")
            key, hours = parts[1], int(parts[2])
            rents = rentals()
            if key in rents:
                rents[key]["expires_at"] = _safe_ts(rents[key].get("expires_at", 0)) + hours * 3600
                rents[key]["hours"] = rents[key].get("hours", 0) + hours
                save_rentals(rents)
                state = warned_state()
                for s_key in ("warned_12h",):
                    if key in state.get(s_key, []):
                        state[s_key].remove(key)
                save_warned(state)
            _safe_answer(bot, call, f"Продлено на {hours}ч.")
            call.data = f"{AC_RENT_INFO}:{key}"
            rent_info(call)
        except Exception as exc:
            logger.error(f"AutoCode do_rent_ext CRASH: {exc}", exc_info=True)
            _safe_answer(bot, call, f"Ошибка: {exc}", show_alert=True)

    @bot.callback_query_handler(func=lambda c: c.data == AC_STATS)
    def open_stats(call: CallbackQuery):
        _safe_edit(bot, call, "📊 Выберите период:", kb_stats_period())

    @bot.callback_query_handler(func=lambda c: c.data in (
        AC_STATS_24H, AC_STATS_48H, AC_STATS_7D, AC_STATS_ALL
    ))
    def open_stats_period(call: CallbackQuery):
        try:
            mapping = {
                AC_STATS_24H: (24,   "24 часа"),
                AC_STATS_48H: (48,   "48 часов"),
                AC_STATS_7D:  (168,  "7 дней"),
                AC_STATS_ALL: (None, "Всё время"),
            }
            hours, label = mapping[call.data]
            entries = _filter_log_by_hours(hours)
            text = _stats_text(entries, label) if entries else (
                f"📊 Статистика — {label}\n\nДанных за этот период нет."
            )
            kb = K(keyboard=[
                [B("⏱ 24ч", callback_data=AC_STATS_24H),
                 B("⏱ 48ч", callback_data=AC_STATS_48H)],
                [B("📅 7д",  callback_data=AC_STATS_7D),
                 B("📋 Всё", callback_data=AC_STATS_ALL)],
                [B("◀ Назад", callback_data=AC_STATS)],
            ])
            _safe_edit(bot, call, text, kb)
        except Exception as e:
            logger.error(f"Stats button error: {e}")
            _safe_answer(bot, call, f"Ошибка: {e}", show_alert=True)

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
            text = _stats_text(entries, label) if entries else (
                f"📊 Статистика — {label}\n\nДанных за этот период нет."
            )
            bot.send_message(message.chat.id, text,
                reply_markup=K(keyboard=[[B("◀ Назад", callback_data=AC_STATS)]]))
        except Exception as ex:
            bot.send_message(message.chat.id,
                f"❌ Ошибка: {ex}\nПример: <code>01.05.2026-31.05.2026</code>",
                parse_mode="HTML")

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
        _safe_edit(bot, call, text, K(keyboard=[
            [B("🔄 Проверить сейчас", callback_data="ac_health_now")],
            [B("◀ Назад", callback_data=AC_MAIN)],
        ]))

    @bot.callback_query_handler(func=lambda c: c.data == "ac_health_now")
    def health_now(call: CallbackQuery):
        _safe_answer(bot, call, "Проверяю...")
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

    @bot.callback_query_handler(func=lambda c: c.data == AC_REVIEW)
    def open_review(call: CallbackQuery):
        s = app_settings()
        text = (
            f"⚙️ Настройки\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 Запрос отзыва: {'✅ вкл' if s.get('review_enabled', True) else '❌ выкл'}\n"
            f"   {s.get('review_template', '')[:80]}\n\n"
            f"✅ Подтверждение заказа: {'✅ вкл' if s.get('confirm_enabled', True) else '❌ выкл'}\n"
            f"   Через 5 мин после кода\n\n"
            f"📋 FAQ доп. текст: {s.get('faq_custom_ru', '')[:60] or '(пусто)'}"
        )
        kb = K(keyboard=[
            [B("📝 Вкл/выкл отзывы",      callback_data="ac_rev_toggle"),
             B("✏️ Шаблон отзыва",         callback_data="ac_rev_tpl")],
            [B("✅ Вкл/выкл подтверждение", callback_data="ac_confirm_toggle")],
            [B("📋 FAQ доп. текст",         callback_data="ac_faq_tpl_ru")],
            [B("📝 Все тексты сообщений",   callback_data="ac_texts_menu")],
            [B("◀ Назад", callback_data=AC_MAIN)],
        ])
        _safe_edit(bot, call, text, kb)

    @bot.callback_query_handler(func=lambda c: c.data == "ac_rev_toggle")
    def rev_toggle(call: CallbackQuery):
        s = app_settings()
        s["review_enabled"] = not s.get("review_enabled", True)
        save_settings(s)
        _safe_answer(bot, call, f"Отзывы: {'вкл' if s['review_enabled'] else 'выкл'}")
        open_review(call)

    @bot.callback_query_handler(func=lambda c: c.data == "ac_rev_tpl")
    def rev_tpl(call: CallbackQuery):
        msg = bot.send_message(call.message.chat.id,
            f"Текущий шаблон отзыва:\n{app_settings().get('review_template', '')}\n\nВведите новый:")
        bot.register_next_step_handler(msg, _save_review_tpl)

    def _save_review_tpl(message: Message):
        s = app_settings()
        s["review_template"] = message.text.strip()
        save_settings(s)
        bot.send_message(message.chat.id, "✅ Шаблон отзыва сохранён.")



    @bot.callback_query_handler(func=lambda c: c.data == "ac_confirm_toggle")
    def confirm_toggle(call: CallbackQuery):
        s = app_settings()
        s["confirm_enabled"] = not s.get("confirm_enabled", True)
        save_settings(s)
        _safe_answer(bot, call, f"Подтверждение заказа: {'вкл' if s['confirm_enabled'] else 'выкл'}")
        open_review(call)

    @bot.callback_query_handler(func=lambda c: c.data == "ac_faq_tpl_ru")
    def faq_tpl_ru(call: CallbackQuery):
        cur = app_settings().get("faq_custom_ru", "")
        msg = bot.send_message(call.message.chat.id,
            f"📋 Доп. текст FAQ (добавляется после списка команд):\n"
            f"{cur or '(пусто)'}\n\nВведите новый (или «—» для очистки):")
        bot.register_next_step_handler(msg, _save_faq_ru)

    def _save_faq_ru(message: Message):
        s = app_settings()
        val = message.text.strip()
        s["faq_custom_ru"] = "" if val in ("—", "-") else val
        save_settings(s)
        bot.send_message(message.chat.id, "✅ FAQ текст сохранён.")

    # ── Editable texts menu (all buyer-facing messages) ──

    _TEXTS_PAGE_SIZE = 6

    @bot.callback_query_handler(func=lambda c: c.data.startswith("ac_texts_menu"))
    def texts_menu(call: CallbackQuery):
        parts = call.data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 0
        items = list(_EDITABLE_TEXTS.items())
        total_pages = (len(items) + _TEXTS_PAGE_SIZE - 1) // _TEXTS_PAGE_SIZE
        start = page * _TEXTS_PAGE_SIZE
        page_items = items[start:start + _TEXTS_PAGE_SIZE]
        s = app_settings()
        overrides = s.get("texts", {})
        lines = ["📝 Все тексты сообщений\n"]
        for display_name, key in page_items:
            is_custom = "✏️" if key in overrides else "📄"
            lines.append(f"{is_custom} {display_name}")
        text = "\n".join(lines)
        text += f"\n\nСтр. {page + 1}/{total_pages}  |  ✏️=изменён  📄=стандарт"
        buttons = []
        for display_name, key in page_items:
            buttons.append([B(f"✏️ {display_name}", callback_data=f"ac_txt_edit:{key}")])
        nav = []
        if page > 0:
            nav.append(B("◀ Назад", callback_data=f"ac_texts_menu:{page - 1}"))
        if page < total_pages - 1:
            nav.append(B("Вперёд ▶", callback_data=f"ac_texts_menu:{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([B("🔄 Сбросить все", callback_data="ac_txt_reset_all"),
                        B("◀ Назад", callback_data=AC_REVIEW)])
        _safe_edit(bot, call, text, K(keyboard=buttons))

    @bot.callback_query_handler(func=lambda c: c.data.startswith("ac_txt_edit:"))
    def txt_edit(call: CallbackQuery):
        key = call.data.split(":", 1)[1]
        s = app_settings()
        overrides = s.get("texts", {})
        current = overrides.get(key) or L_DEFAULTS.get(key, "")
        display_name = next((dn for dn, k in _EDITABLE_TEXTS.items() if k == key), key)
        msg = bot.send_message(call.message.chat.id,
            f"✏️ {display_name}\n\n"
            f"Текущий текст:\n{current}\n\n"
            f"Переменные (оставляйте как есть): " + ", ".join(
                f"{{{v}}}" for v in re.findall(r"\{(\w+)\}", current)
            ) + "\n\nВведите новый текст (или «—» для сброса к стандарту):")
        bot.register_next_step_handler(msg, lambda m, _k=key: _save_txt(m, _k))

    def _save_txt(message: Message, key: str):
        s = app_settings()
        val = message.text.strip()
        if val in ("—", "-"):
            s.setdefault("texts", {}).pop(key, None)
            save_settings(s)
            bot.send_message(message.chat.id, f"✅ Сброшено к стандарту.")
        else:
            s.setdefault("texts", {})[key] = val
            save_settings(s)
            display_name = next((dn for dn, k in _EDITABLE_TEXTS.items() if k == key), key)
            bot.send_message(message.chat.id, f"✅ {display_name} — сохранён.")

    @bot.callback_query_handler(func=lambda c: c.data == "ac_txt_reset_all")
    def txt_reset_all(call: CallbackQuery):
        s = app_settings()
        s["texts"] = {}
        save_settings(s)
        _safe_answer(bot, call, "Все тексты сброшены к стандарту.")
        texts_menu(call)

    @bot.callback_query_handler(func=lambda c: c.data == AC_BROADCAST)
    def open_broadcast(call: CallbackQuery):
        active = get_active_rentals()
        with_chat = sum(1 for r in active if r.get("chat_id"))
        text = (f"📢 Рассылка\n"
                f"Активных аренд: <b>{len(active)}</b>\n"
                f"Получат рассылку (с chat_id): <b>{with_chat}</b>")
        _safe_edit(bot, call, text, kb_broadcast_menu(), parse_mode="HTML")

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
        failed = _bcast.get("failed", [])
        retry_text = _bcast.get("retry_text", _bcast.get("text", ""))
        if not failed or not retry_text:
            _safe_answer(bot, call, "Нет неудачных отправок.")
            return
        _bcast["text"] = retry_text
        _do_broadcast(call, failed, is_retry=True)

    def _do_broadcast(call, targets, is_retry=False):
        text = _bcast.get("text", "")
        if not text:
            _safe_answer(bot, call, "Текст не задан.")
            return
        if not targets:
            _safe_answer(bot, call, "Нет получателей.")
            return
        _safe_answer(bot, call, "Рассылка запущена...")

        snapshot_text = text

        if is_retry:
            pairs = targets
        else:
            pairs = [(r["chat_id"], r.get("chat_name", "")) for r in targets]

        progress_msg = bot.send_message(call.message.chat.id,
            f"📢 Рассылка: 0/{len(pairs)}...")

        def _send_all():
            ok, fail = 0, []
            total = len(pairs)
            last_progress = time.time()

            for i, (chat_id, chat_name) in enumerate(pairs, 1):
                if _shutdown_flag["stop"]:
                    break
                success, err = _send_with_retry(cardinal, chat_id, snapshot_text, chat_name)
                if success:
                    ok += 1
                else:
                    fail.append((chat_id, chat_name))
                    logger.warning(f"Broadcast permanent fail {chat_id} after {BROADCAST_RETRY_MAX} attempts: {err}")

                if i % 5 == 0 or (time.time() - last_progress) > 10:
                    try:
                        bot.edit_message_text(
                            f"📢 Рассылка: {i}/{total}\n✅ {ok} | ❌ {len(fail)}",
                            progress_msg.chat.id,
                            progress_msg.message_id,
                        )
                    except Exception:
                        pass
                    last_progress = time.time()

                time.sleep(BROADCAST_COOLDOWN_SEC)

            _bcast["failed"] = fail
            _bcast["retry_text"] = snapshot_text

            hist = bcast_history()
            hist.append({
                "time":   time.time(),
                "text":   snapshot_text,
                "sent":   ok,
                "failed": len(fail),
            })
            save_bcast_history(hist[-50:])

            kb_rows = []
            if fail:
                kb_rows.append([B(f"🔄 Повторить ({len(fail)})", callback_data=AC_BCAST_RETRY)])
            kb_rows.append([B("◀ Меню рассылки", callback_data=AC_BROADCAST)])

            try:
                bot.edit_message_text(
                    f"📢 Рассылка завершена\n\n"
                    f"✅ Отправлено: {ok}/{total}\n"
                    f"❌ Не доставлено: {len(fail)}\n"
                    f"⏱ Каждое с {BROADCAST_COOLDOWN_SEC}с задержкой + до {BROADCAST_RETRY_MAX} попыток",
                    progress_msg.chat.id,
                    progress_msg.message_id,
                    reply_markup=K(keyboard=kb_rows),
                )
            except Exception:
                bot.send_message(call.message.chat.id,
                    f"📢 Рассылка завершена\n\n✅ Отправлено: {ok}/{total}\n❌ Не доставлено: {len(fail)}",
                    reply_markup=K(keyboard=kb_rows))

        Thread(target=_send_all, daemon=True).start()

    @bot.callback_query_handler(func=lambda c: c.data == AC_BCAST_TPLS)
    def open_templates(call: CallbackQuery):
        tpls = templates()
        if not tpls:
            _safe_edit(bot, call, "Шаблонов нет.",
                K(keyboard=[
                    [B("➕ Добавить", callback_data=AC_BCAST_TPL_ADD)],
                    [B("◀ Назад",    callback_data=AC_BROADCAST)],
                ]))
            return
        _safe_edit(bot, call, "📁 Шаблоны:", kb_templates(tpls))

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
            _safe_answer(bot, call, "Шаблон не найден.")
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
        _safe_answer(bot, call, "Шаблон удалён.")
        tpls_new = templates()
        if not tpls_new:
            _safe_edit(bot, call, "Шаблонов нет.",
                K(keyboard=[
                    [B("➕ Добавить", callback_data=AC_BCAST_TPL_ADD)],
                    [B("◀ Назад",    callback_data=AC_BROADCAST)],
                ]))
        else:
            _safe_edit(bot, call, "📁 Шаблоны:", kb_templates(tpls_new))

    @bot.callback_query_handler(func=lambda c: c.data == AC_BCAST_HIST)
    def open_bcast_hist(call: CallbackQuery):
        hist = bcast_history()[-20:]
        if not hist:
            text = "История рассылок пуста."
        else:
            lines = []
            for h in reversed(hist):
                ts = _safe_ts(h.get("time", 0))
                lines.append(
                    f"{_fmt_time(ts)} | ✅{h.get('sent', 0)} ❌{h.get('failed', 0)}\n"
                    f"  {h.get('text', '')[:60]}{'...' if len(h.get('text', '')) > 60 else ''}"
                )
            text = "📋 История рассылок:\n\n" + "\n\n".join(lines)
        _safe_edit(bot, call, text,
            K(keyboard=[[B("◀ Назад", callback_data=AC_BROADCAST)]]))

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

            scheduled_text = _bcast["text"]

            def _fire():
                targets = [r for r in get_active_rentals() if r.get("chat_id")]
                ok, fail = 0, []
                for r in targets:
                    if _shutdown_flag["stop"]:
                        break
                    success, _ = _send_with_retry(cardinal, r["chat_id"], scheduled_text, r.get("chat_name", ""))
                    if success:
                        ok += 1
                    else:
                        fail.append((r["chat_id"], r.get("chat_name", "")))
                    time.sleep(BROADCAST_COOLDOWN_SEC)
                hist = bcast_history()
                hist.append({"time": time.time(), "text": scheduled_text,
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
                f"Текст: {scheduled_text[:80]}")
        except ValueError:
            bot.send_message(message.chat.id,
                "❌ Неверный формат. Пример: <code>31.05.2026 18:00</code>", parse_mode="HTML")

    Thread(target=_expiry_watcher,           args=(cardinal,), daemon=True).start()
    Thread(target=_startup_imap_test,        args=(cardinal,), daemon=True).start()
    Thread(target=_startup_sales_scan,       args=(cardinal,), daemon=True).start()
    Thread(target=_used_code_cleanup_worker,                   daemon=True).start()
    Thread(target=_weekly_report_worker,     args=(cardinal,), daemon=True).start()
    Thread(target=_imap_health_worker,       args=(cardinal,), daemon=True).start()
    Thread(target=_backup_worker,                              daemon=True).start()
    logger.info(f"AutoCode v{VERSION} инициализирован.")




BIND_TO_PRE_INIT             = [init_autocode_tg]
BIND_TO_NEW_ORDER            = [on_new_order]
BIND_TO_NEW_MESSAGE          = [on_new_message]
BIND_TO_ORDER_STATUS_CHANGED = [on_order_status_changed]
