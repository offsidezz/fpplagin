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
