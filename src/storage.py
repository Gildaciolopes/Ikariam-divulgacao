"""Local persistence models for accounts, servers, users, and settings."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

APP_NAME = "Bot Faker"
DEFAULT_MESSAGE = "Mensagem de divulgacao..."
SETTINGS_DOC_ID = "bot_settings"
INSTANCE_ID_ENV = "BOT_INSTANCE_ID"
INSTANCE_FILE_ENV = "BOT_INSTANCE_FILE"
DEFAULT_RESERVATION_TTL_SECONDS = 900


def _reservation_ttl_seconds() -> int:
    try:
        configured = int(os.getenv("BOT_RESERVATION_TTL_SECONDS", str(DEFAULT_RESERVATION_TTL_SECONDS)))
    except ValueError:
        return DEFAULT_RESERVATION_TTL_SECONDS
    return max(1, configured)


def _default_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path.cwd() / "data"


def _default_instance_file() -> Path:
    return _default_app_dir() / "instance_id.txt"


def _resolve_instance_id() -> str:
    env_value = os.getenv(INSTANCE_ID_ENV)
    if env_value:
        return env_value.strip()
    instance_file = Path(os.getenv(INSTANCE_FILE_ENV) or _default_instance_file())
    try:
        database_value = _database_instance_id(_default_app_dir() / "novo2.sqlite3")
        if database_value:
            instance_file.parent.mkdir(parents=True, exist_ok=True)
            instance_file.write_text(database_value, encoding="utf-8")
            return database_value
        if instance_file.exists():
            value = instance_file.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = uuid.uuid4().hex
        instance_file.parent.mkdir(parents=True, exist_ok=True)
        instance_file.write_text(value, encoding="utf-8")
        return value
    except Exception:
        return uuid.uuid4().hex


def _data_dir() -> Path:
    env_value = os.getenv("BOT_DATA_DIR")
    if env_value:
        return Path(env_value)
    return _default_app_dir()


def app_data_dir() -> Path:
    return _data_dir()


def _db_path() -> Path:
    return _data_dir() / "novo2.sqlite3"


def _database_instance_id(database_path: Path) -> str | None:
    if not database_path.exists():
        return None
    try:
        with sqlite3.connect(database_path) as connection:
            for table in ("accounts", "settings"):
                rows = connection.execute(
                    f"SELECT DISTINCT instance_id FROM {table} WHERE instance_id <> '' LIMIT 2"
                ).fetchall()
                values = [str(row[0]).strip() for row in rows if str(row[0]).strip()]
                if len(values) == 1:
                    return values[0]
    except sqlite3.Error:
        return None
    return None


INSTANCE_ID = _resolve_instance_id()


def _new_id() -> str:
    return uuid.uuid4().hex


def _now_ts() -> float:
    return time.time()


def _normalize_username(username: str) -> str:
    return re.sub(r"\s+", " ", (username or "").strip()).casefold()


def _normalize_server_flag(flag: str | None) -> str:
    return re.sub(r"\s+", " ", (flag or "").strip()).upper()


def _connect() -> sqlite3.Connection:
    _data_dir().mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(_db_path(), timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def init_db() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                password TEXT NOT NULL,
                cache INTEGER NOT NULL DEFAULT 1,
                message TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'inactive',
                last_login TEXT NOT NULL DEFAULT '',
                instance_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_accounts_instance ON accounts(instance_id);

            CREATE TABLE IF NOT EXISTS servers (
                id TEXT PRIMARY KEY,
                server TEXT NOT NULL,
                message_send INTEGER NOT NULL DEFAULT 0,
                flag TEXT NOT NULL DEFAULT '',
                users INTEGER NOT NULL DEFAULT 0,
                instance_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users_send (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                username_key TEXT NOT NULL,
                server_id TEXT NOT NULL,
                account_id TEXT,
                instance_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'sent',
                retry_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_instance_server_username
                ON users_send(instance_id, server_id, username_key);

            CREATE TABLE IF NOT EXISTS settings (
                id TEXT PRIMARY KEY,
                time_wait REAL NOT NULL DEFAULT 1.0,
                post_send_wait REAL NOT NULL DEFAULT 1.0,
                logs INTEGER NOT NULL DEFAULT 1,
                headless INTEGER NOT NULL DEFAULT 0,
                dry_run INTEGER NOT NULL DEFAULT 0,
                save_detailed_logs INTEGER NOT NULL DEFAULT 0,
                default_message TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_logs (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL DEFAULT '',
                level TEXT NOT NULL DEFAULT 'info',
                text TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runtime_logs_instance_account_created
                ON runtime_logs(instance_id, account_id, created_at DESC);
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(settings)").fetchall()}
        if "dry_run" not in columns:
            connection.execute("ALTER TABLE settings ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0")
        if "save_detailed_logs" not in columns:
            connection.execute("ALTER TABLE settings ADD COLUMN save_detailed_logs INTEGER NOT NULL DEFAULT 0")
        if "post_send_wait" not in columns:
            connection.execute("ALTER TABLE settings ADD COLUMN post_send_wait REAL NOT NULL DEFAULT 1.0")
        server_columns = {row["name"] for row in connection.execute("PRAGMA table_info(servers)").fetchall()}
        if "flag" not in server_columns:
            connection.execute("ALTER TABLE servers ADD COLUMN flag TEXT NOT NULL DEFAULT ''")
        connection.execute("DROP INDEX IF EXISTS idx_servers_instance_name")
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_servers_instance_name_flag
                ON servers(instance_id, server COLLATE NOCASE, flag COLLATE NOCASE)
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO settings(id, time_wait, logs, headless, dry_run, default_message, instance_id, updated_at)
            VALUES (?, 1.0, 1, 0, 0, ?, ?, ?)
            """,
            (_settings_doc_id(INSTANCE_ID), DEFAULT_MESSAGE, INSTANCE_ID, _now_ts()),
        )


def _apply_instance_filter(filters: dict[str, Any]) -> dict[str, Any]:
    filters = dict(filters or {})
    filters.setdefault("instance_id", INSTANCE_ID)
    return filters


def _settings_doc_id(instance_id: str) -> str:
    return f"{SETTINGS_DOC_ID}:{instance_id}"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _build_where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    filters = _apply_instance_filter(filters)
    clauses: list[str] = []
    values: list[Any] = []
    for key, value in filters.items():
        if key == "_id":
            key = "id"
        if isinstance(value, dict) and "$in" in value:
            items = list(value["$in"])
            if not items:
                clauses.append("0 = 1")
                continue
            clauses.append(f"{key} IN ({', '.join('?' for _ in items)})")
            values.extend(items)
        else:
            clauses.append(f"{key} = ?")
            values.append(value)
    return " AND ".join(clauses) if clauses else "1 = 1", values


@dataclass
class Accounts:
    email: str
    password: str
    cache: bool = True
    message: str = ""
    status: str = "inactive"
    last_login: str = ""
    instance_id: str = INSTANCE_ID
    id: str = field(default_factory=_new_id)

    collection: ClassVar[str] = "accounts"

    @property
    def id_str(self) -> str:
        return self.id

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "Accounts":
        return cls(
            id=str(doc.get("id") or doc.get("_id") or _new_id()),
            email=str(doc.get("email") or ""),
            password=str(doc.get("password") or ""),
            cache=bool(doc.get("cache", True)),
            message=str(doc.get("message") or ""),
            status=str(doc.get("status") or "inactive"),
            last_login=str(doc.get("last_login") or ""),
            instance_id=str(doc.get("instance_id") or INSTANCE_ID),
        )

    def to_doc(self) -> dict[str, Any]:
        return self.__dict__.copy()

    def save(self) -> "Accounts":
        now = _now_ts()
        with _connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts(id,email,password,cache,message,status,last_login,instance_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    email=excluded.email,password=excluded.password,cache=excluded.cache,message=excluded.message,
                    status=excluded.status,last_login=excluded.last_login,instance_id=excluded.instance_id,
                    updated_at=excluded.updated_at
                """,
                (self.id, self.email, self.password, int(self.cache), self.message, self.status, self.last_login, self.instance_id, now, now),
            )
        return self

    def delete_instance(self) -> None:
        with _connect() as connection:
            connection.execute("DELETE FROM accounts WHERE id = ?", (self.id,))

    @classmethod
    def create(cls, **fields: Any) -> "Accounts":
        account = cls(**fields)
        account.save()
        return account

    @classmethod
    def find(cls, filters: dict[str, Any] | None = None) -> list["Accounts"]:
        where, values = _build_where(filters or {})
        with _connect() as connection:
            rows = connection.execute(f"SELECT * FROM accounts WHERE {where} ORDER BY created_at, email", values).fetchall()
        return [cls.from_doc(dict(row)) for row in rows]

    @classmethod
    def find_one(cls, filters: dict[str, Any]) -> "Accounts | None":
        where, values = _build_where(filters)
        with _connect() as connection:
            row = connection.execute(f"SELECT * FROM accounts WHERE {where} LIMIT 1", values).fetchone()
        doc = _row_to_dict(row)
        return cls.from_doc(doc) if doc else None

    @classmethod
    def find_by_id(cls, id_value: str) -> "Accounts | None":
        return cls.find_one({"id": id_value})


@dataclass
class Servers:
    server: str
    messageSend: int = 0
    flag: str = ""
    users: int = 0
    instance_id: str = INSTANCE_ID
    id: str = field(default_factory=_new_id)

    collection: ClassVar[str] = "servers"

    @property
    def id_str(self) -> str:
        return self.id

    @property
    def display_name(self) -> str:
        country = _normalize_server_flag(self.flag)
        name = (self.server or "").strip() or "Servidor desconhecido"
        return f"{country} / {name}" if country else name

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "Servers":
        return cls(
            id=str(doc.get("id") or doc.get("_id") or _new_id()),
            server=str(doc.get("server") or ""),
            messageSend=int(doc.get("messageSend") or doc.get("message_send") or 0),
            flag=_normalize_server_flag(str(doc.get("flag") or "")),
            users=int(doc.get("users") or 0),
            instance_id=str(doc.get("instance_id") or INSTANCE_ID),
        )

    def save(self) -> "Servers":
        now = _now_ts()
        self.flag = _normalize_server_flag(self.flag)
        with _connect() as connection:
            connection.execute(
                """
                INSERT INTO servers(id,server,message_send,flag,users,instance_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    server=excluded.server,message_send=excluded.message_send,flag=excluded.flag,
                    users=excluded.users,instance_id=excluded.instance_id,updated_at=excluded.updated_at
                """,
                (self.id, self.server, self.messageSend, self.flag, self.users, self.instance_id, now, now),
            )
        return self

    @classmethod
    def create(cls, **fields: Any) -> "Servers":
        item = cls(**fields)
        item.save()
        return item

    @classmethod
    def find(cls, filters: dict[str, Any] | None = None) -> list["Servers"]:
        where, values = _build_where(filters or {})
        with _connect() as connection:
            rows = connection.execute(f"SELECT * FROM servers WHERE {where} ORDER BY server", values).fetchall()
        return [cls.from_doc(dict(row)) for row in rows]

    @classmethod
    def find_one(cls, filters: dict[str, Any]) -> "Servers | None":
        where, values = _build_where(filters)
        with _connect() as connection:
            row = connection.execute(f"SELECT * FROM servers WHERE {where} LIMIT 1", values).fetchone()
        doc = _row_to_dict(row)
        return cls.from_doc(doc) if doc else None

    @classmethod
    def get_or_create(cls, server: str, **defaults: Any) -> "Servers":
        flag = _normalize_server_flag(str(defaults.get("flag") or ""))
        defaults["flag"] = flag
        found = cls.find_one({"server": server, "flag": flag})
        if found:
            return found
        return cls.create(server=server, **defaults)


@dataclass
class UsersSend:
    username: str
    server_id: str
    account_id: str | None = None
    instance_id: str = INSTANCE_ID
    status: str = "sent"
    retry_at: float | None = None
    created_at: float = field(default_factory=_now_ts)
    id: str = field(default_factory=_new_id)

    collection: ClassVar[str] = "users_send"

    @property
    def id_str(self) -> str:
        return self.id

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "UsersSend":
        return cls(
            id=str(doc.get("id") or doc.get("_id") or _new_id()),
            username=str(doc.get("username") or ""),
            server_id=str(doc.get("server_id") or ""),
            account_id=doc.get("account_id"),
            instance_id=str(doc.get("instance_id") or INSTANCE_ID),
            status=str(doc.get("status") or "sent"),
            retry_at=doc.get("retry_at"),
            created_at=float(doc.get("created_at") or _now_ts()),
        )

    def save(self) -> "UsersSend":
        now = _now_ts()
        with _connect() as connection:
            connection.execute(
                """
                INSERT INTO users_send(id,username,username_key,server_id,account_id,instance_id,status,retry_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    username=excluded.username,username_key=excluded.username_key,server_id=excluded.server_id,
                    account_id=excluded.account_id,instance_id=excluded.instance_id,status=excluded.status,
                    retry_at=excluded.retry_at,updated_at=excluded.updated_at
                """,
                (self.id, self.username, _normalize_username(self.username), self.server_id, self.account_id, self.instance_id, self.status, self.retry_at, self.created_at, now),
            )
        return self

    @classmethod
    def create(cls, **fields: Any) -> "UsersSend":
        item = cls(**fields)
        item.save()
        return item

    @classmethod
    def reserve(cls, *, server_id: Any, username: str, account_id: Any | None = None) -> "UsersSend | None":
        username_key = _normalize_username(username)
        if not username_key:
            return None
        item = cls(server_id=str(server_id), username=username.strip(), account_id=str(account_id) if account_id else None, status="reserved")
        now = item.created_at
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id, username, account_id, status, created_at, updated_at
                FROM users_send
                WHERE instance_id = ? AND server_id = ? AND username_key = ?
                """,
                (INSTANCE_ID, item.server_id, username_key),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return None
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO users_send(id,username,username_key,server_id,account_id,instance_id,status,retry_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (item.id, item.username, username_key, item.server_id, item.account_id, INSTANCE_ID, item.status, None, now, now),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                return None
            connection.commit()
        return item

    @classmethod
    def import_sent(cls, *, server_id: Any, username: str, account_id: Any | None = None) -> bool:
        username_key = _normalize_username(username)
        if not username_key:
            return False
        item = cls(server_id=str(server_id), username=username.strip(), account_id=str(account_id) if account_id else None, status="sent")
        now = item.created_at
        with _connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO users_send(id,username,username_key,server_id,account_id,instance_id,status,retry_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (item.id, item.username, username_key, item.server_id, item.account_id, INSTANCE_ID, item.status, None, now, now),
            )
            return cursor.rowcount > 0

    @classmethod
    def reconcile_sent(cls, *, server_id: Any, username: str, account_id: Any | None = None) -> bool:
        """Confirma um destinatario ja reservado ou importa um historico da Outbox."""
        username_key = _normalize_username(username)
        if not username_key:
            return False
        now = _now_ts()
        with _connect() as connection:
            cursor = connection.execute(
                """
                UPDATE users_send
                SET status = 'sent', retry_at = NULL, updated_at = ?,
                    account_id = COALESCE(account_id, ?)
                WHERE instance_id = ? AND server_id = ? AND username_key = ?
                """,
                (now, str(account_id) if account_id else None, INSTANCE_ID, str(server_id), username_key),
            )
            if cursor.rowcount > 0:
                return True
        return cls.import_sent(server_id=server_id, username=username, account_id=account_id)

    @classmethod
    def replace_server_outbox_snapshot(
        cls,
        *,
        server_id: Any,
        usernames: list[str],
        account_id: Any | None = None,
    ) -> int:
        """Importa destinatarios da Outbox sem apagar o historico local do servidor."""
        recipients: list[tuple[str, str]] = []
        seen: set[str] = set()
        for username in usernames:
            username_key = _normalize_username(username)
            if not username_key or username_key in seen:
                continue
            seen.add(username_key)
            recipients.append((username.strip(), username_key))

        now = _now_ts()
        normalized_server_id = str(server_id)
        normalized_account_id = str(account_id) if account_id else None
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                INSERT OR IGNORE INTO users_send(id,username,username_key,server_id,account_id,instance_id,status,retry_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (uuid.uuid4().hex, username, username_key, normalized_server_id, normalized_account_id, INSTANCE_ID, "sent", None, now, now)
                    for username, username_key in recipients
                ],
            )
            connection.commit()
        return len(recipients)

    @classmethod
    def update_status(cls, id_value: Any, status: str) -> None:
        with _connect() as connection:
            connection.execute("UPDATE users_send SET status = ?, updated_at = ? WHERE id = ?", (status, _now_ts(), str(id_value)))

    @classmethod
    def status_for(cls, *, server_id: Any, username: str) -> str | None:
        username_key = _normalize_username(username)
        if not username_key:
            return None
        with _connect() as connection:
            row = connection.execute(
                """
                SELECT status FROM users_send
                WHERE instance_id = ? AND server_id = ? AND username_key = ?
                LIMIT 1
                """,
                (INSTANCE_ID, str(server_id), username_key),
            ).fetchone()
        return str(row["status"]) if row else None

    @classmethod
    def count_for_server(cls, *, server_id: Any, status: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM users_send WHERE instance_id = ? AND server_id = ?"
        values: list[Any] = [INSTANCE_ID, str(server_id)]
        if status is not None:
            query += " AND status = ?"
            values.append(status)
        with _connect() as connection:
            return int(connection.execute(query, values).fetchone()[0])

    @classmethod
    def find(cls, filters: dict[str, Any] | None = None) -> list["UsersSend"]:
        where, values = _build_where(filters or {})
        with _connect() as connection:
            rows = connection.execute(f"SELECT * FROM users_send WHERE {where} ORDER BY created_at", values).fetchall()
        return [cls.from_doc(dict(row)) for row in rows]

    @classmethod
    def find_one(cls, filters: dict[str, Any]) -> "UsersSend | None":
        rows = cls.find(filters)
        return rows[0] if rows else None

    @classmethod
    def delete_many(cls, filters: dict[str, Any]) -> int:
        where, values = _build_where(filters)
        with _connect() as connection:
            cursor = connection.execute(f"DELETE FROM users_send WHERE {where}", values)
            return int(cursor.rowcount)

    @classmethod
    def delete_by_server(cls, server_id: Any) -> int:
        return cls.delete_many({"server_id": str(server_id)})


@dataclass
class RuntimeLog:
    account_id: str = ""
    level: str = "info"
    text: str = ""
    instance_id: str = INSTANCE_ID
    created_at: float = field(default_factory=_now_ts)
    id: str = field(default_factory=_new_id)

    @classmethod
    def add(cls, *, text: str, level: str = "info", account_id: str | None = None) -> None:
        clean_text = str(text or "").strip()
        if not clean_text:
            return
        init_db()
        item = cls(account_id=str(account_id or ""), level=str(level or "info"), text=clean_text)
        with _connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_logs(id,account_id,level,text,instance_id,created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (item.id, item.account_id, item.level, item.text, item.instance_id, item.created_at),
            )
            connection.execute(
                """
                DELETE FROM runtime_logs
                WHERE instance_id = ? AND account_id = ?
                  AND id NOT IN (
                      SELECT id FROM runtime_logs
                      WHERE instance_id = ? AND account_id = ?
                      ORDER BY created_at DESC
                      LIMIT 500
                  )
                """,
                (item.instance_id, item.account_id, item.instance_id, item.account_id),
            )

    @classmethod
    def list_recent(cls, account_id: str | None = None, limit: int = 500) -> list[dict[str, str]]:
        safe_limit = max(1, min(int(limit), 500))
        init_db()
        with _connect() as connection:
            if account_id:
                rows = connection.execute(
                    """
                    SELECT level, text FROM runtime_logs
                    WHERE instance_id = ? AND account_id = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (INSTANCE_ID, str(account_id), safe_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT level, text FROM runtime_logs
                    WHERE instance_id = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (INSTANCE_ID, safe_limit),
                ).fetchall()
        return [{"level": str(row["level"]), "text": str(row["text"])} for row in rows]

    @classmethod
    def clear(cls, account_id: str | None = None) -> None:
        init_db()
        with _connect() as connection:
            if account_id:
                connection.execute(
                    "DELETE FROM runtime_logs WHERE instance_id = ? AND account_id = ?",
                    (INSTANCE_ID, str(account_id)),
                )
            else:
                connection.execute("DELETE FROM runtime_logs WHERE instance_id = ?", (INSTANCE_ID,))


@dataclass
class Settings:
    time_wait: float = 1.0
    post_send_wait: float = 1.0
    logs: bool = True
    headless: bool = False
    dry_run: bool = False
    save_detailed_logs: bool = False
    default_message: str = DEFAULT_MESSAGE
    instance_id: str = INSTANCE_ID

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "Settings":
        return cls(
            time_wait=float(doc.get("time_wait") or 1.0),
            post_send_wait=float(doc.get("post_send_wait") or 1.0),
            logs=bool(doc.get("logs", True)),
            headless=bool(doc.get("headless", False)),
            dry_run=bool(doc.get("dry_run", False)),
            save_detailed_logs=bool(doc.get("save_detailed_logs", False)),
            default_message=str(doc.get("default_message") or DEFAULT_MESSAGE),
            instance_id=str(doc.get("instance_id") or INSTANCE_ID),
        )

    def save(self) -> "Settings":
        now = _now_ts()
        with _connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(id,time_wait,post_send_wait,logs,headless,dry_run,save_detailed_logs,default_message,instance_id,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    time_wait=excluded.time_wait,post_send_wait=excluded.post_send_wait,logs=excluded.logs,headless=excluded.headless,dry_run=excluded.dry_run,
                    save_detailed_logs=excluded.save_detailed_logs,default_message=excluded.default_message,
                    instance_id=excluded.instance_id,updated_at=excluded.updated_at
                """,
                (
                    _settings_doc_id(INSTANCE_ID),
                    float(self.time_wait),
                    float(self.post_send_wait),
                    int(self.logs),
                    int(self.headless),
                    int(self.dry_run),
                    int(self.save_detailed_logs),
                    self.default_message,
                    INSTANCE_ID,
                    now,
                ),
            )
        return self

    @classmethod
    def load(cls) -> "Settings":
        init_db()
        with _connect() as connection:
            row = connection.execute("SELECT * FROM settings WHERE id = ?", (_settings_doc_id(INSTANCE_ID),)).fetchone()
        doc = _row_to_dict(row)
        return cls.from_doc(doc) if doc else cls()
