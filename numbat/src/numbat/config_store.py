"""Persistence for the in-app configuration (issue #5).

Numbat owns its config document: /data/numbat-config.json under the Supervisor
(/data persists across restarts and add-on updates), ./numbat-config.json in a
standalone dev checkout, NUMBAT_CONFIG_FILE to override. The Supervisor add-on
options are reduced to log_level only; everything else is edited in the
dashboard's Settings view and validated against the same pydantic Settings
the planner consumes.

Writes are atomic (tmp + rename) and keep the previous version as .bak. A
schema_version field exists from day one so future migrations are mechanical.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from pydantic import ValidationError

from numbat.config import Settings

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SUPERVISOR_CONFIG_FILE = "/data/numbat-config.json"
DEV_CONFIG_FALLBACK = "numbat-config.json"


def resolve_config_path(explicit: Path | None = None) -> Path:
    if explicit:
        return explicit
    # Gate on the Supervisor token, not on /data existing — any Linux box can
    # have a /data directory, and silently writing there (or failing on its
    # permissions) is a confusing dev experience.
    if os.environ.get("SUPERVISOR_TOKEN") and Path("/data").is_dir():
        return Path(SUPERVISOR_CONFIG_FILE)
    return Path(DEV_CONFIG_FALLBACK)


class ConfigStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Settings | None:
        """None means unconfigured — missing, unreadable, or invalid file.
        The Settings UI is the recovery path either way; an invalid file is
        left on disk untouched (save() moves it to .bak on the next write)."""
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.error("config %s unreadable (%s); starting unconfigured", self.path, e)
            return None
        if not isinstance(raw, dict):
            log.error("config %s is not a JSON object; starting unconfigured", self.path)
            return None
        if raw.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            log.warning(
                "config %s has schema_version %r (this build writes %r); attempting to read",
                self.path,
                raw.get("schema_version"),
                SCHEMA_VERSION,
            )
        try:
            return Settings.model_validate(raw.get("config") or {})
        except ValidationError as e:
            log.error("config %s invalid; starting unconfigured\n%s", self.path, e)
            return None

    def save(self, settings: Settings) -> None:
        doc = {
            "schema_version": SCHEMA_VERSION,
            "config": settings.model_dump(mode="json"),
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(doc, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())  # this file is the only copy of the config
        bak = self.path.with_name(self.path.name + ".bak")
        if self.path.exists():
            # Hardlink (not rename) the previous version to .bak so the live
            # path is never absent — a crash here still leaves the old config.
            bak.unlink(missing_ok=True)
            os.link(self.path, bak)
        os.replace(tmp, self.path)


class ConfigController:
    """The live config, shared by the web API (writes) and the main loop
    (applies). apply() persists first, then flips the in-memory pointer and
    wakes the main loop, which rebuilds its components before the next cycle —
    no add-on restart. Single-threaded asyncio: no locking needed as long as
    readers grab `current` without awaiting in between."""

    def __init__(self, store: ConfigStore, current: Settings | None):
        self.store = store
        self.current = current
        self.changed = asyncio.Event()

    def apply(self, settings: Settings) -> None:
        self.store.save(settings)
        self.current = settings
        self.changed.set()
