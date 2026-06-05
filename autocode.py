from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from card import Cardinal
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
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import partial
from html.parser import HTMLParser
from http.client import HTTPSConnection
from io import BytesIO, StringIO
from pathlib import Path
from threading import Lock, Thread, Event
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from zipfile import ZipFile

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from FunPayAPI.account import Account
from FunPayAPI.categories import CategoriesManager
from FunPayAPI.common import FunPayAPIError
from FunPayAPI.enums import OrderStatus, MessageType
from FunPayAPI.fast_types import FastLot, FastOrder
from FunPayAPI.runner import Runner
from FunPayAPI.types import Message, Order, Lot
from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent, OrderStatusChangedEvent

# Paths and constants
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
LANG_CACHE_FILE = os.path.join(DATA_DIR, "lang_cache.json")
BONUS_LOG_FILE = os.path.join(DATA_DIR, "bonus_hours_log.json")

# Logger setup
logger = logging.getLogger("AutoCode")
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler(os.path.join(DATA_DIR, "autocode.log"), encoding="utf-8")
fh.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
fh.setFormatter(formatter)
ch.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(ch)

# Global state
cardinal: Optional['Cardinal'] = None
config: Dict[str, Any] = {}
locks = {
    "config": Lock(),
    "log": Lock(),
    "bonus_log": Lock(),
}
shutdown_event = Event()

# ============================================================
# CONFIGURATION
# ============================================================

DEFAULT_CONFIG = {
    "bonus_hours_enabled": True,
    "bonus_hours_pattern": r"(\d+)\s*часов\s*бесплатно",
    "bonus_notify_text": "✅ Бонус +{hours}ч применён к заказу #{order_id}",
    "bonus_fallback": 1,
    "language": "ru",
    "auto_reply_enabled": False,
    "auto_reply_text": "",
    "auto_reply_delay": 0,
}

def load_config() -> Dict[str, Any]:
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            logger.info("Config loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            config = DEFAULT_CONFIG.copy()
    else:
        config = DEFAULT_CONFIG.copy()
        save_config()
    return config

def save_config() -> None:
    with locks["config"]:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            logger.debug("Config saved")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

def app_settings() -> Dict[str, Any]:
    return {
        "bonus_hours_enabled": {
            "type": "bool",
            "label": "Включить бонусные часы",
            "default": True,
        },
        "bonus_hours_pattern": {
            "type": "string",
            "label": "Регулярное выражение для поиска часов",
            "default": r"(\d+)\s*часов\s*бесплатно",
        },
        "bonus_notify_text": {
            "type": "string",
            "label": "Текст уведомления о бонусе",
            "default": "✅ Бонус +{hours}ч применён к заказу #{order_id}",
        },
        "bonus_fallback": {
            "type": "int",
            "label": "Бонус по умолчанию (часы)",
            "default": 1,
        },
        "language": {
            "type": "select",
            "label": "Язык",
            "options": ["ru", "en"],
            "default": "ru",
        },
    }

# ============================================================
# BONUS HOURS LOGIC
# ============================================================

def _bonus_load_log() -> List[Dict[str, Any]]:
    with locks["bonus_log"]:
        if os.path.exists(BONUS_LOG_FILE):
            try:
                with open(BONUS_LOG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load bonus log: {e}")
        return []

def _bonus_save_log(log_data: List[Dict[str, Any]]) -> None:
    with locks["bonus_log"]:
        try:
            with open(BONUS_LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(log_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save bonus log: {e}")

def _bonus_is_processed(order_id: Union[str, int]) -> bool:
    log = _bonus_load_log()
    return any(entry.get("order_id") == str(order_id) for entry in log)

def _bonus_mark_processed(order_id: Union[str, int], hours: int, order_info: Optional[Dict] = None) -> None:
    log = _bonus_load_log()
    entry = {
        "order_id": str(order_id),
        "bonus_hours": hours,
        "timestamp": datetime.utcnow().isoformat(),
        "order_info": order_info or {},
    }
    log.append(entry)
    _bonus_save_log(log)
    logger.info(f"Marked order #{order_id} as processed with +{hours}h bonus")

def _bonus_extract_hours(text: str) -> Optional[int]:
    pattern = config.get("bonus_hours_pattern", DEFAULT_CONFIG["bonus_hours_pattern"])
    try:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    except Exception as e:
        logger.error(f"Failed to extract hours from text: {e}")
    fallback = config.get("bonus_fallback", DEFAULT_CONFIG["bonus_fallback"])
    logger.debug(f"No match found, using fallback: {fallback}h")
    return fallback

def _bonus_add_hours_to_rental(order: Order, hours: int) -> bool:
    try:
        if not cardinal or not hasattr(cardinal, 'add_hours_to_order'):
            logger.warning("cardinal.add_hours_to_order not available")
            return False
        result = cardinal.add_hours_to_order(order.id, hours)
        if result:
            logger.info(f"Successfully added {hours}h bonus to order #{order.id}")
        else:
            logger.warning(f"Failed to add {hours}h bonus to order #{order.id}")
        return result
    except Exception as e:
        logger.error(f"Error adding bonus hours to order #{order.id}: {e}")
        return False

def _bonus_on_order_status_changed(event: OrderStatusChangedEvent) -> None:
    try:
        order = event.order
        if not order or not hasattr(order, 'id'):
            return
        order_id = order.id
        if _bonus_is_processed(order_id):
            logger.debug(f"Order #{order_id} already processed for bonus")
            return
        if not config.get("bonus_hours_enabled", DEFAULT_CONFIG["bonus_hours_enabled"]):
            logger.debug("Bonus hours disabled, skipping")
            return
        # Check if order has a message/description with bonus hours info
        order_text = ""
        if hasattr(order, 'description') and order.description:
            order_text += order.description + " "
        if hasattr(order, 'message') and order.message:
            order_text += order.message + " "
        if hasattr(order, 'text') and order.text:
            order_text += order.text
        if not order_text.strip():
            logger.debug(f"No text found for order #{order_id}, skipping bonus")
            return
        hours = _bonus_extract_hours(order_text)
        if hours and hours > 0:
            success = _bonus_add_hours_to_rental(order, hours)
            if success:
                notify_text = config.get("bonus_notify_text", DEFAULT_CONFIG["bonus_notify_text"])
                try:
                    notify_msg = notify_text.format(hours=hours, order_id=order_id)
                except Exception:
                    notify_msg = f"✅ Бонус +{hours}ч применён к заказу #{order_id}"
                logger.info(notify_msg)
                _bonus_mark_processed(order_id, hours, {
                    "extracted_from": order_text[:200],
                })
            else:
                logger.warning(f"Bonus hours application failed for order #{order_id}")
        else:
            logger.debug(f"No bonus hours extracted for order #{order_id}")
    except Exception as e:
        logger.error(f"Error in bonus_on_order_status_changed: {e}")

# ============================================================
# EVENT BINDINGS
# ============================================================

BIND_TO_NEW_MESSAGE = []
BIND_TO_NEW_ORDER = []
BIND_TO_ORDER_STATUS_CHANGED = [_bonus_on_order_status_changed]
BIND_TO_PRE_INIT = []
BIND_TO_POST_INIT = []
BIND_TO_PRE_SHUTDOWN = []
BIND_TO_POST_SHUTDOWN = []

# ============================================================
# MAIN ENTRY POINT
# ============================================================

def init(cardinal_instance: 'Cardinal') -> None:
    global cardinal
    cardinal = cardinal_instance
    load_config()
    logger.info("AutoCode plugin initialized")

def shutdown() -> None:
    shutdown_event.set()
    save_config()
    logger.info("AutoCode plugin shut down")