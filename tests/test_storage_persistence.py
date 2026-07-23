import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_storage_script(
    script: str,
    *,
    appdata: Path,
    cwd: Path,
    bot_data_dir: Path | None = None,
    bot_instance_file: Path | None = None,
) -> str:
    env = os.environ.copy()
    env["APPDATA"] = str(appdata)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env.pop("BOT_INSTANCE_ID", None)
    if bot_instance_file is None:
        env.pop("BOT_INSTANCE_FILE", None)
    else:
        env["BOT_INSTANCE_FILE"] = str(bot_instance_file)
    if bot_data_dir is None:
        env.pop("BOT_DATA_DIR", None)
    else:
        env["BOT_DATA_DIR"] = str(bot_data_dir)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def test_default_database_path_is_created_next_to_launch_directory(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    cwd.mkdir()

    output = run_storage_script(
        "from src.storage import _db_path, init_db; init_db(); print(_db_path())",
        appdata=appdata,
        cwd=cwd,
    )

    assert output == str(cwd / "data" / "novo2.sqlite3")
    assert (cwd / "data" / "novo2.sqlite3").exists()


def test_accounts_persist_between_separate_processes_with_local_data_storage(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    cwd.mkdir()

    run_storage_script(
        "from src.storage import Accounts, init_db; init_db(); Accounts.create(email='persist@example.test', password='secret'); print('saved')",
        appdata=appdata,
        cwd=cwd,
    )
    output = run_storage_script(
        "from src.storage import Accounts, init_db; init_db(); print([account.email for account in Accounts.find()])",
        appdata=appdata,
        cwd=cwd,
    )

    assert output == "['persist@example.test']"


def test_legacy_appdata_db_is_ignored_when_local_data_is_missing(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    legacy_data = appdata / "BotDivulgacao"
    cwd.mkdir()
    legacy_data.mkdir(parents=True)

    run_storage_script(
        "from src.storage import Accounts, init_db; init_db(); Accounts.create(email='legacy@example.test', password='secret'); print('saved')",
        appdata=appdata,
        cwd=cwd,
        bot_data_dir=legacy_data,
    )
    output = run_storage_script(
        "from src.storage import Accounts, init_db, _db_path; init_db(); print(_db_path()); print([account.email for account in Accounts.find()])",
        appdata=appdata,
        cwd=cwd,
    )

    lines = output.splitlines()
    assert lines[0] == str(cwd / "data" / "novo2.sqlite3")
    assert lines[1] == "[]"


def test_new_local_instance_does_not_adopt_legacy_instance_id(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    legacy_data = appdata / "BotDivulgacao"
    cwd.mkdir()
    legacy_data.mkdir(parents=True)

    run_storage_script(
        "from src.storage import Accounts, init_db; init_db(); Accounts.create(email='legacy@example.test', password='secret'); print('saved')",
        appdata=appdata,
        cwd=cwd,
        bot_data_dir=legacy_data,
        bot_instance_file=legacy_data / "instance_id.txt",
    )
    output = run_storage_script(
        "from src.storage import Accounts, init_db; init_db(); print([account.email for account in Accounts.find()])",
        appdata=appdata,
        cwd=cwd,
    )

    assert output == "[]"
    assert (cwd / "data" / "instance_id.txt").read_text(encoding="utf-8") != (legacy_data / "instance_id.txt").read_text(encoding="utf-8")


def test_servers_with_same_name_are_separated_by_country_flag(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    data_dir = tmp_path / "data"
    cwd.mkdir()

    output = run_storage_script(
        "\n".join(
            [
                "from src.storage import Servers, init_db",
                "init_db()",
                "br = Servers.get_or_create('Themis', flag='BR', users=100)",
                "gr = Servers.get_or_create('Themis', flag='GR', users=200)",
                "br_again = Servers.get_or_create('Themis', flag='br')",
                "print(br.id != gr.id)",
                "print(br.id == br_again.id)",
                "print([server.display_name for server in Servers.find()])",
            ]
        ),
        appdata=appdata,
        cwd=cwd,
        bot_data_dir=data_dir,
    )

    lines = output.splitlines()
    assert lines[0] == "True"
    assert lines[1] == "True"
    assert lines[2] == "['BR / Themis', 'GR / Themis']"


def test_settings_default_dry_run_is_disabled(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    cwd.mkdir()

    output = run_storage_script(
        "from src.storage import Settings, init_db; init_db(); print(Settings.load().dry_run)",
        appdata=appdata,
        cwd=cwd,
    )

    assert output == "False"


def test_outbox_reconciliation_promotes_existing_reservation_to_sent(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    cwd.mkdir()

    output = run_storage_script(
        "\n".join(
            [
                "from src.storage import UsersSend, init_db",
                "init_db()",
                "reserved = UsersSend.reserve(server_id='server-1', username='Jair', account_id='account-1')",
                "print(reserved.status)",
                "print(UsersSend.reconcile_sent(server_id='server-1', username='JAIR', account_id='account-1'))",
                "print(UsersSend.find_one({'id': reserved.id}).status)",
            ]
        ),
        appdata=appdata,
        cwd=cwd,
    )

    assert output.splitlines() == ["reserved", "True", "sent"]


def test_empty_outbox_snapshot_never_releases_registered_user(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    cwd.mkdir()

    output = run_storage_script(
        "\n".join(
            [
                "from src.storage import UsersSend, init_db",
                "init_db()",
                "UsersSend.reserve(server_id='server-1', username='Player One', account_id='account-1')",
                "print(UsersSend.replace_server_outbox_snapshot(server_id='server-1', usernames=[], account_id='account-1'))",
                "print(UsersSend.reserve(server_id='server-1', username='Player One', account_id='account-1') is not None)",
            ]
        ),
        appdata=appdata,
        cwd=cwd,
    )

    assert output.splitlines() == ["0", "False"]


def test_failed_and_unconfirmed_users_are_never_reserved_again(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    cwd.mkdir()

    output = run_storage_script(
        "\n".join(
            [
                "from src.storage import UsersSend, init_db",
                "init_db()",
                "first = UsersSend.reserve(server_id='server-1', username='Failed Player', account_id='account-1')",
                "UsersSend.update_status(first.id, 'failed')",
                "retry_failed = UsersSend.reserve(server_id='server-1', username='Failed Player', account_id='account-1')",
                "print(retry_failed is None)",
                "second = UsersSend.reserve(server_id='server-1', username='Unconfirmed Player', account_id='account-1')",
                "UsersSend.update_status(second.id, 'unconfirmed')",
                "retry_unconfirmed = UsersSend.reserve(server_id='server-1', username='Unconfirmed Player', account_id='account-1')",
                "print(retry_unconfirmed is None)",
            ]
        ),
        appdata=appdata,
        cwd=cwd,
    )

    assert output.splitlines() == ["True", "True"]


def test_reservation_is_never_reused_even_after_ttl_or_account_change(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    cwd.mkdir()

    output = run_storage_script(
        "\n".join(
            [
                "import os, sqlite3, time",
                "from src.storage import UsersSend, init_db, _db_path",
                "init_db()",
                "first = UsersSend.reserve(server_id='server-1', username='Player One', account_id='account-1')",
                "print(UsersSend.reserve(server_id='server-1', username='Player One', account_id='account-1') is None)",
                "with sqlite3.connect(_db_path()) as db: db.execute('UPDATE users_send SET updated_at = ? WHERE id = ?', (time.time() - 10, first.id))",
                "os.environ['BOT_RESERVATION_TTL_SECONDS'] = '1'",
                "retry = UsersSend.reserve(server_id='server-1', username='Player One', account_id='account-1')",
                "print(retry is None)",
                "other_account = UsersSend.reserve(server_id='server-1', username='Player One', account_id='account-2')",
                "print(other_account is None)",
            ]
        ),
        appdata=appdata,
        cwd=cwd,
    )

    assert output.splitlines() == ["True", "True", "True"]


def test_user_counts_distinguish_all_records_from_sent_records(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    cwd.mkdir()

    output = run_storage_script(
        "\n".join(
            [
                "from src.storage import UsersSend, init_db",
                "init_db()",
                "sent = UsersSend.reserve(server_id='server-1', username='Sent Player', account_id='account-1')",
                "UsersSend.update_status(sent.id, 'sent')",
                "failed = UsersSend.reserve(server_id='server-1', username='Failed Player', account_id='account-1')",
                "UsersSend.update_status(failed.id, 'failed')",
                "print(UsersSend.count_for_server(server_id='server-1'))",
                "print(UsersSend.count_for_server(server_id='server-1', status='sent'))",
            ]
        ),
        appdata=appdata,
        cwd=cwd,
    )

    assert output.splitlines() == ["2", "1"]


def test_settings_persist_decimal_player_and_post_send_waits(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    cwd.mkdir()

    output = run_storage_script(
        "\n".join(
            [
                "from src.storage import Settings, init_db",
                "init_db()",
                "settings = Settings.load()",
                "print(settings.time_wait, settings.post_send_wait)",
                "settings.time_wait = 0.5; settings.post_send_wait = 0.1; settings.save()",
                "saved = Settings.load()",
                "print(saved.time_wait, saved.post_send_wait)",
            ]
        ),
        appdata=appdata,
        cwd=cwd,
    )

    assert output.splitlines() == ["1.0 1.0", "0.5 0.1"]


def test_server_counters_remain_independent_when_rotating_a_b_a(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    cwd.mkdir()

    output = run_storage_script(
        "\n".join(
            [
                "from src.storage import Servers, init_db",
                "init_db()",
                "a = Servers.get_or_create('Alpha', flag='BR')",
                "b = Servers.get_or_create('Beta', flag='BR')",
                "a.messageSend = 25; a.save()",
                "b.messageSend = 25; b.save()",
                "a = Servers.get_or_create('Alpha', flag='BR')",
                "a.messageSend += 25; a.save()",
                "print(Servers.get_or_create('Alpha', flag='BR').messageSend)",
                "print(Servers.get_or_create('Beta', flag='BR').messageSend)",
            ]
        ),
        appdata=appdata,
        cwd=cwd,
    )

    assert output.splitlines() == ["50", "25"]


def test_runtime_logs_persist_and_can_be_cleared(tmp_path):
    appdata = tmp_path / "appdata"
    cwd = tmp_path / "launch-dir"
    cwd.mkdir()

    run_storage_script(
        "from src.storage import RuntimeLog; RuntimeLog.add(text='Falha no Selenium: teste', level='error', account_id='account-1'); print('saved')",
        appdata=appdata,
        cwd=cwd,
    )
    output = run_storage_script(
        "from src.storage import RuntimeLog; print(RuntimeLog.list_recent('account-1'))",
        appdata=appdata,
        cwd=cwd,
    )
    assert "Falha no Selenium: teste" in output

    output = run_storage_script(
        "from src.storage import RuntimeLog; RuntimeLog.clear('account-1'); print(RuntimeLog.list_recent('account-1'))",
        appdata=appdata,
        cwd=cwd,
    )
    assert output == "[]"
