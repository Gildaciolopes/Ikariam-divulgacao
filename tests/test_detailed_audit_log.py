import json

import web_app
from src.audit_log import DetailedAuditLog


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_detailed_audit_log_is_incremental_and_redacts_secrets(tmp_path):
    path = tmp_path / "data" / "log" / "run.jsonl"
    audit = DetailedAuditLog(path, "run-test")

    audit.write(
        "first_event",
        account_id="account-1",
        email="player@example.test",
        password="never-write-this",
        server_total=40,
    )
    assert len(read_jsonl(path)) == 1

    audit.write("second_event", account_id="account-1", filter="51 - 100", skipped=50)
    rows = read_jsonl(path)

    assert [row["event"] for row in rows] == ["first_event", "second_event"]
    assert rows[0]["data"]["email"] == "[conta]"
    assert rows[0]["data"]["password"] == "[REDACTED]"
    assert "never-write-this" not in path.read_text(encoding="utf-8")
    assert rows[1]["data"] == {"filter": "51 - 100", "skipped": 50}


def test_web_logs_mirror_hidden_messages_and_progress_to_jsonl(tmp_path):
    path = tmp_path / "data" / "log" / "run.jsonl"
    audit = DetailedAuditLog(path, "run-test")
    logger = web_app.WebLogs(account_id="account-1", audit_log=audit)

    logger.addLogs("Abrindo lobby para player@example.test.", "info")
    logger.addAudit(
        "filter_processed",
        filter="1 - 50",
        capture_state={"targets": 50, "skipped_confirmed": 40, "attempted": 10},
        server_total=50,
        session_total=10,
    )
    logger.set_progress({"server": "Nereus", "sent": 50, "total": 100})

    rows = read_jsonl(path)
    assert [row["event"] for row in rows] == ["application_log", "filter_processed", "progress_updated"]
    assert rows[0]["data"]["message"] == "Abrindo lobby para conta."
    assert rows[1]["data"]["capture_state"]["attempted"] == 10
    assert rows[1]["data"]["server_total"] == 50
    assert rows[1]["data"]["session_total"] == 10


def test_settings_form_persists_detailed_log_toggle(monkeypatch):
    settings = web_app.Settings(save_detailed_logs=False)
    saved = []
    monkeypatch.setattr(web_app.Settings, "load", lambda: settings)
    monkeypatch.setattr(settings, "save", lambda: saved.append(settings.save_detailed_logs) or settings)
    monkeypatch.setattr(web_app.Accounts, "find", lambda filters: [])

    response = web_app.app.test_client().post(
        "/bot/settings",
        data={"time_wait": "1", "save_detailed_logs": "1", "default_message": "Teste"},
    )

    assert response.status_code == 302
    assert saved == [True]
    assert web_app.BOT_STATE["settings"]["save_detailed_logs"] is True


def test_settings_page_shows_detailed_log_checkbox_checked(monkeypatch):
    settings = web_app.Settings(save_detailed_logs=True)
    monkeypatch.setattr(web_app.Settings, "load", lambda: settings)
    monkeypatch.setattr(web_app.Accounts, "find", lambda filters: [])
    monkeypatch.setattr(web_app, "_build_server_summary", lambda: [])

    response = web_app.app.test_client().get("/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="save_detailed_logs"' in html
    assert 'name="save_detailed_logs"' in html
    checkbox = html.split('id="save_detailed_logs"', 1)[1].split("/>", 1)[0]
    assert "checked" in checkbox
