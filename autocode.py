from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

import base64
import email
import email.header
import imaplib
import json
import logging
import os
import re
import time
import uuid as uuid_lib
from collections import Counter
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from threading import Thread, Timer

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
VERSION = "5.1.1"
UUID = str(uuid_lib.UUID("b7e21f3a-4c8d-4e2b-9a1f-3c5d6e7f8b9a"))
DESCRIPTION = (
    "Авто-выдача кодов с IMAP-почт по команде !cd / code.\n"
    "Рассылка, аналитика, шаблоны, история рассылок, лимиты, авто-продление аренды.\n"
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

# ── Storage paths ───────────────────────────────────────────────────────────
DATA_DIR        = "storage/autocode"
ACCOUNTS_FILE   = os.path.join(DATA_DIR, "accounts.json")
LOG_FILE        = os.path.join(DATA_DIR, "log.json")
USED_FILE       = os.path.join(DATA_DIR, "used_codes.json")
RENTALS_FILE    = os.path.join(DATA_DIR, "rentals.json")
TEMPLATES_FILE  = os.path.join(DATA_DIR, "templates.json")
BCAST_HIST_FILE = os.path.join(DATA_DIR, "broadcast_history.json")

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

# ── XOR password encryption ─────────────────────────────────────────────────
_SECRET_KEY = (os.environ.get("AC_SECRET") or "ac_fp_secret_2025").encode()

def _xor_crypt(data: str) -> str:
    raw   = data.encode("utf-8")
    key   = _SECRET_KEY
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
    return base64.b64encode(xored).decode()

def _encrypt_password(plain: str) -> str:
    return _xor_crypt(plain)

def _decrypt_password(enc: str) -> str:
    try:
        raw   = base64.b64decode(enc.encode())
        key   = _SECRET_KEY
        xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
        return xored.decode("utf-8")
    except Exception:
        return enc

_cardinal_ref = None

# ── Storage helpers ──────────────────────────────────────────────────────────
def _ensure():
    os.makedirs(DATA_DIR, exist_ok=True)

def _load(path, default):
    _ensure()
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save(path, data):
    _ensure()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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

def add_log(email_addr, lot_id, buyer, code):
    entries = log_entries()
    entries.append({
        "time":   time.time(),
        "email":  email_addr,
        "lot_id": lot_id,
        "buyer":  buyer,
        "code":   code,
    })
    _save(LOG_FILE, entries[-200:])

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

def _check_rate(buyer: str) -> tuple[bool, str]:
    now = time.time()
    last = _last_code_ts.get(buyer, 0)
    if now - last < CODE_CD:
        wait = int(CODE_CD - (now - last))
        return False, f"⏳ Подождите {wait} сек. перед повторным запросом."
    hour_ago = now - 3600
    reqs = [t for t in _code_requests.get(buyer, []) if t > hour_ago]
    _code_requests[buyer] = reqs
    if len(reqs) >= MAX_CODES_HOUR:
        return False, f"❌ Превышен лимит ({MAX_CODES_HOUR} запросов/час). Обратитесь к продавцу."
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
    for i, t in enumerate(tpls):
        rows.append([
            B(f"📄 {t['name']}", callback_data=f"{AC_BCAST_TPL_USE}:{i}"),
            B("🗑",              callback_data=f"{AC_BCAST_TPL_DEL}:{i}"),
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
    for acc in accs:
        result = test_imap(acc)
        if result.startswith("❌"):
            broken.append(f"📧 {acc['email']}\n   {result}")
    if broken:
        msg = "🚨 AutoCode — проблемы с IMAP при старте:\n\n" + "\n\n".join(broken)
        _notify_tg(cardinal, msg)
        logger.warning(f"AutoCode startup IMAP errors: {broken}")
    else:
        logger.info(f"AutoCode: все {len(accs)} IMAP-аккаунтов прошли проверку при старте.")

# ── Used-code cleanup (TTL 3h) ───────────────────────────────────────────────
def _used_code_cleanup_worker():
    while True:
        time.sleep(3600)
        try:
            now  = time.time()
            used = used_codes()
            changed = False
            for email_addr, entries in used.items():
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
                used[email_addr] = new_entries
            if changed:
                save_used(used)
                logger.info("AutoCode: очистка использованных кодов выполнена.")
        except Exception as e:
            logger.error(f"Used-code cleanup error: {e}")

# ── Weekly report ────────────────────────────────────────────────────────────
def _weekly_report_worker(cardinal):
    while True:
        now = datetime.now()
        days_ahead = (WEEKLY_REPORT_DOW - now.weekday()) % 7
        if days_ahead == 0 and now.hour >= WEEKLY_REPORT_HOUR:
            days_ahead = 7
        next_run = now.replace(hour=WEEKLY_REPORT_HOUR, minute=0, second=0, microsecond=0) \
                   + timedelta(days=days_ahead)
        sleep_sec = (next_run - datetime.now()).total_seconds()
        time.sleep(max(sleep_sec, 60))
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

# ── Expiry watcher ───────────────────────────────────────────────────────────
def _expiry_watcher(cardinal):
    warned_4h  = set()
    warned_30m = set()
    while True:
        time.sleep(60)
        try:
            rents   = rentals()
            now     = time.time()
            changed = False
            for key, r in list(rents.items()):
                exp   = r.get("expires_at", 0)
                cid   = r.get("chat_id")
                cname = r.get("chat_name", "")

                if key not in warned_4h and 0 < exp - now <= WARN_BEFORE_H * 3600:
                    warned_4h.add(key)
                    Thread(
                        target=cardinal.send_message,
                        args=(cid,
                              f"⏰ До окончания аренды осталось {WARN_BEFORE_H} часа. "
                              f"Хотите продлить? Просто сделайте новый заказ 😊",
                              cname),
                        daemon=True,
                    ).start()

                if key not in warned_30m and 0 < exp - now <= 1800:
                    warned_30m.add(key)
                    Thread(
                        target=cardinal.send_message,
                        args=(cid, "⏰ До окончания аренды осталось 30 минут!", cname),
                        daemon=True,
                    ).start()

                if now > exp:
                    Thread(
                        target=cardinal.send_message,
                        args=(cid,
                              "✅ Ваша аренда завершена, спасибо за покупку! "
                              "Хотите продлить? Просто сделайте новый заказ 😊",
                              cname),
                        daemon=True,
                    ).start()
                    del rents[key]
                    warned_4h.discard(key)
                    warned_30m.discard(key)
                    changed = True

            if changed:
                save_rentals(rents)
        except Exception as e:
            logger.error(f"Expiry watcher error: {e}")

# ── Parse hours from lot name ────────────────────────────────────────────────
def _parse_hours(text: str) -> int | None:
    m = re.search(r"(\d+)\s*ч", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*д(ень|ня|ней)?", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 24
    return None

# ── on_new_order ─────────────────────────────────────────────────────────────
def on_new_order(c, e: NewOrderEvent):
    global _cardinal_ref
    _cardinal_ref = c

    desc     = getattr(e.order, "description", "") or ""
    lot_name = getattr(e.order, "lot_name", "") or desc
    hours    = _parse_hours(lot_name)
    if not hours:
        return

    buyer     = getattr(e.order, "buyer_username", "") or ""
    chat_id   = getattr(e.order, "chat_id", None)
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

    now   = time.time()
    rents = rentals()

    for k, r in rents.items():
        if r.get("buyer") == buyer and r.get("expires_at", 0) > now:
            rents[k]["expires_at"] += hours * 3600
            rents[k]["hours"]      += hours
            save_rentals(rents)
            Thread(
                target=c.send_message,
                args=(chat_id,
                      f"✅ Аренда продлена на {hours}ч!\n"
                      f"Новое время окончания: {_fmt_time(rents[k]['expires_at'])}",
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
    }
    save_rentals(rents)
    logger.info(f"AutoCode: новая аренда {buyer} | {acc_email} | {hours}ч | до {_fmt_time(expires_at)}")

    Thread(
        target=c.send_message,
        args=(chat_id,
              "🌸 | Аренда активирована, напиши команду в чат: !cd или code",
              chat_name),
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

    if lower == "!time":
        now   = time.time()
        rents = rentals()
        active = sorted(
            [r for r in rents.values() if r.get("buyer") == buyer and r.get("expires_at", 0) > now],
            key=lambda r: r.get("purchase_ts", 0),
            reverse=True,
        )
        if not active:
            reply = "❌ У вас нет активной аренды."
        else:
            r     = active[0]
            reply = (
                f"⏳ Осталось: {_fmt_remaining(r['expires_at'])}\n"
                f"📅 Окончание: {_fmt_time(r['expires_at'])}"
            )
        Thread(target=c.send_message, args=(chat_id, reply, chat_name), daemon=True).start()
        return

    if lower not in ("!cd", "code"):
        return

    allowed, reason = _check_rate(buyer)
    if not allowed:
        Thread(target=c.send_message, args=(chat_id, reason, chat_name), daemon=True).start()
        reqs = _code_requests.get(buyer, [])
        if len(reqs) >= MAX_CODES_HOUR:
            Thread(target=_notify_tg_rate_limit, args=(c, buyer, len(reqs)), daemon=True).start()
        return

    now   = time.time()
    rents = rentals()
    active = sorted(
        [r for r in rents.values() if r.get("buyer") == buyer and r.get("expires_at", 0) > now],
        key=lambda r: r.get("purchase_ts", 0),
        reverse=True,
    )
    if not active:
        Thread(
            target=c.send_message,
            args=(chat_id, "❌ У вас нет активной аренды. Пожалуйста, оформите заказ.", chat_name),
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
            args=(chat_id, "⚠️ Почтовый аккаунт не настроен. Обратитесь к продавцу.", chat_name),
            daemon=True,
        ).start()
        return

    _record_request(buyer)

    used     = used_codes()
    code_val, err = fetch_code(acc, used, not_before_ts=rental.get("purchase_ts", 0))

    if not code_val:
        reason = err or "Код не найден."
        Thread(target=_notify_tg_code_not_found, args=(c, buyer, acc_email, reason), daemon=True).start()
        Thread(
            target=c.send_message,
            args=(chat_id, "❌ Код не найден. Попробуйте через минуту или обратитесь к продавцу.", chat_name),
            daemon=True,
        ).start()
        return

    entry = {"code": code_val, "used_at": time.time()}
    used.setdefault(acc_email, []).append(entry)
    save_used(used)
    add_log(acc_email, rental.get("lot_id", "—"), buyer, code_val)

    received_dt = datetime.now().strftime("%d.%m.%Y %H:%M")
    msg = f"🔑 Ваш код: {code_val}\n📅 Получен: {received_dt}"
    Thread(target=c.send_message, args=(chat_id, msg, chat_name), daemon=True).start()

# ── Telegram UI ──────────────────────────────────────────────────────────────
def init_autocode_tg(cardinal, *args):
    global _cardinal_ref
    _cardinal_ref = cardinal
    bot = cardinal.telegram.bot

    _bcast = {"text": "", "failed": [], "scheduled_timer": None}

    @bot.message_handler(commands=["autocode"])
    def cmd_autocode(message: Message):
        bot.send_message(message.chat.id, "⚙️ AutoCode v5.1.1 — выберите раздел:",
                         reply_markup=kb_main())

    @bot.callback_query_handler(func=lambda c: c.data == AC_MAIN)
    def open_main(call: CallbackQuery):
        bot.edit_message_text("⚙️ AutoCode v5.1.1 — выберите раздел:",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=kb_main())

    # ── Account list ──
    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_LIST}:"))
    def open_list(call: CallbackQuery):
        accs = accounts()
        rows = []
        for i, a in enumerate(accs):
            rows.append([B(f"📧 {a['email']}", callback_data=f"{AC_EDIT}:{i}")])
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
            return
        acc = accs[idx]
        text = (
            f"📧 {acc['email']}\n"
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
        accs[idx]["password"] = _encrypt_password(message.text.strip())
        save_accs(accs)
        bot.send_message(message.chat.id, "✅ Пароль обновлён (зашифрован).")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_IMAP}:"))
    def act_imap(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
        auto = detect_imap_host(accs[idx]["email"])
        msg  = bot.send_message(call.message.chat.id,
            f"Текущий IMAP: {accs[idx].get('imap_host', auto)}\n"
            f"Авто: {auto}\nВведите новый хост или «авто»:")
        bot.register_next_step_handler(msg, lambda m: _save_imap(m, idx))

    def _save_imap(message: Message, idx: int):
        accs = accounts()
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
        result = test_imap(accs[idx])
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
        accs[idx][field] = val
        save_accs(accs)
        bot.send_message(message.chat.id, f"✅ {field} = {val}")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_TYPE}:"))
    def act_type(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
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
        accs[idx][field] = message.text.strip()
        save_accs(accs)
        bot.send_message(message.chat.id, f"✅ {field} сохранён.")

    # ── Lots ──
    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_LOTS}:"))
    def open_lots(call: CallbackQuery):
        idx  = int(call.data.split(":")[1])
        accs = accounts()
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
        accs[idx].setdefault("lot_ids", []).append(message.text.strip())
        save_accs(accs)
        bot.send_message(message.chat.id, "✅ Лот добавлен.")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{AC_LOT_DEL}:"))
    def act_lot_del(call: CallbackQuery):
        parts = call.data.split(":")
        idx, lot = int(parts[1]), parts[2]
        accs = accounts()
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
                f"👤 {r['buyer']} | до {_fmt_time(r['expires_at'])} | {r['email']}"
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
            save_rentals(rents)
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

    # ── Broadcast ──
    @bot.callback_query_handler(func=lambda c: c.data == AC_BROADCAST)
    def open_broadcast(call: CallbackQuery):
        active = get_active_rentals()
        text   = f"📢 Рассылка\nАктивных аренд: <b>{len(active)}</b>"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=kb_broadcast_menu(), parse_mode="HTML")

    @bot.callback_query_handler(func=lambda c: c.data == AC_BCAST_SEND)
    def bcast_new(call: CallbackQuery):
        msg = bot.send_message(call.message.chat.id, "Введите текст рассылки:")
        bot.register_next_step_handler(msg, _handle_bcast_text)

    def _handle_bcast_text(message: Message):
        _bcast["text"] = message.text.strip()
        active = get_active_rentals()
        kb = K(keyboard=[
            [B(f"✅ Отправить ({len(active)} чатов)", callback_data="ac_bcast_confirm")],
            [B("💾 Сохранить как шаблон", callback_data=AC_BCAST_TPL_ADD)],
            [B("❌ Отмена", callback_data=AC_BROADCAST)],
        ])
        bot.send_message(message.chat.id,
            f"Предпросмотр:\n\n{_bcast['text']}", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data == "ac_bcast_confirm")
    def bcast_confirm(call: CallbackQuery):
        _do_broadcast(call, get_active_rentals())

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
            return
        _bcast["text"] = tpls[idx]["text"]
        active = get_active_rentals()
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
                targets = get_active_rentals()
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

            t = Timer(delay, _fire)
            t.daemon = True
            t.start()
            _bcast["scheduled_timer"] = t

            bot.send_message(message.chat.id,
                f"✅ Рассылка запланирована на {dt.strftime('%d.%m.%Y %H:%M')}\n"
                f"Текст: {_bcast['text'][:80]}")
        except ValueError:
            bot.send_message(message.chat.id,
                "❌ Неверный формат. Пример: <code>31.05.2026 18:00</code>", parse_mode="HTML")

    # ── Background workers ──
    Thread(target=_expiry_watcher,           args=(cardinal,), daemon=True).start()
    Thread(target=_startup_imap_test,        args=(cardinal,), daemon=True).start()
    Thread(target=_used_code_cleanup_worker,              daemon=True).start()
    Thread(target=_weekly_report_worker,     args=(cardinal,), daemon=True).start()
    logger.info("AutoCode v5.1.1 инициализирован.")


# ── Plugin hooks ─────────────────────────────────────────────────────────────
BIND_TO_PRE_INIT    = [init_autocode_tg]
BIND_TO_NEW_ORDER   = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
