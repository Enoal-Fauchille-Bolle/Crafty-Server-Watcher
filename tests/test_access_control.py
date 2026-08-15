"""Tests for whitelist-based wake-up control."""

from __future__ import annotations

import json
import os

import pytest

from crafty_server_watcher.access_control import AccessController
from crafty_server_watcher.config import AccessConfig


def write_whitelist(path, names):
    path.write_text(
        json.dumps([{"uuid": f"uuid-{n}", "name": n} for n in names]),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def whitelist(tmp_path):
    return write_whitelist(tmp_path / "whitelist.json", ["AZERTY____", "noritonsvn"])


def test_disabled_mode_allows_everyone(whitelist):
    ac = AccessController("s1", AccessConfig(mode="off", whitelist_file=str(whitelist)))
    assert ac.is_allowed("Cornbread2100_")
    assert not ac.enabled


def test_whitelisted_player_is_allowed(whitelist):
    ac = AccessController("s1", AccessConfig(mode="whitelist", whitelist_file=str(whitelist)))
    assert ac.is_allowed("AZERTY____")


def test_unknown_player_is_denied(whitelist):
    ac = AccessController("s1", AccessConfig(mode="whitelist", whitelist_file=str(whitelist)))
    assert not ac.is_allowed("Cornbread2100_")


def test_name_match_ignores_case(whitelist):
    """Minecraft names are case-insensitive for whitelist purposes."""
    ac = AccessController("s1", AccessConfig(mode="whitelist", whitelist_file=str(whitelist)))
    assert ac.is_allowed("azerty____")
    assert ac.is_allowed("NORITONSVN")


def test_extra_allowed_players_bypass_the_file(whitelist):
    ac = AccessController(
        "s1",
        AccessConfig(
            mode="whitelist",
            whitelist_file=str(whitelist),
            allowed_players=["Enoal"],
        ),
    )
    assert ac.is_allowed("Enoal")
    assert not ac.is_allowed("Cornbread2100_")


def test_missing_file_fails_open(tmp_path):
    """A broken bind mount must not lock the owner out of their own server."""
    ac = AccessController(
        "s1",
        AccessConfig(mode="whitelist", whitelist_file=str(tmp_path / "absent.json")),
    )
    assert ac.is_allowed("anyone")


def test_malformed_file_fails_open(tmp_path):
    bad = tmp_path / "whitelist.json"
    bad.write_text("{not json", encoding="utf-8")
    ac = AccessController("s1", AccessConfig(mode="whitelist", whitelist_file=str(bad)))
    assert ac.is_allowed("anyone")


def test_file_changes_are_picked_up(tmp_path):
    """`/whitelist add` must take effect without restarting the watcher."""
    path = write_whitelist(tmp_path / "whitelist.json", ["AZERTY____"])
    ac = AccessController("s1", AccessConfig(mode="whitelist", whitelist_file=str(path)))
    assert not ac.is_allowed("Nora")

    write_whitelist(path, ["AZERTY____", "Nora"])
    # mtime granularity can be coarse; force a distinct value.
    stat = path.stat()
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))

    assert ac.is_allowed("Nora")


def test_update_applies_reloaded_config(whitelist, tmp_path):
    ac = AccessController("s1", AccessConfig(mode="whitelist", whitelist_file=str(whitelist)))
    assert not ac.is_allowed("Yeth_")

    other = write_whitelist(tmp_path / "other.json", ["Yeth_"])
    ac.update(AccessConfig(mode="whitelist", whitelist_file=str(other)))
    assert ac.is_allowed("Yeth_")
    assert not ac.is_allowed("AZERTY____")
