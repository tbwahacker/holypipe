"""Parses a connection URI/DSN string into a connector config dict.

Lets a user paste `postgresql://user:pass@host:5432/db?sslmode=require`
instead of filling in the individual form fields.
"""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

PG_SCHEMES = {"postgres", "postgresql", "postgres+psycopg2"}
MYSQL_SCHEMES = {"mysql", "mysql+pymysql"}
SQLITE_SCHEMES = {"sqlite", "sqlite3"}


class DsnError(ValueError):
    pass


def parse_dsn(conn_type: str, uri: str) -> dict:
    uri = (uri or "").strip()
    if not uri:
        raise DsnError("Connection URI is empty")

    if conn_type == "sqlite":
        if "://" in uri:
            scheme, rest = uri.split("://", 1)
            if scheme not in SQLITE_SCHEMES:
                raise DsnError(f"Unrecognized SQLite URI scheme '{scheme}'")
            # sqlite:////abs/path -> rest == "/abs/path"; sqlite:///rel/path -> rest == "rel/path"
            path = rest
        else:
            path = uri
        if not path:
            raise DsnError("SQLite URI is missing a file path")
        return {"path": path}

    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if scheme in PG_SCHEMES:
        conn_type_expected = "postgres"
        default_port = 5432
    elif scheme in MYSQL_SCHEMES:
        conn_type_expected = "mysql"
        default_port = 3306
    else:
        raise DsnError(f"Unrecognized connection URI scheme '{parsed.scheme}'")

    if conn_type != conn_type_expected:
        raise DsnError(f"URI scheme '{parsed.scheme}' does not match connector type '{conn_type}'")

    database = (parsed.path or "").lstrip("/")
    config: dict = {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or default_port,
        "database": database or None,
        "username": unquote(parsed.username) if parsed.username else "",
        "password": unquote(parsed.password) if parsed.password else "",
    }

    qs = parse_qs(parsed.query)

    def first(*keys: str) -> str | None:
        for k in keys:
            if k in qs and qs[k]:
                return qs[k][0]
        return None

    if conn_type == "postgres":
        sslmode = first("sslmode", "ssl_mode", "ssl-mode")
        if sslmode:
            config["sslmode"] = sslmode
        schemas = first("schemas")
        if schemas:
            config["schemas"] = schemas
        slot = first("replication_slot", "slot")
        if slot:
            config["replication_slot"] = slot
    elif conn_type == "mysql":
        ssl_mode = first("ssl_mode", "ssl-mode", "sslmode")
        if ssl_mode:
            config["ssl_mode"] = ssl_mode.upper()
        server_id = first("server_id")
        if server_id:
            config["server_id"] = int(server_id)

    return config
