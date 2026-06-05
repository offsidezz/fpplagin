from __future__ import annotations
from typing import TYPE_CHECKING, Optional
import json
import os
import re

if TYPE_CHECKING:
    from cardinal import Cardinal

from FunPayAPI.updater.events import OrderStatusChangedEvent
from threading import Thread, Lock

NAME = "Bonus Hours"
VERSION = "1.3"
DESCRIPTION = "Автоматически начисляет бонусные часы за отзыв. Количество часов берёт из краткого описания товара по паттерну +NЧ."
CREDITS = "@spec"
UUID = "f75e7181-9840-4e0c-8c93-6bf69463ead7"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "bonus_hours_log.json")
DATA_LOCK = Lock()

DEFAULT_SETTINGS = {
    "fallback_hours": 0,
    "notify_user": True,
    "notify_text": "🎁 Бонус +{hours}Ч за отзыв! Спасибо!",
    "only_positive_review": True,
    "bonus_pattern": r"[+➕](\d+)\s*[Чч]"
}

def _ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)

def _load_log() -> dict:
    _ensure_data_dir()
    if not os.path.exists(DATA_FILE):
        return {"processed_orders": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {"processed_orders": []}

def _save_log(log: dict):
    _ensure_data_dir()
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

def _is_processed(order_id) -> bool:
    log = _load_log()
    return str(order_id) in log.get("processed_orders", [])

def _mark_processed(order_id):
    with DATA_LOCK:
        log = _load_log()
        if str(order_id) not in log.get("processed_orders", []):
            log.setdefault("processed_orders", []).append(str(order_id))
            _save_log(log)

def _extract_bonus_hours(short_desc: str) -> Optional[int]:
    if not short_desc:
        return None
    pattern = r"[+➕]\s*(\d+)\s*[Чч]"
    match = re.search(pattern, short_desc)
    if match:
        return int(match.group(1))
    return None

def _apply_bonus_hours(c: Cardinal, order, hours: int):
    if hasattr(c, "rentals") and hasattr(c.rentals, "add_hours"):
        c.rentals.add_hours(order.id, hours)
        return True
    if hasattr(c, "extend_rental"):
        c.extend_rental(order.id, hours)
        return True
    if hasattr(order, "extend"):
        order.extend(hours)
        return True
    return False

def _get_settings(c: Cardinal) -> dict:
    settings = DEFAULT_SETTINGS.copy()
    if hasattr(c, "get_plugin_settings"):
        try:
            saved = c.get_plugin_settings(UUID) or {}
            settings.update(saved)
        except Exception:
            pass
    return settings

def on_order_status_changed(c: Cardinal, e):
    settings = _get_settings(c)
    if not hasattr(e, "order") or not hasattr(e.order, "status"):
        return
    status = getattr(e.order, "status", "").lower()
    if status not in ("completed", "выполнен", "done"):
        return
    order = e.order
    order_id = getattr(order, "id", None)
    if order_id is None:
        return
    if _is_processed(order_id):
        return
    has_review = False
    if hasattr(order, "review") and order.review is not None:
        has_review = True
    if not has_review and hasattr(order, "has_review"):
        has_review = order.has_review
    if not has_review and hasattr(order, "rating") and order.rating is not None and order.rating > 0:
        has_review = True
    if not has_review:
        return
    if settings.get("only_positive_review", True):
        rating = getattr(order, "rating", None) or getattr(order, "review_rating", None)
        if rating is not None and rating < 4:
            return
    short_desc = None
    lot = getattr(order, "lot", None)
    if lot is None and hasattr(order, "product"):
        lot = order.product
    if lot is not None:
        short_desc = (getattr(lot, "short_description", None) or 
                      getattr(lot, "shortdesc", None) or 
                      getattr(lot, "description", None))
    if short_desc is None and hasattr(order, "item_title"):
        short_desc = order.item_title
    hours = _extract_bonus_hours(short_desc) if short_desc else None
    if hours is None:
        hours = settings.get("fallback_hours", 0)
        if hours <= 0:
            return
    bonus_applied = _apply_bonus_hours(c, order, hours)
    _mark_processed(order_id)
    if settings.get("notify_user", True):
        chat_id = getattr(order, "chat_id", None) or getattr(e, "chat_id", None)
        chat_name = getattr(order, "chat_name", None) or getattr(e, "chat_name", None)
        if chat_id is None and hasattr(order, "buyer"):
            buyer = order.buyer
            chat_id = getattr(buyer, "chat_id", None)
            chat_name = getattr(buyer, "username", None)
        if chat_id:
            notify_text = settings.get("notify_text", "🎁 Бонус +{hours}Ч за отзыв! Спасибо!")
            notify_text = notify_text.replace("{hours}", str(hours))
            if not bonus_applied:
                notify_text += "\n(часы будут начислены вручную)"
            Thread(
                target=c.send_message,
                args=(chat_id, notify_text, chat_name or ""),
                daemon=True
            ).start()

BIND_TO_ORDER_STATUS_CHANGED = [on_order_status_changed]