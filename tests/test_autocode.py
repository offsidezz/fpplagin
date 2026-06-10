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
