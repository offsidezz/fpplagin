"""Unit tests for the relay code source (supplier → buyer) in AutoCode."""
import types as _types

import pytest

import autocode as ac


# ── helpers ──────────────────────────────────────────────────────────────
class _SyncThread:
    """Runs the target synchronously on .start() so threaded sends are
    deterministic in tests."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        if self._t:
            self._t(*self._a, **self._k)


def _msg_event(text, author, author_id, chat_id, chat_name=None):
    msg = _types.SimpleNamespace(
        text=text, author=author, author_id=author_id,
        chat_id=chat_id, chat_name=chat_name or author)
    return _types.SimpleNamespace(message=msg)


def _fake_cardinal(seller_id=7028500, name_lookup=None):
    sent = []

    def send_message(cid, text, cname=""):
        sent.append((str(cid), text, cname))

    account = _types.SimpleNamespace(
        id=seller_id,
        get_chat=lambda cid: None,
        get_chat_by_name=lambda name, *a, **k: (name_lookup or {}).get(name),
        telegram=None,
    )
    c = _types.SimpleNamespace(account=account, send_message=send_message,
                               telegram=_types.SimpleNamespace(
                                   bot=None, authorized_users=[]))
    c._sent = sent
    return c


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Reset relay runtime state, make threads synchronous, use real
    templates, and disable the timeout worker + TG notify side effects."""
    ac._relay_pending.clear()
    ac._relay_chat_to_supplier.clear()
    ac._relay_supplier_chat.clear()
    ac._relay_worker_started = False
    ac._code_requests.clear()
    ac._last_code_ts.clear()
    ac.save_rentals({})
    ac.save_used({})
    monkeypatch.setattr(ac, "Thread", _SyncThread)
    monkeypatch.setattr(ac, "_start_relay_worker", lambda *a, **k: None)
    monkeypatch.setattr(ac, "_notify_tg", lambda *a, **k: None)
    monkeypatch.setattr(ac, "_schedule_review_request", lambda *a, **k: None)
    monkeypatch.setattr(ac, "_schedule_order_confirm", lambda *a, **k: None)
    # real-ish templates: format from L_DEFAULTS
    monkeypatch.setattr(ac, "t", lambda key, **kw: (
        ac.L_DEFAULTS.get(key, key).format(**kw) if kw
        else ac.L_DEFAULTS.get(key, key)))
    yield


def _seed_relay_rental(email="relay:Seller", lot_id="555", buyer="Buyer",
                       chat_id="200", order_id="O1"):
    rent = {
        "order_key": order_id, "buyer": buyer, "buyer_id": None,
        "chat_id": chat_id, "chat_name": buyer, "email": email,
        "lot_id": lot_id, "order_id": order_id,
        "purchase_ts": ac.time.time() - 3600,
        "expires_at": ac.time.time() + 36000, "hours": 24, "lang": "ru",
    }
    ac.save_rentals({order_id: rent})
    return rent


# ── small units ──────────────────────────────────────────────────────────
def test_is_relay():
    assert ac._is_relay({"source": "relay"}) is True
    assert ac._is_relay({"source": "imap"}) is False
    assert ac._is_relay({}) is False  # default imap


def test_test_imap_short_circuits_relay():
    res = ac.test_imap({"source": "relay", "supplier": "Seller", "email": "relay:Seller"})
    assert res.startswith("✅") and "Seller" in res


def test_full_check_relay_skips_imap():
    r = ac.full_check_account({"source": "relay", "supplier": "S", "email": "relay:S"})
    assert r["imap_ok"] and r["code_ok"]


def test_extract_code_default():
    assert ac._relay_extract_code(ac.RELAY_DEFAULT_REGEX, "код: 482913 спасибо") == "482913"


def test_extract_code_custom_regex():
    assert ac._relay_extract_code(r"Guard:\s*([A-Z0-9]{5})", "Guard: A1B2C") == "A1B2C"


def test_extract_code_bad_regex_falls_back():
    # invalid regex → falls back to default digit pattern
    assert ac._relay_extract_code(r"([", "code 123456") == "123456"


def test_select_account_email_returns_relay_account(monkeypatch):
    accs = [{"email": "relay:Seller", "source": "relay", "supplier": "Seller",
             "lot_ids": ["555"]}]
    assert ac._select_account_email(accs, "555", set()) == "relay:Seller"


# ── full relay flow ────────────────────────────────────────────────────────
def test_relay_full_flow(monkeypatch):
    relay_acc = {"email": "relay:Seller", "source": "relay", "supplier": "Seller",
                 "lot_ids": ["555"]}
    monkeypatch.setattr(ac, "accounts", lambda: [relay_acc])
    _seed_relay_rental()
    c = _fake_cardinal(name_lookup={"Seller": _types.SimpleNamespace(id=555)})

    # buyer with an active rental asks for the code
    ac.on_new_message(c, _msg_event("code", "Buyer", 42, 200, "Buyer"))

    # supplier (chat 555) was asked, and a pending request is registered
    assert any(cid == "555" for cid, _, _ in c._sent), c._sent
    assert sum(len(q) for q in ac._relay_pending.values()) == 1

    # supplier replies with the code
    ac.on_new_message(c, _msg_event("Ваш код: 482913", "Seller", 999, 555, "Seller"))

    # code delivered to the buyer's chat (200), queue drained
    assert any(cid == "200" and "482913" in txt for cid, txt, _ in c._sent), c._sent
    assert sum(len(q) for q in ac._relay_pending.values()) == 0


def test_relay_supplier_noncode_keeps_waiting(monkeypatch):
    relay_acc = {"email": "relay:Seller", "source": "relay", "supplier": "Seller",
                 "lot_ids": ["555"]}
    monkeypatch.setattr(ac, "accounts", lambda: [relay_acc])
    _seed_relay_rental()
    c = _fake_cardinal(name_lookup={"Seller": _types.SimpleNamespace(id=555)})
    ac.on_new_message(c, _msg_event("code", "Buyer", 42, 200, "Buyer"))
    # supplier sends chit-chat without a code
    ac.on_new_message(c, _msg_event("секунду", "Seller", 999, 555, "Seller"))
    assert sum(len(q) for q in ac._relay_pending.values()) == 1  # still waiting


def test_relay_no_supplier_set(monkeypatch):
    relay_acc = {"email": "relay:x", "source": "relay", "supplier": "",
                 "lot_ids": ["555"]}
    monkeypatch.setattr(ac, "accounts", lambda: [relay_acc])
    _seed_relay_rental(email="relay:x")
    c = _fake_cardinal()
    ac.on_new_message(c, _msg_event("code", "Buyer", 42, 200, "Buyer"))
    # buyer is told the source isn't configured, nothing pending
    assert any("relay_no_supplier" in ac.L_DEFAULTS["relay_no_supplier"]
               or ac.L_DEFAULTS["relay_no_supplier"] in txt
               for _, txt, _ in c._sent)
    assert sum(len(q) for q in ac._relay_pending.values()) == 0


def test_relay_supplier_chat_unresolved(monkeypatch):
    relay_acc = {"email": "relay:Ghost", "source": "relay", "supplier": "Ghost",
                 "lot_ids": ["555"]}
    monkeypatch.setattr(ac, "accounts", lambda: [relay_acc])
    _seed_relay_rental(email="relay:Ghost")
    c = _fake_cardinal(name_lookup={})  # supplier chat not found
    ac.on_new_message(c, _msg_event("code", "Buyer", 42, 200, "Buyer"))
    # buyer gets a failure message, nothing pending
    assert any(ac.L_DEFAULTS["code_not_found"] in txt for _, txt, _ in c._sent)
    assert sum(len(q) for q in ac._relay_pending.values()) == 0


def test_relay_fifo_two_buyers(monkeypatch):
    relay_acc = {"email": "relay:Seller", "source": "relay", "supplier": "Seller",
                 "lot_ids": ["555"]}
    monkeypatch.setattr(ac, "accounts", lambda: [relay_acc])
    # two active rentals, two buyers, same supplier
    now = ac.time.time()
    ac.save_rentals({
        "O1": {"order_key": "O1", "buyer": "BuyerA", "chat_id": "201",
               "chat_name": "BuyerA", "email": "relay:Seller", "lot_id": "555",
               "order_id": "O1", "purchase_ts": now - 3600,
               "expires_at": now + 36000},
        "O2": {"order_key": "O2", "buyer": "BuyerB", "chat_id": "202",
               "chat_name": "BuyerB", "email": "relay:Seller", "lot_id": "555",
               "order_id": "O2", "purchase_ts": now - 3600,
               "expires_at": now + 36000},
    })
    c = _fake_cardinal(name_lookup={"Seller": _types.SimpleNamespace(id=555)})
    ac.on_new_message(c, _msg_event("code", "BuyerA", 1, 201, "BuyerA"))
    ac.on_new_message(c, _msg_event("code", "BuyerB", 2, 202, "BuyerB"))
    # first reply → BuyerA (FIFO)
    ac.on_new_message(c, _msg_event("111111", "Seller", 999, 555, "Seller"))
    assert any(cid == "201" and "111111" in txt for cid, txt, _ in c._sent)
    # second reply → BuyerB
    ac.on_new_message(c, _msg_event("222222", "Seller", 999, 555, "Seller"))
    assert any(cid == "202" and "222222" in txt for cid, txt, _ in c._sent)


def test_relay_reply_hook_ignores_own_messages(monkeypatch):
    relay_acc = {"email": "relay:Seller", "source": "relay", "supplier": "Seller",
                 "lot_ids": ["555"]}
    monkeypatch.setattr(ac, "accounts", lambda: [relay_acc])
    _seed_relay_rental()
    c = _fake_cardinal(name_lookup={"Seller": _types.SimpleNamespace(id=555)})
    ac.on_new_message(c, _msg_event("code", "Buyer", 42, 200, "Buyer"))
    assert sum(len(q) for q in ac._relay_pending.values()) == 1
    # the bot's own outgoing message in the supplier chat must NOT consume the
    # pending request (it's not a supplier reply)
    handled = ac._relay_try_handle_supplier_reply(
        c, _msg_event("482913", "Me", c.account.id, 555, "Me"))
    assert handled is False
    assert sum(len(q) for q in ac._relay_pending.values()) == 1


def test_relay_used_code_logged(monkeypatch):
    relay_acc = {"email": "relay:Seller", "source": "relay", "supplier": "Seller",
                 "lot_ids": ["555"]}
    monkeypatch.setattr(ac, "accounts", lambda: [relay_acc])
    _seed_relay_rental()
    c = _fake_cardinal(name_lookup={"Seller": _types.SimpleNamespace(id=555)})
    ac.on_new_message(c, _msg_event("code", "Buyer", 42, 200, "Buyer"))
    ac.on_new_message(c, _msg_event("482913", "Seller", 999, 555, "Seller"))
    used = ac.used_codes()
    assert any(e.get("code") == "482913"
               for e in used.get("relay:Seller", []))
