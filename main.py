"""App entrypoint for the desktop UI and embedded Flask server."""

from __future__ import annotations

import os
import socket
import threading
import time

from src.storage import init_db

init_db()


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _configure_server_logging() -> None:
    import logging

    logging.getLogger("werkzeug").disabled = True
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        import flask.cli

        flask.cli.show_server_banner = lambda *args, **kwargs: None
    except Exception:
        pass


def _run_flask(port: int) -> None:
    from web_app import app, start_hotkey_listener

    _configure_server_logging()
    start_hotkey_listener()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


def _wait_for_local_server(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _shutdown_application(*_args: object) -> None:
    try:
        from web_app import shutdown_application

        shutdown_application()
    except Exception:
        pass


def _open_desktop_window(url: str) -> None:
    import webview

    window = webview.create_window(
        "Bot Divulgação",
        url,
        width=1200,
        height=720,
        resizable=True,
    )
    window.events.closed += _shutdown_application
    webview.start()


def main() -> None:
    port = int(os.getenv("PORT", "0"))
    if port == 0:
        port = _find_free_port()

    thread = threading.Thread(target=_run_flask, args=(port,), daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"

    if os.getenv("BOT_NO_WEBVIEW") == "1":
        print(f"Interface web: {url}")
        thread.join()
        return

    if not _wait_for_local_server(port):
        raise RuntimeError("O servidor local da interface não iniciou dentro do tempo limite.")
    try:
        _open_desktop_window(url)
    finally:
        _shutdown_application()


if __name__ == "__main__":
    main()
