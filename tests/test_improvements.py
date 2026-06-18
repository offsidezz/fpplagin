"""Tests for the v5.9.1 hardening pass:

- settings in-memory cache (hot-path read) + write invalidation + isolation
- rate-limit / problem-cooldown maps no longer grow unbounded (memory leak)
- IMAP code fetch: server-side SINCE window + header-prefetch + BODY.PEEK
  (so the full body is only downloaded for messages that pass the filters,
  and messages are never marked \\Seen)
- mutate_rentals(): atomic read-modify-write of the live rentals cache
"""
import time
from email.utils import formatdate

import autocode as ac


# ───────────────────────────── settings cache ──────────────────────────────

def test_settings_cache_invalidates_on_save():
    ac.save_settings({"review_enabled": True, "texts": {}})
    assert ac.app_settings()["review_enabled"] is True
    s = ac.app_settings()
    s["review_enabled"] = False
    ac.save_settings(s)
    assert ac.app_settings()["review_enabled"] is False


def test_settings_returned_copy_does_not_corrupt_cache():
    ac.save_settings({"review_enabled": True, "texts": {}})
    a = ac.app_settings()
    a["review_enabled"] = "tampered"
    a["texts"]["x"] = "y"            # mutate nested too
    b = ac.app_settings()
    assert b["review_enabled"] is True
    assert "x" not in b["texts"]


def test_settings_cache_avoids_disk_after_warm(monkeypatch):
    ac._settings_cache = None
    ac.app_settings()               # warm the cache (reads disk once)

    def _boom(*a, **k):
        raise AssertionError("app_settings hit disk after cache was warm")

    monkeypatch.setattr(ac, "_load", _boom)
    assert isinstance(ac.app_settings(), dict)   # served from cache, no disk


# ─────────────────────────── memory-leak guards ────────────────────────────

def test_check_rate_sweeps_stale_buyers(monkeypatch):
    monkeypatch.setattr(ac, "t", lambda k, **kw: k)
    ac._last_code_ts.clear()
    ac._code_requests.clear()
    stale = time.time() - 7200
    for i in range(600):
        ac._last_code_ts[f"buyer{i}"] = stale
        ac._code_requests[f"buyer{i}"] = [stale]
    ok, _ = ac._check_rate("fresh_buyer")     # len > 512 → triggers sweep
    assert ok
    assert len(ac._last_code_ts) == 0
    assert len(ac._code_requests) == 0


# ─────────────────────────── IMAP fetch_code path ──────────────────────────

class _RecordingIMAP:
    """Fake IMAP server with one recent message (id 1, carries the code) and
    one ancient message (id 2). Records search criteria and every fetch spec.
    """
    last = None

    def __init__(self, *a, **k):
        self.search_args = ()
        self.fetch_specs = []
        _RecordingIMAP.last = self

    def login(self, *a, **k):
        return ("OK", [b""])

    def select(self, *a, **k):
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        self.search_args = criteria
        return ("OK", [b"1 2"])

    def fetch(self, mid, spec):
        self.fetch_specs.append((mid, spec))
        recent = formatdate()                          # ~now
        ancient = formatdate(time.time() - 9_000_000)  # ~104 days ago
        is_recent = str(mid).strip("b'\"") == "1"
        date = recent if is_recent else ancient
        if "HEADER.FIELDS" in spec:
            body = (f"Date: {date}\r\nSubject: code\r\n"
                    f"From: shop@example.com\r\n\r\n").encode()
        else:
            body = (f"Date: {date}\r\nSubject: code\r\n"
                    f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                    f"Your code: 246810\r\n").encode()
        return ("OK", [(b"x", body)])

    def logout(self):
        return ("OK", [b""])


def _acc():
    return {"email": "shop@example.com", "password": "", "code_type": "digits",
            "code_len": 6, "max_age_min": 600}


def test_fetch_uses_since_and_peek_and_finds_code(monkeypatch):
    monkeypatch.setattr(ac.imaplib, "IMAP4_SSL", _RecordingIMAP)
    monkeypatch.setattr(ac, "used_codes", lambda: {})
    code, err = ac.fetch_code(_acc(), used={})
    assert code == "246810", err

    inst = _RecordingIMAP.last
    # server-side date window pushed via SINCE
    assert "SINCE" in inst.search_args
    # never use RFC822 (it sets \Seen); always BODY.PEEK
    assert all("RFC822" not in spec for _, spec in inst.fetch_specs)
    assert all("BODY.PEEK" in spec for _, spec in inst.fetch_specs)


def test_fetch_skips_body_download_for_old_message(monkeypatch):
    monkeypatch.setattr(ac.imaplib, "IMAP4_SSL", _RecordingIMAP)
    monkeypatch.setattr(ac, "used_codes", lambda: {})
    ac.fetch_code(_acc(), used={})

    inst = _RecordingIMAP.last
    body_fetches = [str(mid) for mid, spec in inst.fetch_specs
                    if "HEADER.FIELDS" not in spec]
    # only the recent message (id 1) should ever have its full body fetched;
    # the ancient one is filtered out by the cheap header prefetch.
    assert all("2" not in m for m in body_fetches)
    assert any("1" in m for m in body_fetches)


# ───────────────────────────── mutate_rentals ──────────────────────────────

def test_mutate_rentals_is_atomic_and_persists():
    now = time.time()
    ac.save_rentals({"K": {"order_key": "K", "hours": 1,
                           "expires_at": now + 1000}})
    ac.mutate_rentals(lambda live: live["K"].__setitem__("hours", 5))
    ac.mutate_rentals(lambda live: live.__setitem__(
        "K2", {"order_key": "K2", "hours": 2, "expires_at": now + 1000}))

    r = ac.rentals()
    assert r["K"]["hours"] == 5
    assert "K2" in r

    # survives a flush to disk + reload
    ac._flush_rentals()
    ac._rentals_cache = None
    assert ac.rentals()["K"]["hours"] == 5
