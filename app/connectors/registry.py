"""Maps connector type names to implementations and describes their config forms."""
from __future__ import annotations

from .base import BaseDestination, BaseSource, ConnectorError
from .mysql import MysqlDestination, MysqlSource
from .postgres import PostgresDestination, PostgresSource
from .sqlite import SqliteDestination, SqliteSource

SOURCE_TYPES: dict[str, type[BaseSource]] = {
    "postgres": PostgresSource,
    "mysql": MysqlSource,
    "sqlite": SqliteSource,
}

DESTINATION_TYPES: dict[str, type[BaseDestination]] = {
    "postgres": PostgresDestination,
    "mysql": MysqlDestination,
    "sqlite": SqliteDestination,
}

# Field specs drive both the web UI form and light server-side validation.
# kind: text | password | number | checkbox | select (with an "options" list)
_HOST_FIELDS = lambda default_port: [
    {"name": "host", "label": "Host", "kind": "text", "required": True, "default": "localhost"},
    {"name": "port", "label": "Port", "kind": "number", "required": True, "default": default_port},
    {"name": "database", "label": "Database", "kind": "text", "required": True},
    {"name": "username", "label": "Username", "kind": "text", "required": True},
    {"name": "password", "label": "Password", "kind": "password", "required": False},
]

PG_SSL_MODES = ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]
MYSQL_SSL_MODES = ["DISABLED", "PREFERRED", "REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"]

CONFIG_SPEC: dict[str, list[dict]] = {
    "postgres": _HOST_FIELDS(5432) + [
        {"name": "schemas", "label": "Schemas (comma separated, blank = all)", "kind": "text", "required": False},
        {"name": "sslmode", "label": "SSL mode", "kind": "select", "options": PG_SSL_MODES,
         "required": False, "default": "prefer"},
        {"name": "replication_slot", "label": "Replication slot name (CDC)", "kind": "text",
         "required": False, "default": "holypipe_slot"},
    ],
    "mysql": _HOST_FIELDS(3306) + [
        {"name": "ssl_mode", "label": "SSL mode", "kind": "select", "options": MYSQL_SSL_MODES,
         "required": False, "default": "DISABLED"},
        {"name": "server_id", "label": "Binlog server id (CDC, must be unique)", "kind": "number",
         "required": False, "default": 1047200},
    ],
    "sqlite": [
        {"name": "path", "label": "File path (inside the container, e.g. /data/app.db)",
         "kind": "text", "required": True},
    ],
}


def config_spec(kind: str, conn_type: str) -> list[dict]:
    return CONFIG_SPEC.get(conn_type, [])


def _normalize(config: dict) -> dict:
    config = dict(config or {})
    schemas = config.get("schemas")
    if isinstance(schemas, str):
        config["schemas"] = [s.strip() for s in schemas.split(",") if s.strip()]
    return config


def build_source(conn_type: str, config: dict) -> BaseSource:
    cls = SOURCE_TYPES.get(conn_type)
    if not cls:
        raise ConnectorError(f"Unknown source type '{conn_type}'")
    return cls(_normalize(config))


def build_destination(conn_type: str, config: dict) -> BaseDestination:
    cls = DESTINATION_TYPES.get(conn_type)
    if not cls:
        raise ConnectorError(f"Unknown destination type '{conn_type}'")
    return cls(_normalize(config))
