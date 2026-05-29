"""Tests for the broker-switch + self-restart path in the lite server.

The broker switch is the one runtime action that deliberately ends the process
(``os._exit``) after spawning a replacement. A regression here silently takes
the dashboard down, so the guard branches — *never exit without a guaranteed
relaunch* — and the request validation are covered here.
"""

from __future__ import annotations

import pytest

from dashboard import lite_server as ls


# --------------------------------------------------------------- origin guard
@pytest.mark.parametrize(
    "origin,expected",
    [
        (None, True),
        ("", True),
        ("http://127.0.0.1:8050", True),
        ("http://localhost:8050", True),
        ("http://[::1]:8050", True),
        ("http://evil.com", False),
        ("https://attacker.example", False),
        ("garbage-no-host", False),
    ],
)
def test_origin_is_local(origin, expected):
    assert ls._origin_is_local(origin) is expected


# ------------------------------------------------------- .env rewrite helper
def test_rewrite_env_replaces_existing_line(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "FRED_API_KEY=abc\nMT5_TERMINAL_PATH=C:\\old\\terminal64.exe\nX=1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ls, "_ENV_FILE", env)

    ls._rewrite_env_terminal_path(r"C:\new\terminal64.exe")

    out = env.read_text(encoding="utf-8")
    assert r"MT5_TERMINAL_PATH=C:\new\terminal64.exe" in out
    assert "FRED_API_KEY=abc" in out and "X=1" in out          # siblings preserved
    assert out.count("MT5_TERMINAL_PATH=") == 1                # not duplicated


def test_rewrite_env_appends_when_absent(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("FRED_API_KEY=abc\n", encoding="utf-8")
    monkeypatch.setattr(ls, "_ENV_FILE", env)

    ls._rewrite_env_terminal_path(r"C:\new\terminal64.exe")

    out = env.read_text(encoding="utf-8")
    assert "FRED_API_KEY=abc" in out
    assert r"MT5_TERMINAL_PATH=C:\new\terminal64.exe" in out


def test_rewrite_env_strips_utf8_bom(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfFRED_API_KEY=abc\n")          # BOM-prefixed
    monkeypatch.setattr(ls, "_ENV_FILE", env)

    ls._rewrite_env_terminal_path(r"C:\t.exe")

    raw = env.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")                 # BOM gone
    assert b"FRED_API_KEY=abc" in raw


# ------------------------------------------- _spawn_restart_then_exit branches
class _Exited(Exception):
    """Stand-in for os._exit so the test can assert it was reached."""


def _patch_no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)


def test_spawn_writes_bat_and_exits_after_spawn(tmp_path, monkeypatch):
    _patch_no_sleep(monkeypatch)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    calls = {}
    monkeypatch.setattr(ls.subprocess, "Popen",
                        lambda args, **kw: calls.update(args=args, kw=kw))
    monkeypatch.setattr(ls.os, "_exit",
                        lambda code: (_ for _ in ()).throw(_Exited(code)))

    with pytest.raises(_Exited) as exc:
        ls._spawn_restart_then_exit(r"C:\MT5\terminal64.exe")

    assert exc.value.args[0] == 0                              # os._exit(0) reached
    bat = tmp_path / "mt5_broker_switch_relaunch.bat"
    txt = bat.read_text(encoding="utf-8")
    assert r"MT5_TERMINAL_PATH=C:\MT5\terminal64.exe" in txt
    assert "main.py" in txt
    assert calls["args"][:5] == ["cmd", "/c", "start", "", "/MIN"]
    assert calls["args"][-1] == str(bat)                       # launches the .bat


def test_spawn_does_not_exit_when_bat_write_fails(tmp_path, monkeypatch):
    _patch_no_sleep(monkeypatch)
    missing = tmp_path / "no_such_dir"                         # parent absent → OSError
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(missing))
    popen_called, exited = [], []
    monkeypatch.setattr(ls.subprocess, "Popen",
                        lambda *a, **k: popen_called.append(1))
    monkeypatch.setattr(ls.os, "_exit", lambda c: exited.append(c))

    ls._spawn_restart_then_exit("X")                           # must return, not exit

    assert popen_called == []                                  # never tried to spawn
    assert exited == []                                        # never exited


def test_spawn_does_not_exit_when_popen_fails(tmp_path, monkeypatch):
    _patch_no_sleep(monkeypatch)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ls.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    exited = []
    monkeypatch.setattr(ls.os, "_exit", lambda c: exited.append(c))

    ls._spawn_restart_then_exit("X")

    assert exited == []                                        # no exit without relaunch
    assert (tmp_path / "mt5_broker_switch_relaunch.bat").exists()


# ---------------------------------------------------------- /api/broker routes
@pytest.fixture
def client(monkeypatch):
    # Never actually restart the process during a request test.
    monkeypatch.setattr(ls, "_spawn_restart_then_exit", lambda *a, **k: None)
    app = ls.build_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_get_broker_lists_presets(client):
    resp = client.get("/api/broker")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "presets" in body and "current_path" in body


def test_post_broker_rejects_unknown(client):
    resp = client.post("/api/broker", json={"name": "NoSuchBroker"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_post_broker_rejects_cross_origin(client):
    name = next(iter(ls.config.BROKER_PRESETS))
    resp = client.post("/api/broker", json={"name": name},
                       headers={"Origin": "http://evil.com"})
    assert resp.status_code == 403


def test_post_broker_switches_known(client, monkeypatch):
    rewritten = {}
    monkeypatch.setattr(ls, "_rewrite_env_terminal_path",
                        lambda p: rewritten.update(path=p))
    name = next(iter(ls.config.BROKER_PRESETS))

    resp = client.post("/api/broker", json={"name": name})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True and body["name"] == name
    assert rewritten["path"] == ls.config.BROKER_PRESETS[name]


def test_static_assets_revalidate(client):
    assert client.get("/").headers.get("Cache-Control") == "no-cache"
    assert client.get("/static/app.js").headers.get("Cache-Control") == "no-cache"
