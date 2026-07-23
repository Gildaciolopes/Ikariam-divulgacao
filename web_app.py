"""Flask web interface to manage accounts and run the bot."""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, url_for
from selenium.common.exceptions import WebDriverException

try:
    from pynput import keyboard
except Exception:
    keyboard = None

from src.start import BotDriver, BotStopped, _is_driver_connection_error, _terminate_chrome_profile_processes
from src.audit_log import DetailedAuditLog
from src.storage import Accounts, DEFAULT_MESSAGE, INSTANCE_ID, RuntimeLog, Servers, Settings, UsersSend, app_data_dir, init_db

app = Flask(__name__)
init_db()

try:
    MAX_PARALLEL_DRIVERS = int(os.getenv("BOT_MAX_PARALLEL_DRIVERS", "0"))
except ValueError:
    MAX_PARALLEL_DRIVERS = 0

_settings = Settings.load()
LOG_STORE_MAX = 500
LOG_STORE: deque[dict[str, str]] = deque(maxlen=LOG_STORE_MAX)
LOG_STORE_BY_ACCOUNT: dict[str, deque[dict[str, str]]] = {}
LOG_LOCK = threading.Lock()
DRIVER_LOCK = threading.Lock()
ACTIVE_DRIVERS: set[BotDriver] = set()
PROGRESS_ACTIVE_TTL_SECONDS = 90.0
try:
    DRIVER_IDLE_TIMEOUT_SECONDS = float(os.getenv("BOT_DRIVER_IDLE_TIMEOUT", "150"))
except ValueError:
    DRIVER_IDLE_TIMEOUT_SECONDS = 150.0
DRIVER_RECOVERY_LIMIT = 3
PROGRESS_STATE: dict[str, float | int | str | None] = {
    "server": None,
    "sent": 0,
    "total": 0,
    "remaining": 0,
    "percent": 0.0,
    "updated_at": None,
    "updated_epoch": None,
}
PROGRESS_STATE_BY_ACCOUNT: dict[str, dict[str, float | int | str | None]] = {}
PROGRESS_LOCK = threading.Lock()
HOTKEY_LOCK = threading.Lock()
HOTKEY_LISTENER = None
HOTKEY_PRESSED: set[str] = set()

BOT_STATE: dict[str, Any] = {
    "running": False,
    "stopping": False,
    "stop_requested_at": None,
    "stop_cleanup_thread": None,
    "thread": None,
    "threads": [],
    "stop_event": threading.Event(),
    "pause_event": threading.Event(),
    "paused": False,
    "settings": {
        "time_wait": _settings.time_wait,
        "post_send_wait": _settings.post_send_wait,
        "logs": _settings.logs,
        "headless": _settings.headless,
        "dry_run": _settings.dry_run,
        "save_detailed_logs": _settings.save_detailed_logs,
        "show_selenium_errors": False,
    },
}


def _now() -> str:
    return time.strftime("%H:%M:%S")


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
ACCOUNT_LOG_PREFIXES = (
    "Servidor selecionado:",
    "Servidor sem progresso por ",
    "Login nao confirmou acesso ao lobby.",
    "Filtro selecionado:",
    "Filtro nao confirmou selecao:",
    "Filtro sem usuarios novos:",
    "Filtro concluido:",
    "Filtro bloqueado:",
    "Servidor concluido:",
    "Tabela de ranking nao encontrada.",
    "Jogador ignorado:",
    "Jogador bloqueado:",
    "Falha ao preparar mensagem para ",
    "Enviado para ",
    "Simulado para ",
    "Status de envio nao confirmado para ",
    "Sessao do navegador encerrada.",
    "Outbox sincronizada:",
    "Nao foi possivel sincronizar Outbox:",
    "Outbox indisponivel no inicio do servidor.",
    "Nenhum servidor ativo encontrado",
    "Falha no Selenium:",
    "Falha ao iniciar o bot:",
    "Ativacao",
    "Conta ativada:",
    "Pagina atualizou durante o Selenium.",
    "Pagina atualizou repetidamente.",
)


def _sanitize_log_text(text: str) -> str:
    return EMAIL_RE.sub("conta", str(text or "")).strip()


def _is_account_log_visible(text: str) -> bool:
    return any(text.startswith(prefix) for prefix in ACCOUNT_LOG_PREFIXES)


def _register_driver(bot: BotDriver) -> None:
    with DRIVER_LOCK:
        ACTIVE_DRIVERS.add(bot)


def _unregister_driver(bot: BotDriver) -> None:
    with DRIVER_LOCK:
        ACTIVE_DRIVERS.discard(bot)


def _stop_active_drivers() -> None:
    with DRIVER_LOCK:
        drivers = list(ACTIVE_DRIVERS)
    for bot in drivers:
        try:
            bot._stop()
        except Exception:
            pass


def _cleanup_all_bot_profile_processes() -> None:
    profile_base = Path(os.getenv("BOT_CACHE_DIR") or app_data_dir() / "browser_profiles")
    if not profile_base.exists():
        return
    for profile_dir in profile_base.iterdir():
        if profile_dir.is_dir():
            _terminate_chrome_profile_processes(profile_dir)


def _active_account_ids() -> set[str]:
    with PROGRESS_LOCK:
        return set(PROGRESS_STATE_BY_ACCOUNT)


def _is_fresh_progress(payload: dict[str, float | int | str | None], now: float) -> bool:
    updated_epoch = payload.get("updated_epoch")
    return isinstance(updated_epoch, (int, float)) and now - float(updated_epoch) <= PROGRESS_ACTIVE_TTL_SECONDS


def _snapshot_active_progress() -> dict[str, dict[str, float | int | str | None]]:
    now = time.time()
    with PROGRESS_LOCK:
        return {
            key: dict(value)
            for key, value in PROGRESS_STATE_BY_ACCOUNT.items()
            if _is_fresh_progress(value, now)
        }


def _log_hotkey_toggle(enabled: bool) -> None:
    _append_log("Hotkey F8 habilitada." if enabled else "Hotkey F8 indisponivel.", "info" if enabled else "warn")


def _normalize_hotkey_key(key: Any) -> str | None:
    try:
        return str(key.char).lower()
    except Exception:
        return str(key).replace("Key.", "").lower()


def _on_hotkey_press(key: Any) -> None:
    normalized = _normalize_hotkey_key(key)
    if not normalized:
        return
    with HOTKEY_LOCK:
        HOTKEY_PRESSED.add(normalized)
    if normalized == "f8" and BOT_STATE.get("running"):
        stop_event = BOT_STATE.get("stop_event")
        if isinstance(stop_event, threading.Event):
            stop_event.set()
        BOT_STATE["stopping"] = True
        BOT_STATE["stop_requested_at"] = time.time()
        _append_log("Parada solicitada pela hotkey F8.", "warn")


def _on_hotkey_release(key: Any) -> None:
    normalized = _normalize_hotkey_key(key)
    if not normalized:
        return
    with HOTKEY_LOCK:
        HOTKEY_PRESSED.discard(normalized)


def start_hotkey_listener() -> None:
    global HOTKEY_LISTENER
    if HOTKEY_LISTENER is not None or keyboard is None:
        _log_hotkey_toggle(False)
        return
    try:
        HOTKEY_LISTENER = keyboard.Listener(on_press=_on_hotkey_press, on_release=_on_hotkey_release)
        HOTKEY_LISTENER.daemon = True
        HOTKEY_LISTENER.start()
        _log_hotkey_toggle(True)
    except Exception:
        _log_hotkey_toggle(False)


def _finalize_stop(requested_at: float, timeout: float = 20.0) -> None:
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            threads = [thread for thread in BOT_STATE.get("threads", []) if thread.is_alive()]
            if not threads:
                break
            time.sleep(0.2)
        _stop_active_drivers()
        _cleanup_all_bot_profile_processes()
        BOT_STATE["running"] = False
        BOT_STATE["stopping"] = False
        BOT_STATE["stop_requested_at"] = None
        BOT_STATE["threads"] = []
        _append_log("Loop finalizado.", "info")
    finally:
        if BOT_STATE.get("stop_cleanup_thread") is threading.current_thread():
            BOT_STATE["stop_cleanup_thread"] = None


def _request_stop_cleanup(timeout: float = 20.0) -> None:
    cleanup_thread = BOT_STATE.get("stop_cleanup_thread")
    if isinstance(cleanup_thread, threading.Thread) and cleanup_thread.is_alive():
        return
    requested_at = time.time()
    BOT_STATE["stopping"] = True
    BOT_STATE["stop_requested_at"] = requested_at
    cleanup_thread = threading.Thread(target=_finalize_stop, args=(requested_at, timeout), daemon=True)
    BOT_STATE["stop_cleanup_thread"] = cleanup_thread
    cleanup_thread.start()


def shutdown_application() -> None:
    """Encerra workers e navegadores quando a janela desktop é fechada."""
    stop_event = BOT_STATE.get("stop_event")
    pause_event = BOT_STATE.get("pause_event")
    if isinstance(pause_event, threading.Event):
        pause_event.clear()
    if isinstance(stop_event, threading.Event):
        stop_event.set()
    BOT_STATE["paused"] = False
    BOT_STATE["stopping"] = True
    _stop_active_drivers()
    _cleanup_all_bot_profile_processes()
    BOT_STATE["running"] = False


def _append_log(text: str, level: str = "info", account_id: str | None = None, account_label: str | None = None) -> None:
    clean_text = _sanitize_log_text(text)
    if not clean_text:
        return
    entry = {"level": level, "text": clean_text}
    with LOG_LOCK:
        if not account_id:
            LOG_STORE.append(entry)
        if account_id:
            if _is_account_log_visible(clean_text):
                LOG_STORE_BY_ACCOUNT.setdefault(account_id, deque(maxlen=LOG_STORE_MAX)).append(entry)
                RuntimeLog.add(text=clean_text, level=level, account_id=account_id)


class WebLogs:
    def __init__(
        self,
        account_id: str | None = None,
        account_label: str | None = None,
        audit_log: DetailedAuditLog | None = None,
    ) -> None:
        self.account_id = account_id
        self.account_label = account_label
        self.audit_log = audit_log

    def addAudit(self, event: str, **data: Any) -> None:
        if self.audit_log is not None:
            data.pop("account_id", None)
            self.audit_log.write(event, account_id=self.account_id, **data)

    def addLogs(self, text: str, level: str = "info") -> None:
        settings = BOT_STATE.get("settings", {})
        clean_text = _sanitize_log_text(text)
        self.addAudit("application_log", level=level, message=clean_text)
        if not settings.get("logs", True):
            return
        if not _is_account_log_visible(clean_text):
            return
        if level == "error" and not settings.get("show_selenium_errors", False):
            text_lower = str(clean_text).lower()
            noisy = (
                "no such element",
                "unable to locate element",
                "element not interactable",
                "element click intercepted",
                "stale element reference",
                "timeout waiting for condition",
                "httpconnectionpool",
                "max retries exceeded with url",
            )
            if any(marker in text_lower for marker in noisy):
                level = "debug"
        _append_log(clean_text, level, self.account_id, self.account_label)

    def set_progress(self, data: dict[str, Any]) -> None:
        payload = dict(data)
        payload["updated_at"] = _now()
        payload["updated_epoch"] = time.time()
        if self.account_id:
            payload["account_id"] = self.account_id
            with PROGRESS_LOCK:
                PROGRESS_STATE_BY_ACCOUNT[self.account_id] = payload
        with PROGRESS_LOCK:
            PROGRESS_STATE.update(payload)
        self.addAudit("progress_updated", **payload)


def _now_label() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _display_server_name(value: str | None) -> str:
    return (value or "").strip() or "--"


def _server_display_name(server: Servers) -> str:
    return getattr(server, "display_name", None) or _display_server_name(server.server)


def _build_account_server_totals(accounts: list[Accounts]) -> dict[str, list[dict[str, str | int]]]:
    result: dict[str, list[dict[str, str | int]]] = {}
    for account in accounts:
        rows: list[dict[str, str | int]] = []
        for server in Servers.find():
            rows.append({"server": _server_display_name(server), "sent": server.messageSend, "users": server.users})
        result[account.id_str] = rows
    return result


def _build_server_summary() -> list[dict[str, str | int | float | bool | None]]:
    active_progress = _snapshot_active_progress()
    account_order = {
        account.id_str: index
        for index, account in enumerate(Accounts.find({"instance_id": INSTANCE_ID}))
    }
    active_by_server: dict[str, dict[str, float | int | str | None]] = {}
    for payload in active_progress.values():
        server = _display_server_name(str(payload.get("server") or ""))
        if server == "--":
            continue
        active_by_server[server.casefold()] = payload

    server_lookup = {_server_display_name(server).casefold(): server for server in Servers.find()}
    rows: list[dict[str, str | int | float | bool | None]] = []
    for server in Servers.find():
        server_name = _server_display_name(server)
        key = server_name.casefold()
        active_payload = active_by_server.get(key)
        if not active_payload:
            continue
        sent = int(server.messageSend or active_payload.get("sent") or 0)
        total = int(server.users or 0) or None
        percent = round((sent / total) * 100, 1) if total else None
        rows.append(
            {
                "server": server_name,
                "sent": sent,
                "total": total,
                "remaining": max(total - sent, 0) if total else None,
                "percent": percent,
                "updated_at": active_payload.get("updated_at") if active_payload else None,
                "active": bool(active_payload),
                "account_id": str(active_payload.get("account_id") or ""),
            }
        )

    for server_key, payload in active_by_server.items():
        if server_key in server_lookup:
            continue
        sent = int(payload.get("sent") or 0)
        total = int(payload.get("total") or 0) or None
        rows.append(
            {
                "server": _display_server_name(str(payload.get("server") or "")),
                "sent": sent,
                "total": total,
                "remaining": int(payload.get("remaining") or 0) if total else None,
                "percent": float(payload.get("percent") or 0) if total else None,
                "updated_at": payload.get("updated_at"),
                "active": True,
                "account_id": str(payload.get("account_id") or ""),
            }
        )

    rows.sort(
        key=lambda item: (
            account_order.get(str(item.get("account_id") or ""), len(account_order)),
            str(item.get("server") or "").casefold(),
        )
    )
    return rows


def _persist_settings() -> None:
    settings = Settings(
        time_wait=float(BOT_STATE["settings"].get("time_wait", 1.0)),
        post_send_wait=float(BOT_STATE["settings"].get("post_send_wait", 1.0)),
        logs=bool(BOT_STATE["settings"].get("logs", True)),
        headless=bool(BOT_STATE["settings"].get("headless", False)),
        dry_run=bool(BOT_STATE["settings"].get("dry_run", False)),
        save_detailed_logs=bool(BOT_STATE["settings"].get("save_detailed_logs", False)),
        default_message=Settings.load().default_message,
    )
    settings.save()


def _run_bot_loop(
    time_wait: float,
    logs: bool,
    headless: bool,
    dry_run: bool,
    post_send_wait: float = 1.0,
    save_detailed_logs: bool = False,
    audit_path: str | None = None,
) -> None:
    accounts = Accounts.find({"instance_id": INSTANCE_ID})
    max_parallel = MAX_PARALLEL_DRIVERS if MAX_PARALLEL_DRIVERS > 0 else max(1, len(accounts))
    stop_event = BOT_STATE["stop_event"]
    pause_event = BOT_STATE["pause_event"]
    run_id = Path(audit_path).stem if audit_path else uuid.uuid4().hex
    audit_log = DetailedAuditLog(Path(audit_path), run_id) if save_detailed_logs and audit_path else None
    if audit_log:
        audit_log.write(
            "bot_run_started",
            account_count=len(accounts),
            time_wait=time_wait,
            post_send_wait=post_send_wait,
            headless=headless,
            dry_run=dry_run,
            max_parallel=max_parallel,
        )

    def worker(account_id: str) -> None:
        account = Accounts.find_by_id(account_id)
        if not account or stop_event.is_set():
            return
        logger = WebLogs(account.id_str, audit_log=audit_log) if audit_log else WebLogs(account.id_str)
        account.status = "running"
        account.last_login = _now_label()
        account.save()
        for recovery_attempt in range(1, DRIVER_RECOVERY_LIMIT + 1):
            if stop_event.is_set():
                account.status = "stopped"
                break
            bot = None
            watchdog_stop = threading.Event()
            restart_requested = threading.Event()
            try:
                bot = BotDriver(
                    account=account,
                    headless=headless,
                    cache=account.cache,
                    timeWait=time_wait,
                    postSendWait=post_send_wait,
                    logs=logger,
                    stop_event=stop_event,
                    pause_event=pause_event,
                    dry_run=dry_run,
                )
                _register_driver(bot)
                if audit_log:
                    logger.addAudit("driver_started", recovery_attempt=recovery_attempt)

                def watchdog() -> None:
                    while not watchdog_stop.is_set() and not stop_event.is_set():
                        time.sleep(2)
                        if pause_event.is_set():
                            continue
                        idle_for = time.time() - float(getattr(bot, "last_activity_at", time.time()))
                        if idle_for < DRIVER_IDLE_TIMEOUT_SECONDS:
                            continue
                        restart_requested.set()
                        logger.addLogs(
                            f"Servidor sem progresso por {int(idle_for)}s. Reiniciando somente este navegador.",
                            "warn",
                        )
                        try:
                            bot.close()
                        except Exception:
                            pass
                        break

                threading.Thread(target=watchdog, daemon=True).start()
                bot.StartGame(logger)
                if restart_requested.is_set() and not stop_event.is_set():
                    raise WebDriverException("driver restarted by idle watchdog")
                account.status = "activate" if not stop_event.is_set() else "stopped"
                break
            except BotStopped:
                account.status = "stopped"
                logger.addLogs("Bot parado.", "warn")
                break
            except Exception as error:
                driver_lost = restart_requested.is_set() or _is_driver_connection_error(error)
                if driver_lost and recovery_attempt < DRIVER_RECOVERY_LIMIT and not stop_event.is_set():
                    logger.addLogs(
                        f"Sessao do navegador encerrada. Recriando driver ({recovery_attempt}/{DRIVER_RECOVERY_LIMIT}).",
                        "warn",
                    )
                    continue
                account.status = "error"
                logger.addLogs(f"Falha no Selenium: {type(error).__name__}: {str(error).splitlines()[0] if str(error) else ''}", "error")
                break
            finally:
                watchdog_stop.set()
                if bot is not None:
                    _unregister_driver(bot)
                    try:
                        bot.close()
                    except Exception:
                        pass
        account.save()
        if audit_log:
            logger.addAudit("account_worker_finished", status=account.status)

    try:
        pending = [account.id_str for account in accounts]
        running: dict[str, threading.Thread] = {}
        while pending or running:
            if stop_event.is_set():
                break
            for account_id, thread in list(running.items()):
                if not thread.is_alive():
                    running.pop(account_id, None)
            while pending and len(running) < max_parallel:
                account_id = pending.pop(0)
                thread = threading.Thread(target=worker, args=(account_id,), daemon=True)
                running[account_id] = thread
                thread.start()
            BOT_STATE["threads"] = list(running.values())
            time.sleep(0.2)
        for thread in list(running.values()):
            thread.join(timeout=5)
    finally:
        if audit_log:
            audit_log.write("bot_run_finished", stopped=stop_event.is_set())
        _finalize_stop(time.time(), timeout=2.0)


@app.get("/")
def index():
    settings = Settings.load()
    BOT_STATE["settings"]["time_wait"] = settings.time_wait
    BOT_STATE["settings"]["logs"] = settings.logs
    BOT_STATE["settings"]["headless"] = settings.headless
    BOT_STATE["settings"]["dry_run"] = settings.dry_run
    BOT_STATE["settings"]["save_detailed_logs"] = settings.save_detailed_logs
    accounts = Accounts.find({"instance_id": INSTANCE_ID})
    return render_template(
        "index.html",
        accounts=accounts,
        bot_state=BOT_STATE,
        default_message=settings.default_message,
        server_summary=_build_server_summary(),
        account_server_totals=_build_account_server_totals(accounts),
        account_message_map={account.id_str: account.message for account in accounts},
    )


@app.post("/accounts")
def create_account():
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    if email and password:
        Accounts.create(email=email, password=password, cache=True, message=Settings.load().default_message, instance_id=INSTANCE_ID)
        _append_log(f"Conta cadastrada: {email}", "info")
    return redirect(url_for("index"))


@app.post("/accounts/<account_id>/delete")
def delete_account(account_id: str):
    account = Accounts.find_by_id(account_id)
    if account:
        account.delete_instance()
        _append_log(f"Conta removida: {account.email}", "info")
    return redirect(url_for("index"))


def _run_account_activation(account_id: str, bot: BotDriver, logger: WebLogs) -> None:
    """Run visible account activation and persist only a confirmed result."""
    try:
        confirmed = bool(bot.login())
        account = Accounts.find_by_id(account_id)
        if not account:
            return
        if confirmed:
            account.status = "activate"
            account.last_login = _now_label()
            account.save()
            logger.addLogs("Conta ativada: login confirmado no lobby.", "info")
        else:
            account.status = "inactive" if getattr(bot, "activation_cancelled", False) else "error"
            account.save()
            message = (
                "Ativacao cancelada pelo usuario."
                if getattr(bot, "activation_cancelled", False)
                else "Ativacao nao confirmada. A conta nao sera usada pelo bot."
            )
            logger.addLogs(message, "warn")
    except Exception as error:
        account = Accounts.find_by_id(account_id)
        if account:
            account.status = "error"
            account.save()
        logger.addLogs(
            f"Falha na ativacao (Chrome/driver). Erro: {type(error).__name__}: {str(error).splitlines()[0] if str(error) else ''}",
            "warn",
        )
    finally:
        _unregister_driver(bot)
        try:
            bot.close()
        except Exception:
            pass


@app.post("/accounts/<account_id>/activate")
def activate_account(account_id: str):
    """Start a visible login and mark the account active only after confirmation."""
    account = Accounts.find_by_id(account_id)
    bot = None
    if account:
        if account.status in {"activate", "activating"}:
            _append_log("Ativacao ignorada: a conta ja esta ativa ou em ativacao.", "warn", account.id_str, account.email)
            return redirect(url_for("index"))
        try:
            if not account.cache:
                account.cache = True
            account.status = "activating"
            account.save()
            logger = WebLogs(account.id_str, account.email)
            bot = BotDriver(headless=False, account=account, cache=True, logs=logger)
            _register_driver(bot)
            thread = threading.Thread(
                target=_run_account_activation,
                args=(account.id_str, bot, logger),
                daemon=True,
            )
            thread.start()
            _append_log("Ativacao iniciada em navegador visivel.", "info", account.id_str, account.email)
        except Exception as error:
            account.status = "error"
            account.save()
            if bot is not None:
                _unregister_driver(bot)
                try:
                    bot.close()
                except Exception:
                    pass
            _append_log(
                f"Falha ao ativar a conta (Chrome/driver). Erro: {error}",
                "warn",
                account.id_str,
                account.email,
            )
    return redirect(url_for("index"))


@app.post("/accounts/message")
def update_account_message():
    account_id = (request.form.get("account_id") or "").strip()
    message = (request.form.get("message") or "").strip()
    account = Accounts.find_by_id(account_id)
    if account:
        account.message = message
        account.save()
    return redirect(url_for("index"))


@app.post("/bot/start")
def start_bot():
    if BOT_STATE.get("running"):
        return redirect(url_for("index"))
    accounts = Accounts.find({"instance_id": INSTANCE_ID})
    if not accounts:
        _append_log("Nenhuma conta cadastrada. Cadastre uma conta antes de iniciar.", "warn")
        return redirect(url_for("index"))
    settings = Settings.load()
    BOT_STATE["settings"].update(
        {
            "time_wait": settings.time_wait,
            "post_send_wait": settings.post_send_wait,
            "logs": settings.logs,
            "headless": settings.headless,
            "dry_run": settings.dry_run,
            "save_detailed_logs": settings.save_detailed_logs,
        }
    )
    BOT_STATE["stop_event"] = threading.Event()
    BOT_STATE["pause_event"] = threading.Event()
    BOT_STATE["running"] = True
    BOT_STATE["paused"] = False
    BOT_STATE["stopping"] = False
    with PROGRESS_LOCK:
        PROGRESS_STATE_BY_ACCOUNT.clear()
        PROGRESS_STATE.update({"server": None, "sent": 0, "total": 0, "remaining": 0, "percent": 0.0, "updated_at": None, "updated_epoch": None})
    audit_path = None
    if settings.save_detailed_logs:
        run_id = f"{time.strftime('%Y-%m-%d_%H-%M-%S')}_{uuid.uuid4().hex[:8]}"
        audit_path = str(app_data_dir() / "log" / f"bot-run_{run_id}.jsonl")
    thread = threading.Thread(
        target=_run_bot_loop,
        args=(settings.time_wait, settings.logs, settings.headless, settings.dry_run, settings.post_send_wait, settings.save_detailed_logs, audit_path),
        daemon=True,
    )
    BOT_STATE["thread"] = thread
    thread.start()
    return redirect(url_for("index"))


@app.post("/bot/settings")
def update_settings():
    settings = Settings.load()
    try:
        settings.time_wait = max(float((request.form.get("time_wait") or "1").strip().replace(",", ".")), 0.1)
    except ValueError:
        settings.time_wait = 1.0
    try:
        settings.post_send_wait = max(float((request.form.get("post_send_wait") or "1").strip().replace(",", ".")), 0.1)
    except ValueError:
        settings.post_send_wait = 1.0
    settings.headless = bool(request.form.get("headless"))
    settings.dry_run = bool(request.form.get("dry_run"))
    settings.save_detailed_logs = bool(request.form.get("save_detailed_logs"))
    settings.default_message = (request.form.get("default_message") or DEFAULT_MESSAGE).strip()
    settings.save()
    BOT_STATE["settings"].update(
        {
            "time_wait": settings.time_wait,
            "post_send_wait": settings.post_send_wait,
            "headless": settings.headless,
            "dry_run": settings.dry_run,
            "logs": settings.logs,
            "save_detailed_logs": settings.save_detailed_logs,
        }
    )
    Accounts.find({"instance_id": INSTANCE_ID})
    for account in Accounts.find({"instance_id": INSTANCE_ID}):
        if not account.message:
            account.message = settings.default_message
            account.save()
    return redirect(url_for("index"))


@app.post("/bot/toggle-log-errors")
def toggle_log_errors():
    BOT_STATE["settings"]["show_selenium_errors"] = not BOT_STATE["settings"].get("show_selenium_errors", False)
    return redirect(url_for("index"))


@app.post("/bot/stop")
def stop_bot():
    stop_event = BOT_STATE.get("stop_event")
    pause_event = BOT_STATE.get("pause_event")
    if isinstance(pause_event, threading.Event):
        pause_event.clear()
    if isinstance(stop_event, threading.Event):
        stop_event.set()
    BOT_STATE["paused"] = False
    _request_stop_cleanup()
    with PROGRESS_LOCK:
        PROGRESS_STATE_BY_ACCOUNT.clear()
        PROGRESS_STATE.update({"server": None, "sent": 0, "total": 0, "remaining": 0, "percent": 0.0, "updated_at": None, "updated_epoch": None})
    return redirect(url_for("index"))


@app.post("/bot/pause")
def pause_bot():
    if BOT_STATE.get("running"):
        pause_event = BOT_STATE.get("pause_event")
        paused = not BOT_STATE.get("paused")
        BOT_STATE["paused"] = paused
        if isinstance(pause_event, threading.Event):
            pause_event.set() if paused else pause_event.clear()
    return redirect(url_for("index"))


@app.get("/logs")
def logs():
    account_id = (request.args.get("account_id") or "").strip()
    if account_id == "all":
        account_id = ""
    rows = RuntimeLog.list_recent(account_id) if account_id else RuntimeLog.list_recent()
    visible_rows = [
        row
        for row in rows
        if _is_account_log_visible(str(row.get("text") or ""))
    ]
    return jsonify(visible_rows)


@app.get("/progress")
def progress():
    rows = list(_snapshot_active_progress().values())
    if not rows and PROGRESS_STATE.get("server"):
        rows = [dict(PROGRESS_STATE)]
    return jsonify({"accounts": rows, "servers": _build_server_summary()})


@app.post("/logs/clear")
def clear_logs():
    account_id = (request.args.get("account_id") or "").strip()
    with LOG_LOCK:
        if account_id and account_id != "all":
            LOG_STORE_BY_ACCOUNT.pop(account_id, None)
        else:
            LOG_STORE.clear()
            LOG_STORE_BY_ACCOUNT.clear()
    RuntimeLog.clear(account_id if account_id and account_id != "all" else None)
    return jsonify({"ok": True})


@app.get("/status")
def status():
    with LOG_LOCK:
        last_log = LOG_STORE[-1] if LOG_STORE else None
    return jsonify(
        {
            "total_accounts": len(Accounts.find({"instance_id": INSTANCE_ID})),
            "active_accounts": len(Accounts.find({"status": "activate", "instance_id": INSTANCE_ID})),
            "servers": len(Servers.find({"instance_id": INSTANCE_ID})),
            "users_sent": len(UsersSend.find({"instance_id": INSTANCE_ID})),
            "running": bool(BOT_STATE.get("running")),
            "stopping": bool(BOT_STATE.get("stopping")),
            "settings": BOT_STATE.get("settings"),
            "last_log": last_log,
        }
    )
