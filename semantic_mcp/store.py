"""Persistent caller-scoped resolutions, saved specs, cache and audit. No result rows are stored."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, insert, select
from sqlalchemy.exc import IntegrityError

from semantic_mcp.contracts import CannotAnswer, Resolution


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, url):
        if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url)
        metadata = MetaData()
        self.resolutions = Table(
            "resolutions",
            metadata,
            Column("id", String(64), primary_key=True),
            Column("caller", String(200), nullable=False),
            Column("model_hash", String(80), nullable=False),
            Column("payload", Text, nullable=False),
        )
        self.saved = Table(
            "saved_queries",
            metadata,
            Column("id", String(64), primary_key=True),
            Column("caller", String(200), nullable=False),
            Column("name", String(200), nullable=False),
            Column("model_hash", String(80), nullable=False),
            Column("payload", Text, nullable=False),
            Column("created_at", String(40), nullable=False),
        )
        self.cache = Table(
            "spec_cache",
            metadata,
            Column("key", String(64), primary_key=True),
            Column("payload", Text, nullable=False),
        )
        self.audit = Table(
            "audit_log",
            metadata,
            Column("request_id", String(64), primary_key=True),
            Column("caller", String(200), nullable=False),
            Column("created_at", String(40), nullable=False),
            Column("payload", Text, nullable=False),
        )
        metadata.create_all(self.engine)

    def cached(self, key):
        with self.engine.connect() as conn:
            payload = conn.execute(select(self.cache.c.payload).where(self.cache.c.key == key)).scalar()
        return Resolution.model_validate_json(payload) if payload else None

    def cache_put(self, key, resolution):
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(self.cache).values(key=key, payload=resolution.model_dump_json()))
        except IntegrityError:
            pass  # A concurrent request already established the canonical resolution.
        return self.cached(key)

    def record_resolution(self, caller, model_hash, payload):
        resolution_id = new_id("res")
        with self.engine.begin() as conn:
            conn.execute(
                insert(self.resolutions).values(
                    id=resolution_id, caller=caller, model_hash=model_hash, payload=json.dumps(payload)
                )
            )
        return resolution_id

    def save(self, resolution_id, name, caller, model_hash):
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(self.resolutions).where(
                        self.resolutions.c.id == resolution_id, self.resolutions.c.caller == caller
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise CannotAnswer("not_found", "Resolution not found for this caller.")
            if row["model_hash"] != model_hash:
                raise CannotAnswer("model_changed", "The resolution belongs to a different model version.")
            saved_id = new_id("qry")
            conn.execute(
                insert(self.saved).values(
                    id=saved_id,
                    caller=caller,
                    name=name,
                    model_hash=model_hash,
                    payload=row["payload"],
                    created_at=timestamp(),
                )
            )
        payload = json.loads(row["payload"])
        return {"saved_query_id": saved_id, "name": name, "stored_interpretation": payload["interpretation"]}

    def get_saved(self, saved_id, caller):
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(self.saved).where(self.saved.c.id == saved_id, self.saved.c.caller == caller)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise CannotAnswer("not_found", "Saved query not found for this caller.")
        return {
            "saved_query_id": row["id"],
            "name": row["name"],
            "creator": row["caller"],
            "model_file_hash": row["model_hash"],
            "created_at": row["created_at"],
            **json.loads(row["payload"]),
        }

    def log(self, request_id, caller, payload):
        with self.engine.begin() as conn:
            conn.execute(
                insert(self.audit).values(
                    request_id=request_id,
                    caller=caller,
                    created_at=timestamp(),
                    payload=json.dumps(payload, default=str),
                )
            )

    def close(self):
        self.engine.dispose()
