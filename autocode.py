"""
AutoCode v3 — плагин для FunPay Cardinal
Автовыдача кодов с IMAP-почт по команде !cd.

Логика:
  1. Новый заказ (NewOrderEvent) → парсим название лота → извлекаем часы аренды
     → ищем почту из текста автовыдачи → записываем покупателя в базу активных аренд
  2. Покупатель пишет !cd → проверяем что он в базе и аренда не истекла
     → ищем письмо на IMAP ТОЛЬКО после момента покупки → отдаём код
  3. Фоновый поток каждую минуту проверяет истекшие аренды
     → пишет покупателю "Аренда завершена, спасибо!" + предложение продлить
  4. Уведомление в Telegram если код не найден
  5. В Telegram-меню: активные аренды + скоро истекающие
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

import email
import email.header
import imaplib
import json
import logging
import os
import re
import time
import uuid as uuid_lib
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

# ─── Метаданные ───────────────────────────────────────────────────────────────
NAME        = "AutoCode"
VERSION     = "4.0.0"
DESCRIPTION = (
    "Авто-выдача кодов с IMAP-почт по команде !cd.\n"
    "Триггер — новый заказ. Окно доступа считается с момента покупки.\n"
    "Антидубль, лог, уведомления, авто-завершение аренды.\n"
    "Команда: /autocode"
)
CREDITS        = "@offsidezq"
UUID           = str(uuid_lib.UUID("b7e21f3a-4c8d-4e2b-9a1f-3c5d6e7f8b9a"))
SETTINGS_PAGE  = True
BIND_TO_DELETE = None

logger = logging.getLogger("FPC.AutoCode")

# ─── Callback-константы ───────────────────────────────────────────────────────
AC_MAIN     = "AC_MAIN"
AC_LIST     = "AC_LIST"
AC_ADD      = "AC_ADD"
AC_EDIT     = "AC_EDIT"
AC_DEL_ASK  = "AC_DEL_ASK"
AC_DEL_OK   = "AC_DEL_OK"
AC_LOTS     = "AC_LOTS"
AC_LOT_ADD  = "AC_LOT_ADD"
AC_LOT_DEL  = "AC_LOT_DEL"
AC_WINDOW   = "AC_WINDOW"
AC_MAX_AGE  = "AC_MAX_AGE"
AC_LEN      = "AC_LEN"
AC_TYPE     = "AC_TYPE"
AC_SPACES   = "AC_SPACES"
AC_FROM     = "AC_FROM"
AC_SUBJ     = "AC_SUBJ"
AC_BODY     = "AC_BODY"
AC_TEST     = "AC_TEST"
AC_LOG          = "AC_LOG"
AC_RENTALS      = "AC_RENTALS"
AC_RENT_DEL     = "AC_RENT_DEL"
AC_RENT_DEL_OK  = "AC_RENT_DEL_OK"
AC_RENT_EXT     = "AC_RENT_EXT"   # продлить (+N часов)
AC_RENT_EXT_DO  = "AC_RENT_EXT_DO"  # подтвердить продление
AC_STATS        = "AC_STATS"

# ─── Состояния ────────────────────────────────────────────────────────────────
ST_EMAIL   = "AC_ST_EMAIL"
ST_PASS    = "AC_ST_PASS"
ST_IMAP    = "AC_ST_IMAP"
ST_LOT_ADD = "AC_ST_LOT_ADD"
ST_WINDOW  = "AC_ST_WINDOW"
ST_MAX_AGE = "AC_ST_MAX_AGE"
ST_LEN     = "AC_ST_LEN"
ST_FROM    = "AC_ST_FROM"
ST_SUBJ    = "AC_ST_SUBJ"
ST_BODY    = "AC_ST_BODY"

# ─── Хранилище ────────────────────────────────────────────────────────────────
DATA_DIR      = "storage/autocode"
ACCOUNTS_FILE = f"{DATA_DIR}/accounts.json"
LOG_FILE      = f"{DATA_DIR}/log.json"
USED_FILE     = f"{DATA_DIR}/used_codes.json"
RENTALS_FILE  = f"{DATA_DIR}/rentals.json"   # активные аренды покупателей


def _ensure():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    _ensure()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def accounts():       return _load(ACCOUNTS_FILE, [])
def save_accs(d):     _save(ACCOUNTS_FILE, d)
def used_codes():     return _load(USED_FILE, {})
def save_used(d):     _save(USED_FILE, d)
def log_entries():    return _load(LOG_FILE, [])
def rentals():        return _load(RENTALS_FILE, {})
def save_rentals(d):  _save(RENTALS_FILE, d)


def add_log(email_addr, lot_id, buyer, code):
    entries = log_entries()
    entries.append({
        "time":   datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        "email":  email_addr,
        "lot_id": lot_id,
        "buyer":  buyer,
        "code":   code,
    })
    _save(LOG_FILE, entries[-200:])


# ─── Парсинг часов из названия лота ──────────────────────────────────────────

def parse_hours_from_lot(description: str) -> int | None:
    """
    Пробует вытащить количество часов из названия лота.
    Примеры:
      '🌸 NETFLIX Premium 4K 🌸 30 ДНЕЙ / 720ч' → 720
      'Netflix 24ч аренда'                       → 24
      'Netflix 1 день'                            → 24
      'Netflix 7 дней'                            → 168
      'Netflix 30 дней'                           → 720
    """
    # Прямо указаны часы: 720ч, 24ч, 48 ч
    m = re.search(r"(\d+)\s*ч\b", description, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # Указаны дни: 30 дней, 7 дн, 1 день
    m = re.search(r"(\d+)\s*д(ень|ня|ней|н\.?)\b", description, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 24

    return None


# ─── Парсинг почты из текста автовыдачи ──────────────────────────────────────

def parse_email_from_delivery(text: str) -> str | None:
    """
    Ищет email-адрес в тексте автовыдачи.
    Например: 'email: secritos@yandex.ru' → 'secritos@yandex.ru'
    """
    m = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
    return m.group(0) if m else None


# ─── IMAP логика ──────────────────────────────────────────────────────────────

def _decode_str(value: str) -> str:
    parts = email.header.decode_header(value or "")
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(part))
    return "".join(out)


def _html_to_text(html: str) -> str:
    """
    Умно преобразует HTML в текст: удаляет теги, сохраняя переносы строк.
    Блочные теги (<p>, <br>, <div>, <td>, <tr>) → новая строка.
    """
    # Блочные элементы → \n
    html = re.sub(r"<(br|p|div|td|tr|li|h[1-6])[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Удаляем остальные теги
    html = re.sub(r"<[^>]+>", "", html)
    # HTML-энтити
    html = html.replace("&nbsp;", " ").replace("&amp;", "&") \
               .replace("&lt;", "<").replace("&gt;", ">") \
               .replace("&#13;", "").replace("\r", "\n")
    # Убираем пустые строки
    lines = [l.strip() for l in html.splitlines()]
    return "\n".join(l for l in lines if l)


def _get_text(msg) -> tuple[str, str]:
    """
    Возвращает (plain_text, html_text) из письма.
    html_text — очищенный текст HTML-части с сохранёнными переносами строк.
    """
    plain = ""
    html  = ""

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cs = part.get_content_charset() or "utf-8"
            try:
                payload = part.get_payload(decode=True).decode(cs, errors="replace")
            except Exception:
                continue
            if ct == "text/plain" and not plain:
                plain = payload
            elif ct == "text/html" and not html:
                html = _html_to_text(payload)
    else:
        cs  = msg.get_content_charset() or "utf-8"
        ct  = msg.get_content_type()
        try:
            payload = msg.get_payload(decode=True).decode(cs, errors="replace")
            if ct == "text/html":
                html = _html_to_text(payload)
            else:
                plain = payload
        except Exception:
            pass

    return plain, html


def _find_code(plain: str, html: str, subj: str, acc: dict) -> str | None:
    """
    Ищет код в письме.
    plain — plain-text часть, html — очищенный HTML с переносами строк.

    Стратегии по приоритету:
      1. Отдельная строка состоящая только из цифр/букв (Netflix: '4626' отдельная строка в HTML)
      2. Цифры/код рядом с ключевыми словами (код, code, вход, login, enter)
      3. Первое подходящее число по длине в обоих частях
    """
    code_type = acc.get("code_type", "digits")
    code_len  = int(acc.get("code_len", 0))
    body_kw   = (acc.get("filter_body") or "").strip()

    # Объединяем все источники
    combined = (plain or "") + "\n" + (html or "") + "\n" + subj

    # Фильтр по ключевому слову в body
    if body_kw and body_kw.lower() not in combined.lower():
        return None

    # Паттерн для поиска кода
    if code_type == "digits":
        sym_pat = r"\d"
        full_pat_len  = r"\b(\d{%d})\b" % code_len if code_len else None
        full_pat_any  = r"\b(\d{4,8})\b"
        line_re = r"^(\d+)$"
    elif code_type == "alpha":
        sym_pat = r"[A-Za-z]"
        full_pat_len  = r"\b([A-Za-z]{%d})\b" % code_len if code_len else None
        full_pat_any  = r"\b([A-Za-z]{4,12})\b"
        line_re = r"^([A-Za-z]+)$"
    elif code_type == "alnum-dash":
        # Буквенно-цифровые коды с дефисом (пример: ABC-123, AB12-CD34)
        sym_pat = r"[A-Za-z0-9\-]"
        full_pat_len  = None  # длина со знаком - сложно точно, ищем по line_re
        full_pat_any  = r"\b([A-Za-z0-9]{2,6}-[A-Za-z0-9]{2,8})\b"
        line_re = r"^([A-Za-z0-9]+-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)$"
    else:
        sym_pat = r"[A-Za-z0-9]"
        full_pat_len  = r"\b([A-Za-z0-9]{%d})\b" % code_len if code_len else None
        full_pat_any  = r"\b([A-Za-z0-9]{4,12})\b"
        line_re = r"^([A-Za-z0-9]+)$"

    def valid(c: str) -> bool:
        if code_len and len(c) != code_len:
            return False
        return bool(re.match(f"^{sym_pat}+$", c))

    # ── Стратегия 1: отдельная строка целиком состоящая из нужных символов ──
    # Работает и для plain, и для HTML (где код стоит отдельной строкой благодаря _html_to_text)
    for source in [html, plain]:
        if not source:
            continue
        for line in source.splitlines():
            line = line.strip()
            m = re.match(line_re, line)
            if m and valid(m.group(1)):
                return m.group(1)

    # ── Стратегия 2: код рядом с ключевыми словами (в окне ±3 строк) ──
    KW = re.compile(
        r"(\bкод\b|\bcode\b|\benter\b|вход|login|otp|pin|verification|passcode)",
        re.IGNORECASE
    )
    main_pat = re.compile(full_pat_len or full_pat_any)
    lines = combined.splitlines()
    for i, line in enumerate(lines):
        if KW.search(line):
            window = "\n".join(lines[max(0, i-3):i+4])
            m = main_pat.search(window)
            if m and valid(m.group(1)):
                return m.group(1)

    # ── Стратегия 3: первое подходящее число/слово в обеих частях ──
    found = main_pat.findall(combined)
    for c in found:
        if valid(c):
            return c

    return None


def fetch_code(acc: dict, used: dict, not_before_ts: float | None = None) -> tuple[str | None, str | None]:
    """
    Подключается по IMAP, ищет свежий неиспользованный код.
    not_before_ts — timestamp момента покупки, письма раньше него игнорируются.
    Возвращает (код, None) или (None, текст_ошибки).
    """
    imap_host   = acc.get("imap") or "imap.mail.ru"
    email_addr  = acc["email"]
    password    = acc["password"]
    filter_from = (acc.get("filter_from") or "").strip()
    filter_subj = (acc.get("filter_subj") or "").strip()
    max_age_min = int(acc.get("max_age_min", 60))

    try:
        mail = imaplib.IMAP4_SSL(imap_host, timeout=10)
        mail.login(email_addr, password)
        mail.select("INBOX")
    except Exception as e:
        return None, f"IMAP ошибка ({email_addr}): {e}"

    try:
        if filter_from:
            status, data = mail.search(None, "FROM", filter_from)
        else:
            status, data = mail.search(None, "ALL")

        if status != "OK" or not data[0]:
            return None, None

        msg_ids = data[0].split()

        for mid in reversed(msg_ids[-50:]):
            try:
                status, msg_data = mail.fetch(mid, "(RFC822)")
                if status != "OK":
                    continue
                msg = email.message_from_bytes(msg_data[0][1])

                date_str = msg.get("Date", "")
                msg_ts = None
                try:
                    msg_dt = parsedate_to_datetime(date_str)
                    msg_ts = msg_dt.timestamp()
                except Exception:
                    pass

                # Принимаем письма до 2 часов ДО покупки — покупатель мог запросить
                # код заранее чем оформить заказ (Netflix присылает код сразу)
                PRE_WINDOW = 2 * 3600  # 2 часа до покупки
                if not_before_ts and msg_ts and msg_ts < (not_before_ts - PRE_WINDOW):
                    continue

                # Фильтр по возрасту — только если нет not_before_ts
                if not not_before_ts and msg_ts and (time.time() - msg_ts) > max_age_min * 60:
                    continue

                subj = _decode_str(msg.get("Subject", ""))
                if filter_subj and filter_subj.lower() not in subj.lower():
                    continue

                plain, html = _get_text(msg)
                code = _find_code(plain, html, subj, acc)
                if not code:
                    continue

                # Антидубль
                key = f"{email_addr}:{code}"
                if key in used:
                    continue

                return code, None

            except Exception as ex:
                logger.warning(f"[AutoCode] Ошибка чтения письма: {ex}")
                continue

        return None, None

    finally:
        try:
            mail.logout()
        except Exception:
            pass


def test_imap(acc: dict) -> str:
    """Returns connection status + info about the last matching letter."""
    imap_host   = acc.get("imap") or "imap.mail.ru"
    filter_subj = (acc.get("filter_subj") or "").strip()
    filter_from = (acc.get("filter_from") or "").strip()
    try:
        mail = imaplib.IMAP4_SSL(imap_host, timeout=10)
        mail.login(acc["email"], acc["password"])
        mail.select("INBOX")
        _, data = mail.search(None, "ALL")
        ids = data[0].split() if data[0] else []
        count = len(ids)

        # Ищем последнее письмо подходящее по фильтрам
        last_subj = "—"
        last_date = "—"
        last_from = "—"
        for mid in reversed(ids[-50:]):
            try:
                _, msg_data = mail.fetch(mid, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                subj = _decode_str(msg.get("Subject", ""))
                sender = _decode_str(msg.get("From", ""))
                date_str = msg.get("Date", "")
                if filter_subj and filter_subj.lower() not in subj.lower():
                    continue
                if filter_from and filter_from.lower() not in sender.lower():
                    continue
                # Нашли подходящее
                last_subj = subj[:60] if subj else "—"
                last_from = sender[:40] if sender else "—"
                try:
                    dt = parsedate_to_datetime(date_str)
                    last_date = dt.strftime("%d.%m.%Y %H:%M")
                except Exception:
                    last_date = date_str[:20] if date_str else "—"
                break
            except Exception:
                continue

        mail.logout()
        return (
            f"✅ <b>Подключено.</b> Писем в INBOX: {count}\n\n"
            f"📨 <b>Последнее подходящее письмо:</b>\n"
            f"   От: <code>{last_from}</code>\n"
            f"   Тема: {last_subj}\n"
            f"   Дата: {last_date}"
        )
    except Exception as e:
        return f"❌ Ошибка: {e}"


# ─── Вспомогательные функции аренд ───────────────────────────────────────────

def get_active_rentals() -> list[dict]:
    """Возвращает список активных (не истекших) аренд."""
    now = time.time()
    return [r for r in rentals().values() if r["expires_at"] > now]


def get_expiring_rentals(within_hours: float = 2.0) -> list[dict]:
    """Аренды, истекающие в ближайшие within_hours часов."""
    now = time.time()
    threshold = now + within_hours * 3600
    return [r for r in rentals().values()
            if now < r["expires_at"] <= threshold]


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def _fmt_remaining(ts: float) -> str:
    secs = int(ts - time.time())
    if secs <= 0:
        return "истекла"
    h, m = divmod(secs // 60, 60)
    return f"{h}ч {m}м"


# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def kb_main() -> K:
    return (
        K()
        .add(B("📬 Список почт",    callback_data=f"{AC_LIST}:0"))
        .add(B("🏠 Активные аренды", callback_data=AC_RENTALS))
        .add(B("📜 Лог выдач",      callback_data=AC_LOG))
        .add(B("📊 Статистика",       callback_data=AC_STATS))
    )


def kb_list(accs: list) -> K:
    kb = K()
    for i, a in enumerate(accs):
        kb.add(B(f"✏️ {a['email']}", callback_data=f"{AC_EDIT}:{i}"))
    kb.add(B("➕ Добавить почту", callback_data=AC_ADD))
    kb.add(B("◀️ Назад",         callback_data=AC_MAIN))
    return kb


def _type_label(t):
    return {"digits": "Цифры", "alpha": "Буквы", "alnum": "Буквы+Цифры", "alnum-dash": "XXX-000"}.get(t, "Цифры")


def kb_edit(idx: int, acc: dict) -> K:
    kb = K()
    kb.add(
        B(f"🕐 Возраст: {acc.get('max_age_min',60)}м", callback_data=f"{AC_MAX_AGE}:{idx}"),
        B(f"🔢 Длина: {acc.get('code_len',0) or 'любая'}", callback_data=f"{AC_LEN}:{idx}"),
    )
    kb.add(
        B(f"🔡 Тип: {_type_label(acc.get('code_type','digits'))}", callback_data=f"{AC_TYPE}:{idx}"),
        B(f"␣ Пробелы: {'Да' if acc.get('remove_spaces',True) else 'Нет'}", callback_data=f"{AC_SPACES}:{idx}"),
    )
    kb.add(
        B(f"📨 От: {acc.get('filter_from') or '—'}",    callback_data=f"{AC_FROM}:{idx}"),
        B(f"📌 Тема: {acc.get('filter_subj') or '—'}", callback_data=f"{AC_SUBJ}:{idx}"),
    )
    kb.add(B(f"📝 Body: {acc.get('filter_body') or '—'}", callback_data=f"{AC_BODY}:{idx}"))
    kb.add(B("🔌 Тест почты",      callback_data=f"{AC_TEST}:{idx}"))
    kb.add(B("🗑 Удалить аккаунт", callback_data=f"{AC_DEL_ASK}:{idx}"))
    kb.add(B("◀️ Назад",           callback_data=f"{AC_LIST}:0"))
    return kb


def _edit_text(idx: int, acc: dict) -> str:
    return (
        f"⚙️ <b>{acc['email']}</b>\n\n"
        f"🌐 IMAP: <code>{acc.get('imap','imap.mail.ru')}</code>\n"
        f"🕐 Макс. возраст кода: {acc.get('max_age_min',60)} мин.\n"
        f"🔢 Длина кода: {acc.get('code_len',0) or 'любая'}\n"
        f"🔡 Тип кода: {_type_label(acc.get('code_type','digits'))}\n"
        f"␣ Пробелы убирать: {'Да' if acc.get('remove_spaces',True) else 'Нет'}\n"
        f"📨 Фильтр from: {acc.get('filter_from') or '—'}\n"
        f"📌 Фильтр subj: {acc.get('filter_subj') or '—'}\n"
        f"📝 Фильтр body: {acc.get('filter_body') or '—'}\n"
    )


def kb_confirm_del(idx: int) -> K:
    return K().row(
        B("✅ Удалить", callback_data=f"{AC_DEL_OK}:{idx}"),
        B("❌ Отмена",  callback_data=f"{AC_EDIT}:{idx}"),
    )


def kb_lots(idx: int, acc: dict) -> K:
    kb = K()
    for lid in acc.get("lot_ids", []):
        kb.add(B(f"❌ {lid}", callback_data=f"{AC_LOT_DEL}:{idx}:{lid}"))
    kb.add(B("➕ Добавить лот ID", callback_data=f"{AC_LOT_ADD}:{idx}"))
    kb.add(B("◀️ Назад",          callback_data=f"{AC_EDIT}:{idx}"))
    return kb


def kb_type(idx: int) -> K:
    return (
        K()
        .add(B("🔢 Цифры",        callback_data=f"{AC_TYPE}:{idx}:digits"))
        .add(B("🔡 Буквы",        callback_data=f"{AC_TYPE}:{idx}:alpha"))
        .add(B("🔣 Буквы+Цифры",  callback_data=f"{AC_TYPE}:{idx}:alnum"))
        .add(B("🔑 XXX-000 (дефис)", callback_data=f"{AC_TYPE}:{idx}:alnum-dash"))
        .add(B("◀️ Назад",        callback_data=f"{AC_EDIT}:{idx}"))
    )


def kb_cancel(cb: str) -> K:
    return K().add(B("❌ Отмена", callback_data=cb))


# ─── Telegram UI ──────────────────────────────────────────────────────────────

# Глобальная ссылка на Cardinal для фонового потока
_cardinal_ref: Cardinal | None = None


def init_autocode_tg(cardinal: Cardinal, *args):
    global _cardinal_ref
    _cardinal_ref = cardinal
    _ensure()

    tg  = cardinal.telegram
    bot = tg.bot

    def acc_by(idx):
        a = accounts()
        return a[idx] if 0 <= idx < len(a) else None

    # ── Главное меню ─────────────────────────────────────────────────────────

    def show_main(chat_id, msg_id):
        active = len(get_active_rentals())
        expiring = len(get_expiring_rentals(2))
        text = (
            "⚙️ <b>AutoCode v4</b>\n\n"
            f"🏠 Активных аренд: <b>{active}</b>\n"
            f"⚠️ Истекают в ближайшие 2ч: <b>{expiring}</b>\n\n"
            "Выдача кодов по команде <code>!cd</code> в чате FunPay.\n"
            "Триггер — оплата заказа. Окно считается с момента покупки."
        )
        bot.edit_message_text(text, chat_id, msg_id,
                              reply_markup=kb_main(), parse_mode="HTML")

    def open_main(c: CallbackQuery):
        show_main(c.message.chat.id, c.message.id)
        bot.answer_callback_query(c.id)

    def open_plugin_settings(c: CallbackQuery):
        show_main(c.message.chat.id, c.message.id)
        bot.answer_callback_query(c.id)

    # ── Активные аренды ──────────────────────────────────────────────────────

    def _rentals_text_kb():
        """Build rentals list text + inline keyboard with extend/delete buttons."""
        rents = rentals()
        now_ts = time.time()
        active = [(k, r) for k, r in rents.items() if r["expires_at"] > now_ts]
        active.sort(key=lambda x: x[1]["expires_at"])
        expiring_keys = {k for k, r in active if (r["expires_at"] - now_ts) <= 7200}
        if not active:
            return "🏠 <b>Активных аренд нет.</b>", K().add(B("◀️ Назад", callback_data=AC_MAIN))
        lines = []
        kb = K()
        for ok, r in active:
            warn = "⚠️ " if ok in expiring_keys else ""
            lines.append(
                f"{warn}👤 <b>{r['buyer']}</b> (заказ {r.get('order_id', ok)})\n"
                f"   📧 {r['email']}\n"
                f"   ⏳ Осталось: {_fmt_remaining(r['expires_at'])} | До: {_fmt_time(r['expires_at'])}"
            )
            kb.add(
                B(f"➕ +24ч  {r['buyer'][:14]}", callback_data=f"{AC_RENT_EXT}:{ok}:24"),
                B("🗑 Удалить",                  callback_data=f"{AC_RENT_DEL}:{ok}"),
            )
        kb.add(B("◀️ Назад", callback_data=AC_MAIN))
        return f"🏠 <b>Активные аренды ({len(active)}):</b>\n\n" + "\n\n".join(lines), kb

    def open_rentals(c: CallbackQuery):
        text, kb = _rentals_text_kb()
        bot.edit_message_text(text, c.message.chat.id, c.message.id,
                              reply_markup=kb, parse_mode="HTML")
        bot.answer_callback_query(c.id)

    def do_rent_del(c: CallbackQuery):
        """Ask confirmation before deleting a rental."""
        ok = c.data.split(":", 1)[1]
        r = rentals().get(ok)
        if not r:
            bot.answer_callback_query(c.id, "Аренда не найдена", show_alert=True)
            return
        kb = K().row(
            B("✅ Да, удалить", callback_data=f"{AC_RENT_DEL_OK}:{ok}"),
            B("❌ Отмена",       callback_data=AC_RENTALS),
        )
        bot.edit_message_text(
            f"⚠️ Удалить аренду <b>{r['buyer']}</b>?\n"
            f"⏳ Осталось: {_fmt_remaining(r['expires_at'])}",
            c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML"
        )
        bot.answer_callback_query(c.id)

    def do_rent_del_ok(c: CallbackQuery):
        ok = c.data.split(":", 1)[1]
        rents = rentals()
        buyer_name = rents.get(ok, {}).get("buyer", ok)
        if ok in rents:
            del rents[ok]
            save_rentals(rents)
        bot.answer_callback_query(c.id, f"✅ Аренда {buyer_name} удалена")
        text2, kb2 = _rentals_text_kb()
        bot.edit_message_text(text2, c.message.chat.id, c.message.id,
                              reply_markup=kb2, parse_mode="HTML")

    def do_rent_ext(c: CallbackQuery):
        """Manually extend a rental by N hours."""
        parts = c.data.split(":")
        ok    = parts[1]
        hours = int(parts[2]) if len(parts) > 2 else 24
        rents = rentals()
        if ok not in rents:
            bot.answer_callback_query(c.id, "Аренда не найдена", show_alert=True)
            return
        rents[ok]["expires_at"] += hours * 3600
        rents[ok]["hours"] = rents[ok].get("hours", 0) + hours
        save_rentals(rents)
        r = rents[ok]
        bot.answer_callback_query(
            c.id,
            f"✅ Аренда {r['buyer']} продлена на {hours}ч.\nТеперь до: {_fmt_time(r['expires_at'])}",
            show_alert=True
        )
        text3, kb3 = _rentals_text_kb()
        bot.edit_message_text(text3, c.message.chat.id, c.message.id,
                              reply_markup=kb3, parse_mode="HTML")

    # ── Список почт ──────────────────────────────────────────────────────────

    def open_list(c: CallbackQuery):
        accs = accounts()
        text = f"📬 Подключённые почты (Всего: {len(accs)}):"
        if not accs:
            text += "\n\nПочт ещё нет."
        bot.edit_message_text(text, c.message.chat.id, c.message.id,
                              reply_markup=kb_list(accs), parse_mode="HTML")
        bot.answer_callback_query(c.id)

    # ── Добавление почты ─────────────────────────────────────────────────────

    def act_add(c: CallbackQuery):
        msg = bot.send_message(
            c.message.chat.id,
            "📧 <b>Шаг 1/3</b> — Введите Email:",
            reply_markup=kb_cancel(f"{AC_LIST}:0"), parse_mode="HTML"
        )
        tg.set_state(c.message.chat.id, msg.id, c.from_user.id, ST_EMAIL)
        bot.answer_callback_query(c.id)

    def got_email(m: Message):
        v = m.text.strip()
        tg.clear_state(m.chat.id, m.from_user.id, del_msg=True)
        msg = bot.send_message(
            m.chat.id,
            f"🔑 <b>Шаг 2/3</b> — Пароль приложений (IMAP) для {v}:",
            reply_markup=kb_cancel(f"{AC_LIST}:0"), parse_mode="HTML"
        )
        tg.set_state(m.chat.id, msg.id, m.from_user.id, ST_PASS, {"email": v})

    def got_pass(m: Message):
        st = tg.get_state(m.chat.id, m.from_user.id)["data"]
        st["password"] = m.text.strip()
        tg.clear_state(m.chat.id, m.from_user.id, del_msg=True)
        msg = bot.send_message(
            m.chat.id,
            "🌐 <b>Шаг 3/3</b> — IMAP сервер\n"
            "(<code>-</code> = <code>imap.mail.ru</code>):",
            reply_markup=kb_cancel(f"{AC_LIST}:0"), parse_mode="HTML"
        )
        tg.set_state(m.chat.id, msg.id, m.from_user.id, ST_IMAP, st)

    def got_imap(m: Message):
        st = tg.get_state(m.chat.id, m.from_user.id)["data"]
        tg.clear_state(m.chat.id, m.from_user.id, del_msg=True)
        imap_val = m.text.strip()
        if imap_val == "-":
            imap_val = "imap.mail.ru"
        accs = accounts()
        accs.append({
            "email": st["email"], "password": st["password"], "imap": imap_val,
            "lot_ids": [], "max_age_min": 60,
            "code_len": 0, "code_type": "digits", "remove_spaces": True,
            "filter_from": "", "filter_subj": "", "filter_body": "",
        })
        save_accs(accs)
        idx = len(accs) - 1
        bot.send_message(
            m.chat.id,
            f"✅ Почта <b>{accs[idx]['email']}</b> добавлена!",
            reply_markup=kb_edit(idx, accs[idx]), parse_mode="HTML"
        )

    # ── Редактирование ───────────────────────────────────────────────────────

    def open_edit(c: CallbackQuery):
        idx = int(c.data.split(":")[1])
        acc = acc_by(idx)
        if not acc:
            bot.answer_callback_query(c.id, "Не найдено", show_alert=True)
            return
        bot.edit_message_text(_edit_text(idx, acc), c.message.chat.id, c.message.id,
                              reply_markup=kb_edit(idx, acc), parse_mode="HTML")
        bot.answer_callback_query(c.id)

    # ── Удаление ─────────────────────────────────────────────────────────────

    def ask_del(c: CallbackQuery):
        idx = int(c.data.split(":")[1])
        acc = acc_by(idx)
        if not acc:
            bot.answer_callback_query(c.id, "Не найдено", show_alert=True)
            return
        bot.edit_message_text(f"⚠️ Удалить <b>{acc['email']}</b>?",
                              c.message.chat.id, c.message.id,
                              reply_markup=kb_confirm_del(idx), parse_mode="HTML")
        bot.answer_callback_query(c.id)

    def do_del(c: CallbackQuery):
        idx = int(c.data.split(":")[1])
        accs = accounts()
        if 0 <= idx < len(accs):
            accs.pop(idx)
            save_accs(accs)
        bot.edit_message_text("✅ Удалено.", c.message.chat.id, c.message.id,
                              reply_markup=kb_list(accounts()))
        bot.answer_callback_query(c.id)

    # ── Лоты ─────────────────────────────────────────────────────────────────

    def open_lots(c: CallbackQuery):
        idx = int(c.data.split(":")[1])
        acc = acc_by(idx)
        if not acc:
            bot.answer_callback_query(c.id, "Не найдено", show_alert=True)
            return
        bot.edit_message_text(
            f"🗂 <b>Лоты для {acc['email']}</b>\n\nID лота — число из URL лота на FunPay.",
            c.message.chat.id, c.message.id,
            reply_markup=kb_lots(idx, acc), parse_mode="HTML"
        )
        bot.answer_callback_query(c.id)

    def act_lot_add(c: CallbackQuery):
        idx = int(c.data.split(":")[1])
        msg = bot.send_message(
            c.message.chat.id,
            "🆔 Введите <b>ID лота</b> (число из URL на FunPay):",
            reply_markup=kb_cancel(f"{AC_LOTS}:{idx}"), parse_mode="HTML"
        )
        tg.set_state(c.message.chat.id, msg.id, c.from_user.id, ST_LOT_ADD, {"idx": idx})
        bot.answer_callback_query(c.id)

    def got_lot_id(m: Message):
        st  = tg.get_state(m.chat.id, m.from_user.id)
        idx = st["data"]["idx"]
        tg.clear_state(m.chat.id, m.from_user.id, del_msg=True)
        lid = m.text.strip()
        if not lid.isdigit():
            bot.send_message(m.chat.id, "❌ ID должен быть числом.")
            return
        accs = accounts()
        if 0 <= idx < len(accs):
            if lid not in accs[idx].setdefault("lot_ids", []):
                accs[idx]["lot_ids"].append(lid)
                save_accs(accs)
            bot.send_message(m.chat.id, f"✅ Лот <code>{lid}</code> добавлен.",
                             reply_markup=kb_lots(idx, accs[idx]), parse_mode="HTML")

    def do_lot_del(c: CallbackQuery):
        parts = c.data.split(":")
        idx, lid = int(parts[1]), parts[2]
        accs = accounts()
        if 0 <= idx < len(accs):
            accs[idx]["lot_ids"] = [x for x in accs[idx].get("lot_ids", []) if x != lid]
            save_accs(accs)
            bot.edit_message_reply_markup(c.message.chat.id, c.message.id,
                                          reply_markup=kb_lots(idx, accs[idx]))
        bot.answer_callback_query(c.id, f"Лот {lid} удалён")

    # ── Числовые настройки ───────────────────────────────────────────────────

    def _ask_num(c: CallbackQuery, state: str, prompt: str):
        idx = int(c.data.split(":")[1])
        msg = bot.send_message(c.message.chat.id, prompt,
                               reply_markup=kb_cancel(f"{AC_EDIT}:{idx}"), parse_mode="HTML")
        tg.set_state(c.message.chat.id, msg.id, c.from_user.id, state, {"idx": idx})
        bot.answer_callback_query(c.id)

    def act_age(c):  _ask_num(c, ST_MAX_AGE, "🕐 Введите <b>макс. возраст кода в минутах</b> (например: 60):")
    def act_len(c):  _ask_num(c, ST_LEN,     "🔢 Введите <b>длину кода</b> (0 = любая):")

    def _got_num(m: Message, state_key: str, field: str, label: str):
        st = tg.get_state(m.chat.id, m.from_user.id)
        if not st or st["state"] != state_key:
            return
        idx = st["data"]["idx"]
        tg.clear_state(m.chat.id, m.from_user.id, del_msg=True)
        if not m.text.strip().isdigit():
            bot.send_message(m.chat.id, "❌ Введите число.")
            return
        accs = accounts()
        if 0 <= idx < len(accs):
            accs[idx][field] = int(m.text.strip())
            save_accs(accs)
            bot.send_message(m.chat.id, f"✅ {label}: <b>{m.text.strip()}</b>",
                             reply_markup=kb_edit(idx, accs[idx]), parse_mode="HTML")

    def got_age(m):  _got_num(m, ST_MAX_AGE, "max_age_min", "Макс. возраст (мин.)")
    def got_len(m):  _got_num(m, ST_LEN,     "code_len",    "Длина кода")

    # ── Тип кода ─────────────────────────────────────────────────────────────

    def open_type(c: CallbackQuery):
        parts = c.data.split(":")
        idx = int(parts[1])
        if len(parts) == 3:
            accs = accounts()
            if 0 <= idx < len(accs):
                accs[idx]["code_type"] = parts[2]
                save_accs(accs)
                bot.edit_message_reply_markup(c.message.chat.id, c.message.id,
                                              reply_markup=kb_edit(idx, accs[idx]))
            bot.answer_callback_query(c.id, "✅ Сохранено")
        else:
            bot.edit_message_reply_markup(c.message.chat.id, c.message.id,
                                          reply_markup=kb_type(idx))
            bot.answer_callback_query(c.id)

    # ── Пробелы ──────────────────────────────────────────────────────────────

    def toggle_spaces(c: CallbackQuery):
        idx = int(c.data.split(":")[1])
        accs = accounts()
        if 0 <= idx < len(accs):
            accs[idx]["remove_spaces"] = not accs[idx].get("remove_spaces", True)
            save_accs(accs)
            bot.edit_message_reply_markup(c.message.chat.id, c.message.id,
                                          reply_markup=kb_edit(idx, accs[idx]))
        bot.answer_callback_query(c.id)

    # ── Текстовые фильтры ────────────────────────────────────────────────────

    def _ask_text(c: CallbackQuery, state_key: str, prompt: str):
        idx = int(c.data.split(":")[1])
        msg = bot.send_message(c.message.chat.id, prompt,
                               reply_markup=kb_cancel(f"{AC_EDIT}:{idx}"), parse_mode="HTML")
        tg.set_state(c.message.chat.id, msg.id, c.from_user.id, state_key, {"idx": idx})
        bot.answer_callback_query(c.id)

    def act_from(c): _ask_text(c, ST_FROM, "📨 Фильтр <b>отправителя (from)</b> (<code>-</code> = очистить):")
    def act_subj(c): _ask_text(c, ST_SUBJ, "📌 Фильтр <b>темы (subj)</b> (<code>-</code> = очистить):")
    def act_body(c): _ask_text(c, ST_BODY, "📝 <b>Ключевое слово в теле</b> (<code>-</code> = очистить):")

    def _got_text(m: Message, state_key: str, field: str, label: str):
        st = tg.get_state(m.chat.id, m.from_user.id)
        if not st or st["state"] != state_key:
            return
        idx = st["data"]["idx"]
        tg.clear_state(m.chat.id, m.from_user.id, del_msg=True)
        val = m.text.strip()
        if val == "-":
            val = ""
        accs = accounts()
        if 0 <= idx < len(accs):
            accs[idx][field] = val
            save_accs(accs)
            bot.send_message(m.chat.id, f"✅ {label}: <b>{val or '(очищен)'}</b>",
                             reply_markup=kb_edit(idx, accs[idx]), parse_mode="HTML")

    def got_from(m): _got_text(m, ST_FROM, "filter_from", "Фильтр from")
    def got_subj(m): _got_text(m, ST_SUBJ, "filter_subj", "Фильтр subj")
    def got_body(m): _got_text(m, ST_BODY, "filter_body", "Ключ. слово body")

    # ── Тест ─────────────────────────────────────────────────────────────────

    def do_test(c: CallbackQuery):
        idx = int(c.data.split(":")[1])
        acc = acc_by(idx)
        if not acc:
            bot.answer_callback_query(c.id, "Не найдено", show_alert=True)
            return
        bot.answer_callback_query(c.id, "⏳ Проверяю...")

        def run():
            # Проверка подключения
            conn_result = test_imap(acc)

            # Поиск последнего доступного кода (без антидубля — только для превью)
            code, err = fetch_code(acc, {}, not_before_ts=None)

            if code:
                code_line = f"🔑 Последний доступный код: <code>{code}</code>"
            elif err:
                code_line = f"❌ Ошибка поиска: {err}"
            else:
                code_line = "📩 Подходящего письма с кодом не найдено"

            bot.send_message(
                c.message.chat.id,
                f"{conn_result}\n\n{code_line}",
                parse_mode="HTML"
            )

        Thread(target=run, daemon=True).start()

    # ── Лог ──────────────────────────────────────────────────────────────────

    def open_log(c: CallbackQuery):
        entries = log_entries()
        if not entries:
            text = "📜 Лог выдач пуст."
        else:
            lines = []
            for e in reversed(entries[-30:]):
                lines.append(
                    f"🕐 {e['time']} | 👤 {e['buyer']}\n"
                    f"   📧 {e['email']} → <code>{e['code']}</code> | лот {e['lot_id']}"
                )
            text = "📜 <b>Последние выдачи (30):</b>\n\n" + "\n\n".join(lines)
        bot.edit_message_text(text, c.message.chat.id, c.message.id,
                              reply_markup=K().add(B("◀️ Назад", callback_data=AC_MAIN)),
                              parse_mode="HTML")
        bot.answer_callback_query(c.id)

    # ── Статистика ──────────────────────────────────────────────────────────

    def open_stats(c: CallbackQuery):
        entries = log_entries()
        if not entries:
            bot.edit_message_text(
                "📊 <b>Статистика выдач</b>\n\nЛог пуст.",
                c.message.chat.id, c.message.id,
                reply_markup=K().add(B("◀️ Назад", callback_data=AC_MAIN)), parse_mode="HTML"
            )
            bot.answer_callback_query(c.id)
            return

        from datetime import date as _date, timedelta as _td
        today_str  = _date.today().strftime("%d.%m.%Y")
        week_dates = {(_date.today() - _td(days=i)).strftime("%d.%m.%Y") for i in range(7)}

        total      = len(entries)
        today_cnt  = sum(1 for e in entries if e["time"].startswith(today_str))
        week_cnt   = sum(1 for e in entries if e["time"][:10] in week_dates)

        from collections import Counter
        email_cnt   = Counter(e["email"]  for e in entries)
        buyer_cnt   = Counter(e["buyer"]  for e in entries)
        top_emails  = email_cnt.most_common(5)
        top_buyers  = buyer_cnt.most_common(3)
        email_lines = "\n".join(f"   • <code>{em}</code>: {cnt}\u00a0выдач" for em, cnt in top_emails)
        buyer_lines = "\n".join(f"   • <b>{b}</b>: {cnt}" for b, cnt in top_buyers)

        text = (
            "📊 <b>Статистика выдач</b>\n\n"
            f"📌 Всего в логе: <b>{total}</b>\n"
            f"📅 Сегодня: <b>{today_cnt}</b>\n"
            f"📆 За 7 дней: <b>{week_cnt}</b>\n\n"
            f"📧 Топ почт (по выдачам):\n{email_lines or '   —'}\n\n"
            f"👤 Топ покупателей:\n{buyer_lines or '   —'}"
        )
        bot.edit_message_text(
            text, c.message.chat.id, c.message.id,
            reply_markup=K().add(B("◀️ Назад", callback_data=AC_MAIN)), parse_mode="HTML"
        )
        bot.answer_callback_query(c.id)

    # ── /autocode ─────────────────────────────────────────────────────────────

    def cmd_autocode(m: Message):
        active = len(get_active_rentals())
        expiring = len(get_expiring_rentals(2))
        bot.send_message(
            m.chat.id,
            f"⚙️ <b>AutoCode v3</b>\n\n"
            f"🏠 Активных аренд: <b>{active}</b>\n"
            f"⚠️ Истекают в 2ч: <b>{expiring}</b>",
            reply_markup=kb_main(), parse_mode="HTML"
        )

    # ── Регистрация ──────────────────────────────────────────────────────────

    tg.cbq_handler(open_plugin_settings, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}:"))
    tg.cbq_handler(open_main,     lambda c: c.data == AC_MAIN)
    tg.cbq_handler(open_list,     lambda c: c.data.startswith(f"{AC_LIST}:"))
    tg.cbq_handler(act_add,       lambda c: c.data == AC_ADD)
    tg.cbq_handler(open_edit,     lambda c: c.data.startswith(f"{AC_EDIT}:") and len(c.data.split(":")) == 2)
    tg.cbq_handler(ask_del,       lambda c: c.data.startswith(f"{AC_DEL_ASK}:"))
    tg.cbq_handler(do_del,        lambda c: c.data.startswith(f"{AC_DEL_OK}:"))

    tg.cbq_handler(act_age,       lambda c: c.data.startswith(f"{AC_MAX_AGE}:"))
    tg.cbq_handler(act_len,       lambda c: c.data.startswith(f"{AC_LEN}:"))
    tg.cbq_handler(open_type,     lambda c: c.data.startswith(f"{AC_TYPE}:"))
    tg.cbq_handler(toggle_spaces, lambda c: c.data.startswith(f"{AC_SPACES}:"))
    tg.cbq_handler(act_from,      lambda c: c.data.startswith(f"{AC_FROM}:"))
    tg.cbq_handler(act_subj,      lambda c: c.data.startswith(f"{AC_SUBJ}:"))
    tg.cbq_handler(act_body,      lambda c: c.data.startswith(f"{AC_BODY}:"))
    tg.cbq_handler(do_test,       lambda c: c.data.startswith(f"{AC_TEST}:"))
    tg.cbq_handler(open_log,      lambda c: c.data == AC_LOG)
    tg.cbq_handler(open_rentals,  lambda c: c.data == AC_RENTALS)
    tg.cbq_handler(do_rent_del,   lambda c: c.data.startswith(f"{AC_RENT_DEL}:") and not c.data.startswith(f"{AC_RENT_DEL_OK}:"))
    tg.cbq_handler(do_rent_del_ok,lambda c: c.data.startswith(f"{AC_RENT_DEL_OK}:"))
    tg.cbq_handler(do_rent_ext,   lambda c: c.data.startswith(f"{AC_RENT_EXT}:"))
    tg.cbq_handler(open_stats,    lambda c: c.data == AC_STATS)

    tg.msg_handler(got_email,  func=lambda m: tg.check_state(m.chat.id, m.from_user.id, ST_EMAIL))
    tg.msg_handler(got_pass,   func=lambda m: tg.check_state(m.chat.id, m.from_user.id, ST_PASS))
    tg.msg_handler(got_imap,   func=lambda m: tg.check_state(m.chat.id, m.from_user.id, ST_IMAP))
    tg.msg_handler(got_age,    func=lambda m: tg.check_state(m.chat.id, m.from_user.id, ST_MAX_AGE))
    tg.msg_handler(got_len,    func=lambda m: tg.check_state(m.chat.id, m.from_user.id, ST_LEN))
    tg.msg_handler(got_from,   func=lambda m: tg.check_state(m.chat.id, m.from_user.id, ST_FROM))
    tg.msg_handler(got_subj,   func=lambda m: tg.check_state(m.chat.id, m.from_user.id, ST_SUBJ))
    tg.msg_handler(got_body,   func=lambda m: tg.check_state(m.chat.id, m.from_user.id, ST_BODY))

    tg.msg_handler(cmd_autocode, commands=["autocode"])

    cardinal.add_telegram_commands(UUID, [
        ("autocode", "AutoCode — управление IMAP выдачей кодов", True)
    ])

    # Запускаем фоновый поток проверки истекших аренд
    Thread(target=_expiry_watcher, args=(cardinal,), daemon=True).start()


# ─── Фоновый поток: следит за истечением аренд ───────────────────────────────

def _expiry_watcher(cardinal: Cardinal):
    """
    Каждую минуту проверяет аренды.
    Если аренда истекла — пишет покупателю в чат FunPay и удаляет из базы.
    """
    notified_expiring = set()  # чтобы не спамить уведомлениями "скоро истекает"

    while True:
        try:
            time.sleep(60)
            now = time.time()
            rents = rentals()
            changed = False

            for order_key, r in list(rents.items()):
                expires_at = r.get("expires_at", 0)
                chat_id    = r.get("chat_id")
                chat_name  = r.get("buyer")
                buyer_name = r.get("buyer", order_key)

                # Уведомление за 30 минут до истечения (один раз)
                warn_key = f"{order_key}_warn"
                if (expires_at - now) <= 1800 and warn_key not in notified_expiring:
                    notified_expiring.add(warn_key)
                    if chat_id:
                        remaining = _fmt_remaining(expires_at)
                        Thread(
                            target=cardinal.send_message,
                            args=(chat_id,
                                  f"⚠️ Ваша аренда истекает через ~{remaining}.\n"
                                  f"Хотите продлить? Напишите нам!",
                                  chat_name),
                            daemon=True
                        ).start()

                # Аренда истекла
                if now >= expires_at:
                    if chat_id:
                        Thread(
                            target=cardinal.send_message,
                            args=(chat_id,
                                  f"✅ Ваша аренда завершена, спасибо за покупку!\n\n"
                                  f"Хотите продлить? Просто сделайте новый заказ 😊",
                                  chat_name),
                            daemon=True
                        ).start()
                    del rents[order_key]
                    notified_expiring.discard(warn_key)
                    changed = True
                    logger.info(f"[AutoCode] Аренда {order_key} ({buyer_name}) истекла и закрыта.")

            if changed:
                save_rentals(rents)

        except Exception as ex:
            logger.error(f"[AutoCode] Ошибка в фоновом потоке: {ex}")


# ─── Обработчик нового заказа (триггер) ──────────────────────────────────────

def on_new_order(c: Cardinal, e: NewOrderEvent):
    """
    Срабатывает когда FunPay фиксирует новый заказ.
    Парсим название лота → часы аренды → ищем почту в тексте автовыдачи.
    """
    order = e.order
    description = order.description or ""
    buyer       = order.buyer_username or ""
    chat_id     = order.chat_id
    lot_id      = str(getattr(order, "lot_id", "") or "")
    purchase_ts = order.date.timestamp() if order.date else time.time()

    # Ищем часы аренды в названии лота
    hours = parse_hours_from_lot(description)
    if not hours:
        # Лот без указания часов — пропускаем
        return

    # Ищем почту из настроенных аккаунтов которая привязана к этому лоту
    accs = accounts()
    matched_email = None
    matched_acc   = None
    for acc in accs:
        if not acc.get("lot_ids") or lot_id in acc.get("lot_ids", []):
            matched_email = acc["email"]
            matched_acc   = acc
            break

    if not matched_email:
        logger.info(f"[AutoCode] Заказ {order.id}: подходящей почты нет.")
        return

    expires_at = purchase_ts + hours * 3600

    # Сохраняем аренду по order_id — один покупатель может иметь несколько активных аренд
    rents = rentals()
    order_key = str(order.id) if order.id else f"{buyer}_{int(purchase_ts)}"
    rents[order_key] = {
        "order_key":   order_key,
        "buyer":       buyer,
        "chat_id":     chat_id,
        "email":       matched_email,
        "lot_id":      lot_id,
        "order_id":    order.id,
        "purchase_ts": purchase_ts,
        "expires_at":  expires_at,
        "hours":       hours,
    }
    save_rentals(rents)

    logger.info(
        f"[AutoCode] Новая аренда: {buyer} | {matched_email} | {hours}ч "
        f"| истекает {_fmt_time(expires_at)}"
    )


# ─── Обработчик команды !cd в чате FunPay ────────────────────────────────────

def on_new_message(c: Cardinal, e: NewMessageEvent):
    if e.message.author_id == c.account.id:
        return

    # Обрабатываем только команду !cd
    if (e.message.text or "").strip().lower() != "!cd":
        return

    chat_id   = e.message.chat_id
    chat_name = e.message.chat_name
    buyer     = e.message.author

    def process():
        # Проверяем есть ли активные аренды у покупателя (ищем по buyer в значениях)
        rents = rentals()
        now = time.time()
        # Все активные аренды этого покупателя, отсортированые по времени покупки (новейшие впереди)
        buyer_rentals = sorted(
            [r for r in rents.values() if r["buyer"] == buyer and r["expires_at"] > now],
            key=lambda r: r["purchase_ts"],
            reverse=True
        )

        if not buyer_rentals:
            c.send_message(chat_id,
                           "❌ У вас нет активной аренды. Пожалуйста, оформите заказ.",
                           chat_name)
            return

        # Берём самую позднюю активную аренду
        rental = buyer_rentals[0]

        # Находим настройки почты
        accs = accounts()
        acc = next((a for a in accs if a["email"] == rental["email"]), None)
        if not acc:
            # Почта была удалена — берём первую подходящую
            lot_id = rental.get("lot_id", "")
            acc = next(
                (a for a in accs if not a.get("lot_ids") or lot_id in a.get("lot_ids", [])),
                None
            )
        if not acc:
            c.send_message(chat_id, "❌ Почта не настроена. Обратитесь к продавцу.", chat_name)
            return

        purchase_ts = rental.get("purchase_ts")
        remaining   = _fmt_remaining(rental["expires_at"])

        c.send_message(chat_id, f"⏳ Ищу код...", chat_name)

        used = used_codes()

        # Ищем код ТОЛЬКО в письмах после момента покупки
        code, err = fetch_code(acc, used, not_before_ts=purchase_ts)

        if err:
            logger.warning(f"[AutoCode] {err}")
            # Уведомление в Telegram что код не найден
            _notify_tg_code_not_found(c, buyer, rental["email"], err)
            c.send_message(chat_id,
                           "❌ Не удалось получить код. Продавец уже уведомлён и поможет вам вручную.",
                           chat_name)
            return

        if not code:
            # Уведомление в Telegram
            _notify_tg_code_not_found(c, buyer, rental["email"], "Письмо не найдено")
            c.send_message(chat_id,
                           "❌ Код не найден. Попробуйте через минуту или обратитесь к продавцу.",
                           chat_name)
            return

        # Антидубль
        key = f"{acc['email']}:{code}"
        used[key] = {
            "buyer":  buyer,
            "lot_id": rental.get("lot_id", "—"),
            "time":   datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        }
        save_used(used)

        # Лог
        add_log(acc["email"], rental.get("lot_id", "—"), buyer, code)

        c.send_message(
            chat_id,
            f"📧 {acc['email']}: {code}\n⏳ Аренда активна ещё: {remaining}",
            chat_name
        )

    Thread(target=process, daemon=True).start()


def _notify_tg_code_not_found(cardinal: Cardinal, buyer: str, email_addr: str, reason: str):
    """Отправляет уведомление в Telegram когда код не найден."""
    try:
        text = (
            f"⚠️ <b>AutoCode: код не найден!</b>\n\n"
            f"👤 Покупатель: <b>{buyer}</b>\n"
            f"📧 Почта: <code>{email_addr}</code>\n"
            f"❗ Причина: {reason}\n\n"
            f"Требуется ручная помощь!"
        )
        cardinal.telegram.send_notification(text, notification_type="autocode_error")
    except Exception as ex:
        logger.warning(f"[AutoCode] Не удалось отправить уведомление: {ex}")


# ─── Хуки ────────────────────────────────────────────────────────────────────
BIND_TO_PRE_INIT    = [init_autocode_tg]
BIND_TO_NEW_ORDER   = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
