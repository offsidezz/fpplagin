"""Unit tests for AutoCode pure logic and the regression fixes.

Run with:  uv run --with pytest python -m pytest tests/ -v
"""
import email as email_mod

import autocode as ac


# ───────────────────────── _find_code ─────────────────────────

def test_find_code_strict_len_digits():
    acc = {"code_type": "digits", "code_len": 6}
    body = "Здравствуйте!\nВаш код: 482915\nСпасибо."
    assert ac._find_code(body, "", "subj", acc) == "482915"


def test_find_code_strict_len_rejects_wrong_length():
    acc = {"code_type": "digits", "code_len": 6}
    # only a 4-digit number present → must not match a 6-digit pattern
    assert ac._find_code("Код: 1234", "", "", acc) is None


def test_find_code_alnum_requires_digit_in_auto_mode():
    acc = {"code_type": "alnum", "code_len": 0}
    # pure-letter token should be ignored, alphanumeric with a digit accepted
    assert ac._find_code("verification code AB12CD", "", "", acc) == "AB12CD"


def test_find_code_uses_html_when_no_plain():
    acc = {"code_type": "digits", "code_len": 4}
    html = "<html><body><p>Your code is <b>7788</b></p></body></html>"
    assert ac._find_code("", html, "", acc) == "7788"


def test_find_code_returns_none_when_absent():
    acc = {"code_type": "digits", "code_len": 6}
    assert ac._find_code("no codes here", "", "", acc) is None


# ───────────────────────── _get_text None-guard (fix #3) ─────────────────────────

def test_get_text_handles_empty_multipart_without_crashing():
    raw = (
        "From: a@b.com\r\n"
        "To: c@d.com\r\n"
        "Subject: test\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="BOUND"\r\n'
        "\r\n"
        "--BOUND\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 7bit\r\n"
        "\r\n"
        "Ваш код: 135790\r\n"
        "--BOUND\r\n"
        # An attachment-like part whose decoded payload can be None/empty
        "Content-Type: application/octet-stream\r\n"
        "\r\n"
        "\r\n"
        "--BOUND--\r\n"
    )
    msg = email_mod.message_from_string(raw)
    plain, html = ac._get_text(msg)  # must not raise AttributeError
    assert "135790" in plain


# ───────────────────────── detect_lang / helpers ─────────────────────────

def test_detect_lang_russian_and_english():
    assert ac.detect_lang("Привет, как дела") == "ru"
    assert ac.detect_lang("Hello how are you") == "en"
    assert ac.detect_lang("12") is None


def test_strip_invisible_removes_zero_width():
    assert ac._strip_invisible("!cd\u200b") == "!cd"


def test_parse_hours_variants():
    assert ac._parse_hours("Аренда 24ч") == 24
    # 30 days → 720 hours
    assert ac._parse_hours("подписка 30 дней") == 720


# ───────────────────────── double-delivery race (fix #1) ─────────────────────────

class _FakeIMAP:
    """Minimal fake IMAP server returning one canned code email."""

    _RAW = (
        b"From: shop@example.com\r\n"
        b"Subject: code\r\n"
        b"Date: " + email_mod.utils.formatdate().encode() + b"\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Your code: 246810\r\n"
    )

    def __init__(self, *a, **k):
        pass

    def login(self, *a, **k):
        return ("OK", [b""])

    def select(self, *a, **k):
        return ("OK", [b"1"])

    def search(self, *a, **k):
        return ("OK", [b"1"])

    def fetch(self, mid, spec):
        return ("OK", [(b"1 (RFC822)", self._RAW)])

    def logout(self):
        return ("OK", [b""])


def _patch_imap(monkeypatch):
    monkeypatch.setattr(ac.imaplib, "IMAP4_SSL", _FakeIMAP)


def test_fetch_code_delivers_when_unused(monkeypatch):
    _patch_imap(monkeypatch)
    monkeypatch.setattr(ac, "used_codes", lambda: {})
    acc = {"email": "shop@example.com", "password": "", "code_type": "digits",
           "code_len": 6, "max_age_min": 600}
    code, err = ac.fetch_code(acc, used={})
    assert code == "246810", err


def test_fetch_code_skips_already_used_via_fresh_read(monkeypatch):
    """The core race fix: even if the caller passes a stale empty snapshot,
    fetch_code re-reads used_codes() and must NOT re-deliver a used code."""
    _patch_imap(monkeypatch)
    # Fresh state says the code was already delivered by a prior queue task
    monkeypatch.setattr(
        ac, "used_codes",
        lambda: {"shop@example.com": [{"code": "246810", "used_at": 0}]},
    )
    acc = {"email": "shop@example.com", "password": "", "code_type": "digits",
           "code_len": 6, "max_age_min": 600}
    # Caller passes a STALE snapshot that does not contain the code
    code, err = ac.fetch_code(acc, used={})
    assert code is None  # must not double-deliver


# ───────────────────────── chat_id normalization (no-active-rental bug) ─────

def test_cid_norm_unifies_str_and_int():
    # Restored rentals store chat_id as str; e.message.chat_id is int.
    assert ac._cid_norm("12345") == ac._cid_norm(12345)
    assert ac._cid_norm(None) is None
    assert ac._cid_norm("") is None
    assert ac._cid_norm("  users-1-2  ") == "users-1-2"


def test_name_norm_tolerant_match():
    assert ac._name_norm(" Offsidez ") == ac._name_norm("offsidez")
    assert ac._name_norm(None) == ""


# ───────── buyer_id matching + chat_id repair (user-id vs chat-id bug) ─────────

import types as _types


def _msg_event(text, author, author_id, chat_id, chat_name=None):
    msg = _types.SimpleNamespace(
        text=text, author=author, author_id=author_id,
        chat_id=chat_id, chat_name=chat_name or author)
    return _types.SimpleNamespace(message=msg)


def _fake_cardinal(seller_id=7028500, chat_lookup=None, name_lookup=None,
                   fail_chat_ids=()):
    sent = []

    def send_message(cid, text, cname=""):
        if str(cid) in {str(x) for x in fail_chat_ids}:
            raise RuntimeError("Доступ запрещен.")
        sent.append((str(cid), text, cname))

    account = _types.SimpleNamespace(
        id=seller_id,
        get_chat=lambda cid: (chat_lookup or {}).get(int(cid) if str(cid).isdigit() else cid),
        get_chat_by_name=lambda name, *a, **k: (name_lookup or {}).get(name),
    )
    c = _types.SimpleNamespace(account=account, send_message=send_message)
    c._sent = sent
    return c


def _seed_rental(monkeypatch, **over):
    base = {
        "order_key": "ORD1", "buyer": "Dima548", "buyer_id": None,
        "chat_id": None, "chat_name": "Dima548", "email": "m@x.ru",
        "lot_id": "", "order_id": "ORD1",
        "purchase_ts": ac.time.time() - 3600,
        "expires_at": ac.time.time() + 36000, "hours": 24, "lang": "ru",
    }
    base.update(over)
    ac.save_rentals({base["order_key"]: base})
    # neutralize templates so we don't depend on template files
    monkeypatch.setattr(ac, "t", lambda key, **kw: key)
    return base


def test_bid_norm():
    assert ac._bid_norm(12345) == ac._bid_norm("12345") == "12345"
    assert ac._bid_norm(None) is None
    assert ac._bid_norm("  ") is None


def test_match_by_buyer_id_heals_chat_id(monkeypatch):
    """Buyer name changed, chat_id stored is a stale user-id; match by buyer_id
    and heal chat_id to the real incoming chat id."""
    _seed_rental(monkeypatch, buyer="OldName", buyer_id="12345", chat_id="999")
    c = _fake_cardinal()
    e = _msg_event("!time", author="RenamedBuyer", author_id=12345,
                   chat_id=264909029, chat_name="RenamedBuyer")
    ac.on_new_message(c, e)
    healed = ac.rentals()["ORD1"]["chat_id"]
    assert healed == "264909029", healed


def test_no_match_when_buyer_id_differs(monkeypatch):
    _seed_rental(monkeypatch, buyer="OldName", buyer_id="12345", chat_id="999")
    c = _fake_cardinal()
    e = _msg_event("!time", author="Someone", author_id=99999, chat_id=264909029)
    ac.on_new_message(c, e)
    # unrelated buyer → rental must NOT be healed/stolen
    assert ac.rentals()["ORD1"]["chat_id"] == "999"


def test_seller_test_resolves_interlocutor(monkeypatch):
    """Seller types !time in the buyer's chat: resolve the chat interlocutor
    and match the rental by that buyer name."""
    _seed_rental(monkeypatch, buyer="Dima548", buyer_id="55", chat_id="55")
    chat_obj = _types.SimpleNamespace(name="Dima548")
    c = _fake_cardinal(seller_id=7028500, chat_lookup={264575303: chat_obj})
    e = _msg_event("!time", author="offsidez", author_id=7028500,
                   chat_id=264575303, chat_name="offsidez")
    ac.on_new_message(c, e)
    assert ac.rentals()["ORD1"]["chat_id"] == "264575303"


def test_send_to_buyer_repairs_stale_chat_id(monkeypatch):
    """Sending to a stale user-id fails; _send_to_buyer resolves the real
    chat_id via get_chat_by_name, persists it, and retries successfully."""
    rental = _seed_rental(monkeypatch, buyer="Dima548", buyer_id="55", chat_id="55")
    chat_obj = _types.SimpleNamespace(id=264575303, name="Dima548")
    c = _fake_cardinal(name_lookup={"Dima548": chat_obj}, fail_chat_ids=("55",))
    ok = ac._send_to_buyer(c, dict(rental), "ваш код: 1234")
    assert ok is True
    assert c._sent and c._sent[-1][0] == "264575303"
    # persisted back onto the live rental record
    assert ac.rentals()["ORD1"]["chat_id"] == "264575303"


def test_send_to_buyer_returns_false_when_unresolvable(monkeypatch):
    rental = _seed_rental(monkeypatch, buyer="Ghost", buyer_id="55", chat_id="55")
    c = _fake_cardinal(name_lookup={}, fail_chat_ids=("55",))
    ok = ac._send_to_buyer(c, dict(rental), "hi")
    assert ok is False


# ───────── buyer-id extraction from composite chat string ─────────

def test_buyer_id_from_chat_seller_first():
    # users-{seller}-{buyer}: seller in front, buyer is the other segment
    assert ac._buyer_id_from_chat("users-7028500-16710870", 7028500) == "16710870"


def test_buyer_id_from_chat_seller_second():
    # users-{buyer}-{seller}: seller can also be the trailing segment
    assert ac._buyer_id_from_chat("users-3223981-7028500", 7028500) == "3223981"


def test_buyer_id_from_chat_plain_and_empty():
    assert ac._buyer_id_from_chat("16710870", 7028500) == "16710870"
    assert ac._buyer_id_from_chat(None, 7028500) is None
    assert ac._buyer_id_from_chat("  ", 7028500) is None


def test_buyer_id_from_chat_matches_author_id_for_matching():
    # The extracted buyer_id must equal the buyer's author_id so the buyer_id
    # matching tier in on_new_message works.
    bid = ac._buyer_id_from_chat("users-7028500-16710870", 7028500)
    assert ac._bid_norm(bid) == ac._bid_norm(16710870)


def test_safe_edit_defaults_to_plain_text():
    """Regression for the TG 'Unsupported start tag \"2ч\"' crash: the panel
    legend contains a literal '<2ч' and the host bot defaults to HTML parse
    mode. _safe_edit must default parse_mode to '' (plain text), not None."""
    import inspect
    sig = inspect.signature(ac._safe_edit)
    assert sig.parameters["parse_mode"].default == ""

    captured = {}

    class _Bot:
        def edit_message_text(self, text, chat_id, message_id,
                              reply_markup=None, parse_mode=None):
            captured["parse_mode"] = parse_mode

    call = _types.SimpleNamespace(
        message=_types.SimpleNamespace(
            chat=_types.SimpleNamespace(id=1), message_id=2),
        id="cb")
    ac._safe_edit(_Bot(), call, "🟢 >24ч 🔴 <2ч", kb=None)
    # plain text → no HTML parsing of the literal '<2ч'
    assert captured["parse_mode"] == ""


# ───────── B2 stacking of multiple purchases ─────────

def test_stack_b2_single_order():
    o = [{"order_id": "A", "purchase_ts": 1000.0, "hours": 24}]
    earliest, total, exp, ids = ac._stack_b2(o)
    assert earliest == 1000.0
    assert total == 24
    assert exp == 1000.0 + 24 * 3600
    assert ids == ["A"]


def test_stack_b2_overlapping_adds_up():
    # Two purchases close together → durations add up (sequential extension).
    o = [
        {"order_id": "A", "purchase_ts": 1000.0, "hours": 24},
        {"order_id": "B", "purchase_ts": 2000.0, "hours": 24},
    ]
    earliest, total, exp, ids = ac._stack_b2(o)
    assert total == 48
    # second extends from first's expiry (1000+24h) not from its own purchase
    assert exp == 1000.0 + 48 * 3600
    assert ids == ["A", "B"]


def test_stack_b2_gap_restarts_from_purchase():
    # First window long-expired before the second purchase → restart from it.
    base = 1_000_000.0
    o = [
        {"order_id": "A", "purchase_ts": base, "hours": 24},
        {"order_id": "B", "purchase_ts": base + 100 * 3600, "hours": 24},
    ]
    earliest, total, exp, ids = ac._stack_b2(o)
    assert earliest == base
    assert total == 48
    # second purchase is after the first expired → expiry = its purchase + 24h
    assert exp == (base + 100 * 3600) + 24 * 3600


def test_stack_b2_unsorted_input():
    o = [
        {"order_id": "B", "purchase_ts": 2000.0, "hours": 10},
        {"order_id": "A", "purchase_ts": 1000.0, "hours": 10},
    ]
    earliest, total, exp, ids = ac._stack_b2(o)
    assert earliest == 1000.0
    assert ids == ["A", "B"]


# ───────── restore scan: reconcile + stack (startup) ─────────

def _make_shortcut(order_id, buyer, seller_buyer_chat, lot_name, lot_id, date_ts):
    return _types.SimpleNamespace(
        id=order_id, buyer_username=buyer, chat_id=seller_buyer_chat,
        description=lot_name, lot_id=lot_id, date_ts=date_ts)


def _make_scan_cardinal(shortcuts, seller_id=7028500):
    pages = [shortcuts, []]

    def get_sales(start_from=None, **k):
        idx = 0 if start_from is None else 1
        page = pages[idx] if idx < len(pages) else []
        nxt = "next" if idx == 0 and page else None
        return (nxt, page)

    account = _types.SimpleNamespace(id=seller_id, get_sales=get_sales)
    return _types.SimpleNamespace(account=account)


def test_restore_scan_stacks_and_backfills(monkeypatch):
    monkeypatch.setattr(ac.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(ac, "_notify_tg", lambda *a, **k: None)
    monkeypatch.setattr(ac, "accounts", lambda: [{"email": "m@x.ru", "lot_ids": []}])
    ac.save_rentals({})
    monkeypatch.setattr(ac, "_shutdown_flag", {"stop": False}, raising=False)

    now = ac.time.time()
    # Two recent purchases by the same buyer for the same account → stack to 48h
    lot = "NETFLIX Premium 30 ДНЕЙ / 720ч • +12ч ЗА ОТЗЫВ"
    s1 = _make_shortcut("O1", "Dima548", "users-7028500-16710870",
                        "Netflix 1 ДЕНЬ / 24ч", "111", now - 2 * 3600)
    s2 = _make_shortcut("O2", "Dima548", "users-7028500-16710870",
                        "Netflix 1 ДЕНЬ / 24ч", "111", now - 1 * 3600)
    c = _make_scan_cardinal([s1, s2])
    ac._startup_sales_scan(c)

    rents = ac.rentals()
    assert len(rents) == 1, rents
    r = list(rents.values())[0]
    assert r["buyer"] == "Dima548"
    assert ac._bid_norm(r["buyer_id"]) == "16710870"   # extracted from composite
    assert r["chat_id"] is None                          # resolved lazily
    assert r["hours"] == 48                              # stacked
    assert set(r["order_ids"]) == {"O1", "O2"}


def test_restore_scan_merges_existing_and_never_shortens(monkeypatch):
    monkeypatch.setattr(ac.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(ac, "_notify_tg", lambda *a, **k: None)
    monkeypatch.setattr(ac, "accounts", lambda: [{"email": "m@x.ru", "lot_ids": []}])
    monkeypatch.setattr(ac, "_shutdown_flag", {"stop": False}, raising=False)

    now = ac.time.time()
    # Pre-existing rental from the buggy era: wrong chat_id (= buyer user-id),
    # no buyer_id, and a manually extended (far-future) expiry.
    manual_exp = now + 1000 * 3600
    ac.save_rentals({"O1": {
        "order_key": "O1", "buyer": "Dima548", "buyer_id": None,
        "chat_id": "16710870", "chat_name": "Dima548", "email": "m@x.ru",
        "lot_id": "111", "order_id": "O1",
        "purchase_ts": now - 2 * 3600, "expires_at": manual_exp,
        "hours": 24, "lang": "ru",
    }})

    s1 = _make_shortcut("O1", "Dima548", "users-7028500-16710870",
                        "Netflix 1 ДЕНЬ / 24ч", "111", now - 2 * 3600)
    c = _make_scan_cardinal([s1])
    ac._startup_sales_scan(c)

    rents = ac.rentals()
    assert len(rents) == 1
    r = rents["O1"]
    assert ac._bid_norm(r["buyer_id"]) == "16710870"   # backfilled
    assert r["chat_id"] is None                          # broken user-id cleared
    assert r["expires_at"] == manual_exp                 # never shortened


# ───────── review bonus parsing ─────────

def test_parse_review_bonus():
    assert ac._parse_review_bonus("NETFLIX 30 ДНЕЙ / 720ч • +12ч ЗА ОТЗЫВ") == 12
    assert ac._parse_review_bonus("Подписка 24ч +6ч за отзыв") == 6
    assert ac._parse_review_bonus("Netflix 1 ДЕНЬ / 24ч") == 0
    assert ac._parse_review_bonus("") == 0
    assert ac._parse_review_bonus("просто отзыв без бонуса") == 0


# ───────── refund subtraction helper ─────────

def test_apply_refund_subtracts_and_is_idempotent():
    now = ac.time.time()
    r = {
        "order_id": "A", "order_ids": ["A", "B"],
        "orders": [{"order_id": "A", "purchase_ts": now, "hours": 24},
                   {"order_id": "B", "purchase_ts": now, "hours": 24}],
        "expires_at": now + 48 * 3600, "hours": 48,
    }
    keep = ac._apply_refund_to_rental(r, "B", 24)
    assert keep is True
    assert r["hours"] == 24
    assert abs(r["expires_at"] - (now + 24 * 3600)) < 2
    assert r["order_ids"] == ["A"]
    assert r["refunded_ids"] == ["B"]
    # second call for same order must not subtract again
    keep2 = ac._apply_refund_to_rental(r, "B", 24)
    assert keep2 is True
    assert r["hours"] == 24


def test_apply_refund_removes_last_order():
    now = ac.time.time()
    r = {"order_id": "A", "order_ids": ["A"],
         "orders": [{"order_id": "A", "purchase_ts": now, "hours": 24}],
         "expires_at": now + 24 * 3600, "hours": 24}
    keep = ac._apply_refund_to_rental(r, "A", 24)
    assert keep is False    # nothing left → caller should drop it


# ───────── order review existence check ─────────

def test_order_review_exists():
    acc_with = _types.SimpleNamespace(
        get_order=lambda oid: _types.SimpleNamespace(review=_types.SimpleNamespace(stars=5)))
    assert ac._order_review_exists(_types.SimpleNamespace(account=acc_with), "O1") is True

    acc_without = _types.SimpleNamespace(
        get_order=lambda oid: _types.SimpleNamespace(review=None))
    assert ac._order_review_exists(_types.SimpleNamespace(account=acc_without), "O1") is False

    assert ac._order_review_exists(_types.SimpleNamespace(account=acc_with), "") is False

    acc_err = _types.SimpleNamespace(
        get_order=lambda oid: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ac._order_review_exists(_types.SimpleNamespace(account=acc_err), "O1") is False


# ───────── review message grants bonus hours once ─────────

def _review_event(author, author_id, chat_id, chat_name=None):
    msg = _types.SimpleNamespace(
        text="спасибо, всё ок", author=author, author_id=author_id,
        chat_id=chat_id, chat_name=chat_name or author,
        type=_types.SimpleNamespace(name="NEW_FEEDBACK"))
    return _types.SimpleNamespace(message=msg)


def test_review_grants_bonus_once(monkeypatch):
    now = ac.time.time()
    _seed_rental(monkeypatch, buyer="Dima548", buyer_id="16710870",
                 chat_id="264909029", review_bonus=12,
                 expires_at=now + 10 * 3600, hours=24)
    c = _fake_cardinal()
    e = _review_event("Dima548", 16710870, 264909029)
    ac.on_new_message(c, e)
    r = ac.rentals()["ORD1"]
    assert r["review_bonus_given"] is True
    assert r["hours"] == 36
    assert abs(r["expires_at"] - (now + 22 * 3600)) < 2
    # a second review must not stack again
    ac.on_new_message(c, _review_event("Dima548", 16710870, 264909029))
    assert ac.rentals()["ORD1"]["hours"] == 36


# ───────── live refund event ─────────

def test_on_order_status_changed_refund(monkeypatch):
    now = ac.time.time()
    ac.save_rentals({"A": {
        "order_key": "A", "buyer": "Dima548", "buyer_id": "16710870",
        "chat_id": "264909029", "email": "m@x.ru", "lot_id": "111",
        "order_id": "A", "order_ids": ["A"],
        "orders": [{"order_id": "A", "purchase_ts": now, "hours": 24}],
        "expires_at": now + 24 * 3600, "hours": 24, "lang": "ru",
    }})
    monkeypatch.setattr(ac, "_notify_tg", lambda *a, **k: None)
    order = _types.SimpleNamespace(id="A", description="Netflix 1 ДЕНЬ / 24ч",
                                   status=_types.SimpleNamespace(name="REFUNDED"))
    e = _types.SimpleNamespace(order=order)
    c = _fake_cardinal()
    ac.on_order_status_changed(c, e)
    assert "A" not in ac.rentals()    # single refunded order → rental removed


# ───────── restore scan subtracts a refunded order ─────────

def test_restore_scan_subtracts_refund(monkeypatch):
    monkeypatch.setattr(ac.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(ac, "_notify_tg", lambda *a, **k: None)
    monkeypatch.setattr(ac, "accounts", lambda: [{"email": "m@x.ru", "lot_ids": []}])
    monkeypatch.setattr(ac, "_shutdown_flag", {"stop": False}, raising=False)
    now = ac.time.time()
    # existing rental that stacked O1(24h)+O2(24h)=48h; O2 gets refunded.
    ac.save_rentals({"O1": {
        "order_key": "O1", "buyer": "Dima548", "buyer_id": "16710870",
        "chat_id": "264818884", "chat_name": "Dima548", "email": "m@x.ru",
        "lot_id": "111", "order_id": "O1", "order_ids": ["O1", "O2"],
        "orders": [{"order_id": "O1", "purchase_ts": now - 3 * 3600, "hours": 24},
                   {"order_id": "O2", "purchase_ts": now - 2 * 3600, "hours": 24}],
        "purchase_ts": now - 3 * 3600, "expires_at": now + 45 * 3600,
        "hours": 48, "lang": "ru",
    }})
    paid = _make_shortcut("O1", "Dima548", "users-7028500-16710870",
                          "Netflix 1 ДЕНЬ / 24ч", "111", now - 3 * 3600)
    refunded = _make_shortcut("O2", "Dima548", "users-7028500-16710870",
                              "Netflix 1 ДЕНЬ / 24ч", "111", now - 2 * 3600)
    refunded.status = _types.SimpleNamespace(name="REFUNDED")
    c = _make_scan_cardinal([paid, refunded])
    ac._startup_sales_scan(c)
    rents = ac.rentals()
    assert "O1" in rents
    r = rents["O1"]
    assert "O2" not in [str(x) for x in r["order_ids"]]
    assert r["hours"] == 24            # 48 - 24 refunded
    assert "O2" in r["refunded_ids"]


# ───────── rentals in-memory write-back cache ─────────

def test_rentals_cache_roundtrip_and_flush():
    now = ac.time.time()
    data = {"X": {"order_id": "X", "buyer": "Ann", "email": "m@x.ru",
                  "expires_at": now + 3600, "hours": 1}}
    ac.save_rentals(data)                 # deferred write → cache only
    # reads come straight from the in-memory cache
    assert ac.rentals()["X"]["buyer"] == "Ann"
    # caller mutating the returned snapshot must NOT corrupt the cache
    snap = ac.rentals()
    snap["X"]["buyer"] = "HACKED"
    assert ac.rentals()["X"]["buyer"] == "Ann"
    # flush persists to disk
    ac._flush_rentals()
    on_disk = ac._load(ac.RENTALS_FILE, {})
    assert on_disk["X"]["buyer"] == "Ann"


def test_save_rentals_immediate_writes_disk():
    now = ac.time.time()
    ac.save_rentals({"Y": {"order_id": "Y", "email": "m@x.ru",
                           "expires_at": now + 3600, "hours": 1}}, immediate=True)
    assert "Y" in ac._load(ac.RENTALS_FILE, {})


# ───────── used-code cleanup keeps codes while rental is active ─────────

def test_prune_used_codes_respects_active_rental():
    now = ac.time.time()
    old = now - (ac.USED_CODE_TTL_SEC + 3600)     # well past the TTL
    # active rental on a@x.ru, expired rental on b@x.ru
    ac.save_rentals({
        "R1": {"order_id": "R1", "email": "a@x.ru", "expires_at": now + 10 * 3600},
        "R2": {"order_id": "R2", "email": "b@x.ru", "expires_at": now - 3600},
    }, immediate=True)
    ac.save_used({
        "a@x.ru": [{"code": "AAA", "used_at": old}],   # old but rental ACTIVE → keep
        "b@x.ru": [{"code": "BBB", "used_at": old}],   # old + rental expired → drop
    })
    ac._prune_used_codes(now=now)
    u = ac.used_codes()
    assert [e["code"] for e in u["a@x.ru"]] == ["AAA"]   # kept
    assert u["b@x.ru"] == []                              # pruned


def test_prune_used_codes_keeps_fresh_even_if_inactive():
    now = ac.time.time()
    ac.save_rentals({}, immediate=True)                  # no active rentals
    ac.save_used({"c@x.ru": [{"code": "CCC", "used_at": now - 60}]})  # fresh
    ac._prune_used_codes(now=now)
    assert [e["code"] for e in ac.used_codes()["c@x.ru"]] == ["CCC"]
