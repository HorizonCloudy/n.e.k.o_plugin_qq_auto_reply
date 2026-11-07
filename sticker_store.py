"""Single durable inventory for uploaded stickers and collected gallery images.

Transactions are synchronous on the plugin event loop: callers must reload after
any await, never write a snapshot held across model/network calls.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .sticker_scope import entry_scope


class StickerStore:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    @property
    def index(self) -> Path:
        return Path(self.plugin.data_path("sticker.json"))

    @property
    def directory(self) -> Path:
        return Path(self.plugin.data_path("sticker"))

    def read(self) -> dict:
        try:
            data = json.loads(self.index.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError("sticker.json must contain an object")
        return data

    def write(self, data: dict) -> None:
        self.index.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.index.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.index)
        service = getattr(self.plugin, "session_instruction_service", None)
        if service is not None:
            service._sticker_catalog_cache = ""

    def update(self, sid: str, expected_path: str, fields: dict) -> bool:
        data = self.read()
        entry = data.get(sid)
        if not isinstance(entry, dict) or entry.get("path") != expected_path:
            return False  # Deleted/replaced while an async operation was in flight.
        entry.update(fields)
        self.write(data)
        return True

    def find(self, data: dict, digest: str, scope: str):
        for sid, entry in data.items():
            if not isinstance(entry, dict) or entry_scope(entry) != scope:
                continue
            if entry.get("md5") == digest or f"sticker_{digest[:12]}_" in str(entry.get("path") or ""):
                return sid, entry
        return None

    def add(self, raw: bytes, ext: str, fields: dict, *, limit: int | None = None):
        data = self.read()
        digest = hashlib.md5(raw).hexdigest()
        scope = entry_scope(fields)
        found = self.find(data, digest, scope)
        if found:
            sid, entry = found
            root = self.directory.resolve()
            path = (root / str(entry.get("path") or "")).resolve()
            if path.parent != root:
                raise ValueError("Unsafe image path in sticker inventory")
            # Inbox sources may only be removed once a real durable image exists.
            # Repair a missing file instead of reporting a metadata-only duplicate.
            if not path.is_file():
                self.directory.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            entry["times"] = int(entry.get("times") or 1) + 1
            entry["last_seen"] = fields.get("last_seen", fields.get("added_at", 0))
            if fields.get("desc") and not entry.get("desc"):
                entry["desc"] = fields["desc"]
            if fields.get("kind") == "sticker":
                entry.update(kind="sticker", sendable=True)
            self.write(data)
            return sid, entry, False
        if limit is not None and len(data) >= limit:
            return None  # Never evict the user's manually maintained inventory.
        scope_hash = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
        name = f"sticker_{digest[:12]}_{scope_hash}.{ext.lstrip('.')}"
        target = self.directory / name
        self.directory.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        sid = str(max((int(k) for k in data if k.isdigit()), default=0) + 1)
        entry = {**fields, "md5": digest, "path": name}
        data[sid] = entry
        try:
            if not existed:
                target.write_bytes(raw)
            self.write(data)
        except Exception:
            if not existed:
                target.unlink(missing_ok=True)
            raise
        return sid, entry, True
