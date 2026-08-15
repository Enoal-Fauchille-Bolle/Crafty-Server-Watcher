"""Tests for the transition graph — specifically its dead ends.

Every state must have a way out.  A rejected transition leaves the
machine parked, and several states (STOPPING, STARTING, CRASHED) are
returned from early in the poll loop, so parking there means the server
is never shut down again.
"""

from __future__ import annotations

import pytest

from crafty_server_watcher.config import CooldownConfig, ServerConfig
from crafty_server_watcher.server_state import _VALID_TRANSITIONS, ServerStateMachine, State


@pytest.fixture
def sm():
    return ServerStateMachine(
        cfg=ServerConfig(name="server-1", crafty_server_id="uuid-1", listen_port=25500),
        cooldowns=CooldownConfig(),
    )


def test_no_state_is_a_dead_end():
    for state, targets in _VALID_TRANSITIONS.items():
        assert targets, f"{state.value} has no outgoing transition"


def test_crashed_can_reach_idle(sm):
    """Crafty reports crashed=true transiently while a server boots.

    Once it clears, a running server with no players is IDLE.  Without
    this edge the machine stays CRASHED forever and never shuts down.
    """
    sm.transition(State.CRASHED)
    sm.transition(State.IDLE)
    assert sm.state is State.IDLE


def test_stopping_can_roll_back_to_idle(sm):
    """A failed stop_server call must be retryable on the next poll."""
    sm.transition(State.IDLE)
    sm.transition(State.STOPPING)
    sm.transition(State.IDLE)
    assert sm.state is State.IDLE


def test_starting_can_fall_back_to_idle(sm):
    """Start timed out while the process runs: treat it as up, not stuck."""
    sm.transition(State.STOPPED)
    sm.transition(State.STARTING)
    sm.transition(State.IDLE)
    assert sm.state is State.IDLE


def test_crashed_can_be_woken(sm):
    """_handle_login() wakes servers from CRASHED as well as STOPPED."""
    sm.transition(State.CRASHED)
    sm.transition(State.STARTING)
    assert sm.state is State.STARTING


def test_invalid_transition_is_ignored(sm):
    sm.transition(State.STOPPED)
    sm.transition(State.IDLE)  # STOPPED → IDLE is not allowed
    assert sm.state is State.STOPPED


def test_idle_clock_starts_on_idle_and_clears_on_online(sm):
    sm.transition(State.IDLE)
    assert sm.idle_since is not None
    sm.transition(State.ONLINE)
    assert sm.idle_since is None
    assert sm.idle_elapsed() == 0.0


def test_proxy_is_only_needed_when_the_port_is_free(sm):
    """STARTING must not hold the port — the MC server needs it."""
    for state in (State.STOPPED, State.CRASHED):
        sm.state = state
        assert sm.is_proxy_needed

    for state in (State.ONLINE, State.IDLE, State.STARTING, State.STOPPING):
        sm.state = state
        assert not sm.is_proxy_needed
