import logging
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import main


def test_configure_server_logging_suppresses_werkzeug_access_logs():
    logger = logging.getLogger("werkzeug")
    logger.disabled = False
    logger.setLevel(logging.NOTSET)

    main._configure_server_logging()

    assert logger.disabled is True
    assert logger.level == logging.ERROR


class FakeClosedEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


def test_open_desktop_window_uses_webview_and_registers_close_cleanup(monkeypatch):
    closed = FakeClosedEvent()
    calls = []
    window = SimpleNamespace(events=SimpleNamespace(closed=closed))
    fake_webview = SimpleNamespace(
        create_window=lambda *args, **kwargs: calls.append((args, kwargs)) or window,
        start=lambda: calls.append(("start", {})),
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(main, "_shutdown_application", lambda *_args: calls.append(("shutdown", {})))

    main._open_desktop_window("http://127.0.0.1:5017")

    assert calls[0] == (
        ("Bot Divulgação", "http://127.0.0.1:5017"),
        {"width": 1200, "height": 720, "resizable": True},
    )
    assert calls[1] == ("start", {})
    assert len(closed.handlers) == 1
    closed.handlers[0]()
    assert calls[2] == ("shutdown", {})


def test_wait_for_local_server_returns_true_after_port_is_available(monkeypatch):
    attempts = []

    def create_connection(address, timeout):
        attempts.append((address, timeout))
        if len(attempts) == 1:
            raise OSError("not ready")
        return nullcontext()

    monkeypatch.setattr(main.socket, "create_connection", create_connection)
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    assert main._wait_for_local_server(5017, timeout=1.0) is True
    assert attempts[-1][0] == ("127.0.0.1", 5017)


def test_main_waits_for_flask_opens_desktop_window_and_shuts_down(monkeypatch):
    calls = []
    fake_thread = SimpleNamespace(
        start=lambda: calls.append("thread-start"),
        join=lambda: calls.append("thread-join"),
    )
    monkeypatch.delenv("BOT_NO_WEBVIEW", raising=False)
    monkeypatch.setenv("PORT", "5017")
    monkeypatch.setattr(main.threading, "Thread", lambda **kwargs: fake_thread)
    monkeypatch.setattr(main, "_wait_for_local_server", lambda port: calls.append(("wait", port)) or True)
    monkeypatch.setattr(main, "_open_desktop_window", lambda url: calls.append(("window", url)))
    monkeypatch.setattr(main, "_shutdown_application", lambda *_args: calls.append("shutdown"))

    main.main()

    assert calls == [
        "thread-start",
        ("wait", 5017),
        ("window", "http://127.0.0.1:5017"),
        "shutdown",
    ]
    assert "thread-join" not in calls


def test_main_does_not_open_window_when_flask_fails_to_start(monkeypatch):
    calls = []
    fake_thread = SimpleNamespace(start=lambda: None)
    monkeypatch.delenv("BOT_NO_WEBVIEW", raising=False)
    monkeypatch.setenv("PORT", "5017")
    monkeypatch.setattr(main.threading, "Thread", lambda **kwargs: fake_thread)
    monkeypatch.setattr(main, "_wait_for_local_server", lambda port: False)
    monkeypatch.setattr(main, "_open_desktop_window", lambda url: calls.append(url))

    try:
        main.main()
    except RuntimeError as error:
        assert "servidor local" in str(error)
    else:
        raise AssertionError("main deveria falhar quando o Flask nao inicia")

    assert calls == []
