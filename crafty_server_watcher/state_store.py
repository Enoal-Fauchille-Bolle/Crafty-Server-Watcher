"""Crash-safe persistence of per-server state across watcher restarts.

Without this, every restart of the watcher (container recreation, image
update, reboot) throws away ``idle_since`` and the idle countdown starts
over.  A watcher restarted more often than ``idle_timeout_minutes`` can
therefore *never* shut a server down.

The timestamps in :class:`~.server_state.ServerStateMachine` come from
``time.monotonic()``, whose origin is arbitrary and changes on every
process start — they are meaningless once written to disk.  This module
converts them to and from wall-clock time (``time.time()``) on the way
through, so a saved deadline still means the same instant after a
restart.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Bump when the on-disk layout changes incompatibly.
SCHEMA_VERSION = 1

# Ignore snapshots older than this: after a long outage the recorded idle
# time is stale, and resuming a half-finished countdown is worse than
# starting a fresh one.
MAX_SNAPSHOT_AGE_SECONDS = 24 * 3600

# Fields holding a monotonic timestamp that may be None.
_TIMESTAMP_FIELDS = ("idle_since", "last_stop_time", "last_start_time")


class StateStore:
    """Reads and writes a JSON snapshot of every server's state machine.

    Parameters
    ----------
    path:
        File to persist to.  Its parent directory must exist and be
        writable; the store degrades to a no-op if it is not.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._enabled = True

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def save(self, state_machines: dict[str, Any]) -> None:
        """Write a snapshot of *state_machines* atomically.

        Failures are logged once and then silently tolerated: losing the
        snapshot degrades behaviour back to the in-memory-only default,
        which must never take the watcher down.
        """
        if not self._enabled:
            return

        now_mono = time.monotonic()
        now_wall = time.time()

        payload = {
            "schema": SCHEMA_VERSION,
            "saved_at": now_wall,
            "servers": {
                name: self._dump_one(sm, now_mono, now_wall) for name, sm in state_machines.items()
            },
        }

        try:
            self._write_atomic(payload)
        except OSError as exc:
            log.error(
                f"Cannot persist watcher state to {self._path}: {exc} — continuing without it"
            )
            self._enabled = False

    def _write_atomic(self, payload: dict[str, Any]) -> None:
        """Write via a temp file in the same directory, then rename.

        ``os.replace`` is atomic on POSIX, so a crash mid-write leaves the
        previous snapshot intact rather than a truncated file.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    @staticmethod
    def _dump_one(sm: Any, now_mono: float, now_wall: float) -> dict[str, Any]:
        """Serialise one state machine, monotonic → wall clock."""

        def to_wall(value: float | None) -> float | None:
            if value is None:
                return None
            return now_wall - (now_mono - value)

        return {
            "state": sm.state.value,
            "idle_since": to_wall(sm.idle_since),
            "last_stop_time": to_wall(sm.last_stop_time),
            "last_start_time": to_wall(sm.last_start_time),
            "start_stop_history": [to_wall(ts) for ts in sm.start_stop_history],
            "start_count": sm.start_count,
            "stop_count": sm.stop_count,
        }

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def load(self) -> dict[str, dict[str, Any]]:
        """Return the saved per-server snapshots, wall clock → monotonic.

        Returns an empty mapping when there is nothing usable to restore:
        no file, unreadable file, wrong schema, or a snapshot too old to
        be meaningful.  Callers treat that as "start fresh".
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.info(f"No previous watcher state at {self._path} — starting fresh")
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(f"Ignoring unreadable watcher state at {self._path}: {exc}")
            return {}

        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
            log.warning(f"Ignoring watcher state at {self._path}: unsupported schema")
            return {}

        saved_at = raw.get("saved_at")
        if not isinstance(saved_at, (int, float)):
            log.warning(f"Ignoring watcher state at {self._path}: missing save timestamp")
            return {}

        age = time.time() - saved_at
        if age < 0 or age > MAX_SNAPSHOT_AGE_SECONDS:
            log.warning(
                f"Ignoring watcher state at {self._path}: snapshot is {age / 3600:.1f}h old",
            )
            return {}

        now_mono = time.monotonic()
        now_wall = time.time()

        def to_mono(value: Any) -> float | None:
            if not isinstance(value, (int, float)):
                return None
            return now_mono - (now_wall - value)

        restored: dict[str, dict[str, Any]] = {}
        for name, entry in (raw.get("servers") or {}).items():
            if not isinstance(entry, dict):
                continue
            restored[name] = {
                "state": entry.get("state"),
                **{field: to_mono(entry.get(field)) for field in _TIMESTAMP_FIELDS},
                "start_stop_history": [
                    ts for ts in (to_mono(v) for v in entry.get("start_stop_history") or []) if ts
                ],
                "start_count": int(entry.get("start_count") or 0),
                "stop_count": int(entry.get("stop_count") or 0),
            }

        if restored:
            log.info(
                f"Restored watcher state for {len(restored)} server(s) from {self._path} "
                f"({age:.0f}s old)",
            )
        return restored
