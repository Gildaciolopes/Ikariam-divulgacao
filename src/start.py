"""Selenium automation for the Ikariam disclosure bot."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from time import sleep
from typing import Any, Protocol
from urllib.parse import parse_qs, urljoin, urlparse

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, NoSuchWindowException, SessionNotCreatedException, StaleElementReferenceException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from src.storage import Accounts, DEFAULT_MESSAGE, Servers, UsersSend, _normalize_server_flag, app_data_dir

CLICK_DELAY_SECONDS = 0.25
CLOSE_TAB_DELAY_SECONDS = 0.35
FAST_WAIT_SECONDS = 0.5
SHORT_WAIT_SECONDS = 1.0
MESSAGE_TEXTAREA_TIMEOUT_SECONDS = 25
MESSAGE_SUBMIT_TIMEOUT_SECONDS = 20
MESSAGE_PREPARE_ATTEMPTS = 2
MESSAGE_TEXT_SETTLE_SECONDS = 0.1
MESSAGE_SCROLL_SETTLE_SECONDS = 0.1
SEND_FEEDBACK_TIMEOUT_SECONDS = 0.8
IGNORE_FEEDBACK_TIMEOUT_SECONDS = 0.5
SERVER_SEND_BATCH_LIMIT = 25
ACTIVATION_WAIT_SECONDS = 300.0
STALE_ELEMENT_RECOVERY_LIMIT = 3
DRIVER_IDLE_TIMEOUT_SECONDS = float(os.getenv("BOT_DRIVER_IDLE_TIMEOUT", "150"))
LOBBY_URL = "https://lobby.ikariam.gameforge.com/pt_BR/"
ACCOUNTS_URL = "https://lobby.ikariam.gameforge.com/pt_BR/accounts"
SERVER_START_BUTTON_MARKERS = ("jogar", "começar", "comecar", "play", "start")
OWNED_SERVER_BUTTON_MARKERS = ("jogar", "play")


def _server_send_batch_limit() -> int:
    try:
        configured = int(os.getenv("BOT_SERVER_SEND_BATCH_LIMIT", str(SERVER_SEND_BATCH_LIMIT)))
    except ValueError:
        return SERVER_SEND_BATCH_LIMIT
    return max(1, min(configured, SERVER_SEND_BATCH_LIMIT))
IGNORED_SERVER_MARKERS = (
    "asphodel",
    "banido",
    "banida",
    "banned",
    "transparente",
    "transparent",
    "sua conta esta banida",
    "account transfer",
    "violation of terms",
    "terms and conditions",
    "spam",
)


class LogSink(Protocol):
    def addLogs(self, text: str, level: str = "info") -> None: ...

    def set_progress(self, data: dict[str, Any]) -> None: ...

    def addAudit(self, event: str, **data: Any) -> None: ...


def _audit(text_logs: LogSink | None, event: str, **data: Any) -> None:
    writer = getattr(text_logs, "addAudit", None)
    if callable(writer):
        writer(event, **data)


def _audit_database_counts(text_logs: LogSink | None, server: Servers | None) -> dict[str, int]:
    if server is None or getattr(text_logs, "audit_log", None) is None:
        return {}
    return {
        "database_record_total": UsersSend.count_for_server(server_id=server.id),
        "database_sent_total": UsersSend.count_for_server(server_id=server.id, status="sent"),
    }


class BotStopped(Exception):
    """Raised when the bot is stopped by UI or hotkey."""


class BotCooldown(Exception):
    """Raised when Ikariam asks the account to wait before sending again."""

    def __init__(self, wait_seconds: int = 60) -> None:
        self.wait_seconds = wait_seconds
        super().__init__(f"Cooldown detected: wait {wait_seconds}s")


class BotUnconfirmed(Exception):
    """Raised when the page does not confirm whether a send succeeded."""

    def __init__(self, wait_seconds: int = 10) -> None:
        self.wait_seconds = wait_seconds
        super().__init__(f"Send status unconfirmed: wait {wait_seconds}s")


def _error_summary(error: Exception) -> str:
    return str(error).splitlines()[0] if str(error) else type(error).__name__


def _cleanup_chrome_profile(profile_dir: Path | None) -> None:
    if profile_dir and profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)


def _make_temp_profile_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="ikariam-bot-chrome-"))


def _safe_profile_name(email: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", email or "profile").strip("_")
    return (safe_name or "profile")[:32]


def _send_diagnostics_enabled() -> bool:
    return os.getenv("BOT_SEND_DIAGNOSTICS", "").strip().casefold() in {"1", "true", "yes", "on"}


def _safe_url_path(value: str) -> str:
    """Keep diagnostic URLs useful without retaining query tokens."""
    try:
        parsed = urlparse(value)
        return parsed.path or "/"
    except Exception:
        return "/"


def _make_cached_profile_dir(account: Accounts) -> Path:
    base_cache = os.getenv("BOT_CACHE_DIR")
    cache_base_dir = Path(base_cache) if base_cache else app_data_dir() / "browser_profiles"
    profile_dir = cache_base_dir / _safe_profile_name(account.email)
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def _build_chrome_options(headless: bool, profile_dir: Path) -> ChromeOptions:
    options = ChromeOptions()
    options.page_load_strategy = "none"
    options.add_argument(f"--user-data-dir={profile_dir.resolve()}")
    options.add_argument("--remote-debugging-port=0")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--window-size=1366,900")
    if _send_diagnostics_enabled():
        options.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
    return options


def _hidden_subprocess_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _build_chromedriver_service(driver_path: str) -> Service:
    service = Service(driver_path, log_output=subprocess.DEVNULL)
    creation_flags = _hidden_subprocess_creation_flags()
    if creation_flags:
        service.creation_flags = creation_flags
    return service


def _installed_chrome_major() -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None

    registry_locations = (
        (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Google\Chrome\BLBeacon"),
    )
    for root_key, sub_key in registry_locations:
        try:
            with winreg.OpenKey(root_key, sub_key) as key:
                output, _ = winreg.QueryValueEx(key, "version")
        except Exception:
            continue
        match = re.match(r"^(\d+)\.", str(output))
        if match:
            return match.group(1)
    return None


def _cached_chromedriver_path() -> str | None:
    cache_root = Path.home() / ".wdm" / "drivers" / "chromedriver"
    if not cache_root.exists():
        return None
    drivers = [candidate for candidate in cache_root.rglob("chromedriver.exe") if candidate.is_file()]
    if not drivers:
        return None
    chrome_major = _installed_chrome_major()

    def version_key(path: Path) -> tuple[int, int, int, int, float]:
        version_text = next((part for part in path.parts if re.match(r"^\d+(?:\.\d+)+$", part)), "")
        numbers = [int(value) for value in re.findall(r"\d+", version_text)[:4]]
        numbers.extend([0] * (4 - len(numbers)))
        return (numbers[0], numbers[1], numbers[2], numbers[3], path.stat().st_mtime)

    if chrome_major:
        matching = [path for path in drivers if any(part.startswith(f"{chrome_major}.") for part in path.parts)]
        if matching:
            return str(sorted(matching, key=version_key, reverse=True)[0])
    return str(sorted(drivers, key=version_key, reverse=True)[0])


def _resolve_chromedriver_path(installed_path: str) -> str:
    path = Path(installed_path)
    if path.name.lower() == "chromedriver.exe" and path.is_file():
        return str(path)

    search_roots = []
    if path.is_dir():
        search_roots.append(path)
    else:
        search_roots.extend(parent for parent in (path.parent, path.parent.parent) if parent and parent.exists())

    for root in search_roots:
        for candidate in root.rglob("chromedriver.exe"):
            if candidate.is_file():
                return str(candidate)

    raise FileNotFoundError(f"chromedriver.exe nao encontrado a partir de: {installed_path}")


def _is_profile_lock_error(error: Exception) -> bool:
    text = str(error).lower()
    return (
        "user data directory is already in use" in text
        or "devtoolsactiveport file doesn't exist" in text
        or "chrome failed to start: crashed" in text
    )


def _terminate_chrome_profile_processes(profile_dir: Path) -> None:
    if os.name != "nt":
        return
    resolved = str(profile_dir.resolve())
    script = (
        "$profile = $env:BOT_CHROME_PROFILE_TO_KILL; "
        "Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe' or name = 'chromedriver.exe'\" | "
        "Where-Object { $_.CommandLine -like \"*$profile*\" } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        env = dict(os.environ)
        env["BOT_CHROME_PROFILE_TO_KILL"] = resolved
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            env=env,
            creationflags=_hidden_subprocess_creation_flags(),
        )
        sleep(5)
    except Exception:
        pass


def _dismiss_cookie_banner(driver: Any) -> None:
    xpaths = (
        "//button[contains(., 'Aceitar')]",
        "//button[contains(., 'Accept')]",
        "//button[contains(., 'Concordo')]",
        "//button[contains(., 'OK')]",
    )
    for xpath in xpaths:
        try:
            for button in driver.find_elements(By.XPATH, xpath):
                driver.execute_script("arguments[0].click();", button)
                sleep(CLICK_DELAY_SECONDS)
                return
        except Exception:
            continue


def _try_click_login_tab(driver: Any, wait: WebDriverWait) -> bool:
    candidates = (
        (By.XPATH, '//*[@id="loginRegisterTabs"]/ul/li[1]'),
        (By.CSS_SELECTOR, "#loginRegisterTabs li:first-child"),
        (By.XPATH, "//*[@id='loginRegisterTabs']//li[contains(., 'Login')]"),
        (By.XPATH, "//*[@id='loginRegisterTabs']//li[contains(., 'Entrar')]"),
    )

    def try_context() -> bool:
        for by, selector in candidates:
            try:
                element = wait.until(EC.element_to_be_clickable((by, selector)))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                driver.execute_script("arguments[0].click();", element)
                sleep(CLICK_DELAY_SECONDS)
                return True
            except (TimeoutException, Exception):
                continue
        return False

    if try_context():
        return True
    for frame in driver.find_elements(By.TAG_NAME, "iframe"):
        try:
            driver.switch_to.frame(frame)
            if try_context():
                driver.switch_to.default_content()
                return True
        except Exception:
            pass
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    return False


def _find_login_inputs(driver: Any, wait_seconds: float = 12) -> bool:
    wait = WebDriverWait(driver, wait_seconds)
    selectors = (
        (By.NAME, "email"),
        (By.CSS_SELECTOR, "input[type='email']"),
        (By.ID, "email"),
    )

    def try_context() -> bool:
        for by, selector in selectors:
            try:
                wait.until(EC.presence_of_element_located((by, selector)))
                return True
            except TimeoutException:
                continue
        return False

    if try_context():
        return True
    for frame in driver.find_elements(By.TAG_NAME, "iframe"):
        try:
            driver.switch_to.frame(frame)
            if try_context():
                driver.switch_to.default_content()
                return True
        except Exception:
            pass
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    return False


def _visible_submit_button(driver: Any, wait: WebDriverWait) -> Any | None:
    selectors = (
        (By.XPATH, "//button[@type='submit' and normalize-space()='LOGIN']"),
        (By.XPATH, "//button[@type='submit' and contains(., 'Login')]"),
        (By.XPATH, "//button[@type='submit' and contains(., 'Entrar')]"),
        (By.CSS_SELECTOR, "button[type='submit']"),
    )
    for by, selector in selectors:
        try:
            elements = driver.find_elements(by, selector)
            for element in elements:
                if element.is_displayed() and element.is_enabled():
                    return element
            element = wait.until(EC.element_to_be_clickable((by, selector)))
            if element:
                return element
        except Exception:
            continue
    return None


def _is_driver_connection_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "invalid session id",
            "connection refused",
            "failed to establish a new connection",
            "max retries exceeded",
            "chrome not reachable",
            "disconnected",
        )
    )


def _compact_page_text(text: str, limit: int = 220) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


class BotDriver:
    """Selenium driver that navigates Ikariam and sends one message per user."""

    def __init__(
        self,
        *,
        account: Accounts,
        headless: bool = False,
        cache: bool = False,
        timeWait: float = 5.0,
        postSendWait: float = 1.0,
        logs: LogSink | None = None,
        pause_event: threading.Event | None = None,
        stop_event: threading.Event | None = None,
        dry_run: bool = False,
        driver: Any | None = None,
    ) -> None:
        self.account = account
        self.headless = headless
        self.cache = cache
        self.timeWait = max(float(timeWait), 0.1)
        self.postSendWait = max(float(postSendWait), 0.1)
        self.logs = logs
        self.annunce = bool(logs)
        self.dry_run = bool(dry_run)
        self.serverGlobal: Servers | None = None
        self.main_tab_handle: str | None = None
        self.message_tab_handle: str | None = None
        self.pause_event = pause_event or threading.Event()
        self.stop_event = stop_event or threading.Event()
        self.messageSendCount = 0
        self.totalSentSession = 0
        self._server_cycle_send_count = 0
        self._active_highscore_offset: str | None = None
        self._last_resolving_log = 0.0
        self._last_send_attempt_at = 0.0
        self._stale_recovery_count = 0
        self.last_activity_at = time.time()
        self._last_accounts_page_only_inactive = False
        self._persistent_profile = bool(cache)
        self._temp_profile_dir = None if self._persistent_profile else _make_temp_profile_dir()
        self._profile_dir = _make_cached_profile_dir(account) if self._persistent_profile else self._temp_profile_dir
        self.running = False
        self.activation_cancelled = False
        self.listener = None
        self.driver = driver or self._create_driver()

    def _create_driver(self) -> Any:
        if self._profile_dir is None:
            self._profile_dir = _make_temp_profile_dir()
            self._temp_profile_dir = self._profile_dir
        options = _build_chrome_options(self.headless, self._profile_dir)
        driver_path = _cached_chromedriver_path() or _resolve_chromedriver_path(ChromeDriverManager().install())
        try:
            driver = webdriver.Chrome(service=_build_chromedriver_service(driver_path), options=options)
        except SessionNotCreatedException as error:
            if not self._persistent_profile or not _is_profile_lock_error(error):
                raise
            _terminate_chrome_profile_processes(self._profile_dir)
            driver = webdriver.Chrome(service=_build_chromedriver_service(driver_path), options=options)
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(20)
        return driver

    def _safe_get(self, url: str, timeout: float = 60) -> None:
        self._check_stop()
        target_netloc = urlparse(url).netloc
        try:
            previous_handles = set(self.driver.window_handles)
            result = self.driver.execute_cdp_cmd("Target.createTarget", {"url": url})
            target_id = result.get("targetId")
            self._wait_for(
                lambda driver: len(set(driver.window_handles) - previous_handles) > 0
                or (target_id and target_id in driver.window_handles),
                timeout=min(timeout, 10),
                poll=0.5,
            )
            new_handles = [handle for handle in self.driver.window_handles if handle not in previous_handles]
            handle = target_id if target_id in self.driver.window_handles else (new_handles[-1] if new_handles else self.driver.window_handles[-1])
            self.driver.switch_to.window(handle)
        except Exception:
            self.driver.switch_to.new_window("tab")
            self.driver.execute_cdp_cmd("Page.navigate", {"url": url})

        def navigation_started(driver: Any) -> bool:
            try:
                current = driver.execute_script("return location.href") or ""
            except Exception:
                current = getattr(driver, "current_url", "") or ""
            return current != "about:blank" and urlparse(current).netloc == target_netloc

        self._wait_for(navigation_started, timeout=timeout, poll=0.5)
        try:
            self._wait_for(
                lambda driver: driver.execute_script("return document.readyState")
                in {"interactive", "complete"},
                timeout=10,
                poll=0.5,
            )
        except TimeoutException:
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                pass

    def _is_stopped(self) -> bool:
        return self.stop_event.is_set()

    def _touch_activity(self) -> None:
        self.last_activity_at = time.time()

    def _check_stop(self) -> None:
        if self._is_stopped():
            raise BotStopped()

    def _wait_if_paused(self) -> None:
        while self.pause_event.is_set():
            if self._is_stopped():
                raise BotStopped()
            sleep(0.2)

    def _sleep(self, seconds: float) -> None:
        deadline = time.time() + max(seconds, 0)
        while time.time() < deadline:
            self._wait_if_paused()
            if self._is_stopped():
                raise BotStopped()
            sleep(min(0.2, deadline - time.time()))

    def _click(self, element: Any) -> None:
        self._check_stop()
        self._touch_activity()
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
            self.driver.execute_script("arguments[0].click();", element)
        except Exception:
            element.click()
        self._sleep(CLICK_DELAY_SECONDS)

    def _close_extra_windows(self, keep_handles: set[str | None]) -> None:
        keep = {handle for handle in keep_handles if handle}
        for handle in list(self.driver.window_handles):
            if handle in keep:
                continue
            try:
                self.driver.switch_to.window(handle)
                self._sleep(CLOSE_TAB_DELAY_SECONDS)
                self.driver.close()
            except Exception:
                continue
        if self.main_tab_handle and self.main_tab_handle in self.driver.window_handles:
            self.driver.switch_to.window(self.main_tab_handle)

    @staticmethod
    def _parse_int_from_text(text: str) -> int | None:
        numbers = re.findall(r"\d+", re.sub(r"[.,]", "", text))
        return int(numbers[-1]) if numbers else None

    def _coerce_total_users(self) -> int | None:
        if not self.serverGlobal:
            return None
        value = self.serverGlobal.users
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            digits = re.sub(r"\D+", "", value)
            return int(digits) if digits else None
        return None

    def _compute_max_send(self) -> int | None:
        total = self._coerce_total_users()
        if total is None or total <= 0:
            return None
        return max(total - 1, 0)

    @staticmethod
    def _highscore_total_from_options(options: list[str]) -> int | None:
        max_end = 0
        for option in options:
            match = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", option or "")
            if match:
                max_end = max(max_end, int(match.group(2)))
        return max_end or None

    @staticmethod
    def _normalize_lobby_text(value: str | None) -> str:
        normalized = unicodedata.normalize("NFKD", (value or "").strip().casefold())
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        return re.sub(r"\s+", " ", normalized)

    @staticmethod
    def _server_display_name(server_name: str | None, flag_name: str | None = None) -> str:
        server = (server_name or "").strip() or "Servidor desconhecido"
        flag = _normalize_server_flag(flag_name)
        return f"{flag} / {server}" if flag else server

    @classmethod
    def _server_identity_key(cls, server_name: str | None, flag_name: str | None = None) -> str:
        return cls._server_display_name(server_name, flag_name).casefold()

    @staticmethod
    def _extract_server_flag(card: Any) -> str:
        class_name = ""
        for selector in ("span.flag", "span[class*='flag-']"):
            try:
                flag_el = card.find_element(By.CSS_SELECTOR, selector)
                class_name = flag_el.get_attribute("class") or ""
                if class_name:
                    break
            except Exception:
                continue
        for token in class_name.split():
            if "-" not in token:
                continue
            suffix = token.rsplit("-", 1)[-1]
            if 2 <= len(suffix) <= 3 and suffix.isalpha():
                return _normalize_server_flag(suffix)
        return ""

    @classmethod
    def _is_ignored_server_card(cls, server_name: str | None, card_text: str | None = None) -> bool:
        server_key = cls._normalize_lobby_text(server_name)
        text_key = cls._normalize_lobby_text(card_text)
        if any(marker in server_key for marker in IGNORED_SERVER_MARKERS):
            return True
        if any(marker in text_key for marker in IGNORED_SERVER_MARKERS):
            return True
        return "2038" in text_key and ("razao:" in text_key or "razao" in text_key or "reason:" in text_key)

    @classmethod
    def _is_usable_server_button_text(cls, value: str | None) -> bool:
        text_key = cls._normalize_lobby_text(value)
        if not text_key:
            return True
        if re.search(r"\bcome.?ar\b", text_key):
            return True
        return any(marker in text_key for marker in SERVER_START_BUTTON_MARKERS)

    @classmethod
    def _is_owned_server_button_text(cls, value: str | None) -> bool:
        text_key = cls._normalize_lobby_text(value)
        if not text_key:
            return False
        return any(marker in text_key for marker in OWNED_SERVER_BUTTON_MARKERS)

    @staticmethod
    def _is_primary_lobby_button(element: Any) -> bool:
        try:
            class_name = element.get_attribute("class") or ""
        except Exception:
            class_name = ""
        class_key = re.sub(r"\s+", " ", class_name.strip().casefold())
        return "btn btn-primary" in class_key or "button-primary" in class_key

    @classmethod
    def _is_enabled_lobby_button(cls, element: Any) -> bool:
        try:
            is_enabled = getattr(element, "is_enabled", lambda: True)
            if not is_enabled():
                return False
        except Exception:
            pass
        for attr in ("disabled", "aria-disabled"):
            try:
                value = element.get_attribute(attr)
            except Exception:
                value = None
            if cls._normalize_lobby_text(value) in {"true", "disabled", "1"}:
                return False
        try:
            class_name = element.get_attribute("class") or ""
        except Exception:
            class_name = ""
        class_key = cls._normalize_lobby_text(class_name)
        return "disabled" not in class_key and "transparent" not in class_key and "transparente" not in class_key

    @staticmethod
    def _normalize_highscore_options(options: list[str]) -> list[str]:
        ranges: list[tuple[int, str]] = []
        fallback: list[str] = []
        seen: set[str] = set()
        for raw_text in options:
            text = re.sub(r"\s+", " ", (raw_text or "").strip())
            if not text:
                continue
            key = text.casefold()
            if key in seen or "own position" in key or "propria posicao" in key or "própria posição" in key:
                continue
            seen.add(key)
            match = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", text)
            if match:
                ranges.append((int(match.group(1)), text))
                continue
            fallback.append(text)
        if ranges:
            ranges.sort(key=lambda item: item[0])
            return [text for _, text in ranges]
        return fallback

    @staticmethod
    def _is_success_text(text: str | None) -> bool:
        if not text:
            return False
        lower = text.lower()
        return any(
            phrase in lower
            for phrase in (
                "your order has been carried out",
                "sua ordem foi executada",
                "ordem foi executada",
                "mensagem enviada",
                "emriniz yerine getirildi",
                "emrin yerine getirildi",
                "mesaj gonderildi",
                "mesaj gönderildi",
            )
        )

    @staticmethod
    def _is_explicit_empty_outbox_text(text: str | None) -> bool:
        if not text:
            return False
        normalized = unicodedata.normalize("NFKD", text.casefold())
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        return any(
            marker in normalized
            for marker in (
                "caixa de saida vazia",
                "nenhuma mensagem enviada",
                "nenhuma mensagem na caixa de saida",
                "sem mensagens enviadas",
                "outbox vazia",
                "empty outbox",
                "no sent messages",
                "no messages sent",
                "no hay mensajes enviados",
                "sin mensajes enviados",
                "giden kutusu bos",
                "gonderilmis mesaj yok",
            )
        )

    @staticmethod
    def _is_outbox_text(text: str | None, username: str | None = None) -> bool:
        if not text:
            return False
        normalized = unicodedata.normalize("NFKD", text.casefold())
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        markers = (
            "giden kutusu",
            "caixa de saida",
            "caixa de saida",
            "saida",
            "salida",
            "outbox",
        )
        if not any(marker in normalized for marker in markers):
            return False
        grid_markers = (
            "destinatario",
            "destinatari",
            "alici",
            "asunto",
            "assunto",
            "subject",
            "fecha",
            "data",
            "date",
        )
        has_grid = any(marker in normalized for marker in grid_markers)
        if not has_grid and not BotDriver._is_explicit_empty_outbox_text(text):
            return False
        if not username:
            return True
        user_key = unicodedata.normalize("NFKD", username.casefold())
        user_key = "".join(char for char in user_key if not unicodedata.combining(char))
        return user_key in normalized

    @staticmethod
    def _clean_outbox_recipient(value: str | None) -> str:
        text = re.sub(r"\s+", " ", (value or "").strip())
        text = re.sub(r"^[^\wÀ-ÿ]+", "", text, flags=re.UNICODE).strip()
        return text[:80]

    @classmethod
    def _extract_outbox_recipients_from_rows(cls, rows: list[Any]) -> list[str]:
        recipients: list[str] = []
        seen: set[str] = set()
        for row in rows:
            try:
                cells = row.find_elements(By.XPATH, ".//td")
            except Exception:
                continue
            if len(cells) < 2:
                continue
            cell = cells[1]
            try:
                links = cell.find_elements(By.XPATH, ".//a")
            except Exception:
                links = []
            raw_name = ""
            for link in links:
                raw_name = link.get_attribute("title") or getattr(link, "text", "") or ""
                if raw_name.strip():
                    break
            if not raw_name:
                raw_name = getattr(cell, "text", "") or ""
            name = cls._clean_outbox_recipient(raw_name)
            key = unicodedata.normalize("NFKD", name.casefold())
            key = "".join(char for char in key if not unicodedata.combining(char))
            if not key or key in seen:
                continue
            seen.add(key)
            recipients.append(name)
        return recipients

    def _extract_current_outbox_recipients(self, attempts: int = 3) -> list[str]:
        for attempt in range(attempts):
            recipients: list[str] = []
            seen: set[str] = set()
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, "span.avatarName")
            except Exception:
                elements = []
            for element in elements:
                try:
                    name = self._clean_outbox_recipient(getattr(element, "text", "") or "")
                except StaleElementReferenceException:
                    continue
                key = self._outbox_recipient_key(name)
                if key and key not in seen:
                    seen.add(key)
                    recipients.append(name)
            if recipients or attempt == attempts - 1:
                return recipients
            self._sleep(MESSAGE_TEXT_SETTLE_SECONDS)
        return []

    def _count_current_outbox_messages(self, attempts: int = 3) -> int:
        for attempt in range(attempts):
            try:
                entries = self.driver.find_elements(By.CSS_SELECTOR, "span.avatarName")
            except Exception:
                entries = []
            stable_entries = []
            for entry in entries:
                try:
                    getattr(entry, "text", "")
                    stable_entries.append(entry)
                except StaleElementReferenceException:
                    continue
            if stable_entries or attempt == attempts - 1:
                if stable_entries:
                    return len(stable_entries)
                break
            self._sleep(MESSAGE_TEXT_SETTLE_SECONDS)
        for selector in ("table tbody tr", "tbody tr"):
            try:
                rows = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            visible_rows = []
            for row in rows:
                try:
                    if getattr(row, "is_displayed", lambda: True)():
                        visible_rows.append(row)
                except Exception:
                    continue
            if visible_rows:
                return len(visible_rows)
        return 0

    def _find_outbox_link(self) -> Any | None:
        candidates = (
            (By.CSS_SELECTOR, "a[href*='tab=outbox']"),
            (By.CSS_SELECTOR, "a[href*='view=messages'][href*='outbox']"),
            (By.XPATH, "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'outbox')]"),
            (By.XPATH, "//a[contains(normalize-space(.), 'Giden') or contains(normalize-space(.), 'Caixa de saída') or contains(normalize-space(.), 'Caixa de saida')]"),
        )
        for by, selector in candidates:
            try:
                return self._find_or_wait(by, selector, timeout=2, clickable=True)
            except Exception:
                continue
        return None

    def _open_outbox_page(self) -> bool:
        link = self._find_outbox_link()
        if link is not None:
            self._click(link)
            self._sleep(MESSAGE_TEXT_SETTLE_SECONDS)
            return True
        try:
            current_url = self.driver.current_url or ""
            parsed = urlparse(current_url)
            if not parsed.scheme or not parsed.netloc:
                return False
            diplomacy_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?view=diplomacyAdvisor"
            self.driver.get(diplomacy_url)
            self._sleep(SHORT_WAIT_SECONDS)
            link = self._find_outbox_link()
            if link is None:
                return False
            self._click(link)
            self._sleep(MESSAGE_TEXT_SETTLE_SECONDS)
            return True
        except Exception:
            return False

    def _find_outbox_link_robust(self) -> Any | None:
        selectors = (
            "a[href*='diplomacyAdvisorOutBox']",
            "a[href*='diplomacyAdvisorOutbox']",
            "a[href*='tab=outbox']",
            "a[href*='view=outbox']",
            "a[href*='view=messages'][href*='outbox']",
            "a[href*='diplomacyAdvisor'][href*='outbox']",
        )
        markers = (
            "outbox",
            "caixa de saida",
            "saida",
            "salida",
            "mensagens enviadas",
            "sent messages",
            "giden kutusu",
            "gonderilen mesajlar",
        )
        for _ in range(3):
            for selector in selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    elements = []
                for element in elements:
                    try:
                        if getattr(element, "is_displayed", lambda: True)():
                            return element
                    except Exception:
                        continue
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, "a, button, [role='tab']")
            except Exception:
                elements = []
            for element in elements:
                try:
                    searchable = " ".join(
                        (
                            getattr(element, "text", "") or "",
                            element.get_attribute("title") or "",
                            element.get_attribute("aria-label") or "",
                            element.get_attribute("data-tooltip-content") or "",
                            element.get_attribute("href") or "",
                        )
                    )
                except Exception:
                    continue
                normalized = self._normalize_lobby_text(searchable)
                if any(marker in normalized for marker in markers):
                    return element
            self._sleep(MESSAGE_TEXT_SETTLE_SECONDS)
        return None

    def _current_page_is_outbox(self) -> bool:
        try:
            return self._is_outbox_text(self.driver.find_element(By.TAG_NAME, "body").text)
        except Exception:
            return False

    def _wait_for_outbox_context(self, timeout: float = 2.0) -> bool:
        try:
            return bool(self._wait_for(lambda driver: self._current_page_is_outbox(), timeout=timeout, poll=0.2))
        except TimeoutException:
            return False

    @staticmethod
    def _safe_outbox_route(value: str) -> str:
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        safe_parts = [f"{key}={query[key][0]}" for key in ("view", "tab") if query.get(key)]
        return parsed.path + ("?" + "&".join(safe_parts) if safe_parts else "")

    def _open_outbox_page_robust(self) -> bool:
        self._last_outbox_open_attempts = []
        if self._current_page_is_outbox():
            self._last_outbox_open_attempts.append("pagina-atual:ok")
            return True
        try:
            current_url = self.driver.current_url or ""
            parsed = urlparse(current_url)
            if not parsed.scheme or not parsed.netloc:
                self._last_outbox_open_attempts.append("url-atual:invalida")
                return False
            diplomacy_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?view=diplomacyAdvisor"
            self.driver.get(diplomacy_url)
            self._sleep(SHORT_WAIT_SECONDS)
            self._last_outbox_open_attempts.append(self._safe_outbox_route(diplomacy_url))
            if self._current_page_is_outbox():
                return True
            link = self._find_outbox_link_robust()
            if link is not None:
                try:
                    href = link.get_attribute("href") or ""
                except Exception:
                    href = ""
                if href:
                    self.driver.get(urljoin(diplomacy_url, href))
                else:
                    self._click(link)
                self._sleep(MESSAGE_TEXT_SETTLE_SECONDS)
                self._last_outbox_open_attempts.append(
                    "link-diplomacia:" + (self._safe_outbox_route(href) if href else "click")
                )
                if self._wait_for_outbox_context():
                    return True
            for candidate_url in (
                f"{parsed.scheme}://{parsed.netloc}{parsed.path}?view=diplomacyAdvisorOutBox",
                f"{parsed.scheme}://{parsed.netloc}{parsed.path}?view=diplomacyAdvisorOutbox",
                f"{parsed.scheme}://{parsed.netloc}{parsed.path}?view=diplomacyAdvisor&tab=outbox",
                f"{parsed.scheme}://{parsed.netloc}{parsed.path}?view=messages&tab=outbox",
                f"{parsed.scheme}://{parsed.netloc}{parsed.path}?view=outbox",
            ):
                self.driver.get(candidate_url)
                self._sleep(MESSAGE_TEXT_SETTLE_SECONDS)
                self._last_outbox_open_attempts.append(self._safe_outbox_route(candidate_url))
                if self._wait_for_outbox_context():
                    return True
            return False
        except Exception as error:
            self._last_outbox_open_attempts.append(f"erro:{type(error).__name__}")
            return False

    @staticmethod
    def _is_disabled_pagination_element(element: Any) -> bool:
        try:
            class_name = element.get_attribute("class") or ""
        except Exception:
            class_name = ""
        class_key = re.sub(r"\s+", " ", class_name.strip().casefold())
        if "disabled" in class_key or "inactive" in class_key:
            return True
        try:
            aria_disabled = element.get_attribute("aria-disabled") or ""
        except Exception:
            aria_disabled = ""
        return aria_disabled.strip().casefold() in {"true", "1", "disabled"}

    def _click_next_outbox_page(self) -> bool:
        next_xpaths = (
            "//a[contains(@href, 'diplomacyAdvisorOutBox') and contains(@href, 'start=')]",
            "//a[normalize-space()='>' or normalize-space()='»']",
            "//button[normalize-space()='>' or normalize-space()='»']",
            "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
            "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÃÇÉÍÓÕÚ', 'abcdefghijklmnopqrstuvwxyzáãçéíóõú'), 'proxima')]",
            "//a[contains(normalize-space(.), 'próximo') or contains(normalize-space(.), 'próximos') or contains(normalize-space(.), 'Proximo') or contains(normalize-space(.), 'Proximos')]",
            "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sonraki')]",
            "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'ileri')]",
        )
        for xpath in next_xpaths:
            try:
                elements = self.driver.find_elements(By.XPATH, xpath)
            except Exception:
                continue
            for element in elements:
                try:
                    is_displayed = getattr(element, "is_displayed", lambda: True)
                    if not is_displayed() or self._is_disabled_pagination_element(element):
                        continue
                    before_text = self.driver.find_element(By.TAG_NAME, "body").text
                    self._click(element)
                    self._sleep(SHORT_WAIT_SECONDS)
                    after_text = self.driver.find_element(By.TAG_NAME, "body").text
                    return after_text != before_text
                except Exception:
                    continue
        return False

    @staticmethod
    def _parse_outbox_count(value: str | None) -> int | None:
        """Parse an integer count with localized thousands separators."""
        normalized = unicodedata.normalize("NFKC", value or "")
        normalized = normalized.replace("\u00a0", " ").replace("\u202f", " ")
        digits = re.sub(r"[^0-9]", "", normalized)
        return int(digits) if digits else None

    def _extract_outbox_total(self) -> int | None:
        """Read only the localized Outbox total shown by the game navigation."""
        selectors = (
            "a[href*='diplomacyAdvisorOutBox']",
            "a[href*='diplomacyAdvisorOutbox']",
            "a[href*='tab=outbox']",
            "a[href*='view=outbox']",
        )
        totals: list[int] = []
        for selector in selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                elements = []
            for element in elements:
                try:
                    searchable = " ".join(
                        (
                            getattr(element, "text", "") or "",
                            element.get_attribute("title") or "",
                            element.get_attribute("aria-label") or "",
                        )
                    )
                except Exception:
                    continue
                match = re.search(r"\(\s*([0-9][0-9\s.,]*)\s*\)", searchable)
                if match:
                    parsed_total = self._parse_outbox_count(match.group(1))
                    if parsed_total is not None:
                        totals.append(parsed_total)
        if totals:
            return max(totals)
        try:
            body_text = self.driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            body_text = ""
        normalized = self._normalize_lobby_text(body_text)
        match = re.search(
            r"(?:caixa de saida|mensagens enviadas|giden kutusu|gonderilen mesajlar|saida|salida|outbox)\s*\(\s*([0-9][0-9\s.,]*)\s*\)",
            normalized,
        )
        return self._parse_outbox_count(match.group(1)) if match else None

    def _sync_outbox_sent_users(
        self,
        textLogs: LogSink | None = None,
        *,
        full_history: bool = True,
        update_total: bool = True,
        log_result: bool = True,
    ) -> int | None:
        if not self.serverGlobal:
            return 0
        self._check_stop()
        self._last_outbox_recipients = set()
        self._last_outbox_snapshot_complete = False
        try:
            current_handle = self.driver.current_window_handle
        except Exception:
            if textLogs:
                textLogs.addLogs("Nao foi possivel sincronizar Outbox: sessao do navegador indisponivel.", "warn")
            return None
        try:
            origin_url = self.driver.current_url or ""
        except Exception:
            origin_url = ""
        try:
            if not self._open_outbox_page_robust():
                if textLogs:
                    textLogs.addLogs("Nao foi possivel sincronizar Outbox: link/rota nao encontrada.", "warn")
                _audit(
                    textLogs,
                    "outbox_open_failed",
                    attempts=list(getattr(self, "_last_outbox_open_attempts", [])),
                    current_route=self._safe_outbox_route(getattr(self.driver, "current_url", "") or ""),
                )
                return None
            try:
                page_text = self.driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                page_text = ""
            if not self._is_outbox_text(page_text):
                if textLogs:
                    textLogs.addLogs(
                        "Nao foi possivel sincronizar Outbox: a rota abriu sem a grade reconhecida. Progresso local preservado.",
                        "warn",
                    )
                _audit(
                    textLogs,
                    "outbox_content_unrecognized",
                    attempts=list(getattr(self, "_last_outbox_open_attempts", [])),
                    current_route=self._safe_outbox_route(getattr(self.driver, "current_url", "") or ""),
                )
                return None
            outbox_total = 0
            count_source = "visible_pages"
            page_limit = 30 if full_history else 1
            snapshot_complete = False
            header_total = self._extract_outbox_total() if full_history else None
            explicit_empty = self._is_explicit_empty_outbox_text(page_text)
            visible_rows_seen = 0
            if header_total is not None and header_total > 0:
                outbox_total = header_total
                count_source = "header"
                snapshot_complete = True
            elif header_total == 0 and explicit_empty:
                outbox_total = 0
                count_source = "explicit_empty"
                snapshot_complete = True
            else:
                for _ in range(page_limit):
                    current_page_count = self._count_current_outbox_messages()
                    visible_rows_seen += current_page_count
                    outbox_total += current_page_count
                    if not full_history:
                        break
                    if not self._click_next_outbox_page():
                        snapshot_complete = True
                        break
                if full_history and page_limit == 0:
                    snapshot_complete = True
                if full_history and snapshot_complete and outbox_total == 0 and not explicit_empty:
                    _audit(
                        textLogs,
                        "outbox_sync_inconclusive",
                        reason="pagina_reconhecida_sem_linhas_ou_estado_vazio_explicito",
                        header_total=header_total,
                        visible_rows=visible_rows_seen,
                        explicit_empty=explicit_empty,
                        attempts=list(getattr(self, "_last_outbox_open_attempts", [])),
                        current_route=self._safe_outbox_route(getattr(self.driver, "current_url", "") or ""),
                    )
                    if textLogs and log_result:
                        textLogs.addLogs(
                            "Outbox aberta, mas o total nao foi comprovado. Progresso local preservado.",
                            "warn",
                        )
                    return None
            if full_history and header_total == 0 and visible_rows_seen > 0:
                count_source = "visible_pages_after_zero_header"
            self._last_outbox_snapshot_complete = snapshot_complete
            if update_total:
                database_sent_total = UsersSend.count_for_server(server_id=self.serverGlobal.id, status="sent")
                resolved_total = max(int(self.serverGlobal.messageSend or 0), database_sent_total, outbox_total)
                if int(self.serverGlobal.messageSend or 0) != resolved_total:
                    self.serverGlobal.messageSend = resolved_total
                    self.serverGlobal.save()
            if textLogs and log_result:
                textLogs.addLogs(f"Outbox sincronizada: {outbox_total} mensagens encontradas.", "info")
            _audit(
                textLogs,
                "outbox_sync_succeeded",
                attempts=list(getattr(self, "_last_outbox_open_attempts", [])),
                current_route=self._safe_outbox_route(getattr(self.driver, "current_url", "") or ""),
                messages_counted=outbox_total,
                count_source=count_source,
                snapshot_complete=snapshot_complete,
                header_total=header_total,
                visible_rows=visible_rows_seen,
                explicit_empty=explicit_empty,
                server_total=int(self.serverGlobal.messageSend or 0),
            )
            if update_total:
                self._log_progress(textLogs)
            return outbox_total
        finally:
            try:
                if current_handle in self.driver.window_handles:
                    self.driver.switch_to.window(current_handle)
            except Exception:
                pass
            try:
                current_url = self.driver.current_url or ""
                if origin_url and current_url != origin_url:
                    self.driver.get(origin_url)
            except Exception:
                pass

    @staticmethod
    def _outbox_recipient_key(username: str) -> str:
        normalized = unicodedata.normalize("NFKD", username.casefold())
        return "".join(char for char in normalized if not unicodedata.combining(char))

    def _log_progress(self, textLogs: LogSink | None) -> None:
        if not self.serverGlobal or not textLogs or not hasattr(textLogs, "set_progress"):
            return
        total = self._coerce_total_users()
        if total is not None and total <= 0:
            total = None
        sent = int(self.serverGlobal.messageSend or 0)
        remaining = max(total - sent, 0) if total else None
        percent = round((sent / total) * 100, 1) if total else None
        textLogs.set_progress(
            {
                "server": self.serverGlobal.display_name,
                "sent": sent,
                "total": total,
                "remaining": remaining,
                "percent": percent,
            }
        )

    def _ensure_server_global(
        self,
        name_server: str,
        flag_name: str | None = None,
        users_count: int | None = None,
        textLogs: LogSink | None = None,
    ) -> None:
        server = Servers.get_or_create(
            name_server.strip() or "Servidor desconhecido",
            flag=flag_name or "",
            users=users_count if users_count and users_count > 0 else 0,
        )
        if users_count is not None and users_count > 0 and server.users != users_count:
            server.users = users_count
            server.save()
        self.serverGlobal = server
        if textLogs:
            textLogs.addLogs(f"Servidor selecionado: {server.display_name}")
        self._log_progress(textLogs)

    def _update_server_users_count(self, users_count: int | None, textLogs: LogSink | None = None) -> None:
        if not self.serverGlobal or not users_count or users_count <= 0:
            return
        current_total = self._coerce_total_users()
        if current_total and current_total > 0 and users_count < current_total:
            self._log_progress(textLogs)
            return
        if int(self.serverGlobal.users or 0) == int(users_count):
            self._log_progress(textLogs)
            return
        self.serverGlobal.users = int(users_count)
        self.serverGlobal.save()
        self._log_progress(textLogs)

    def _log_resolving(self, textLogs: LogSink | None) -> None:
        if not textLogs:
            return
        now = time.time()
        if now - self._last_resolving_log > 5:
            textLogs.addLogs("Resolvendo proximo usuario...", "info")
            self._last_resolving_log = now

    def _wait_for(self, condition: Any, timeout: float = 10, poll: float = 0.3) -> Any:
        end = time.time() + timeout
        last_error: Exception | None = None
        while time.time() < end:
            self._wait_if_paused()
            if self._is_stopped():
                raise BotStopped()
            self._touch_activity()
            try:
                value = condition(self.driver)
                if value:
                    return value
            except Exception as error:
                last_error = error
            sleep(poll)
        raise TimeoutException(str(last_error) if last_error else "timeout waiting for condition")

    def _find_or_wait(
        self,
        by: str,
        selector: str,
        timeout: float = 10,
        clickable: bool = False,
    ) -> Any:
        self._check_stop()
        self._touch_activity()
        try:
            elements = self.driver.find_elements(by, selector)
            if elements:
                if not clickable:
                    return elements[0]
                for element in elements:
                    is_displayed = getattr(element, "is_displayed", lambda: True)
                    is_enabled = getattr(element, "is_enabled", lambda: True)
                    if is_displayed() and is_enabled():
                        return element
        except Exception:
            pass
        condition = (
            EC.element_to_be_clickable((by, selector))
            if clickable
            else EC.presence_of_element_located((by, selector))
        )
        return self._wait_for(condition, timeout=timeout)

    def _find_all_or_wait(self, by: str, selector: str, timeout: float = 10) -> list[Any]:
        self._check_stop()
        self._touch_activity()
        try:
            elements = self.driver.find_elements(by, selector)
            if elements:
                return list(elements)
        except Exception:
            pass
        return list(self._wait_for(EC.presence_of_all_elements_located((by, selector)), timeout=timeout))

    def _fetch_users_from_hub(self, server_handle: str) -> int | None:
        driver = self.driver
        hub_handle: str | None = None
        try:
            self._check_stop()
            previous_handles = set(driver.window_handles)
            driver.execute_script("window.open('about:blank', '_blank');")
            self._wait_for(lambda item: len(item.window_handles) > len(previous_handles), timeout=5)
            handles = list(driver.window_handles)
            hub_handle = next(
                (handle for handle in handles if handle not in previous_handles and handle != server_handle),
                handles[-1] if handles else None,
            )
            if not hub_handle:
                return None
            driver.switch_to.window(hub_handle)
            self._safe_get("https://lobby.ikariam.gameforge.com/pt_BR/hub", timeout=15)
            self._sleep(3)
            button_hub = self._find_or_wait(By.CLASS_NAME, "serverDetails", timeout=15, clickable=True)
            return self._parse_int_from_text(getattr(button_hub, "text", "") or "")
        except Exception:
            return None
        finally:
            try:
                handles = set(driver.window_handles)
                if hub_handle and hub_handle in handles and hub_handle != server_handle:
                    self._sleep(FAST_WAIT_SECONDS)
                    driver.switch_to.window(hub_handle)
                    self._sleep(FAST_WAIT_SECONDS)
                    driver.close()
            except Exception:
                pass
            try:
                if server_handle in driver.window_handles:
                    driver.switch_to.window(server_handle)
            except Exception:
                try:
                    if driver.window_handles:
                        driver.switch_to.window(driver.window_handles[0])
                except Exception:
                    pass
            self._close_extra_windows({server_handle, self.message_tab_handle})

    def _ensure_message_tab(self) -> str:
        handles = set(self.driver.window_handles)
        if self.message_tab_handle and self.message_tab_handle in handles:
            return self.message_tab_handle
        self.driver.switch_to.new_window("tab")
        self.message_tab_handle = self.driver.current_window_handle
        return self.message_tab_handle

    def _switch_to_game_tab(self, previous_handles: set[str], timeout: float = 20) -> None:
        def find_game_handle(driver: Any) -> str | None:
            try:
                handles = list(driver.window_handles)
            except WebDriverException:
                return None
            new_handles = [handle for handle in handles if handle not in previous_handles]
            candidates = new_handles or handles
            try:
                current = driver.current_window_handle
            except WebDriverException:
                current = handles[-1] if handles else None
            for handle in reversed(candidates):
                try:
                    driver.switch_to.window(handle)
                    url = (driver.current_url or "").strip()
                    parsed_url = urlparse(url)
                    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                        continue
                    if "lobby.ikariam.gameforge.com" not in url.casefold():
                        return handle
                except (NoSuchWindowException, WebDriverException):
                    continue
            try:
                if current in driver.window_handles:
                    driver.switch_to.window(current)
            except WebDriverException:
                return None
            return None

        try:
            handle = self._wait_for(find_game_handle, timeout=timeout, poll=0.5)
            self.driver.switch_to.window(handle)
            self.main_tab_handle = handle
        except (TimeoutException, NoSuchWindowException, WebDriverException):
            raise TimeoutException("A aba do jogo nao abriu uma URL HTTP/HTTPS valida.")

    def _find_game_start_button(self) -> Any | None:
        selectors = (
            (By.ID, "joinGame"),
            (
                By.XPATH,
                "//button[contains(., 'Jogou pela') or contains(., 'última vez') or contains(., 'ultima vez')]",
            ),
            (
                By.XPATH,
                "//button[contains(@class, 'button-primary') and (normalize-space()='JOGAR' or normalize-space()='Jogar')]",
            ),
            (By.XPATH, "//button[normalize-space()='JOGAR' or normalize-space()='Jogar']"),
            (By.XPATH, "//a[normalize-space()='JOGAR' or normalize-space()='Jogar']"),
            (
                By.XPATH,
                "//input[translate(normalize-space(@value), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')='JOGAR']",
            ),
        )
        for by, selector in selectors:
            try:
                return self._find_or_wait(by, selector, timeout=2, clickable=True)
            except Exception:
                continue
        return None

    def _click_game_start_if_present(self, textLogs: LogSink | None = None) -> bool:
        button = self._find_game_start_button()
        if not button:
            return False
        if textLogs:
            textLogs.addLogs("Tela inicial do jogo detectada. Clicando em Jogar.", "info")
        current_url = getattr(self.driver, "current_url", "")
        self._click(button)
        self._sleep(3)

        def game_started(driver: Any) -> bool:
            if self._find_game_start_button() is None:
                return True
            try:
                url_changed = bool(current_url) and getattr(driver, "current_url", "") != current_url
                highscore_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='view=highscore']")
                return url_changed or bool(highscore_links)
            except Exception:
                return False

        try:
            self._wait_for(game_started, timeout=20, poll=0.5)
        except TimeoutException:
            if textLogs:
                textLogs.addLogs("Clique em Jogar nao abriu a interface do jogo dentro do tempo limite.", "error")
            raise
        return True

    def _open_highscore_dropdown(self) -> bool:
        try:
            dropdown = self._find_or_wait(
                By.XPATH,
                '//*[@id="js_highscoreOffsetContainer"]/span',
                timeout=6,
                clickable=True,
            )
            self._click(dropdown)
            return True
        except Exception:
            return False

    def _highscore_dropdown_scroll_state(self, container: Any) -> tuple[int, int, int]:
        try:
            state = self.driver.execute_script(
                """
                const root = arguments[0];
                const candidates = [root, ...root.querySelectorAll('*')];
                let best = root;
                for (const el of candidates) {
                    if ((el.scrollHeight || 0) > (el.clientHeight || 0)
                        && (el.scrollHeight || 0) >= (best.scrollHeight || 0)) {
                        best = el;
                    }
                }
                return [best.scrollTop || 0, best.scrollHeight || 0, best.clientHeight || 0];
                """,
                container,
            )
            return int(state[0]), int(state[1]), int(state[2])
        except Exception:
            return 0, 0, 0

    def _scroll_highscore_dropdown(self, container: Any) -> bool:
        before_top, before_height, before_client = self._highscore_dropdown_scroll_state(container)
        if before_height <= before_client:
            return False
        try:
            after = self.driver.execute_script(
                """
                const root = arguments[0];
                const candidates = [root, ...root.querySelectorAll('*')];
                let best = root;
                for (const el of candidates) {
                    if ((el.scrollHeight || 0) > (el.clientHeight || 0)
                        && (el.scrollHeight || 0) >= (best.scrollHeight || 0)) {
                        best = el;
                    }
                }
                best.scrollTop = Math.min(
                    best.scrollTop + Math.max(best.clientHeight * 0.85, 120),
                    best.scrollHeight
                );
                return best.scrollTop || 0;
                """,
                container,
            )
            return int(after or 0) > before_top
        except Exception:
            return False

    def _reset_highscore_dropdown_scroll(self, container: Any) -> None:
        try:
            self.driver.execute_script(
                """
                const root = arguments[0];
                const candidates = [root, ...root.querySelectorAll('*')];
                let best = root;
                for (const el of candidates) {
                    if ((el.scrollHeight || 0) > (el.clientHeight || 0)
                        && (el.scrollHeight || 0) >= (best.scrollHeight || 0)) {
                        best = el;
                    }
                }
                best.scrollTop = 0;
                """,
                container,
            )
            self._sleep(0.1)
        except Exception:
            pass

    def _collect_highscore_option_texts(self, container: Any, max_scrolls: int = 80) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()
        stale_scrolls = 0
        self._reset_highscore_dropdown_scroll(container)

        for _ in range(max_scrolls):
            self._check_stop()
            links = container.find_elements(By.XPATH, ".//li//a")
            before_count = len(seen)
            for link in links:
                text = re.sub(r"\s+", " ", (link.text or "").strip())
                key = text.casefold()
                if text and key not in seen:
                    seen.add(key)
                    collected.append(text)
            if len(seen) == before_count:
                stale_scrolls += 1
            else:
                stale_scrolls = 0
            if not self._scroll_highscore_dropdown(container):
                break
            self._sleep(0.1)
            if stale_scrolls >= 3:
                break

        return collected

    def _open_highscore_page(self) -> Any:
        selectors = (
            (By.CSS_SELECTOR, "#GF_toolbar a[href*='view=highscore']"),
            (By.CSS_SELECTOR, "a[href*='view=highscore']"),
            (By.XPATH, "//a[contains(., 'Pontuação') or contains(., 'Pontuacao')]"),
            (By.XPATH, "//a[contains(., 'Highscore') or contains(., 'Ranking')]"),
        )
        last_error: Exception | None = None
        for by, selector in selectors:
            try:
                element = self._find_or_wait(by, selector, timeout=8, clickable=True)
                self._click(element)
                return element
            except Exception as error:
                last_error = error
                continue
        raise TimeoutException(str(last_error) if last_error else "highscore link not found")

    def _get_highscore_options(
        self,
        attempts: int = 1,
        delay: float = 0.8,
        reopen: Any | None = None,
    ) -> list[str]:
        options: list[str] = []
        for _ in range(max(attempts, 1)):
            self._check_stop()
            if not self._open_highscore_dropdown() and reopen is not None:
                try:
                    self._click(reopen)
                    self._sleep(delay)
                    self._open_highscore_dropdown()
                except Exception:
                    pass
            try:
                container = self._find_or_wait(By.ID, "dropDown_js_highscoreOffsetContainer", timeout=3)
                options = self._normalize_highscore_options(self._collect_highscore_option_texts(container))
                if options:
                    return options
            except Exception:
                self._sleep(delay)
        return options

    def _find_highscore_option_link(self, option_text: str) -> Any | None:
        try:
            container = self._find_or_wait(By.ID, "dropDown_js_highscoreOffsetContainer", timeout=3)
        except Exception:
            return None
        self._reset_highscore_dropdown_scroll(container)

        for _ in range(80):
            self._check_stop()
            try:
                links = container.find_elements(By.XPATH, ".//li//a")
                for link in links:
                    text = re.sub(r"\s+", " ", (link.text or "").strip())
                    if text == option_text:
                        return link
            except Exception:
                return None
            if not self._scroll_highscore_dropdown(container):
                return None
            self._sleep(0.1)
        return None

    def _highscore_offset_visible(self, option_text: str) -> bool:
        expected = re.sub(r"\s+", " ", option_text or "").strip()
        if not expected:
            return False
        try:
            text = self.driver.execute_script(
                """
                const selected = document.querySelector('#js_highscoreOffsetContainer');
                return selected ? selected.innerText : '';
                """
            )
        except Exception:
            return False
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        return expected in compact

    def _select_highscore_offset(
        self,
        option_text: str,
        force_open: bool = False,
        textLogs: LogSink | None = None,
    ) -> bool:
        self._check_stop()
        for attempt in range(2):
            try:
                if force_open or attempt:
                    self._open_highscore_dropdown()
                element = self._find_highscore_option_link(option_text)
                if element is None:
                    continue
                self._click(element)
                try:
                    self._wait_for(lambda driver: self._highscore_offset_visible(option_text), timeout=8, poll=0.4)
                except TimeoutException:
                    if textLogs:
                        textLogs.addLogs(f"Filtro nao confirmou selecao: {option_text}", "warn")
                    continue
                if textLogs:
                    textLogs.addLogs(f"Filtro selecionado: {option_text}", "info")
                self._active_highscore_offset = option_text
                return True
            except Exception:
                continue
        return False

    def _start_activation_listener(self, textLogs: LogSink | None = None) -> None:
        if self.listener is not None:
            return
        try:
            from pynput import keyboard

            self.listener = keyboard.Listener(on_press=self.on_key_press)
            self.listener.daemon = True
            self.listener.start()
            if textLogs:
                textLogs.addLogs("Atalho F habilitado para finalizar a ativacao.", "info")
        except Exception:
            self.listener = None
            if textLogs:
                textLogs.addLogs("Atalho F indisponivel neste ambiente.", "warn")

    def _stop_activation_listener(self) -> None:
        listener = self.listener
        self.listener = None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception:
            pass

    def _install_activation_page_hotkey(self) -> None:
        try:
            self.driver.execute_script(
                """
                if (!window.__ikariamActivationHotkeyInstalled) {
                    window.__ikariamActivationHotkeyInstalled = true;
                    window.__ikariamActivationCloseRequested = false;
                    document.addEventListener('keydown', function(event) {
                        if ((event.key || '').toLowerCase() === 'f') {
                            window.__ikariamActivationCloseRequested = true;
                        }
                    }, true);
                }
                """
            )
        except Exception:
            pass

    def _is_activation_page_hotkey_requested(self) -> bool:
        try:
            return bool(self.driver.execute_script("return !!window.__ikariamActivationCloseRequested;"))
        except Exception:
            return False

    def _lobby_session_confirmed(self) -> bool:
        """Return whether the lobby has completed authentication for activation."""
        try:
            current_url = str(self.driver.current_url or "").casefold()
        except Exception:
            current_url = ""
        if "/hub" in current_url:
            return True
        for by, selector in (
            (By.ID, "joinGame"),
            (By.ID, "accountlist"),
            (By.CLASS_NAME, "serverDetails"),
        ):
            try:
                if self.driver.find_elements(by, selector):
                    return True
            except Exception:
                continue
        return False

    def login(self) -> bool:
        confirmed = False
        try:
            self._start_activation_listener(self.logs)
            self.running = True
            if not self._login_to_lobby(self.logs, wait_after_submit=False):
                if self.logs:
                    self.logs.addLogs("Ativacao nao confirmou o envio das credenciais.", "warn")
                return False
            if self.logs:
                self.logs.addLogs("Credenciais preenchidas. Conclua o login se houver verificacao manual.", "info")
            deadline = time.monotonic() + ACTIVATION_WAIT_SECONDS
            while self.running:
                self._install_activation_page_hotkey()
                if self._is_activation_page_hotkey_requested():
                    self.activation_cancelled = True
                    self._stop()
                    break
                if self._lobby_session_confirmed():
                    confirmed = True
                    if self.logs:
                        self.logs.addLogs("Login confirmado no lobby. Conta pronta para uso.", "info")
                    break
                try:
                    if not self.driver.window_handles:
                        if self.logs:
                            self.logs.addLogs("Ativacao interrompida: a janela do navegador foi fechada.", "warn")
                        self._stop()
                        break
                except Exception:
                    self._stop()
                    break
                if time.monotonic() >= deadline:
                    if self.logs:
                        self.logs.addLogs("Ativacao expirou sem confirmar o lobby.", "warn")
                    break
                sleep(0.1)
            return confirmed
        except Exception as error:
            if _is_driver_connection_error(error):
                self._stop()
                return False
            raise
        finally:
            self._stop_activation_listener()
            if not confirmed and self.running:
                self.close()

    def _login_to_lobby(self, textLogs: LogSink | None, wait_after_submit: bool = True) -> bool:
        self._check_stop()
        driver = self.driver
        if textLogs:
            textLogs.addLogs("Abrindo lobby do Ikariam.", "info")
        self._safe_get(LOBBY_URL, timeout=20)
        if textLogs:
            textLogs.addLogs("Lobby carregado em aba controlada pelo Selenium.", "info")

        def read_lobby_state(active_driver: Any) -> dict[str, Any]:
            return active_driver.execute_script(
                """
                const text = document.body ? document.body.innerText : "";
                return {
                    url: location.href,
                    readyState: document.readyState,
                    text: text.slice(0, 500),
                    hasJoinGame: !!document.querySelector('#joinGame'),
                    hasServerDetails: !!document.querySelector('.serverDetails'),
                    hasAccounts: !!document.querySelector('a[href*="/accounts"]'),
                    hasEmail: !!document.querySelector('input[name="email"]'),
                    hasPassword: !!document.querySelector('input[name="password"]')
                };
                """
            )

        def lobby_loaded(active_driver: Any) -> bool:
            try:
                return bool(
                    (state := read_lobby_state(active_driver)).get("hasJoinGame")
                    or state.get("hasServerDetails")
                    or state.get("hasAccounts")
                    or state.get("hasEmail")
                    or state.get("hasPassword")
                )
            except Exception:
                return False

        self._wait_for(lobby_loaded, timeout=20, poll=0.5)
        self._sleep(1)
        state = read_lobby_state(driver)
        current_url = str(state.get("url") or "").lower()
        if textLogs:
            markers = ", ".join(
                name
                for name in ("hasJoinGame", "hasServerDetails", "hasAccounts", "hasEmail", "hasPassword")
                if state.get(name)
            ) or "sem marcadores"
            textLogs.addLogs(f"Estado do lobby: {current_url or 'sem url'} ({markers}).", "info")
        if (
            state.get("hasJoinGame")
            or state.get("hasServerDetails")
            or "/hub" in current_url
            or state.get("hasAccounts")
        ):
            if textLogs:
                textLogs.addLogs("Sessao ja autenticada no lobby.", "info")
            return True

        wait = WebDriverWait(driver, 4)
        _dismiss_cookie_banner(driver)
        _try_click_login_tab(driver, wait)
        if not _find_login_inputs(driver, wait_seconds=5):
            if textLogs:
                textLogs.addLogs("Nao foi possivel localizar os campos de login.", "warn")
            return False

        email_input = driver.find_element(By.XPATH, '//input[@name="email"]')
        password_input = driver.find_element(By.XPATH, '//input[@name="password"]')
        email_input.clear()
        email_input.send_keys(self.account.email)
        password_input.clear()
        password_input.send_keys(self.account.password)

        submit_button = _visible_submit_button(driver, wait)
        if not submit_button:
            if textLogs:
                textLogs.addLogs("Nao foi possivel localizar o botao de login.", "warn")
            return False
        if textLogs:
            textLogs.addLogs("Credenciais preenchidas. Enviando login no lobby.", "info")
        self._click(submit_button)
        if not wait_after_submit:
            return True

        try:
            self._wait_for(
                lambda active_driver: active_driver.find_elements(By.ID, "joinGame")
                or active_driver.find_elements(By.CLASS_NAME, "serverDetails")
                or active_driver.find_elements(By.ID, "accountlist"),
                timeout=35,
                poll=0.5,
            )
            if textLogs:
                textLogs.addLogs("Login confirmado no lobby.", "info")
            return True
        except TimeoutException:
            if textLogs:
                try:
                    state = read_lobby_state(driver)
                    page_text = _compact_page_text(str(state.get("text") or ""))
                    textLogs.addLogs(
                        f"Login nao confirmou acesso ao lobby. URL atual: {state.get('url') or 'sem url'}. Texto: {page_text or 'sem texto visivel'}.",
                        "warn",
                    )
                except Exception:
                    textLogs.addLogs("Login nao confirmou acesso ao lobby e nao foi possivel ler a pagina.", "warn")
                textLogs.addLogs("Login nao confirmou acesso ao lobby dentro do tempo limite.", "warn")
            return False

    def on_key_press(self, key: Any) -> None:
        try:
            if getattr(key, "char", "").lower() == "f":
                self._stop()
        except AttributeError:
            return

    def StartGame(self, textLogs: LogSink | None = None) -> None:
        logger = textLogs or self.logs
        try:
            self._check_stop()
            login_ok = False
            for attempt in range(1, 4):
                if self._login_to_lobby(logger):
                    login_ok = True
                    break
                if logger:
                    logger.addLogs(f"Login nao confirmou acesso ao lobby. Tentativa {attempt}/3.", "warn")
                self._sleep(3)
            if not login_ok:
                raise TimeoutException("lobby login was not available")
            completed_servers: set[str] = set()
            current_round_servers: set[str] = set()
            selection_failures = 0
            while not self._is_stopped():
                try:
                    excluded_servers = completed_servers | current_round_servers
                    next_server = self._enter_server_from_accounts(logger, exclude_servers=excluded_servers)
                    if not next_server and current_round_servers:
                        current_round_servers.clear()
                        next_server = self._enter_server_from_accounts(logger, exclude_servers=completed_servers)
                    if not next_server:
                        if getattr(self, "_last_accounts_page_only_inactive", False):
                            break
                        if completed_servers or current_round_servers:
                            break
                        self._enter_default_server_from_lobby(logger)
                        next_server = self.serverGlobal.server if self.serverGlobal else None
                    selection_failures = 0
                except (BotCooldown, BotStopped):
                    raise
                except Exception as error:
                    selection_failures += 1
                    if logger:
                        logger.addLogs(
                            f"Falha ao selecionar servidor. Tentativa {selection_failures}/3. Erro: {type(error).__name__}: {_error_summary(error)}",
                            "warn",
                        )
                    try:
                        self._close_extra_windows({self.main_tab_handle, self.message_tab_handle})
                    except Exception:
                        pass
                    if selection_failures >= 3:
                        break
                    continue
                if not next_server:
                    break
                current_server = self.serverGlobal.display_name if self.serverGlobal else next_server
                self._server_cycle_send_count = 0
                try:
                    completed, sent_any, batch_exhausted = self._run_current_server_flow(logger)
                except (BotCooldown, BotStopped):
                    raise
                except Exception as error:
                    if logger:
                        logger.addLogs(
                            f"Servidor {current_server} apresentou erro e sera pulado. Erro: {type(error).__name__}: {_error_summary(error)}",
                            "warn",
                        )
                    try:
                        self._close_extra_windows({self.main_tab_handle, self.message_tab_handle})
                    except Exception:
                        pass
                    completed_servers.add(current_server)
                    continue
                if completed and current_server:
                    completed_servers.add(current_server)
                if current_server:
                    current_round_servers.add(current_server)
                if batch_exhausted and logger:
                    logger.addLogs(
                        f"Lote de {_server_send_batch_limit()} mensagens concluido em {current_server}. Alternando conta/servidor.",
                        "info",
                    )
                if not sent_any and not completed and current_server:
                    completed_servers.add(current_server)
        except BotCooldown as cooldown:
            if logger:
                logger.addLogs(f"Cooldown detectado. Aguardando {cooldown.wait_seconds}s.", "warn")
            self._sleep(cooldown.wait_seconds)
        except BotUnconfirmed as unconfirmed:
            if logger:
                logger.addLogs(f"Status de envio nao confirmado. Aguardando {unconfirmed.wait_seconds}s.", "warn")
            self._sleep(unconfirmed.wait_seconds)
        except BotStopped:
            if logger:
                logger.addLogs("Bot parado.", "info")
        except StaleElementReferenceException:
            recovery_count = int(getattr(self, "_stale_recovery_count", 0)) + 1
            self._stale_recovery_count = recovery_count
            if logger:
                logger.addLogs(
                    f"Pagina atualizou durante o Selenium. Recarregando o fluxo ({recovery_count}/{STALE_ELEMENT_RECOVERY_LIMIT}).",
                    "warn",
                )
            if recovery_count >= STALE_ELEMENT_RECOVERY_LIMIT:
                if logger:
                    logger.addLogs("Pagina atualizou repetidamente. Conta pausada sem enviar novas mensagens.", "warn")
                return
            try:
                self._close_extra_windows({self.main_tab_handle, self.message_tab_handle})
            except Exception:
                pass
            self._sleep(1)
            self.StartGame(logger)

    def _enter_default_server_from_lobby(self, textLogs: LogSink | None) -> None:
        self._check_stop()
        self._safe_get(LOBBY_URL)
        self._sleep(3)
        body_text = ""
        try:
            body_text = self.driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            pass
        if "Começar num Novo Servidor" in body_text and "Jogou pela" not in body_text:
            if textLogs:
                textLogs.addLogs("Conta sem servidor existente no lobby. Pulando para a proxima conta.", "warn")
            raise TimeoutException("account has no existing Ikariam server")
        if self._click_game_start_if_present(textLogs):
            match = re.search(r"([A-Za-z][A-Za-z0-9 _-]{1,40})\s*[–-]\s*Jogadores:\s*([\d.]+)", body_text)
            name_server = match.group(1).strip() if match else "Servidor desconhecido"
            users_count = self._parse_int_from_text(match.group(2)) if match else self._parse_int_from_text(body_text)
            self._ensure_server_global(name_server, users_count=users_count, textLogs=textLogs)
            self.main_tab_handle = self.driver.current_window_handle
            return
        self._find_or_wait(By.ID, "joinGame", timeout=12, clickable=True)
        button_game = self._find_or_wait(By.CLASS_NAME, "serverDetails", timeout=12, clickable=True)
        self._sleep(SHORT_WAIT_SECONDS)
        previous_handles = set(self.driver.window_handles)
        self._click(button_game)
        if textLogs:
            textLogs.addLogs(button_game.text)
        name_server = button_game.text.split("–")[0].strip()
        users_count = self._parse_int_from_text(button_game.text)
        self._ensure_server_global(name_server, users_count=users_count, textLogs=textLogs)
        self._switch_to_game_tab(previous_handles)
        self._click_game_start_if_present(textLogs)
        self.main_tab_handle = self.driver.current_window_handle

    def _open_accounts_page(self, textLogs: LogSink | None) -> None:
        if textLogs:
            textLogs.addLogs("Abrindo lista de contas/servidores.", "info")
        self._safe_get(ACCOUNTS_URL, timeout=45)
        self._sleep(2)
        if self.driver.find_elements(By.ID, "accountlist") or self.driver.find_elements(By.CSS_SELECTOR, ".rt-tbody"):
            return
        current_url = (getattr(self.driver, "current_url", "") or "").lower()
        if "/hub" in current_url:
            return
        account_links = self.driver.find_elements(By.CSS_SELECTOR, "a[href*='/accounts']")
        for link in account_links:
            try:
                text = (getattr(link, "text", "") or "").strip().casefold()
                is_displayed = getattr(link, "is_displayed", lambda: True)
                if "jogar" not in text or not is_displayed():
                    continue
                if textLogs:
                    textLogs.addLogs("Hub detectado. Clicando em Jogar para abrir a lista de contas.", "info")
                self._click(link)
                self._wait_for(
                    lambda driver: driver.find_elements(By.ID, "accountlist")
                    or driver.find_elements(By.CSS_SELECTOR, ".rt-tbody")
                    or "/accounts" in (getattr(driver, "current_url", "") or ""),
                    timeout=30,
                    poll=0.5,
                )
                self._sleep(2)
                return
            except Exception:
                continue

    def _find_owned_server_cards(self) -> list[Any]:
        selectors = (
            (By.CSS_SELECTOR, "#accountlist .rt-tbody .rt-tr-group"),
            (By.XPATH, '//*[@id="accountlist"]/div/div[1]/div[2]/div'),
            (By.XPATH, "//*[contains(normalize-space(.), 'Suas Contas')]//following::table[1]//tbody/tr"),
        )
        for by, selector in selectors:
            try:
                cards = self.driver.find_elements(by, selector)
                if cards:
                    return list(cards)
            except Exception:
                continue
        return []

    def _enter_server_from_accounts(
        self,
        textLogs: LogSink | None,
        target_server: str | None = None,
        exclude_server: str | None = None,
        exclude_servers: set[str] | None = None,
    ) -> str | None:
        self._check_stop()
        exclude = {server.casefold() for server in (exclude_servers or set())}
        if exclude_server:
            exclude.add(exclude_server.casefold())
        self._open_accounts_page(textLogs)
        self._last_accounts_page_only_inactive = False
        try:
            cards = self._wait_for(lambda driver: self._find_owned_server_cards(), timeout=12, poll=0.5)
        except TimeoutException:
            cards = self._find_owned_server_cards()
        inactive_cards = 0
        visible_cards = 0
        for card in cards:
            text = getattr(card, "text", "").strip()
            if not text:
                continue
            visible_cards += 1
            try:
                server_name = card.find_element(By.CSS_SELECTOR, ".server-name-cell").text.strip()
            except Exception:
                server_name = text.split("–")[0].splitlines()[0].strip()
            flag_name = self._extract_server_flag(card)
            server_identity = self._server_display_name(server_name, flag_name)
            if self._is_ignored_server_card(server_name, text):
                inactive_cards += 1
                if textLogs:
                    textLogs.addLogs(f"Ignorando servidor/conta inativa: {server_identity or 'Asphodel'}.", "warn")
                continue
            if target_server and server_name.casefold() != target_server.casefold() and server_identity.casefold() != target_server.casefold():
                continue
            if server_name.casefold() in exclude or server_identity.casefold() in exclude:
                continue
            button = None
            for by, selector in (
                (By.CSS_SELECTOR, ".action-cell button"),
                (By.XPATH, ".//button[normalize-space()='Jogar' or normalize-space()='JOGAR' or normalize-space()='Play' or normalize-space()='PLAY']"),
                (By.XPATH, ".//a[normalize-space()='Jogar' or normalize-space()='JOGAR' or normalize-space()='Play' or normalize-space()='PLAY']"),
                (By.XPATH, ".//button[contains(@class, 'btn-primary') or contains(@class, 'button-primary')]"),
            ):
                try:
                    candidate = card.find_element(by, selector)
                    candidate_text = (getattr(candidate, "text", "") or "").strip()
                    if not self._is_enabled_lobby_button(candidate):
                        continue
                    if self._is_owned_server_button_text(candidate_text) or (
                        not candidate_text and self._is_primary_lobby_button(candidate)
                    ):
                        button = candidate
                        break
                except Exception:
                    continue
            if button is None:
                continue
            try:
                previous_handles = set(self.driver.window_handles)
                self._click(button)
                self._switch_to_game_tab(previous_handles)
                self._click_game_start_if_present(textLogs)
                self.main_tab_handle = self.driver.current_window_handle
                users_count = self._fetch_users_from_hub(self.main_tab_handle)
                self._ensure_server_global(server_name, flag_name=flag_name, users_count=users_count, textLogs=textLogs)
                return server_identity
            except (BotCooldown, BotStopped):
                raise
            except Exception as error:
                if textLogs:
                    textLogs.addLogs(
                        f"Falha ao abrir servidor {server_name or 'desconhecido'}. Pulando. Erro: {type(error).__name__}: {_error_summary(error)}",
                        "warn",
                    )
                try:
                    self._close_extra_windows({self.main_tab_handle, self.message_tab_handle})
                except Exception:
                    pass
                continue
        if visible_cards and inactive_cards >= visible_cards:
            self._last_accounts_page_only_inactive = True
            if textLogs:
                textLogs.addLogs("Nenhum servidor ativo encontrado nesta conta.", "warn")
        return None

    def _run_current_server_flow(self, textLogs: LogSink | None) -> tuple[bool, bool, bool]:
        self._check_stop()
        self._active_highscore_offset = None
        server_global = getattr(self, "serverGlobal", None)
        _audit(
            textLogs,
            "server_flow_started",
            server=server_global.display_name if server_global else None,
            server_total=int(server_global.messageSend or 0) if server_global else 0,
            session_total=int(getattr(self, "totalSentSession", 0)),
            cycle_total=int(getattr(self, "_server_cycle_send_count", 0)),
            batch_limit=_server_send_batch_limit(),
            **_audit_database_counts(textLogs, server_global),
        )
        self._wait_for(lambda driver: len(driver.window_handles) >= 1, timeout=10)
        handles = list(self.driver.window_handles)
        main_handle = (
            self.main_tab_handle
            if self.main_tab_handle in handles and self.main_tab_handle != self.message_tab_handle
            else next((handle for handle in handles if handle != self.message_tab_handle), handles[-1])
        )
        self.main_tab_handle = main_handle
        self.driver.switch_to.window(main_handle)
        self._close_extra_windows({self.main_tab_handle, self.message_tab_handle})
        self._click_game_start_if_present(textLogs)
        outbox_total = self._sync_outbox_sent_users(textLogs)
        if outbox_total is None:
            if textLogs:
                textLogs.addLogs(
                    "Outbox indisponivel no inicio do servidor. Continuando pelo contador local e pelo banco.",
                    "warn",
                )
            outbox_total = int(server_global.messageSend or 0) if server_global else 0
        _audit(
            textLogs,
            "server_resume_synchronized",
            server=server_global.display_name if server_global else None,
            outbox_total=outbox_total,
            resume_total=int(server_global.messageSend or 0) if server_global else outbox_total,
            **_audit_database_counts(textLogs, server_global),
        )
        highscore_link = self._open_highscore_page()
        self._sleep(SHORT_WAIT_SECONDS)
        discovered_option_texts = self._get_highscore_options(attempts=3, delay=1.5, reopen=highscore_link)
        resume_total = int(server_global.messageSend or 0) if server_global else int(outbox_total or 0)
        option_texts = self._resume_highscore_options(discovered_option_texts, resume_total)
        _audit(textLogs, "highscore_filters_discovered", filters=option_texts, filter_count=len(option_texts))
        self._update_server_users_count(self._highscore_total_from_options(discovered_option_texts), textLogs)
        if discovered_option_texts and not option_texts:
            _audit(
                textLogs,
                "resume_filter_unavailable",
                resume_total=resume_total,
                next_position=resume_total + 1,
                highest_available_position=self._highscore_total_from_options(discovered_option_texts),
                discovered_filter_count=len(discovered_option_texts),
            )
            if textLogs:
                textLogs.addLogs(
                    f"Servidor concluido: {resume_total} mensagens ja contabilizadas e nenhuma faixa posterior disponivel.",
                    "info",
                )
            return True, False, False
        if not discovered_option_texts:
            completed, sent_any, batch_exhausted = self._captureusers(textLogs=textLogs)
            if completed:
                return True, sent_any, batch_exhausted
            if batch_exhausted:
                return False, sent_any, True
            raise BotCooldown(wait_seconds=30)
        force_next_select = False
        sent_any_total = False
        batch_exhausted_total = False
        for option_text in option_texts:
            if self._is_stopped():
                raise BotStopped()
            if not self._select_highscore_offset(option_text, force_open=force_next_select, textLogs=textLogs):
                _audit(textLogs, "filter_selection_failed", filter=option_text)
                continue
            force_next_select = True
            self._sleep(SHORT_WAIT_SECONDS)
            completed, sent_any, batch_exhausted = self._captureusers(textLogs=textLogs)
            _audit(
                textLogs,
                "filter_processed",
                filter=option_text,
                capture_state=dict(getattr(self, "_last_capture_state", {})),
                completed=completed,
                sent_any=sent_any,
                batch_exhausted=batch_exhausted,
                server_total=int(server_global.messageSend or 0) if server_global else 0,
                session_total=int(getattr(self, "totalSentSession", 0)),
                cycle_total=int(getattr(self, "_server_cycle_send_count", 0)),
                **_audit_database_counts(textLogs, server_global),
            )
            sent_any_total = sent_any_total or sent_any
            batch_exhausted_total = batch_exhausted_total or batch_exhausted
            if completed:
                return True, sent_any_total, batch_exhausted_total
            if batch_exhausted:
                return False, sent_any_total, True
            if not sent_any:
                capture_state = getattr(self, "_last_capture_state", {})
                target_count = int(capture_state.get("targets", 0))
                row_count = int(capture_state.get("rows", 0))
                row_errors = int(capture_state.get("row_errors", 0))
                reserved_count = int(capture_state.get("reserved", 0))
                registered_skips = int(capture_state.get("skipped_confirmed", 0))
                if target_count == 0:
                    if textLogs:
                        textLogs.addLogs(
                            f"Filtro sem destinatarios reconhecidos: {row_count} linhas lidas, {row_errors} linhas invalidas. Avancando para a proxima faixa.",
                            "warn",
                        )
                    force_next_select = True
                    continue
                if registered_skips == target_count and reserved_count == 0:
                    if textLogs:
                        textLogs.addLogs(
                            f"Filtro concluido: {target_count} jogadores ja registrados no banco. Avancando para a proxima faixa.",
                            "info",
                        )
                    force_next_select = True
                    continue
                if target_count > 0:
                    if textLogs:
                        textLogs.addLogs(
                            f"Filtro bloqueado: {target_count} jogadores reconhecidos, {reserved_count} reservas novas e {registered_skips} ja registrados. Nenhum envio foi concluido; nao avancando para a proxima faixa.",
                            "error",
                        )
                    raise TimeoutException("highscore range has targets without confirmed sends")
        return not sent_any_total, sent_any_total, batch_exhausted_total

    @staticmethod
    def _resume_highscore_options(option_texts: list[str], sent_total: int) -> list[str]:
        if sent_total <= 0:
            return option_texts
        target_position = sent_total + 1
        resumed: list[str] = []
        for option_text in option_texts:
            match = re.search(r"(\d[\d.,]*)\s*-\s*(\d[\d.,]*)", option_text)
            if not match:
                continue
            range_end = int(re.sub(r"\D", "", match.group(2)))
            if range_end >= target_position:
                resumed.append(option_text)
        return resumed

    def _wait_before_next_send(self) -> None:
        elapsed = time.time() - self._last_send_attempt_at
        remaining = self.timeWait - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def _current_filter_resume_offset(self) -> int:
        """Return how many rows in the active ranking range are already sent."""
        filter_text = getattr(self, "_active_highscore_offset", None) or ""
        match = re.search(r"(\d[\d.,\s]*)\s*-\s*(\d[\d.,\s]*)", filter_text)
        if not match or not self.serverGlobal:
            return 0
        range_start = self._parse_outbox_count(match.group(1))
        range_end = self._parse_outbox_count(match.group(2))
        server_total = int(self.serverGlobal.messageSend or 0)
        if range_start is None or range_end is None or not range_start <= server_total < range_end:
            return 0
        return max(server_total - range_start + 1, 0)

    def _captureusers(self, textLogs: LogSink | None = None) -> tuple[bool, bool, bool]:
        self._last_capture_state = {
            "rows": 0,
            "resume_offset": 0,
            "targets": 0,
            "reserved": 0,
            "attempted": 0,
            "row_errors": 0,
            "skipped_confirmed": 0,
            "skipped_pending": 0,
        }
        try:
            users = self._find_all_or_wait(By.XPATH, '//*[@id="tab_highscore"]/div[1]/table/tbody/tr', timeout=10)
        except TimeoutException:
            if textLogs:
                textLogs.addLogs("Tabela de ranking nao encontrada.", "warn")
            return False, False, False
        rows_before_resume = len(users)
        resume_offset = self._current_filter_resume_offset()
        self._last_capture_state["rows"] = rows_before_resume
        self._last_capture_state["resume_offset"] = resume_offset
        if resume_offset:
            users = users[resume_offset:]
            _audit(
                textLogs,
                "ranking_resume_offset_applied",
                filter=getattr(self, "_active_highscore_offset", None),
                offset=resume_offset,
                rows_before=rows_before_resume,
                rows_after=len(users),
                server=self.serverGlobal.display_name if self.serverGlobal else None,
                server_total=int(self.serverGlobal.messageSend or 0) if self.serverGlobal else 0,
            )

        max_send = self._compute_max_send()
        _audit(
            textLogs,
            "ranking_rows_loaded",
            rows=rows_before_resume,
            rows_after_resume=len(users),
            resume_offset=resume_offset,
            max_send=max_send,
            server=self.serverGlobal.display_name if self.serverGlobal else None,
            server_total=int(self.serverGlobal.messageSend or 0) if self.serverGlobal else 0,
            session_total=self.totalSentSession,
            cycle_total=self._server_cycle_send_count,
            **_audit_database_counts(textLogs, self.serverGlobal),
        )
        if self.serverGlobal and max_send is not None and int(self.serverGlobal.messageSend or 0) >= max_send:
            if textLogs:
                textLogs.addLogs("Lista finalizada. Usuarios ja registrados na database.", "warn")
            return True, False, False
        if self._server_cycle_send_count >= _server_send_batch_limit():
            return False, False, True

        targets: list[tuple[str, str]] = []
        for user in users:
            try:
                user_name_find = user.find_element(By.CLASS_NAME, "name")
                try:
                    name_links = user_name_find.find_elements(By.XPATH, ".//a")
                except Exception:
                    name_links = []
                if not name_links:
                    name_links = [user_name_find.find_element(By.XPATH, ".//div/a")]
                raw_name = ""
                for user_span in name_links:
                    raw_name = user_span.get_attribute("title") or user_span.text or ""
                    if raw_name.strip():
                        break
                user_name = re.sub(r"\s+", " ", raw_name).strip()
                if not user_name:
                    continue
                button_send_action = user.find_element(By.CLASS_NAME, "action")
                try:
                    send_links = button_send_action.find_elements(
                        By.XPATH,
                        ".//a[contains(@onclick, 'ajaxHandlerCall') or contains(@href, 'message') or contains(@href, 'sendIKMessage')]",
                    )
                except Exception:
                    send_links = []
                if not send_links:
                    send_links = [
                        button_send_action.find_element(
                            By.XPATH,
                            './/a[@onclick="ajaxHandlerCall(this.href); return false;"]',
                        )
                    ]
                send_url = ""
                for button_send in send_links:
                    send_url = button_send.get_attribute("href") or ""
                    if send_url:
                        break
                if not send_url:
                    continue
                targets.append((user_name, send_url))
            except (NoSuchElementException, TimeoutException, StaleElementReferenceException, AttributeError):
                self._last_capture_state["row_errors"] += 1
                continue
        self._last_capture_state["targets"] = len(targets)
        _audit(
            textLogs,
            "ranking_targets_prepared",
            rows=len(users),
            targets=len(targets),
            row_errors=self._last_capture_state["row_errors"],
        )

        sent_any = False
        for user_name, send_url in targets:
            self._wait_if_paused()
            if self._is_stopped():
                raise BotStopped()
            if self._server_cycle_send_count >= _server_send_batch_limit():
                return False, sent_any, True
            try:
                if not self.serverGlobal:
                    continue
                reservation = UsersSend.reserve(
                    server_id=self.serverGlobal.id,
                    username=user_name,
                    account_id=self.account.id,
                )
                if not reservation:
                    status = UsersSend.status_for(server_id=self.serverGlobal.id, username=user_name)
                    self._last_capture_state["skipped_confirmed"] += 1
                    _audit(
                        textLogs,
                        "player_skipped_existing_record",
                        player=user_name,
                        database_status=status,
                        server=self.serverGlobal.display_name,
                        server_total=int(self.serverGlobal.messageSend or 0),
                        session_total=self.totalSentSession,
                        cycle_total=self._server_cycle_send_count,
                    )
                    continue
                self._last_capture_state["reserved"] += 1
                _audit(
                    textLogs,
                    "player_reserved",
                    player=user_name,
                    reservation_id=reservation.id_str,
                    server=self.serverGlobal.display_name,
                    server_total=int(self.serverGlobal.messageSend or 0),
                    session_total=self.totalSentSession,
                    cycle_total=self._server_cycle_send_count,
                )
                self._wait_before_next_send()
                send_started_at = time.time()
                _audit(
                    textLogs,
                    "send_attempt_started",
                    player=user_name,
                    configured_wait=self.timeWait,
                    seconds_since_previous_attempt=round(send_started_at - self._last_send_attempt_at, 3),
                )
                try:
                    status_send, status_feedback, cooldown_seconds = self._sendMessage(
                        username=user_name,
                        send_url=send_url,
                    )
                except Exception as error:
                    UsersSend.update_status(reservation.id_str, "failed")
                    _audit(
                        textLogs,
                        "send_attempt_failed",
                        player=user_name,
                        error_type=type(error).__name__,
                        error=str(error).splitlines()[0] if str(error) else "",
                        duration=round(time.time() - send_started_at, 3),
                    )
                    if textLogs:
                        summary = str(error).splitlines()[0] if str(error) else type(error).__name__
                        textLogs.addLogs(f"Falha ao preparar mensagem para {user_name}: {type(error).__name__}: {summary}", "error")
                    continue
                self._last_capture_state["attempted"] += 1
                self._last_send_attempt_at = time.time()
                _audit(
                    textLogs,
                    "send_attempt_finished",
                    player=user_name,
                    status=status_send,
                    feedback=status_feedback,
                    cooldown_seconds=cooldown_seconds,
                    duration=round(self._last_send_attempt_at - send_started_at, 3),
                )
                if status_send == "allowed":
                    self._mark_sent(reservation.id_str, user_name, textLogs)
                    sent_any = True
                    if max_send is not None and self.serverGlobal and self.serverGlobal.messageSend >= max_send:
                        if textLogs:
                            textLogs.addLogs("Lista finalizada. Usuarios ja registrados na database.", "warn")
                        return True, sent_any, False
                    if self._server_cycle_send_count >= _server_send_batch_limit():
                        return False, sent_any, True
                    continue
                if status_send == "cooldown":
                    if self._is_success_text(status_feedback):
                        self._mark_sent(reservation.id_str, user_name, textLogs)
                        sent_any = True
                        if self._server_cycle_send_count >= _server_send_batch_limit():
                            return False, sent_any, True
                        continue
                    UsersSend.update_status(reservation.id_str, "cooldown")
                    raise BotCooldown(wait_seconds=cooldown_seconds or 60)
                if status_send == "success":
                    self._mark_sent(reservation.id_str, user_name, textLogs)
                    sent_any = True
                    if max_send is not None and self.serverGlobal and self.serverGlobal.messageSend >= max_send:
                        if textLogs:
                            textLogs.addLogs("Lista finalizada. Usuarios ja registrados na database.", "warn")
                        return True, sent_any, False
                    if self._server_cycle_send_count >= _server_send_batch_limit():
                        return False, sent_any, True
                    continue
                if status_send == "ignore list":
                    UsersSend.update_status(reservation.id_str, "ignored")
            except (NoSuchElementException, TimeoutException):
                continue
        return False, sent_any, False

    def _mark_sent(
        self,
        reservation_id: str,
        user_name: str,
        textLogs: LogSink | None,
    ) -> None:
        before_server_total = int(self.serverGlobal.messageSend or 0) if self.serverGlobal else 0
        before_session_total = self.totalSentSession
        before_cycle_total = self._server_cycle_send_count
        UsersSend.update_status(reservation_id, "sent")
        server_total = 0
        if self.serverGlobal:
            self.serverGlobal.messageSend += 1
            self.serverGlobal.save()
            server_total = int(self.serverGlobal.messageSend or 0)
        self.messageSendCount += 1
        self.totalSentSession += 1
        self._server_cycle_send_count += 1
        _audit(
            textLogs,
            "player_marked_sent",
            player=user_name,
            reservation_id=reservation_id,
            dry_run=self.dry_run,
            server=self.serverGlobal.display_name if self.serverGlobal else None,
            server_total_before=before_server_total,
            server_total_after=server_total,
            session_total_before=before_session_total,
            session_total_after=self.totalSentSession,
            cycle_total_before=before_cycle_total,
            cycle_total_after=self._server_cycle_send_count,
            **_audit_database_counts(textLogs, self.serverGlobal),
        )
        if textLogs:
            if self.dry_run:
                textLogs.addLogs(f"Simulado para {user_name} - Total: {server_total}")
            else:
                textLogs.addLogs(f"Enviado para {user_name} - Total: {server_total}")
        self._log_progress(textLogs)

    def _install_send_trace(self) -> None:
        """Observe the submit path in an opt-in diagnostic run without recording secrets."""
        if not _send_diagnostics_enabled():
            return
        self.driver.execute_script(
            """
            if (!window.__botSendTrace) {
                const safePath = (value) => {
                    try { return new URL(value, location.href).pathname || '/'; }
                    catch (_) { return '/'; }
                };
                const trace = [];
                const add = (kind, data) => trace.push({ kind, ...data, at: Date.now() });
                const originalFetch = window.fetch;
                if (originalFetch) {
                    window.fetch = function(input, init) {
                        const method = (init && init.method) || 'GET';
                        add('fetch', { method, path: safePath(typeof input === 'string' ? input : input.url) });
                        return originalFetch.apply(this, arguments).then((response) => {
                            add('fetch-response', { status: response.status, path: safePath(response.url) });
                            return response;
                        });
                    };
                }
                const originalOpen = XMLHttpRequest.prototype.open;
                const originalSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(method, url) {
                    this.__botSendTrace = { method: method || 'GET', path: safePath(url) };
                    return originalOpen.apply(this, arguments);
                };
                XMLHttpRequest.prototype.send = function() {
                    const details = this.__botSendTrace || { method: 'GET', path: '/' };
                    add('xhr', details);
                    this.addEventListener('loadend', () => add('xhr-response', { ...details, status: this.status }));
                    return originalSend.apply(this, arguments);
                };
                document.addEventListener('submit', (event) => {
                    add('form-submit', { path: safePath(event.target.action || location.href), prevented: event.defaultPrevented });
                }, true);
                window.__botSendTrace = trace;
            }
            """
        )

    def _read_send_diagnostics(self) -> str | None:
        if not _send_diagnostics_enabled():
            return None
        details: list[str] = []
        try:
            page_state = self.driver.execute_script(
                """
                const submit = document.querySelector('#js_messageSubmitButton');
                const form = submit && submit.closest('form');
                const texts = Array.from(document.querySelectorAll(
                    '[role="alert"], .error, .errorMessage, .notice, .message, .alert'
                )).filter((element) => element.offsetParent !== null)
                  .map((element) => (element.innerText || '').trim())
                  .filter(Boolean).slice(0, 4).map((text) => text.slice(0, 180));
                return {
                    path: location.pathname || '/',
                    ready: document.readyState,
                    button: submit ? {
                        disabled: Boolean(submit.disabled),
                        ariaDisabled: submit.getAttribute('aria-disabled') || '',
                        type: submit.getAttribute('type') || '',
                        className: submit.className || ''
                    } : null,
                    form: form ? { action: new URL(form.action || location.href, location.href).pathname, method: form.method || 'GET' } : null,
                    visibleMessages: texts,
                    trace: (window.__botSendTrace || []).slice(-12)
                };
                """
            )
            if page_state:
                button = page_state.get("button") or {}
                button_state = "ausente"
                if button:
                    button_state = (
                        f"disabled={button.get('disabled')} "
                        f"aria={button.get('ariaDisabled', '')} type={button.get('type', '')}"
                    )
                details.append(
                    "pagina="
                    f"{page_state.get('path', '/')} pronto={page_state.get('ready', '?')} "
                    f"botao={button_state}"
                )
                form = page_state.get("form")
                if form:
                    details.append(f"formulario={form.get('method', 'GET')} {form.get('action', '/')}")
                messages = page_state.get("visibleMessages") or []
                if messages:
                    details.append("pagina_msg=" + " | ".join(str(message) for message in messages))
                trace = page_state.get("trace") or []
                if trace:
                    details.append(
                        "eventos=" + ", ".join(
                            f"{item.get('kind')}:{item.get('method', '')} {item.get('path', '/')} {item.get('status', '')}".strip()
                            for item in trace
                        )
                    )
        except Exception as error:
            details.append(f"dom={type(error).__name__}")

        try:
            requests: dict[str, str] = {}
            responses: list[str] = []
            for entry in self.driver.get_log("performance"):
                message = json.loads(entry["message"])["message"]
                method = message.get("method")
                params = message.get("params") or {}
                if method == "Network.requestWillBeSent":
                    request = params.get("request") or {}
                    requests[params.get("requestId", "")] = (
                        f"{request.get('method', 'GET')} {_safe_url_path(str(request.get('url', '')))}"
                    )
                elif method == "Network.responseReceived":
                    response = params.get("response") or {}
                    request_id = params.get("requestId", "")
                    request = requests.get(request_id, _safe_url_path(str(response.get("url", ""))))
                    responses.append(f"{request} -> {response.get('status', '?')}")
            if responses:
                details.append("rede=" + ", ".join(responses[-12:]))
        except Exception as error:
            details.append(f"rede={type(error).__name__}")

        try:
            errors = [
                str(entry.get("message", "")).replace("\n", " ")[:180]
                for entry in self.driver.get_log("browser")
                if str(entry.get("level", "")).upper() in {"SEVERE", "ERROR"}
            ]
            if errors:
                details.append("console=" + " | ".join(errors[-4:]))
        except Exception as error:
            details.append(f"console={type(error).__name__}")
        return "; ".join(details) if details else None

    def _sendMessage(self, username: str, send_url: str) -> tuple[str, str | None, int | None]:
        self._check_stop()
        driver = self.driver
        main_tab = self.main_tab_handle if self.main_tab_handle in driver.window_handles else driver.current_window_handle
        self.main_tab_handle = main_tab
        message_tab = self._ensure_message_tab()
        driver.switch_to.window(message_tab)
        last_error: Exception | None = None
        for attempt in range(1, MESSAGE_PREPARE_ATTEMPTS + 1):
            try:
                driver.get(send_url)
            except TimeoutException as error:
                last_error = error
                try:
                    driver.execute_script("window.stop();")
                except Exception:
                    pass
            try:
                text = self._find_or_wait(
                    By.ID,
                    "js_msgTextConfirm",
                    timeout=MESSAGE_TEXTAREA_TIMEOUT_SECONDS,
                )
                text.send_keys(self.account.message or DEFAULT_MESSAGE)
                self._sleep(SHORT_WAIT_SECONDS)
                try:
                    driver.execute_script(
                        """
                        document.querySelector('#sendIKMessage').scrollTop =
                        document.querySelector('#sendIKMessage').scrollHeight;
                        """
                    )
                except Exception:
                    pass
                self._sleep(MESSAGE_SCROLL_SETTLE_SECONDS)
                submit_button = self._find_or_wait(
                    By.ID,
                    "js_messageSubmitButton",
                    timeout=MESSAGE_SUBMIT_TIMEOUT_SECONDS,
                    clickable=True,
                )
                break
            except Exception as error:
                last_error = error
                if attempt < MESSAGE_PREPARE_ATTEMPTS:
                    self._sleep(2)
                    continue
                raise last_error
        if self.dry_run:
            self._return_to_main_tab(main_tab, message_tab)
            return "success", f"SIMULACAO: mensagem preparada e botao enviar localizado para {username}", None
        self._install_send_trace()
        if _send_diagnostics_enabled():
            try:
                self.driver.get_log("performance")
                self.driver.get_log("browser")
            except Exception:
                pass
        try:
            submit_button.click()
            self._sleep(CLICK_DELAY_SECONDS)
        except Exception:
            self._click(submit_button)
        self._return_to_main_tab(main_tab, message_tab)
        self._sleep(getattr(self, "postSendWait", 1.0))
        return "success", "Mensagem enviada; seguindo sem verificacao posterior.", None

        if _send_diagnostics_enabled():
            self._sleep(0.75)

        success_xpaths = [
            "//*[contains(normalize-space(text()), 'Your order has been carried out')]",
            "//*[contains(normalize-space(text()), 'Sua ordem foi executada')]",
            "//*[contains(normalize-space(text()), 'ordem foi executada')]",
            "//*[contains(normalize-space(text()), 'Emriniz yerine getirildi')]",
            "//*[contains(normalize-space(text()), 'Emrin yerine getirildi')]",
            "//*[contains(text(), 'Mesaj gönderildi')]",
            "//*[contains(text(), 'Mesaj gonderildi')]",
            "//*[contains(translate(normalize-space(text()), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'your order has been carried out')]",
            "//*[contains(translate(normalize-space(text()), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sua ordem foi executada')]",
            "//*[contains(translate(normalize-space(text()), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'ordem foi executada')]",
        ]
        ignore_xpaths = [
            "//*[contains(text(), 'You are on this player`s ignore list.')]",
            "//*[contains(text(), 'ignore list')]",
            "//*[contains(text(), 'lista de ignorados')]",
        ]
        diagnostic = self._read_send_diagnostics()
        feedback = self._find_feedback(success_xpaths, timeout=SEND_FEEDBACK_TIMEOUT_SECONDS)
        if feedback or self._page_has_outbox_sent(username):
            self._return_to_main_tab(main_tab, message_tab)
            detail = feedback or "Mensagem enviada com sucesso."
            return "success", f"{detail} Diagnostico: {diagnostic}" if diagnostic else detail, None
        feedback = self._find_feedback(ignore_xpaths, timeout=IGNORE_FEEDBACK_TIMEOUT_SECONDS)
        if feedback:
            self._return_to_main_tab(main_tab, message_tab)
            return "ignore list", feedback, None
        cooldown_text = self._detect_cooldown_message(timeout=3.0, check_handles=[message_tab, main_tab])
        if cooldown_text:
            if self._is_success_text(cooldown_text):
                self._return_to_main_tab(main_tab, message_tab)
                return "success", "Mensagem enviada com sucesso.", None
            if self._is_cooldown_text(cooldown_text):
                self._return_to_main_tab(main_tab, message_tab)
                return "cooldown", cooldown_text, self._extract_cooldown_seconds(cooldown_text)
        self._return_to_main_tab(main_tab, message_tab)
        if diagnostic:
            return "allowed", f"Mensagem enviada (status nao confirmado). Diagnostico: {diagnostic}", None
        return "allowed", "Mensagem enviada (status nao confirmado).", None

    def _find_feedback(self, xpaths: list[str], timeout: float) -> str | None:
        combined_xpath = " | ".join(f"({xpath})" for xpath in xpaths if xpath)
        if not combined_xpath:
            return None
        try:
            element = self._wait_for(EC.presence_of_element_located((By.XPATH, combined_xpath)), timeout=timeout, poll=0.2)
            if hasattr(element, "is_displayed") and not element.is_displayed():
                return None
            text = getattr(element, "text", "").strip()
            if text:
                return text
        except TimeoutException:
            return None
        return None

    def _page_has_success(self) -> bool:
        try:
            page = self.driver.page_source.lower()
        except Exception:
            return False
        return self._is_success_text(page)

    def _page_has_outbox_sent(self, username: str | None = None) -> bool:
        try:
            page = self.driver.page_source
        except Exception:
            return False
        return self._is_outbox_text(page, username=username)

    def _return_to_main_tab(self, main_tab: str, message_tab: str) -> None:
        try:
            if main_tab in self.driver.window_handles:
                self.driver.switch_to.window(main_tab)
        except Exception:
            if self.driver.window_handles:
                self.driver.switch_to.window(self.driver.window_handles[0])

    def _detect_cooldown_message(self, timeout: float = 3.0, check_handles: list[str] | None = None) -> str | None:
        handles = check_handles or list(self.driver.window_handles)
        current = self.driver.current_window_handle
        for handle in handles:
            try:
                if handle in self.driver.window_handles:
                    self.driver.switch_to.window(handle)
                    text = self._detect_cooldown_message_in_context(timeout=timeout)
                    if text:
                        return text
            except Exception:
                continue
        if current in self.driver.window_handles:
            self.driver.switch_to.window(current)
        return None

    def _detect_cooldown_message_in_context(self, timeout: float = 3.0) -> str | None:
        cooldown_xpaths = [
            "//*[contains(text(), 'Tem de esperar')]",
            "//*[contains(text(), 'tem de esperar')]",
            "//*[contains(text(), 'Tem que esperar')]",
            "//*[contains(text(), 'tem que esperar')]",
            "//*[contains(text(), 'Precisa esperar')]",
            "//*[contains(text(), 'precisa esperar')]",
            "//*[contains(text(), 'Aguarde')]",
            "//*[contains(text(), 'aguarde')]",
            "//*[contains(text(), 'antes de enviar')]",
            "//*[contains(text(), 'antes de poder enviar')]",
            "//*[contains(text(), 'enviar mensagens novamente')]",
            "//*[contains(text(), 'mensagens novamente')]",
            "//*[contains(text(), 'You can send')]",
            "//*[contains(text(), 'You have to wait')]",
            "//*[contains(text(), 'You must wait')]",
            "//*[contains(text(), 'before you can send')]",
            "//*[contains(text(), 'cooldown')]",
        ]
        for xpath in cooldown_xpaths:
            try:
                elements = self.driver.find_elements(By.XPATH, xpath)
                for element in elements:
                    text = getattr(element, "text", "").strip()
                    if self._is_cooldown_text(text) or self._is_success_text(text):
                        return text
            except Exception:
                continue
        end = time.time() + timeout
        while time.time() < end:
            try:
                page = self.driver.page_source
                if self._is_cooldown_text(page) or self._is_success_text(page):
                    return "Cooldown detectado pelo site."
            except Exception:
                pass
            sleep(0.2)
        return None

    @staticmethod
    def _is_cooldown_text(text: str | None) -> bool:
        if not text:
            return False
        lower = text.lower()
        exact_markers = (
            "tem de esperar",
            "tem que esperar",
            "precisa esperar",
            "aguarde",
            "cooldown",
            "have to wait",
            "you have to wait",
            "you must wait",
            "before you can send",
            "enviar mensagens novamente",
            "mensagens novamente",
        )
        if any(marker in lower for marker in exact_markers):
            return True
        return bool(re.search(r"\b\d+\s*(segundos?|seconds?|minutos?|minutes?)\b", lower))

    @staticmethod
    def _extract_cooldown_seconds(message: str | None) -> int:
        if not message:
            return 60
        lower = message.lower()
        minute_match = re.search(r"(\d+)\s*(minuto|minutos|minute|minutes)", lower)
        if minute_match:
            return int(minute_match.group(1)) * 60
        second_match = re.search(r"(\d+)\s*(segundo|segundos|second|seconds)", lower)
        if second_match:
            return int(second_match.group(1))
        return 60

    def _stop(self) -> None:
        self.stop_event.set()
        self.close()

    def close(self) -> None:
        self.running = False
        self._stop_activation_listener()
        try:
            self.driver.quit()
        except Exception:
            pass
        if self._profile_dir is not None:
            _terminate_chrome_profile_processes(self._profile_dir)
        self._cleanup_temp_profile_dir()

    def _cleanup_temp_profile_dir(self) -> None:
        if not self._persistent_profile:
            _cleanup_chrome_profile(self._temp_profile_dir)
