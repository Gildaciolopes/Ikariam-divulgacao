from types import SimpleNamespace

from selenium.webdriver.common.by import By

import src.start as start_module
from src.start import BotDriver


class FakeServer:
    def __init__(self, users=0, message_send=0):
        self.id = "server-1"
        self.server = "Pangaia 1"
        self.flag = ""
        self.users = users
        self.messageSend = message_send

    def save(self):
        return self

    @property
    def display_name(self):
        return f"{self.flag} / {self.server}" if self.flag else self.server


class FakeLink:
    def __init__(self, text="Player One", title="Player One", href="https://example.test/message"):
        self.text = text
        self._title = title
        self._href = href

    def get_attribute(self, name):
        if name == "title":
            return self._title
        if name == "href":
            return self._href
        return None


class FakeNameCell:
    def find_elements(self, by, selector):
        assert by == By.XPATH
        assert selector == ".//a"
        return [FakeLink()]

    def find_element(self, by, selector):
        assert by == By.XPATH
        assert selector == ".//div/a"
        return FakeLink()


class FakeActionCell:
    def find_elements(self, by, selector):
        assert by == By.XPATH
        assert "ajaxHandlerCall" in selector
        return [FakeLink()]

    def find_element(self, by, selector):
        assert by == By.XPATH
        assert "ajaxHandlerCall" in selector
        return FakeLink()


class FakeRow:
    def find_element(self, by, selector):
        assert by == By.CLASS_NAME
        if selector == "name":
            return FakeNameCell()
        if selector == "action":
            return FakeActionCell()
        raise AssertionError(selector)


class FakeLogs:
    def __init__(self):
        self.lines = []
        self.progress = None

    def addLogs(self, text, level="info"):
        self.lines.append(text)

    def set_progress(self, data):
        self.progress = data


class FakeMessageDriver:
    def __init__(self):
        self.window_handles = ["main", "message"]
        self.current_window_handle = "main"
        self.visited = []
        self.scripts = []

    def switch_to_window(self, handle):
        self.current_window_handle = handle

    @property
    def switch_to(self):
        return SimpleNamespace(window=self.switch_to_window)

    def get(self, url):
        self.visited.append(url)

    def execute_script(self, script, *args):
        self.scripts.append(script)


class FakeTextArea:
    def __init__(self):
        self.sent_keys = []

    def send_keys(self, value):
        self.sent_keys.append(value)


class FakeSubmitButton:
    clicked = False

    def click(self):
        self.clicked = True


def make_bot(users=0):
    bot = BotDriver.__new__(BotDriver)
    bot.serverGlobal = FakeServer(users=users)
    bot.account = SimpleNamespace(id="account-1")
    bot._server_cycle_send_count = 0
    bot.messageSendCount = 0
    bot.totalSentSession = 0
    bot.dry_run = True
    bot._last_send_attempt_at = 0
    bot.timeWait = 0
    bot._find_all_or_wait = lambda *args, **kwargs: [object(), FakeRow()]
    bot._wait_if_paused = lambda: None
    bot._is_stopped = lambda: False
    bot._wait_before_next_send = lambda: None
    bot._sleep = lambda seconds: None
    bot._sendMessage = lambda username, send_url: ("success", None, None)
    return bot


def test_send_message_dry_run_prepares_message_and_does_not_click_submit():
    bot = BotDriver.__new__(BotDriver)
    driver = FakeMessageDriver()
    text_area = FakeTextArea()
    submit_button = FakeSubmitButton()
    clicked = []
    returned = []

    def find_or_wait(by, selector, **kwargs):
        if selector == "js_msgTextConfirm":
            return text_area
        if selector == "js_messageSubmitButton":
            return submit_button
        raise AssertionError(selector)

    bot.driver = driver
    bot.account = SimpleNamespace(message="Texto de teste")
    bot.main_tab_handle = "main"
    bot.message_tab_handle = "message"
    bot.dry_run = True
    bot._check_stop = lambda: None
    bot._ensure_message_tab = lambda: "message"
    bot._find_or_wait = find_or_wait
    bot._sleep = lambda seconds: None
    bot._click = lambda element: clicked.append(element)
    bot._return_to_main_tab = lambda main_tab, message_tab: returned.append((main_tab, message_tab))

    status, feedback, cooldown = bot._sendMessage("Player One", "https://example.test/message")

    assert status == "success"
    assert cooldown is None
    assert "SIMULACAO" in feedback
    assert driver.visited == ["https://example.test/message"]
    assert text_area.sent_keys == ["Texto de teste"]
    assert clicked == []
    assert submit_button.clicked is False
    assert returned == [("main", "message")]


def test_real_send_returns_immediately_after_click_without_post_checks():
    bot = BotDriver.__new__(BotDriver)
    driver = FakeMessageDriver()
    text_area = FakeTextArea()
    submit_button = FakeSubmitButton()
    returned = []

    def find_or_wait(by, selector, **kwargs):
        if selector == "js_msgTextConfirm":
            return text_area
        if selector == "js_messageSubmitButton":
            return submit_button
        raise AssertionError(selector)

    bot.driver = driver
    bot.account = SimpleNamespace(message="Texto de teste")
    bot.main_tab_handle = "main"
    bot.message_tab_handle = "message"
    bot.dry_run = False
    bot._check_stop = lambda: None
    bot._ensure_message_tab = lambda: "message"
    bot._find_or_wait = find_or_wait
    bot._sleep = lambda seconds: None
    bot._install_send_trace = lambda: None
    bot._return_to_main_tab = lambda main_tab, message_tab: returned.append((main_tab, message_tab))
    bot._find_feedback = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("nao deve verificar feedback"))
    bot._detect_cooldown_message = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("nao deve verificar cooldown"))
    bot._page_has_outbox_sent = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("nao deve verificar Outbox"))

    status, feedback, cooldown = bot._sendMessage("Player One", "https://example.test/message")

    assert status == "success"
    assert feedback == "Mensagem enviada; seguindo sem verificacao posterior."
    assert cooldown is None
    assert submit_button.clicked is True
    assert returned == [("main", "message")]


def test_real_send_waits_configured_decimal_after_click():
    bot = BotDriver.__new__(BotDriver)
    driver = FakeMessageDriver()
    sleeps = []
    bot.driver = driver
    bot.account = SimpleNamespace(message="Texto de teste")
    bot.main_tab_handle = "main"
    bot.message_tab_handle = "message"
    bot.dry_run = False
    bot.postSendWait = 0.1
    bot._check_stop = lambda: None
    bot._ensure_message_tab = lambda: "message"
    bot._find_or_wait = lambda by, selector, **kwargs: FakeTextArea() if selector == "js_msgTextConfirm" else FakeSubmitButton()
    bot._sleep = lambda seconds: sleeps.append(seconds)
    bot._install_send_trace = lambda: None
    bot._return_to_main_tab = lambda main_tab, message_tab: None

    bot._sendMessage("Player One", "https://example.test/message")

    assert sleeps[-1] == 0.1


def test_unknown_server_total_does_not_complete_before_capture(monkeypatch):
    reservations = []

    def reserve(**kwargs):
        reservations.append(kwargs)
        return SimpleNamespace(id_str="reservation-1")

    monkeypatch.setattr(start_module.UsersSend, "reserve", reserve)
    monkeypatch.setattr(start_module.UsersSend, "update_status", lambda *args, **kwargs: None)

    logs = FakeLogs()
    completed, sent_any, batch_exhausted = make_bot(users=0)._captureusers(logs)

    assert completed is False
    assert sent_any is True
    assert batch_exhausted is False
    assert reservations == [
        {
            "server_id": "server-1",
            "username": "Player One",
            "account_id": "account-1",
        }
    ]
    assert logs.progress["sent"] == 1
    assert logs.progress["total"] is None


def test_positive_server_total_still_finishes_at_limit():
    bot = make_bot(users=1)

    completed, sent_any, batch_exhausted = bot._captureusers(FakeLogs())

    assert completed is True
    assert sent_any is False
    assert batch_exhausted is False


def test_turkish_outbox_with_recipient_is_success_evidence():
    page = """
    <section>
      <h3>Giden kutusu</h3>
      <table>
          <tr><th>Alici</th><th>Konu</th></tr>
        <tr><td>William Wallace</td><td>Mesaj</td></tr>
      </table>
    </section>
    """

    assert BotDriver._is_outbox_text(page, "William Wallace") is True
    assert BotDriver._is_outbox_text(page, "Outro Jogador") is False


def test_outbox_text_requires_grid_or_explicit_empty_state():
    assert BotDriver._is_outbox_text("Outbox") is False
    assert BotDriver._is_outbox_text("Outbox Destinatario Assunto Fecha") is True
    assert BotDriver._is_outbox_text("Outbox vazia") is True


def test_activation_login_returns_only_after_lobby_confirmation():
    bot = BotDriver.__new__(BotDriver)
    bot.logs = FakeLogs()
    bot.listener = None
    bot.running = False
    bot.activation_cancelled = False
    bot._start_activation_listener = lambda logs: None
    bot._stop_activation_listener = lambda: None
    bot._login_to_lobby = lambda logs, wait_after_submit=False: True
    bot._install_activation_page_hotkey = lambda: None
    bot._is_activation_page_hotkey_requested = lambda: False
    bot._lobby_session_confirmed = lambda: True

    assert bot.login() is True


def test_find_feedback_uses_one_combined_wait(monkeypatch):
    calls = []
    bot = BotDriver.__new__(BotDriver)

    def fake_presence(locator):
        return locator

    def fake_wait(condition, timeout, poll):
        calls.append((condition, timeout, poll))
        return SimpleNamespace(text="Mensagem enviada")

    monkeypatch.setattr(start_module.EC, "presence_of_element_located", fake_presence)
    bot._wait_for = fake_wait

    feedback = bot._find_feedback(["//div[contains(., 'ok')]", "//span[contains(., 'ok')]"], timeout=0.8)

    assert feedback == "Mensagem enviada"
    assert len(calls) == 1
    assert calls[0][0][0] == By.XPATH
    assert "//div[contains(., 'ok')]" in calls[0][0][1]
    assert "//span[contains(., 'ok')]" in calls[0][0][1]
    assert calls[0][1] == 0.8


def test_message_preparation_fixed_waits_are_short():
    assert start_module.MESSAGE_TEXT_SETTLE_SECONDS <= 0.2
    assert start_module.MESSAGE_SCROLL_SETTLE_SECONDS <= 0.2
    assert start_module.SEND_FEEDBACK_TIMEOUT_SECONDS <= 1.0


def test_next_message_waits_only_the_remaining_configured_interval(monkeypatch):
    bot = BotDriver.__new__(BotDriver)
    bot.timeWait = 1.0
    bot._last_send_attempt_at = 100.0
    sleeps = []
    bot._sleep = lambda seconds: sleeps.append(seconds)
    monkeypatch.setattr(start_module.time, "time", lambda: 100.25)

    bot._wait_before_next_send()

    assert sleeps == [0.75]


def test_server_send_batch_limit_can_be_reduced_for_controlled_test(monkeypatch):
    monkeypatch.setenv("BOT_SERVER_SEND_BATCH_LIMIT", "1")
    assert start_module._server_send_batch_limit() == 1


def test_server_send_batch_limit_rejects_invalid_or_unsafe_values(monkeypatch):
    monkeypatch.setenv("BOT_SERVER_SEND_BATCH_LIMIT", "invalid")
    assert start_module._server_send_batch_limit() == start_module.SERVER_SEND_BATCH_LIMIT
    monkeypatch.setenv("BOT_SERVER_SEND_BATCH_LIMIT", "0")
    assert start_module._server_send_batch_limit() == 1
    monkeypatch.setenv("BOT_SERVER_SEND_BATCH_LIMIT", "999")
    assert start_module._server_send_batch_limit() == start_module.SERVER_SEND_BATCH_LIMIT


def test_allowed_send_is_recorded_without_outbox_confirmation(monkeypatch):
    reservations = []
    statuses = []

    def reserve(**kwargs):
        reservations.append(kwargs)
        return SimpleNamespace(id_str="reservation-1")

    def update_status(reservation_id, status):
        statuses.append((reservation_id, status))

    monkeypatch.setattr(start_module.UsersSend, "reserve", reserve)
    monkeypatch.setattr(start_module.UsersSend, "update_status", update_status)

    bot = make_bot(users=0)
    bot.dry_run = False
    sleeps = []
    bot._sleep = lambda seconds: sleeps.append(seconds)
    bot._sendMessage = lambda username, send_url: ("allowed", "Mensagem enviada (status nao confirmado).", None)

    logs = FakeLogs()
    completed, sent_any, batch_exhausted = bot._captureusers(logs)

    assert completed is False
    assert sent_any is True
    assert batch_exhausted is False
    assert statuses == [("reservation-1", "sent")]
    assert bot.serverGlobal.messageSend == 1
    assert bot.totalSentSession == 1
    assert "Enviado para Player One - Total: 1" in logs.lines
    assert sleeps == []


def test_game_success_does_not_consult_outbox_or_retry(monkeypatch):
    statuses = []

    monkeypatch.setattr(start_module.UsersSend, "reserve", lambda **kwargs: SimpleNamespace(id_str="reservation-1"))
    monkeypatch.setattr(start_module.UsersSend, "update_status", lambda reservation_id, status: statuses.append((reservation_id, status)))

    bot = make_bot(users=700)
    bot.dry_run = False
    logs = FakeLogs()

    completed, sent_any, batch_exhausted = bot._captureusers(logs)

    assert completed is False
    assert sent_any is True
    assert batch_exhausted is False
    assert statuses == [("reservation-1", "sent")]
    assert bot.serverGlobal.messageSend == 1
    assert bot.totalSentSession == 1
    assert "Enviado para Player One - Total: 1" in logs.lines


def test_game_success_is_counted_without_outbox(monkeypatch):
    statuses = []

    monkeypatch.setattr(start_module.UsersSend, "reserve", lambda **kwargs: SimpleNamespace(id_str="reservation-1"))
    monkeypatch.setattr(start_module.UsersSend, "update_status", lambda reservation_id, status: statuses.append((reservation_id, status)))

    bot = make_bot(users=700)
    bot.dry_run = False
    logs = FakeLogs()

    completed, sent_any, batch_exhausted = bot._captureusers(logs)

    assert completed is False
    assert sent_any is True
    assert batch_exhausted is False
    assert statuses == [("reservation-1", "sent")]
    assert bot.serverGlobal.messageSend == 1
    assert bot.totalSentSession == 1
    assert "Enviado para Player One - Total: 1" in logs.lines


def test_existing_failed_record_is_skipped_without_visible_player_log(monkeypatch):
    monkeypatch.setattr(start_module.UsersSend, "reserve", lambda **kwargs: None)
    monkeypatch.setattr(start_module.UsersSend, "status_for", lambda **kwargs: "failed")
    bot = make_bot(users=700)
    bot._sendMessage = lambda **kwargs: (_ for _ in ()).throw(AssertionError("nao deve reenviar"))
    logs = FakeLogs()

    bot._captureusers(logs)

    assert not any(line.startswith("Pulando jogador ") for line in logs.lines)


class FakeSwitch:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        self.driver.current_window_handle = handle


class FakeDriverForTabs:
    def __init__(self):
        self.window_handles = ["main", "message", "popup"]
        self.current_window_handle = "message"
        self.closed = []
        self.switch_to = FakeSwitch(self)

    def close(self):
        self.closed.append(self.current_window_handle)
        self.window_handles.remove(self.current_window_handle)


def test_return_to_main_tab_does_not_close_tabs_each_send():
    bot = BotDriver.__new__(BotDriver)
    bot.driver = FakeDriverForTabs()

    bot._return_to_main_tab("main", "message")

    assert bot.driver.current_window_handle == "main"
    assert bot.driver.closed == []
    assert bot.driver.window_handles == ["main", "message", "popup"]


def test_highscore_total_is_recovered_from_range_options():
    assert BotDriver._highscore_total_from_options(["Own position", "1 - 50", "51 - 100", "1601 - 1664"]) == 1664
    assert BotDriver._highscore_total_from_options(["Own position"]) is None


def test_highscore_resume_starts_at_range_containing_next_position():
    options = ["1 - 50", "51 - 100", "101 - 150", "151 - 200"]

    assert BotDriver._resume_highscore_options(options, 0) == options
    assert BotDriver._resume_highscore_options(options, 25) == options
    assert BotDriver._resume_highscore_options(options, 50) == options[1:]
    assert BotDriver._resume_highscore_options(options, 75) == options[1:]
    assert BotDriver._resume_highscore_options(options, 100) == options[2:]
    assert BotDriver._resume_highscore_options(options, 200) == []


def test_server_flow_syncs_outbox_once_before_resuming_highscore():
    bot = BotDriver.__new__(BotDriver)
    logs = FakeLogs()
    events = []

    class OneTabDriver:
        window_handles = ["main"]

        @property
        def switch_to(self):
            return SimpleNamespace(window=lambda handle: None)

    bot.driver = OneTabDriver()
    bot.serverGlobal = FakeServer(users=200, message_send=0)
    bot.main_tab_handle = "main"
    bot.message_tab_handle = None
    bot.pause_event = SimpleNamespace(is_set=lambda: False)
    bot.last_activity_at = 0
    bot._server_cycle_send_count = 0
    bot.totalSentSession = 0
    bot._check_stop = lambda: None
    bot._is_stopped = lambda: False
    bot._close_extra_windows = lambda keep: None
    bot._click_game_start_if_present = lambda logger: None

    def sync_outbox(logger):
        events.append("outbox")
        bot.serverGlobal.messageSend = 50
        return 50

    bot._sync_outbox_sent_users = sync_outbox
    bot._open_highscore_page = lambda: events.append("highscore") or object()
    bot._sleep = lambda seconds: None
    bot._get_highscore_options = lambda attempts=1, delay=0.8, reopen=None: ["1 - 50", "51 - 100", "101 - 150"]
    bot._update_server_users_count = lambda users_count, logger: None
    bot._select_highscore_offset = lambda option, force_open=False, textLogs=None: events.append(option) or True
    bot._captureusers = lambda textLogs=None: (False, True, True)

    completed, sent_any, batch_exhausted = bot._run_current_server_flow(logs)

    assert events[:3] == ["outbox", "highscore", "51 - 100"]
    assert completed is False
    assert sent_any is True
    assert batch_exhausted is True


def test_server_user_count_update_refreshes_progress():
    bot = make_bot(users=0)
    logs = FakeLogs()

    bot._update_server_users_count(1664, logs)

    assert bot.serverGlobal.users == 1664
    assert logs.progress["total"] == 1664
    assert logs.progress["remaining"] == 1664
    assert logs.progress["percent"] == 0.0


def test_server_user_count_update_does_not_downgrade_hub_total_to_visible_dropdown_cap():
    bot = make_bot(users=1664)
    logs = FakeLogs()

    bot._update_server_users_count(700, logs)

    assert bot.serverGlobal.users == 1664
    assert logs.progress["total"] == 1664


class FakeHighscoreOptionLink:
    def __init__(self, text):
        self.text = text


class FakeHighscoreDropdown:
    def __init__(self):
        self.page = 0
        self.pages = [
            ["Own position", "1 - 50", "51 - 100", "101 - 150"],
            ["151 - 200", "201 - 250", "251 - 300"],
            ["301 - 350", "351 - 400", "401 - 450"],
            ["451 - 500", "501 - 550", "551 - 600"],
            ["601 - 650", "651 - 700", "701 - 750"],
            ["751 - 800", "801 - 850", "851 - 900"],
        ]

    def find_elements(self, by, selector):
        assert by == By.XPATH
        assert selector == ".//li//a"
        return [FakeHighscoreOptionLink(text) for text in self.pages[self.page]]


class FakeHighscoreDriver:
    def execute_script(self, script, container):
        if "return [best.scrollTop" in script:
            return [container.page * 100, 1000, 100]
        if "best.scrollTop = 0" in script:
            container.page = 0
            return None
        if "best.scrollTop = Math.min" in script:
            if container.page >= len(container.pages) - 1:
                return container.page * 100
            container.page += 1
            return container.page * 100
        raise AssertionError(script)


def test_highscore_options_are_collected_across_dropdown_scroll_pages():
    bot = BotDriver.__new__(BotDriver)
    bot.driver = FakeHighscoreDriver()
    bot._check_stop = lambda: None
    bot._sleep = lambda seconds: None
    dropdown = FakeHighscoreDropdown()
    dropdown.page = 4

    options = bot._normalize_highscore_options(
        bot._collect_highscore_option_texts(dropdown, max_scrolls=20)
    )

    assert options[0] == "1 - 50"
    assert options[-1] == "851 - 900"
    assert BotDriver._highscore_total_from_options(options) == 900


def test_highscore_filter_must_confirm_before_logging_selection():
    bot = BotDriver.__new__(BotDriver)
    logs = FakeLogs()
    clicked = []

    bot._check_stop = lambda: None
    bot._open_highscore_dropdown = lambda: True
    bot._find_highscore_option_link = lambda option: object()
    bot._click = lambda element: clicked.append(element)
    bot._wait_for = lambda *args, **kwargs: (_ for _ in ()).throw(start_module.TimeoutException("not confirmed"))

    assert bot._select_highscore_offset("51 - 100", force_open=True, textLogs=logs) is False
    assert clicked
    assert "Filtro selecionado: 51 - 100" not in logs.lines
    assert "Filtro nao confirmou selecao: 51 - 100" in logs.lines


def test_highscore_filter_does_not_confirm_from_open_dropdown_text():
    bot = BotDriver.__new__(BotDriver)

    class DropdownOnlyDriver:
        def execute_script(self, script):
            assert "#dropDown_js_highscoreOffsetContainer" not in script
            return "1 - 50"

    bot.driver = DropdownOnlyDriver()

    assert bot._highscore_offset_visible("51 - 100") is False


def test_current_server_flow_logs_filter_without_new_users():
    bot = BotDriver.__new__(BotDriver)
    logs = FakeLogs()

    class OneTabDriver:
        window_handles = ["main"]

        @property
        def switch_to(self):
            return SimpleNamespace(window=lambda handle: None)

    bot.driver = OneTabDriver()
    bot.main_tab_handle = "main"
    bot.message_tab_handle = None
    bot.pause_event = SimpleNamespace(is_set=lambda: False)
    bot.last_activity_at = 0
    bot._check_stop = lambda: None
    bot._is_stopped = lambda: False
    bot._close_extra_windows = lambda keep: None
    bot._click_game_start_if_present = lambda logger: None
    bot._sync_outbox_sent_users = lambda logger: 0
    bot._open_highscore_page = lambda: object()
    bot._sleep = lambda seconds: None
    bot._get_highscore_options = lambda attempts=1, delay=0.8, reopen=None: ["1 - 50"]
    bot._update_server_users_count = lambda users_count, logger: None
    bot._select_highscore_offset = lambda option, force_open=False, textLogs=None: True
    bot._captureusers = lambda textLogs=None: (False, False, False)

    completed, sent_any, batch_exhausted = bot._run_current_server_flow(logs)

    assert completed is True
    assert sent_any is False
    assert batch_exhausted is False
    assert "Filtro sem destinatarios reconhecidos: 0 linhas lidas, 0 linhas invalidas. Avancando para a proxima faixa." in logs.lines


def test_current_server_flow_does_not_advance_when_targets_lack_confirmation():
    bot = BotDriver.__new__(BotDriver)
    logs = FakeLogs()

    class OneTabDriver:
        window_handles = ["main"]

        @property
        def switch_to(self):
            return SimpleNamespace(window=lambda handle: None)

    bot.driver = OneTabDriver()
    bot.main_tab_handle = "main"
    bot.message_tab_handle = None
    bot.pause_event = SimpleNamespace(is_set=lambda: False)
    bot.last_activity_at = 0
    bot._last_capture_state = {"targets": 1}
    bot._check_stop = lambda: None
    bot._is_stopped = lambda: False
    bot._close_extra_windows = lambda keep: None
    bot._click_game_start_if_present = lambda logger: None
    bot._sync_outbox_sent_users = lambda logger: 0
    bot._open_highscore_page = lambda: object()
    bot._sleep = lambda seconds: None
    bot._get_highscore_options = lambda attempts=1, delay=0.8, reopen=None: ["1 - 50", "51 - 100"]
    bot._update_server_users_count = lambda users_count, logger: None
    bot._select_highscore_offset = lambda option, force_open=False, textLogs=None: True
    bot._captureusers = lambda textLogs=None: (False, False, False)

    try:
        bot._run_current_server_flow(logs)
        assert False, "a faixa nao pode avancar sem envio confirmado"
    except start_module.TimeoutException:
        pass

    assert "Filtro bloqueado: 1 jogadores reconhecidos, 0 reservas novas e 0 ja registrados. Nenhum envio foi concluido; nao avancando para a proxima faixa." in logs.lines


def test_capture_silently_skips_player_already_confirmed(monkeypatch):
    monkeypatch.setattr(start_module.UsersSend, "reserve", lambda **kwargs: None)
    monkeypatch.setattr(start_module.UsersSend, "status_for", lambda **kwargs: "sent")

    bot = make_bot(users=700)
    logs = FakeLogs()

    completed, sent_any, batch_exhausted = bot._captureusers(logs)

    assert completed is False
    assert sent_any is False
    assert batch_exhausted is False
    assert not any(line.startswith("Pulando jogador ") for line in logs.lines)
    assert bot._last_capture_state["skipped_confirmed"] == 1


def test_banned_transparent_and_asphodel_server_cards_are_ignored():
    assert BotDriver._is_ignored_server_card("Asphodel 586", "Sua conta está banida até 2038 Razão: Account Transfer")
    assert BotDriver._is_ignored_server_card("Pangaia 2", "Sua conta está banida até 2038 Razão: Violation of Terms and Conditions")
    assert BotDriver._is_ignored_server_card("Pangaia 1", "Sua conta está banida até 2038 Razão: SPAM")
    assert BotDriver._is_ignored_server_card("Servidor Transparente", "Transparente")
    assert not BotDriver._is_ignored_server_card("Cyclops", "Cyclops Brasil Online 138 Jogador Adonis-9544 Rank 4082")


def test_server_identity_includes_country_flag_from_lobby_card():
    class FakeFlagElement:
        def get_attribute(self, name):
            assert name == "class"
            return "flag flag-br"

    class FakeCard:
        def find_element(self, by, selector):
            assert by == By.CSS_SELECTOR
            assert selector == "span.flag"
            return FakeFlagElement()

    flag = BotDriver._extract_server_flag(FakeCard())

    assert flag == "BR"
    assert BotDriver._server_display_name("Themis", flag) == "BR / Themis"
    assert BotDriver._server_identity_key("Themis", "BR") != BotDriver._server_identity_key("Themis", "GR")


def test_server_identity_reads_live_lobby_flag_class():
    class FakeFlagElement:
        def get_attribute(self, name):
            assert name == "class"
            return "flag-en flag-s1"

    class FakeCard:
        def find_element(self, by, selector):
            assert by == By.CSS_SELECTOR
            if selector == "span.flag":
                raise AssertionError("live lobby has no generic span.flag")
            assert selector == "span[class*='flag-']"
            return FakeFlagElement()

    assert BotDriver._extract_server_flag(FakeCard()) == "EN"


class FakeLobbyButton:
    def __init__(self, *, enabled=True, disabled=None, aria_disabled=None, class_name="btn btn-primary"):
        self._enabled = enabled
        self._attrs = {
            "disabled": disabled,
            "aria-disabled": aria_disabled,
            "class": class_name,
        }

    def is_enabled(self):
        return self._enabled

    def get_attribute(self, name):
        return self._attrs.get(name)


def test_lobby_button_must_be_really_enabled():
    assert BotDriver._is_enabled_lobby_button(FakeLobbyButton()) is True
    assert BotDriver._is_enabled_lobby_button(FakeLobbyButton(enabled=False)) is False


def test_switch_to_game_tab_ignores_chrome_new_tab_until_game_url_exists():
    class SwitchTo:
        def __init__(self, driver):
            self.driver = driver

        def window(self, handle):
            self.driver.current_handle = handle

    class Driver:
        window_handles = ["lobby", "game", "new-tab"]

        def __init__(self):
            self.current_handle = "lobby"
            self.switch_to = SwitchTo(self)
            self.urls = {
                "lobby": "https://lobby.ikariam.gameforge.com/pt_BR/accounts",
                "game": "https://s68-en.ikariam.gameforge.com/?view=city&cityId=20061",
                "new-tab": "chrome://new-tab-page/",
            }

        @property
        def current_url(self):
            return self.urls[self.current_handle]

        @property
        def current_window_handle(self):
            return self.current_handle

    bot = BotDriver.__new__(BotDriver)
    bot.driver = Driver()
    bot.main_tab_handle = "lobby"
    bot._wait_for = lambda condition, timeout=20, poll=0.5: condition(bot.driver)

    bot._switch_to_game_tab({"lobby"})

    assert bot.main_tab_handle == "game"
    assert bot.driver.current_handle == "game"


def test_switch_to_game_tab_fails_explicitly_when_only_loading_tabs_exist():
    class SwitchTo:
        def __init__(self, driver):
            self.driver = driver

        def window(self, handle):
            self.driver.current_handle = handle

    class Driver:
        window_handles = ["lobby", "loading"]

        def __init__(self):
            self.current_handle = "loading"
            self.switch_to = SwitchTo(self)
            self.urls = {
                "lobby": "https://lobby.ikariam.gameforge.com/pt_BR/accounts",
                "loading": "https://lobby.ikariam.gameforge.com/pt_BR/loading",
            }

        @property
        def current_url(self):
            return self.urls[self.current_handle]

        @property
        def current_window_handle(self):
            return self.current_handle

    bot = BotDriver.__new__(BotDriver)
    bot.driver = Driver()
    bot._wait_for = lambda condition, timeout=20, poll=0.5: (_ for _ in ()).throw(start_module.TimeoutException("timeout"))

    try:
        bot._switch_to_game_tab({"lobby"})
        assert False, "deve falhar sem uma URL do jogo"
    except start_module.TimeoutException as error:
        assert "URL HTTP/HTTPS valida" in str(error)
    assert BotDriver._is_enabled_lobby_button(FakeLobbyButton(disabled="disabled")) is False
    assert BotDriver._is_enabled_lobby_button(FakeLobbyButton(aria_disabled="true")) is False
    assert BotDriver._is_enabled_lobby_button(FakeLobbyButton(class_name="btn btn-primary disabled transparent")) is False


def test_chromedriver_service_hides_console_window_on_windows(monkeypatch):
    monkeypatch.setattr(start_module.os, "name", "nt")
    monkeypatch.setattr(start_module.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    service = start_module._build_chromedriver_service("chromedriver.exe")

    assert service.creation_flags == 0x08000000


def test_cached_chromedriver_prefers_installed_chrome_major(monkeypatch, tmp_path):
    old_driver = tmp_path / ".wdm" / "drivers" / "chromedriver" / "win64" / "148.0.1.1" / "chromedriver-win32" / "chromedriver.exe"
    current_driver = tmp_path / ".wdm" / "drivers" / "chromedriver" / "win64" / "149.0.7827.155" / "chromedriver-win32" / "chromedriver.exe"
    old_driver.parent.mkdir(parents=True)
    current_driver.parent.mkdir(parents=True)
    old_driver.write_text("old")
    current_driver.write_text("current")
    monkeypatch.setattr(start_module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(start_module, "_installed_chrome_major", lambda: "149")

    assert start_module._cached_chromedriver_path() == str(current_driver)


def test_profile_process_cleanup_runs_powershell_without_console_window(monkeypatch, tmp_path):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(start_module.os, "name", "nt")
    monkeypatch.setattr(start_module.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(start_module.subprocess, "run", fake_run)
    monkeypatch.setattr(start_module, "sleep", lambda seconds: None)

    start_module._terminate_chrome_profile_processes(tmp_path)

    assert calls
    assert calls[0][1]["creationflags"] == 0x08000000
    assert calls[0][1]["stdout"] == start_module.subprocess.DEVNULL
    assert calls[0][1]["stderr"] == start_module.subprocess.DEVNULL


def test_start_game_skips_server_after_unexpected_flow_error():
    bot = BotDriver.__new__(BotDriver)
    logs = FakeLogs()
    entered = []
    closed = []

    def enter_server(logger, exclude_servers=None):
        if entered:
            return None
        entered.append(tuple(sorted(exclude_servers or [])))
        bot.serverGlobal = FakeServer(users=100)
        bot.serverGlobal.server = "Pangaia 2"
        return "Pangaia 2"

    bot.logs = logs
    bot.serverGlobal = None
    bot.message_tab_handle = None
    bot.main_tab_handle = "main"
    bot._check_stop = lambda: None
    bot._is_stopped = lambda: False
    bot._login_to_lobby = lambda logger: True
    bot._enter_server_from_accounts = enter_server
    bot._enter_default_server_from_lobby = lambda logger: None
    bot._run_current_server_flow = lambda logger: (_ for _ in ()).throw(RuntimeError("ranking travado"))
    bot._close_extra_windows = lambda keep: closed.append(set(keep))
    bot._sleep = lambda seconds: None

    bot.StartGame(logs)

    assert entered == [()]
    assert closed == [{"main", None}]
    assert any("Servidor Pangaia 2 apresentou erro e sera pulado" in line for line in logs.lines)


def test_start_game_stops_after_repeated_server_selection_errors():
    bot = BotDriver.__new__(BotDriver)
    logs = FakeLogs()
    attempts = []

    def fail_selection(logger, exclude_servers=None):
        attempts.append(tuple(sorted(exclude_servers or [])))
        raise RuntimeError("lobby instavel")

    bot.logs = logs
    bot.serverGlobal = None
    bot.message_tab_handle = None
    bot.main_tab_handle = "main"
    bot._check_stop = lambda: None
    bot._is_stopped = lambda: False
    bot._login_to_lobby = lambda logger: True
    bot._enter_server_from_accounts = fail_selection
    bot._enter_default_server_from_lobby = lambda logger: None
    bot._close_extra_windows = lambda keep: None
    bot._sleep = lambda seconds: None

    bot.StartGame(logs)

    assert len(attempts) == 3
    assert any("Falha ao selecionar servidor. Tentativa 3/3" in line for line in logs.lines)


def test_start_game_retries_lobby_login_before_running_flow():
    bot = BotDriver.__new__(BotDriver)
    logs = FakeLogs()
    attempts = []

    def login(logger):
        attempts.append(1)
        return len(attempts) == 2

    bot.logs = logs
    bot.serverGlobal = None
    bot.message_tab_handle = None
    bot.main_tab_handle = "main"
    bot._check_stop = lambda: None
    bot._is_stopped = lambda: False
    bot._login_to_lobby = login
    bot._enter_server_from_accounts = lambda logger, exclude_servers=None: None
    bot._enter_default_server_from_lobby = lambda logger: None
    bot._sleep = lambda seconds: None

    bot.StartGame(logs)

    assert len(attempts) == 2
    assert any("Login nao confirmou acesso ao lobby. Tentativa 1/3." in line for line in logs.lines)


def test_start_game_recovers_from_stale_element_by_restarting_flow():
    bot = BotDriver.__new__(BotDriver)
    logs = FakeLogs()
    login_attempts = []
    closed = []

    def login(logger):
        login_attempts.append(1)
        if len(login_attempts) == 1:
            raise start_module.StaleElementReferenceException("stale page")
        return True

    bot.logs = logs
    bot.serverGlobal = None
    bot.message_tab_handle = None
    bot.main_tab_handle = "main"
    bot._check_stop = lambda: None
    bot._is_stopped = lambda: False
    bot._login_to_lobby = login
    bot._enter_server_from_accounts = lambda logger, exclude_servers=None: None
    bot._enter_default_server_from_lobby = lambda logger: None
    bot._close_extra_windows = lambda keep: closed.append(set(keep))
    bot._sleep = lambda seconds: None

    bot.StartGame(logs)

    assert len(login_attempts) == 2
    assert closed == [{"main", None}]
    assert any("Pagina atualizou durante o Selenium. Recarregando o fluxo (1/3)." in line for line in logs.lines)


class FakeOutboxCell:
    def __init__(self, text="", links=None):
        self.text = text
        self._links = links or []

    def find_elements(self, by, selector):
        assert by == By.XPATH
        assert selector == ".//a"
        return self._links


class FakeOutboxRow:
    def __init__(self, recipient):
        self.recipient = recipient

    def find_elements(self, by, selector):
        assert by == By.XPATH
        assert selector == ".//td"
        return [
            FakeOutboxCell("acao"),
            FakeOutboxCell(self.recipient, [FakeLink(text=self.recipient, title=self.recipient)]),
            FakeOutboxCell("Mensagem"),
        ]


class FakeOutboxBody:
    text = "Giden kutusu Alici Konu Tarih"


class FakeOutboxDriver:
    current_window_handle = "main"
    window_handles = ["main"]

    def __init__(self):
        self.page = 0
        self.pages = [
            [FakeOutboxRow("JAIR"), FakeOutboxRow("march")],
            [FakeOutboxRow("Ikaro"), FakeOutboxRow("Sete Mares")],
        ]

    def find_element(self, by, selector):
        assert by == By.TAG_NAME
        assert selector == "body"
        return FakeOutboxBody()

    def find_elements(self, by, selector):
        if by == By.CSS_SELECTOR and selector == "span.avatarName":
            return [FakeLink(text=row.recipient, title=row.recipient) for row in self.pages[self.page]]
        raise AssertionError((by, selector))


def test_open_outbox_opens_diplomacy_advisor_before_clicking_real_outbox():
    class FallbackOutboxDriver:
        current_url = "https://s1-br.ikariam.gameforge.com/index.php?view=highscore"

        def __init__(self):
            self.visited = []

        def find_elements(self, by, selector):
            return []

        def get(self, url):
            self.visited.append(url)
            self.current_url = url

    bot = BotDriver.__new__(BotDriver)
    bot.driver = FallbackOutboxDriver()
    outbox_link = object()

    def find_or_wait(*args, **kwargs):
        if bot.driver.visited:
            return outbox_link
        raise start_module.TimeoutException("missing")

    clicked = []
    bot._find_or_wait = find_or_wait
    bot._click = clicked.append
    bot._sleep = lambda seconds: None

    assert bot._open_outbox_page() is True
    assert bot.driver.visited == ["https://s1-br.ikariam.gameforge.com/index.php?view=diplomacyAdvisor"]
    assert clicked == [outbox_link]


def test_robust_outbox_finds_sent_messages_by_title_and_validates_context():
    class SentMessagesLink:
        text = ""

        def is_displayed(self):
            return True

        def get_attribute(self, name):
            values = {
                "title": "Mensagens enviadas",
                "aria-label": "",
                "data-tooltip-content": "",
                "href": "?view=diplomacyAdvisor&tab=sent",
            }
            return values.get(name, "")

    class Body:
        def __init__(self, text):
            self.text = text

    class Driver:
        current_url = "https://s1-br.ikariam.gameforge.com/index.php?view=highscore&actionRequest=secret"

        def __init__(self):
            self.visited = []

        def get(self, url):
            self.current_url = url
            self.visited.append(url)

        def find_element(self, by, selector):
            assert (by, selector) == (By.TAG_NAME, "body")
            text = "Caixa de saida Destinatario Assunto Fecha" if "tab=sent" in self.current_url else "Conselheiro de diplomacia"
            return Body(text)

        def find_elements(self, by, selector):
            if selector == "a, button, [role='tab']" and "diplomacyAdvisor" in self.current_url:
                return [SentMessagesLink()]
            return []

    bot = BotDriver.__new__(BotDriver)
    bot.driver = Driver()
    bot._sleep = lambda seconds: None
    bot._wait_for_outbox_context = lambda timeout=2.0: bot._current_page_is_outbox()

    assert bot._open_outbox_page_robust() is True
    assert bot.driver.visited[-1].endswith("?view=diplomacyAdvisor&tab=sent")
    assert "actionRequest" not in " ".join(bot._last_outbox_open_attempts)


def test_robust_outbox_tries_validated_direct_routes_and_records_attempts():
    class Body:
        def __init__(self, text):
            self.text = text

    class Driver:
        current_url = "https://s1-br.ikariam.gameforge.com/index.php?view=highscore"

        def __init__(self):
            self.visited = []

        def get(self, url):
            self.current_url = url
            self.visited.append(url)

        def find_element(self, by, selector):
            assert (by, selector) == (By.TAG_NAME, "body")
            return Body("Outbox Destinatario Assunto Fecha" if "view=outbox" in self.current_url else "Diplomacia")

        def find_elements(self, by, selector):
            return []

    bot = BotDriver.__new__(BotDriver)
    bot.driver = Driver()
    bot._sleep = lambda seconds: None
    bot._wait_for_outbox_context = lambda timeout=2.0: bot._current_page_is_outbox()

    assert bot._open_outbox_page_robust() is True
    assert bot._last_outbox_open_attempts[-1] == "/index.php?view=outbox"
    assert len(bot.driver.visited) == 6


def test_robust_outbox_uses_live_diplomacy_advisor_outbox_route():
    class OutboxLink:
        text = "Salida (150)"

        def is_displayed(self):
            return True

        def get_attribute(self, name):
            return {
                "href": "https://s68-en.ikariam.gameforge.com/?view=diplomacyAdvisorOutBox",
                "title": "Salida",
                "aria-label": "",
                "data-tooltip-content": "",
            }.get(name, "")

    class Body:
        def __init__(self, text):
            self.text = text

    class Driver:
        current_url = "https://s68-en.ikariam.gameforge.com/?view=diplomacyAdvisor"

        def __init__(self):
            self.visited = []

        def get(self, url):
            self.current_url = url
            self.visited.append(url)

        def find_element(self, by, selector):
            assert (by, selector) == (By.TAG_NAME, "body")
            return Body(
                "Entrada (0) Salida (150) Destinatario Asunto Fecha"
                if "OutBox" in self.current_url
                else "Entrada (0) Salida (150) Diplomacia"
            )

        def find_elements(self, by, selector):
            if selector == "a[href*='diplomacyAdvisorOutBox']":
                return [OutboxLink()]
            return []

    bot = BotDriver.__new__(BotDriver)
    bot.driver = Driver()
    bot._sleep = lambda seconds: None
    bot._wait_for_outbox_context = lambda timeout=2.0: bot._current_page_is_outbox()

    assert bot._open_outbox_page_robust() is True
    assert bot.driver.visited[-1].endswith("?view=diplomacyAdvisorOutBox")


def test_extract_outbox_total_reads_localized_header_without_avatar_names():
    class OutboxLink:
        text = "Outbox (2.364)"

        def get_attribute(self, name):
            return {
                "href": "?view=diplomacyAdvisorOutBox",
                "title": "Salida",
                "aria-label": "",
            }.get(name, "")

    class Driver:
        def find_elements(self, by, selector):
            if selector == "a[href*='diplomacyAdvisorOutBox']":
                return [OutboxLink()]
            if selector == "span.avatarName":
                raise AssertionError("total must not inspect recipient names")
            return []

        def find_element(self, by, selector):
            raise AssertionError("header link should provide the total")

    bot = BotDriver.__new__(BotDriver)
    bot.driver = Driver()

    assert bot._extract_outbox_total() == 2364


def test_extract_outbox_total_accepts_comma_and_space_thousands_separators():
    class Link:
        def __init__(self, text):
            self.text = text

        def get_attribute(self, name):
            return {"href": "?view=diplomacyAdvisorOutBox", "title": "Outbox"}.get(name, "")

    class Driver:
        def find_elements(self, by, selector):
            if selector == "a[href*='diplomacyAdvisorOutBox']":
                return [Link("Outbox (2,509)"), Link("Outbox (2 120)")]
            return []

        def find_element(self, by, selector):
            raise AssertionError("os links devem fornecer o total")

    bot = BotDriver.__new__(BotDriver)
    bot.driver = Driver()

    assert bot._extract_outbox_total() == 2509


def test_current_filter_resume_offset_skips_rows_already_counted_by_outbox():
    bot = make_bot(users=700)
    bot.serverGlobal.messageSend = 75
    bot._active_highscore_offset = "51 - 100"

    assert bot._current_filter_resume_offset() == 25

    bot.serverGlobal.messageSend = 2509
    bot._active_highscore_offset = "2.501 - 2.550"
    assert bot._current_filter_resume_offset() == 9


def test_capture_applies_outbox_offset_before_preparing_targets(monkeypatch):
    bot = make_bot(users=700)
    bot.serverGlobal.messageSend = 3
    bot._active_highscore_offset = "1 - 50"
    rows = [FakeRow(), FakeRow(), FakeRow(), FakeRow(), FakeRow()]
    bot._find_all_or_wait = lambda *args, **kwargs: rows
    monkeypatch.setattr(
        start_module.UsersSend,
        "reserve",
        lambda **kwargs: SimpleNamespace(id_str=f"reservation-{kwargs['username']}"),
    )
    monkeypatch.setattr(start_module.UsersSend, "update_status", lambda *args, **kwargs: None)

    completed, sent_any, batch_exhausted = bot._captureusers(FakeLogs())

    assert completed is False
    assert sent_any is True
    assert batch_exhausted is False
    assert bot._last_capture_state["rows"] == 5
    assert bot._last_capture_state["resume_offset"] == 3
    assert bot._last_capture_state["targets"] == 2
    assert bot._last_capture_state["attempted"] == 2


def test_click_next_outbox_page_accepts_localized_start_link():
    class Link:
        def is_displayed(self):
            return True

        def get_attribute(self, name):
            return {
                "href": "?view=diplomacyAdvisorOutBox&start=10",
                "class": "",
                "aria-disabled": "",
            }.get(name, "")

    class Body:
        @property
        def text(self):
            return "pagina-1" if driver.page == 0 else "pagina-2"

    class Driver:
        def __init__(self):
            self.page = 0

        def find_elements(self, by, selector):
            if by == By.XPATH and "diplomacyAdvisorOutBox" in selector and "start=" in selector:
                return [Link()]
            return []

        def find_element(self, by, selector):
            assert (by, selector) == (By.TAG_NAME, "body")
            return Body()

    bot = BotDriver.__new__(BotDriver)
    driver = Driver()
    bot.driver = driver
    bot._check_stop = lambda: None
    bot._click = lambda element: setattr(driver, "page", 1)
    bot._sleep = lambda seconds: None

    assert bot._click_next_outbox_page() is True


def test_extract_outbox_total_ignores_a_zero_candidate_when_a_real_total_exists():
    class Link:
        def __init__(self, text):
            self.text = text

        def get_attribute(self, name):
            return {"href": "?view=diplomacyAdvisorOutBox", "title": "Salida"}.get(name, "")

    class Driver:
        def find_elements(self, by, selector):
            if selector == "a[href*='diplomacyAdvisorOutBox']":
                return [Link("Salida (0)"), Link("Salida (150)")]
            return []

        def find_element(self, by, selector):
            raise AssertionError("a lista de links deve fornecer o total")

    bot = BotDriver.__new__(BotDriver)
    bot.driver = Driver()

    assert bot._extract_outbox_total() == 150


def test_extract_outbox_recipients_from_rows_deduplicates_and_cleans_names():
    rows = [FakeOutboxRow(" JAIR "), FakeOutboxRow("JAIR"), FakeOutboxRow("march")]

    recipients = BotDriver._extract_outbox_recipients_from_rows(rows)

    assert recipients == ["JAIR", "march"]


def test_extract_current_outbox_recipients_reads_stable_avatar_names():
    bot = BotDriver.__new__(BotDriver)
    bot.driver = FakeOutboxDriver()
    bot._sleep = lambda seconds: None

    assert bot._extract_current_outbox_recipients() == ["JAIR", "march"]


def test_sync_outbox_counts_complete_paginated_messages_without_importing_users(monkeypatch):
    driver = FakeOutboxDriver()
    bot = BotDriver.__new__(BotDriver)
    bot.driver = driver
    bot.serverGlobal = FakeServer(users=700, message_send=0)
    bot.account = SimpleNamespace(id="account-1")
    bot._check_stop = lambda: None
    bot._open_outbox_page_robust = lambda: True
    bot._log_progress = lambda logs: logs.set_progress({"sent": bot.serverGlobal.messageSend, "total": bot.serverGlobal.users})

    def next_page():
        if driver.page >= len(driver.pages) - 1:
            return False
        driver.page += 1
        return True

    bot._click_next_outbox_page = next_page

    monkeypatch.setattr(start_module.UsersSend, "replace_server_outbox_snapshot", lambda **kwargs: (_ for _ in ()).throw(AssertionError("nao deve importar usuarios")))
    monkeypatch.setattr(start_module.UsersSend, "reconcile_sent", lambda **kwargs: (_ for _ in ()).throw(AssertionError("nao deve reconciliar usuarios")))
    logs = FakeLogs()

    count = bot._sync_outbox_sent_users(logs)

    assert count == 4
    assert bot._last_outbox_snapshot_complete is True
    assert bot.serverGlobal.messageSend == 4
    assert logs.progress == {"sent": 4, "total": 700}
    assert "Outbox sincronizada: 4 mensagens encontradas." in logs.lines


def test_sync_outbox_prefers_live_header_total_without_paginating():
    class OutboxLink:
        text = "Salida (150)"

        def get_attribute(self, name):
            return {
                "href": "?view=diplomacyAdvisorOutBox",
                "title": "Salida",
                "aria-label": "",
            }.get(name, "")

    class Body:
        text = "Entrada (0) Salida (150) Destinatario Asunto Fecha"

    class Driver:
        current_window_handle = "main"
        window_handles = ["main"]
        current_url = "https://s68-en.ikariam.gameforge.com/?view=diplomacyAdvisorOutBox"

        def find_element(self, by, selector):
            assert (by, selector) == (By.TAG_NAME, "body")
            return Body()

        def find_elements(self, by, selector):
            if selector == "a[href*='diplomacyAdvisorOutBox']":
                return [OutboxLink()]
            if selector == "span.avatarName":
                raise AssertionError("header total should avoid recipient scan")
            return []

    bot = BotDriver.__new__(BotDriver)
    bot.driver = Driver()
    bot.serverGlobal = FakeServer(users=700, message_send=0)
    bot.account = SimpleNamespace(id="account-1")
    bot._check_stop = lambda: None
    bot._open_outbox_page_robust = lambda: True
    bot._click_next_outbox_page = lambda: (_ for _ in ()).throw(AssertionError("header total should avoid pagination"))
    bot._log_progress = lambda logs: logs.set_progress({"sent": bot.serverGlobal.messageSend, "total": bot.serverGlobal.users})
    logs = FakeLogs()

    assert bot._sync_outbox_sent_users(logs) == 150
    assert bot.serverGlobal.messageSend == 150
    assert bot._last_outbox_snapshot_complete is True
    assert "Outbox sincronizada: 150 mensagens encontradas." in logs.lines


def test_sync_outbox_preserves_stale_server_total_when_empty_state_is_not_proven(monkeypatch):
    driver = FakeOutboxDriver()
    driver.pages = [[]]
    bot = BotDriver.__new__(BotDriver)
    bot.driver = driver
    bot.serverGlobal = FakeServer(users=1783, message_send=642)
    bot.account = SimpleNamespace(id="account-1")
    bot._check_stop = lambda: None
    bot._open_outbox_page_robust = lambda: True
    bot._click_next_outbox_page = lambda: False
    bot._sleep = lambda seconds: None
    bot._log_progress = lambda logs: logs.set_progress({"sent": bot.serverGlobal.messageSend, "total": bot.serverGlobal.users})
    monkeypatch.setattr(start_module.UsersSend, "replace_server_outbox_snapshot", lambda **kwargs: (_ for _ in ()).throw(AssertionError("nao deve importar usuarios")))
    logs = FakeLogs()

    count = bot._sync_outbox_sent_users(logs)

    assert count is None
    assert bot.serverGlobal.messageSend == 642
    assert logs.progress is None
    assert "Outbox aberta, mas o total nao foi comprovado. Progresso local preservado." in logs.lines


def test_sync_outbox_accepts_explicit_empty_state_without_decreasing_local_total(monkeypatch):
    driver = FakeOutboxDriver()
    driver.pages = [[]]

    class Body:
        text = "Giden kutusu Alici Konu Tarih Nenhuma mensagem enviada"

    driver.find_element = lambda by, selector: Body()
    bot = BotDriver.__new__(BotDriver)
    bot.driver = driver
    bot.serverGlobal = FakeServer(users=1783, message_send=642)
    bot.account = SimpleNamespace(id="account-1")
    bot._check_stop = lambda: None
    bot._open_outbox_page_robust = lambda: True
    bot._click_next_outbox_page = lambda: False
    bot._sleep = lambda seconds: None
    bot._log_progress = lambda logs: logs.set_progress({"sent": bot.serverGlobal.messageSend, "total": bot.serverGlobal.users})
    logs = FakeLogs()

    assert bot._sync_outbox_sent_users(logs) == 0
    assert bot.serverGlobal.messageSend == 642
    assert bot._last_outbox_snapshot_complete is True
    assert logs.progress == {"sent": 642, "total": 1783}


def test_sync_outbox_does_not_trust_zero_header_when_rows_are_visible(monkeypatch):
    class Link:
        text = "Salida (0)"

        def get_attribute(self, name):
            return {"href": "?view=diplomacyAdvisorOutBox", "title": "Salida"}.get(name, "")

    class Body:
        text = "Entrada (0) Salida (0) Destinatario Assunto Fecha"

    class Driver:
        current_window_handle = "main"
        window_handles = ["main"]
        current_url = "https://s1-br.ikariam.gameforge.com/?view=diplomacyAdvisorOutBox"

        def find_element(self, by, selector):
            return Body()

        def find_elements(self, by, selector):
            if selector == "a[href*='diplomacyAdvisorOutBox']":
                return [Link()]
            if selector == "span.avatarName":
                return [SimpleNamespace(text="A"), SimpleNamespace(text="B")]
            return []

    bot = BotDriver.__new__(BotDriver)
    bot.driver = Driver()
    bot.serverGlobal = FakeServer(users=700, message_send=0)
    bot.account = SimpleNamespace(id="account-1")
    bot._check_stop = lambda: None
    bot._open_outbox_page_robust = lambda: True
    bot._click_next_outbox_page = lambda: False
    bot._sleep = lambda seconds: None
    bot._log_progress = lambda logs: logs.set_progress({"sent": bot.serverGlobal.messageSend, "total": bot.serverGlobal.users})
    logs = FakeLogs()

    assert bot._sync_outbox_sent_users(logs) == 2
    assert bot.serverGlobal.messageSend == 2


def test_unrecognized_outbox_route_preserves_local_progress(monkeypatch):
    driver = FakeOutboxDriver()
    driver.current_url = "https://s1.example/index.php?view=messages&tab=outbox"
    driver.find_element = lambda by, selector: SimpleNamespace(text="City overview")
    bot = BotDriver.__new__(BotDriver)
    bot.driver = driver
    bot.serverGlobal = FakeServer(users=1783, message_send=25)
    bot.account = SimpleNamespace(id="account-1")
    bot._check_stop = lambda: None
    bot._open_outbox_page_robust = lambda: True
    bot._log_progress = lambda logs: (_ for _ in ()).throw(AssertionError("nao deve atualizar progresso"))
    monkeypatch.setattr(
        start_module.UsersSend,
        "replace_server_outbox_snapshot",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("nao deve apagar estado local")),
    )
    logs = FakeLogs()

    count = bot._sync_outbox_sent_users(logs)

    assert count is None
    assert bot.serverGlobal.messageSend == 25
    assert "Nao foi possivel sincronizar Outbox: a rota abriu sem a grade reconhecida. Progresso local preservado." in logs.lines


def test_outbox_count_reads_only_first_page_without_resetting_total(monkeypatch):
    driver = FakeOutboxDriver()
    bot = BotDriver.__new__(BotDriver)
    bot.driver = driver
    bot.serverGlobal = FakeServer(users=700, message_send=12)
    bot.account = SimpleNamespace(id="account-1")
    bot._check_stop = lambda: None
    bot._open_outbox_page_robust = lambda: True
    bot._click_next_outbox_page = lambda: (_ for _ in ()).throw(AssertionError("nao deve paginar"))
    bot._log_progress = lambda logs: (_ for _ in ()).throw(AssertionError("nao deve atualizar o total"))
    monkeypatch.setattr(start_module.UsersSend, "reconcile_sent", lambda **kwargs: (_ for _ in ()).throw(AssertionError("nao deve reconciliar usuarios")))

    count = bot._sync_outbox_sent_users(full_history=False, update_total=False, log_result=False)

    assert count == 2
    assert bot.serverGlobal.messageSend == 12
    assert bot._last_outbox_recipients == set()
