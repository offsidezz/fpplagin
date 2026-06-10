"""Test bootstrap for AutoCode.

autocode.py imports a handful of FunPay / Telegram modules that are only
available inside a running FunPay Cardinal host. We stub them here so the
plugin's pure logic can be imported and unit-tested in isolation.
"""
import os
import sys
import types
import tempfile

# ── Stub external host modules before importing autocode ──────────────────


def _make_module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# FunPayAPI.updater.events
fpa = _make_module("FunPayAPI")
fpa_updater = _make_module("FunPayAPI.updater")
fpa_events = _make_module("FunPayAPI.updater.events")
for _cls in ("NewMessageEvent", "NewOrderEvent", "OrderStatusChangedEvent"):
    setattr(fpa_events, _cls, type(_cls, (), {}))
fpa.updater = fpa_updater
fpa_updater.events = fpa_events

# FunPayAPI.common.enums
fpa_common = _make_module("FunPayAPI.common")
fpa_enums = _make_module("FunPayAPI.common.enums")
fpa_enums.MessageTypes = type("MessageTypes", (), {})
fpa.common = fpa_common
fpa_common.enums = fpa_enums

# tg_bot
tg_bot = _make_module("tg_bot")
tg_bot.CBT = type("CBT", (), {})

# telebot.types
telebot = _make_module("telebot")
telebot_types = _make_module("telebot.types")
for _cls in ("InlineKeyboardMarkup", "InlineKeyboardButton", "Message", "CallbackQuery"):
    setattr(telebot_types, _cls, type(_cls, (), {}))
telebot.types = telebot_types


# ── Isolate plugin storage into a temp dir so tests never touch real data ──
_TMP = tempfile.mkdtemp(prefix="autocode_test_")
os.chdir(_TMP)

# Make autocode.py importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
