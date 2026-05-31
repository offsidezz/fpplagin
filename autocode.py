# autocode.py  –  AutoCode plugin for FunPay Cardinal
# VERSION = "5.1.3"

VERSION = "5.1.3"

import imaplib
import email as email_lib
import re
import time
import json
import os
import uuid as uuid_lib
import logging
from threading import Thread, Lock
from datetime import datetime, timedelta

logger = logging.getLogger("AutoCode")

# ── paths ────────────────────────────────────────────────────────────────────
_DIR   = os.path.dirname(__file__)
_RENT  = os.path.join(_DIR, "rentals.json")
_ACCS  = os.path.join(_DIR, "accounts.json")
_USED  = os.path.join(_DIR, "used_codes.json")
_CFG   = os.path.join(_DIR, "autocode.cfg")

_rent_lock = Lock()
_used_lock = Lock()

# ── tiny helpers ─────────────────────────────────────────────────────────────
def rentals():
    try:
        with open(_RENT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_rentals(data):
    with _rent_lock:
        with open(_RENT, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def accounts():
    try:
        with open(_ACCS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def used_codes():
    try:
        with open(_USED, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_used_codes(data):
    with _used_lock:
        with open(_USED, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def _cfg():
    cfg = {}
    try:
        with open(_CFG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg

def _fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")

def _notify_tg(cardinal, text):
    try:
        cardinal.telegram.send_notification(text)
    except Exception as e:
        logger.warning(f"AutoCode: TG notify error: {e}")

# ── parse hours from lot name ─────────────────────────────────────────────────
_H_RE = re.compile(
    r'(\d+)\s*(?:час[аов]*|h(?:ours?)?)',
    re.IGNORECASE | re.UNICODE,
)
_D_RE = re.compile(
    r'(\d+)\s*(?:дн[ейя]|day[s]?)',
    re.IGNORECASE | re.UNICODE,
)

def _parse_hours(text: str) -> int:
    m = _H_RE.search(text)
    if m:
        return int(m.group(1))
    m = _D_RE.search(text)
    if m:
        return int(m.group(1)) * 24
    return 0

# ── IMAP helpers ──────────────────────────────────────────────────────────────
def _imap_connect(acc: dict):
    host = acc.get("imap_host", "imap.gmail.com")
    port = int(acc.get("imap_port", 993))
    m    = imaplib.IMAP4_SSL(host, port)
    m.login(acc["email"], acc["password"])
    return m

def _fetch_code(acc: dict, buyer: str, hours: int) -> str | None:
    """Fetch activation code from email for buyer."""
    try:
        m = _imap_connect(acc)
        m.select("INBOX")
        since = (datetime.now() - timedelta(hours=max(hours * 2, 48))).strftime("%d-%b-%Y")
        _, data = m.search(None, f'(SINCE "{since}")')
        ids = data[0].split()
        for eid in reversed(ids[-50:]):
            _, msg_data = m.fetch(eid, "(RFC822)")
            msg = email_lib.message_from_bytes(msg_data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body += part.get_payload(decode=True).decode(errors="ignore")
            else:
                body = msg.get_payload(decode=True).decode(errors="ignore")
            code_match = re.search(r'\b([A-Z0-9]{4,6}-[A-Z0-9]{4,6}-[A-Z0-9]{4,6})\b', body)
            if code_match:
                m.logout()
                return code_match.group(1)
        m.logout()
    except Exception as e:
        logger.warning(f"AutoCode: IMAP error for {acc.get('email')}: {e}")
    return None

# ── expiry watcher ────────────────────────────────────────────────────────────
def _expiry_watcher(cardinal):
    """Background thread: checks rental expiry every minute."""
    while True:
        try:
            now   = time.time()
            rents = rentals()
            changed = False
            for key, r in list(rents.items()):
                exp = r.get("expires_at", 0)
                if exp and now >= exp:
                    buyer     = r.get("buyer", "?")
                    acc_email = r.get("email", "?")
                    logger.info(f"AutoCode: аренда истекла — {buyer} | {acc_email}")
                    _notify_tg(cardinal,
                        f"⏰ AutoCode: аренда истекла!\n"
                        f"Покупатель: {buyer}\n"
                        f"Аккаунт: {acc_email}\n"
                        f"Истекла: {_fmt_time(exp)}"
                    )
                    del rents[key]
                    changed = True
            if changed:
                save_rentals(rents)
        except Exception as e:
            logger.error(f"AutoCode: expiry watcher error: {e}")
        time.sleep(60)

# ── startup IMAP test ─────────────────────────────────────────────────────────
def _startup_imap_test(cardinal):
    """Test IMAP connections on startup."""
    time.sleep(5)
    accs = accounts()
    if not accs:
        logger.warning("AutoCode: нет аккаунтов в accounts.json")
        return
    ok  = []
    bad = []
    for acc in accs:
        try:
            m = _imap_connect(acc)
            m.logout()
            ok.append(acc["email"])
        except Exception as e:
            bad.append(f"{acc['email']} ({e})")
    if ok:
        logger.info(f"AutoCode: IMAP OK: {', '.join(ok)}")
    if bad:
        logger.warning(f"AutoCode: IMAP FAIL: {', '.join(bad)}")
        _notify_tg(cardinal,
            f"⚠️ AutoCode: ошибка IMAP при старте:\n" + "\n".join(bad)
        )

# ── Startup sales scan (restore rentals from last 30 days) ──────────────────
def _startup_sales_scan(cardinal):
    """
    При старте сканирует продажи за последние 30 дней через cardinal.account.get_sales().
    Для каждой продажи у которой:
      - название лота содержит часы/дни (parse_hours > 0)
      - аренда ещё не истекла (purchase_ts + hours*3600 > now)
      - аренда ещё не записана в rentals.json
    — создаёт запись в rentals.json.
    Это позволяет восстановить активные аренды после рестарта Cardinal.
    """
    time.sleep(10)  # Ждём пока Cardinal полностью инициализируется
    try:
        acc_obj = cardinal.account
        now     = time.time()
        cutoff  = now - 30 * 24 * 3600  # 30 дней назад

        logger.info("AutoCode: сканирование продаж за 30 дней...")

        all_shortcuts = []
        start_from    = None
        pages         = 0

        while pages < 20:  # Максимум 20 страниц (защита от бесконечного цикла)
            try:
                result = acc_obj.get_sales(
                    start_from=start_from,
                    include_paid=True,
                    include_closed=True,
                    include_refunded=False,
                )
                # get_sales возвращает tuple: (next_id, list[OrderShortcut], locale, subcats)
                if isinstance(result, tuple):
                    next_id    = result[0]
                    shortcuts  = result[1] if len(result) > 1 else []
                else:
                    break

                if not shortcuts:
                    break

                all_shortcuts.extend(shortcuts)
                pages += 1

                # Проверяем дату последнего заказа на странице
                # OrderShortcut имеет атрибут date (datetime) или date_ts (timestamp)
                last = shortcuts[-1]
                last_ts = None
                if hasattr(last, "date_ts"):
                    last_ts = last.date_ts
                elif hasattr(last, "date") and last.date:
                    try:
                        last_ts = last.date.timestamp()
                    except Exception:
                        pass

                if last_ts and last_ts < cutoff:
                    break  # Дошли до заказов старше 30 дней

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

        # Собираем уже известные order_id чтобы не дублировать
        existing_order_ids = {r.get("order_id") for r in rents.values()}

        for shortcut in all_shortcuts:
            try:
                # Получаем данные из OrderShortcut
                order_id   = str(getattr(shortcut, "id", "") or getattr(shortcut, "order_id", ""))
                lot_name   = str(getattr(shortcut, "description", "") or getattr(shortcut, "lot_name", "") or "")
                buyer      = str(getattr(shortcut, "buyer_username", "") or getattr(shortcut, "buyer", "") or "")
                lot_id     = str(getattr(shortcut, "lot_id", "") or "")

                # Дата заказа
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

                # Пропускаем заказы старше 30 дней
                if purchase_ts < cutoff:
                    skipped += 1
                    continue

                # Пропускаем если уже есть в rentals
                if order_id and order_id in existing_order_ids:
                    skipped += 1
                    continue

                # Определяем часы аренды
                hours = _parse_hours(lot_name)
                if not hours:
                    skipped += 1
                    continue

                # Вычисляем expires_at
                expires_at = purchase_ts + hours * 3600

                # Пропускаем уже истёкшие
                if expires_at <= now:
                    skipped += 1
                    continue

                # Находим email аккаунта для этого лота
                acc_email = None
                for a in accs:
                    if lot_id in a.get("lot_ids", []) or not a.get("lot_ids"):
                        acc_email = a["email"]
                        break

                if not acc_email:
                    skipped += 1
                    continue

                # Получаем chat_id — пробуем через get_chat_by_name
                chat_id   = None
                chat_name = buyer
                try:
                    chat = acc_obj.get_chat_by_name(buyer, True)
                    if chat:
                        chat_id   = chat.id
                        chat_name = getattr(chat, "name", buyer)
                except Exception:
                    pass

                if not chat_id:
                    skipped += 1
                    logger.debug(f"AutoCode: не удалось найти чат для {buyer}, пропускаем.")
                    continue

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
                    "restored":    True,  # Метка что аренда восстановлена при старте
                }
                existing_order_ids.add(order_id)
                restored += 1
                logger.info(
                    f"AutoCode: восстановлена аренда {buyer} | {acc_email} | "
                    f"{hours}ч | до {_fmt_time(expires_at)} | order={order_id}"
                )

            except Exception as e:
                logger.warning(f"AutoCode: ошибка обработки заказа: {e}")
                continue

        if restored > 0:
            save_rentals(rents)

        logger.info(
            f"AutoCode: сканирование завершено. "
            f"Восстановлено: {restored}, пропущено: {skipped}."
        )

        if restored > 0:
            _notify_tg(cardinal,
                f"✅ AutoCode: при старте восстановлено {restored} активных аренд "
                f"из истории продаж за 30 дней."
            )

    except Exception as e:
        logger.error(f"AutoCode: ошибка сканирования продаж при старте: {e}")

# ── used-code cleanup worker ──────────────────────────────────────────────────
def _used_code_cleanup_worker():
    """Remove used codes older than 30 days."""
    while True:
        try:
            now   = time.time()
            codes = used_codes()
            changed = False
            for code, ts in list(codes.items()):
                if now - ts > 30 * 24 * 3600:
                    del codes[code]
                    changed = True
            if changed:
                save_used_codes(codes)
        except Exception as e:
            logger.error(f"AutoCode: used_code cleanup error: {e}")
        time.sleep(3600)

# ── weekly report worker ──────────────────────────────────────────────────────
def _weekly_report_worker(cardinal):
    """Send weekly rental summary."""
    while True:
        time.sleep(7 * 24 * 3600)
        try:
            rents = rentals()
            if not rents:
                _notify_tg(cardinal, "📊 AutoCode: активных аренд нет.")
                continue
            lines = [f"📊 AutoCode: активные аренды ({len(rents)}):"]
            for r in rents.values():
                lines.append(
                    f"• {r.get('buyer','?')} | {r.get('email','?')} | "
                    f"до {_fmt_time(r.get('expires_at', 0))}"
                )
            _notify_tg(cardinal, "\n".join(lines))
        except Exception as e:
            logger.error(f"AutoCode: weekly report error: {e}")

# ── order handler ─────────────────────────────────────────────────────────────
def handle_new_order(cardinal, event):
    """Called on new order event."""
    try:
        order    = event.order
        lot_name = str(getattr(order, "description", "") or getattr(order, "lot_name", "") or "")
        hours    = _parse_hours(lot_name)
        if not hours:
            return

        buyer    = str(getattr(order, "buyer_username", "") or getattr(order, "buyer", "") or "")
        lot_id   = str(getattr(order, "lot_id", "") or "")
        order_id = str(getattr(order, "id", "") or getattr(order, "order_id", "") or "")

        accs = accounts()
        acc_email = None
        acc_obj   = None
        for a in accs:
            if lot_id in a.get("lot_ids", []) or not a.get("lot_ids"):
                acc_email = a["email"]
                acc_obj   = a
                break

        if not acc_email:
            logger.warning(f"AutoCode: нет аккаунта для лота {lot_id}")
            return

        code = _fetch_code(acc_obj, buyer, hours)
        if not code:
            logger.warning(f"AutoCode: код не найден для {buyer}")
            _notify_tg(cardinal, f"⚠️ AutoCode: код не найден для {buyer} | {acc_email}")
            return

        # Mark code as used
        uc = used_codes()
        uc[code] = time.time()
        save_used_codes(uc)

        now        = time.time()
        expires_at = now + hours * 3600

        # Save rental
        key = order_id or str(uuid_lib.uuid4())
        rents = rentals()

        chat_id   = None
        chat_name = buyer
        try:
            chat = cardinal.account.get_chat_by_name(buyer, True)
            if chat:
                chat_id   = chat.id
                chat_name = getattr(chat, "name", buyer)
        except Exception:
            pass

        rents[key] = {
            "order_key":   key,
            "buyer":       buyer,
            "chat_id":     chat_id,
            "chat_name":   chat_name,
            "email":       acc_email,
            "lot_id":      lot_id,
            "order_id":    order_id,
            "purchase_ts": now,
            "expires_at":  expires_at,
            "hours":       hours,
            "code":        code,
        }
        save_rentals(rents)

        # Send code to buyer
        if chat_id:
            try:
                cardinal.account.send_message(chat_id, f"Ваш код активации: {code}")
            except Exception as e:
                logger.warning(f"AutoCode: не удалось отправить код {buyer}: {e}")

        logger.info(f"AutoCode: выдан код {buyer} | {acc_email} | {hours}ч | до {_fmt_time(expires_at)}")
        _notify_tg(cardinal,
            f"✅ AutoCode: выдан код!\n"
            f"Покупатель: {buyer}\n"
            f"Аккаунт: {acc_email}\n"
            f"Аренда: {hours}ч до {_fmt_time(expires_at)}\n"
            f"Код: {code}"
        )

    except Exception as e:
        logger.error(f"AutoCode: handle_new_order error: {e}")

# ── Telegram command handlers ─────────────────────────────────────────────────
def cmd_autocode(cardinal, message, args):
    """Handle /autocode command."""
    rents = rentals()
    if not rents:
        cardinal.telegram.send_message(message.chat.id, "AutoCode v5.1.3: активных аренд нет.")
        return
    lines = [f"AutoCode v5.1.3: активные аренды ({len(rents)}):"]
    for r in rents.values():
        lines.append(
            f"• {r.get('buyer','?')} | {r.get('email','?')} | "
            f"до {_fmt_time(r.get('expires_at', 0))}"
        )
    cardinal.telegram.send_message(message.chat.id, "\n".join(lines))

def open_main(cardinal, call):
    """Handle main menu callback."""
    rents = rentals()
    text  = (
        f"AutoCode v5.1.3\n"
        f"Активных аренд: {len(rents)}\n"
    )
    cardinal.telegram.send_message(call.message.chat.id, text)

# ── init ──────────────────────────────────────────────────────────────────────
def init_autocode_tg(cardinal):
    """Initialize AutoCode plugin."""
    logger.info("AutoCode: инициализация...")

    # Ensure data files exist
    for path, default in [(_RENT, {}), (_USED, {}), (_ACCS, [])]:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f)

    Thread(target=_expiry_watcher,           args=(cardinal,), daemon=True).start()
    Thread(target=_startup_imap_test,        args=(cardinal,), daemon=True).start()
    Thread(target=_startup_sales_scan,       args=(cardinal,), daemon=True).start()
    Thread(target=_used_code_cleanup_worker,              daemon=True).start()
    Thread(target=_weekly_report_worker,     args=(cardinal,), daemon=True).start()
    logger.info("AutoCode v5.1.3 инициализирован.")
