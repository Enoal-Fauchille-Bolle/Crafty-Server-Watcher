"""Tests for state persistence across watcher restarts."""

from __future__ import annotations

import json
import time

import pytest

from crafty_server_watcher.config import CooldownConfig, ServerConfig
from crafty_server_watcher.server_state import ServerStateMachine, State
from crafty_server_watcher.state_store import StateStore


def make_sm(name="server-1", port=25500):
    return ServerStateMachine(
        cfg=ServerConfig(name=name, crafty_server_id=f"uuid-{name}", listen_port=port),
        cooldowns=CooldownConfig(),
    )


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state.json")


def test_load_without_file_returns_empty(store):
    assert store.load() == {}


def test_idle_clock_survives_a_restart(store):
    """The whole point: a restart must not reset the idle countdown."""
    sm = make_sm()
    sm.transition(State.IDLE)
    sm.idle_since = time.monotonic() - 300  # already idle for 5 minutes
    store.save({"server-1": sm})

    # A restart: fresh machine, fresh monotonic origin.
    restored = store.load()
    fresh = make_sm()
    assert fresh.restore(restored["server-1"], running=True)

    assert fresh.state is State.IDLE
    assert fresh.idle_elapsed() == pytest.approx(300, abs=5)


def test_snapshot_is_discarded_when_server_is_no_longer_running(store):
    """Someone stopped the server while the watcher was down."""
    sm = make_sm()
    sm.transition(State.IDLE)
    store.save({"server-1": sm})

    fresh = make_sm()
    assert not fresh.restore(store.load()["server-1"], running=False)
    assert fresh.state is State.UNKNOWN


def test_snapshot_is_discarded_when_server_started_meanwhile(store):
    sm = make_sm()
    sm.transition(State.STOPPED)
    store.save({"server-1": sm})

    fresh = make_sm()
    assert not fresh.restore(store.load()["server-1"], running=True)


def test_stale_snapshot_is_ignored(tmp_path):
    """After a long outage the recorded idle time is meaningless."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "saved_at": time.time() - 48 * 3600,
                "servers": {"server-1": {"state": "IDLE"}},
            }
        ),
        encoding="utf-8",
    )
    assert StateStore(path).load() == {}


def test_unknown_schema_is_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"schema": 999, "saved_at": time.time(), "servers": {}}),
        encoding="utf-8",
    )
    assert StateStore(path).load() == {}


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{truncated", encoding="utf-8")
    assert StateStore(path).load() == {}


def test_save_leaves_no_temp_files_behind(store, tmp_path):
    sm = make_sm()
    sm.transition(State.IDLE)
    store.save({"server-1": sm})
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_on_change_hook_persists_every_transition(store):
    sm = make_sm()
    sm.on_change = lambda: store.save({"server-1": sm})

    sm.transition(State.STOPPED)
    sm.transition(State.STARTING)

    assert store.load()["server-1"]["state"] == "STARTING"
