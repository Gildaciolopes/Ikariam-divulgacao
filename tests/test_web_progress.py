from types import SimpleNamespace
import threading
import time

import web_app
from selenium.common.exceptions import WebDriverException


def reset_progress_state():
    with web_app.PROGRESS_LOCK:
        web_app.PROGRESS_STATE_BY_ACCOUNT.clear()
        web_app.PROGRESS_STATE.update(
            {
                "server": None,
                "sent": 0,
                "total": 0,
                "remaining": 0,
                "percent": 0.0,
                "updated_at": None,
                "updated_epoch": None,
            }
        )


def test_worker_recreates_driver_after_invalid_session_without_stopping_bot(monkeypatch):
    attempts = []
    log_lines = []
    account = SimpleNamespace(
        id_str="account-1",
        cache=True,
        status="inactive",
        last_login="",
        save=lambda: None,
    )

    class FakeLogs:
        def __init__(self, account_id):
            assert account_id == "account-1"

        def addLogs(self, text, level="info"):
            log_lines.append((text, level))

    class FakeBot:
        def __init__(self, **kwargs):
            attempts.append(kwargs)
            self.last_activity_at = time.time()

        def StartGame(self, logger):
            if len(attempts) == 1:
                raise WebDriverException("invalid session id")

        def close(self):
            return None

    stop_event = threading.Event()
    monkeypatch.setattr(web_app.Accounts, "find", lambda filters: [account])
    monkeypatch.setattr(web_app.Accounts, "find_by_id", lambda account_id: account)
    monkeypatch.setattr(web_app, "BotDriver", FakeBot)
    monkeypatch.setattr(web_app, "WebLogs", FakeLogs)
    monkeypatch.setattr(web_app, "_register_driver", lambda bot: None)
    monkeypatch.setattr(web_app, "_unregister_driver", lambda bot: None)
    monkeypatch.setattr(web_app, "_finalize_stop", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_app, "MAX_PARALLEL_DRIVERS", 1)
    monkeypatch.setitem(web_app.BOT_STATE, "stop_event", stop_event)
    monkeypatch.setitem(web_app.BOT_STATE, "pause_event", threading.Event())

    web_app._run_bot_loop(time_wait=1, logs=True, headless=True, dry_run=True)

    assert len(attempts) == 2
    assert stop_event.is_set() is False
    assert account.status == "activate"
    assert any(line.startswith("Sessao do navegador encerrada. Recriando driver (1/3).") for line, _ in log_lines)


def test_activate_account_starts_visible_selenium_and_waits_for_confirmation(monkeypatch):
    driver_calls = []
    thread_calls = []
    account = SimpleNamespace(
        id_str="account-1",
        email="player@example.test",
        cache=True,
        status="inactive",
        last_login="",
        save=lambda: None,
    )

    class FakeBot:
        def __init__(self, **kwargs):
            driver_calls.append(kwargs)

        def login(self):
            return None

    class FakeThread:
        def __init__(self, *, target, args=(), daemon):
            thread_calls.append({"target": target, "args": args, "daemon": daemon})

        def start(self):
            thread_calls[-1]["started"] = True

    monkeypatch.setattr(web_app.Accounts, "find_by_id", lambda account_id: account)
    monkeypatch.setattr(web_app, "BotDriver", FakeBot)
    monkeypatch.setattr(web_app.threading, "Thread", FakeThread)
    monkeypatch.setattr(web_app, "_append_log", lambda *args, **kwargs: None)

    with web_app.app.test_request_context("/accounts/account-1/activate", method="POST"):
        response = web_app.activate_account("account-1")

    assert response.status_code == 302
    assert driver_calls[0]["headless"] is False
    assert driver_calls[0]["account"] is account
    assert driver_calls[0]["cache"] is True
    assert "logs" in driver_calls[0]
    assert thread_calls[0]["daemon"] is True
    assert thread_calls[0]["started"] is True
    assert account.status == "activating"


def test_activation_worker_marks_account_active_only_after_login_confirmation(monkeypatch):
    account = SimpleNamespace(id_str="account-1", status="activating", last_login="", save=lambda: None)
    events = []

    class FakeBot:
        activation_cancelled = False

        def login(self):
            events.append("login")
            return True

        def close(self):
            events.append("close")

    class FakeLogs:
        def addLogs(self, text, level="info"):
            events.append((text, level))

    monkeypatch.setattr(web_app.Accounts, "find_by_id", lambda account_id: account)
    monkeypatch.setattr(web_app, "_unregister_driver", lambda bot: events.append("unregister"))

    web_app._run_account_activation("account-1", FakeBot(), FakeLogs())

    assert account.status == "activate"
    assert events[:2] == ["login", "close"] or events[:2] == ["login", ("Conta ativada: login confirmado no lobby.", "info")]
    assert "unregister" in events


def test_activation_worker_keeps_unconfirmed_account_out_of_bot(monkeypatch):
    account = SimpleNamespace(id_str="account-1", status="activating", last_login="", save=lambda: None)
    events = []

    class FakeBot:
        activation_cancelled = False

        def login(self):
            return False

        def close(self):
            events.append("close")

    class FakeLogs:
        def addLogs(self, text, level="info"):
            events.append((text, level))

    monkeypatch.setattr(web_app.Accounts, "find_by_id", lambda account_id: account)
    monkeypatch.setattr(web_app, "_unregister_driver", lambda bot: None)

    web_app._run_account_activation("account-1", FakeBot(), FakeLogs())

    assert account.status == "error"
    assert "close" in events
    assert ("Ativacao nao confirmada. A conta nao sera usada pelo bot.", "warn") in events


def test_server_summary_ignores_persisted_servers_without_active_progress(monkeypatch):
    reset_progress_state()
    monkeypatch.setattr(
        web_app.Servers,
        "find",
        lambda: [
            SimpleNamespace(server="Keto", messageSend=6, users=700),
            SimpleNamespace(server="Gigas", messageSend=25, users=1664),
            SimpleNamespace(server="Sem envio", messageSend=0, users=100),
        ],
    )

    rows = web_app._build_server_summary()

    assert rows == []


def test_server_summary_keeps_current_active_server_even_before_first_send(monkeypatch):
    reset_progress_state()
    monkeypatch.setattr(
        web_app.Servers,
        "find",
        lambda: [
            SimpleNamespace(server="Keto", messageSend=6, users=700),
            SimpleNamespace(server="Pangaia", messageSend=0, users=300),
        ],
    )
    web_app.WebLogs(account_id="account-1").set_progress(
        {"server": "Pangaia", "sent": 0, "total": 300, "remaining": 300, "percent": 0.0}
    )

    rows = web_app._build_server_summary()

    assert [row["server"] for row in rows] == ["Pangaia"]
    assert rows[0]["active"] is True
    assert rows[0]["sent"] == 0
    assert rows[0]["total"] == 300


def test_progress_endpoint_returns_only_active_server_rows_for_floating_progress(monkeypatch):
    reset_progress_state()
    monkeypatch.setattr(
        web_app.Servers,
        "find",
        lambda: [
            SimpleNamespace(server="Keto", messageSend=6, users=700),
            SimpleNamespace(server="Gigas", messageSend=25, users=1664),
        ],
    )
    web_app.WebLogs(account_id="account-1").set_progress(
        {"server": "Gigas", "sent": 25, "total": 1664, "remaining": 1639, "percent": 1.5}
    )

    response = web_app.app.test_client().get("/progress")
    payload = response.get_json()

    assert response.status_code == 200
    assert [row["server"] for row in payload["servers"]] == ["Gigas"]
    assert payload["servers"][0]["sent"] == 25
    assert payload["servers"][0]["account_id"] == "account-1"


def test_server_summary_follows_same_account_order_as_log_panels(monkeypatch):
    reset_progress_state()
    monkeypatch.setattr(
        web_app.Accounts,
        "find",
        lambda filters: [SimpleNamespace(id_str="account-1"), SimpleNamespace(id_str="account-2")],
    )
    monkeypatch.setattr(
        web_app.Servers,
        "find",
        lambda: [
            SimpleNamespace(server="BR / Ikaros", messageSend=25, users=1126),
            SimpleNamespace(server="ES / Leto", messageSend=725, users=3555),
        ],
    )
    web_app.WebLogs(account_id="account-2").set_progress(
        {"server": "BR / Ikaros", "sent": 25, "total": 1126, "remaining": 1101, "percent": 2.2}
    )
    web_app.WebLogs(account_id="account-1").set_progress(
        {"server": "ES / Leto", "sent": 725, "total": 3555, "remaining": 2830, "percent": 20.4}
    )

    rows = web_app._build_server_summary()

    assert [(row["account_id"], row["server"]) for row in rows] == [
        ("account-1", "ES / Leto"),
        ("account-2", "BR / Ikaros"),
    ]


def test_recovery_logs_are_visible_in_account_panel():
    assert web_app._is_account_log_visible("Filtro nao confirmou selecao: 51 - 100")
    assert web_app._is_account_log_visible("Filtro sem usuarios novos: 51 - 100")
    assert web_app._is_account_log_visible("Filtro concluido: 50 jogadores ja confirmados na Outbox.")
    assert web_app._is_account_log_visible("Filtro bloqueado: nenhum envio foi confirmado.")
    assert web_app._is_account_log_visible("Tabela de ranking nao encontrada.")
    assert web_app._is_account_log_visible("Jogador ignorado: Player One - confirmado na Outbox.")
    assert web_app._is_account_log_visible("Jogador bloqueado: Player Two - reserva existente.")
    assert web_app._is_account_log_visible("Falha ao preparar mensagem para Player Three: TimeoutException")
    assert web_app._is_account_log_visible("Servidor sem progresso por 151s. Reiniciando navegador e pulando fluxo travado.")
    assert web_app._is_account_log_visible("Falha no Selenium: SessionNotCreatedException: Chrome failed to start")
    assert not web_app._is_account_log_visible("Pulando jogador Player One: mensagem ja registrada no banco.")
    assert web_app._is_account_log_visible("Outbox indisponivel no inicio do servidor. Continuando pelo contador local e pelo banco.")
    assert web_app._is_account_log_visible("Servidor concluido: 150 mensagens ja contabilizadas.")


def test_selenium_startup_error_is_persisted_and_returned_by_logs_route(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        web_app.RuntimeLog,
        "add",
        lambda *, text, level, account_id: persisted.append(
            {"text": text, "level": level, "account_id": account_id}
        ),
    )
    monkeypatch.setattr(
        web_app.RuntimeLog,
        "list_recent",
        lambda account_id=None: [{"level": "error", "text": "Falha no Selenium: Chrome failed to start"}],
    )

    web_app.WebLogs(account_id="account-1").addLogs("Falha no Selenium: Chrome failed to start", "error")
    response = web_app.app.test_client().get("/logs?account_id=account-1")

    assert persisted == [
        {"text": "Falha no Selenium: Chrome failed to start", "level": "error", "account_id": "account-1"}
    ]
    assert response.get_json() == [{"level": "error", "text": "Falha no Selenium: Chrome failed to start"}]


def test_logs_route_hides_historical_player_skip_lines(monkeypatch):
    monkeypatch.setattr(
        web_app.RuntimeLog,
        "list_recent",
        lambda account_id=None: [
            {"level": "info", "text": "Pulando jogador Player One: mensagem ja registrada no banco."},
            {"level": "info", "text": "Enviado para Player Two - Total: 1"},
        ],
    )

    response = web_app.app.test_client().get("/logs?account_id=account-1")

    assert response.get_json() == [{"level": "info", "text": "Enviado para Player Two - Total: 1"}]


def test_stop_endpoint_returns_before_heavy_cleanup_finishes(monkeypatch):
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def slow_stop_active_drivers():
        cleanup_started.set()
        release_cleanup.wait(timeout=5)

    monkeypatch.setattr(web_app, "_stop_active_drivers", slow_stop_active_drivers)
    monkeypatch.setattr(web_app, "_cleanup_all_bot_profile_processes", lambda: None)
    web_app.BOT_STATE["running"] = True
    web_app.BOT_STATE["stopping"] = False
    web_app.BOT_STATE["stop_event"] = threading.Event()
    web_app.BOT_STATE["pause_event"] = threading.Event()
    web_app.BOT_STATE["threads"] = []
    web_app.BOT_STATE["stop_cleanup_thread"] = None

    started = time.monotonic()
    response = web_app.app.test_client().post("/bot/stop")
    elapsed = time.monotonic() - started

    assert response.status_code == 302
    assert elapsed < 1.0
    assert cleanup_started.wait(timeout=1)
    assert web_app.BOT_STATE["stopping"] is True

    release_cleanup.set()
    cleanup_thread = web_app.BOT_STATE.get("stop_cleanup_thread")
    if cleanup_thread:
        cleanup_thread.join(timeout=2)
def test_shutdown_application_stops_workers_and_profile_processes(monkeypatch):
    stopped = []
    cleaned = []
    stop_event = threading.Event()
    pause_event = threading.Event()
    pause_event.set()
    monkeypatch.setitem(web_app.BOT_STATE, "stop_event", stop_event)
    monkeypatch.setitem(web_app.BOT_STATE, "pause_event", pause_event)
    monkeypatch.setitem(web_app.BOT_STATE, "running", True)
    monkeypatch.setitem(web_app.BOT_STATE, "paused", True)
    monkeypatch.setattr(web_app, "_stop_active_drivers", lambda: stopped.append(True))
    monkeypatch.setattr(web_app, "_cleanup_all_bot_profile_processes", lambda: cleaned.append(True))

    web_app.shutdown_application()

    assert stop_event.is_set()
    assert not pause_event.is_set()
    assert web_app.BOT_STATE["running"] is False
    assert web_app.BOT_STATE["paused"] is False
    assert web_app.BOT_STATE["stopping"] is True
    assert stopped == [True]
    assert cleaned == [True]
