# Crafty Server Watcher

[![Lint](https://github.com/Soveticka/crafty-server-watcher/actions/workflows/lint.yml/badge.svg)](https://github.com/Soveticka/crafty-server-watcher/actions/workflows/lint.yml)
[![Docker Build](https://github.com/Soveticka/crafty-server-watcher/actions/workflows/docker-build.yml/badge.svg)](https://github.com/Soveticka/crafty-server-watcher/actions/workflows/docker-build.yml)
[![CodeQL](https://github.com/Soveticka/crafty-server-watcher/actions/workflows/codeql.yml/badge.svg)](https://github.com/Soveticka/crafty-server-watcher/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/github/license/Soveticka/crafty-server-watcher)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

Auto-hibernate idle Minecraft servers and wake them on player connect, powered by the [Crafty Controller](https://craftycontrol.com) ([GitHub](https://gitlab.com/crafty-controller/crafty-4)) API v2.

---

> ### About this fork
>
> This is [Enoal-Fauchille-Bolle/Crafty-Server-Watcher](https://github.com/Enoal-Fauchille-Bolle/Crafty-Server-Watcher), a fork of [Soveticka/Crafty-Server-Watcher](https://github.com/Soveticka/Crafty-Server-Watcher) used in production by the [Astra-ops](https://github.com/Enoal-Fauchille-Bolle/Astra-ops) homelab. `main` tracks upstream plus the changes below; images are published to `ghcr.io/enoal-fauchille-bolle/crafty-server-watcher`.
>
> Each change is offered upstream as its own pull request. Everything is opt-in and defaults to upstream behaviour.
>
> | Change | Why |
> |---|---|
> | **Wake-up whitelist** (`access:`) | Internet-wide scanners boot your servers just by connecting. The server's own whitelist refuses them *after* the JVM is up; this refuses them before. Off by default. |
> | **State persistence** (`state:`) | Idle countdowns lived in memory only. A watcher restarted more often than `idle_timeout_minutes` — a GitOps redeploy loop, say — can never reach the threshold, so servers run forever. Off by default. |
> | **No dead-end states** | `CRASHED → IDLE`, `STOPPING → IDLE`, `STARTING → IDLE` and `STOPPED → IDLE` were missing from the transition graph. Each rejected transition parked a server in a state the poll loop returns from early, so it was never shut down again. |
> | **Start deadline while running** | In `STARTING`, leaving the state depended solely on Crafty's internal ping. If that never went green the server stayed `STARTING` forever. `start_timeout_seconds` now applies there too. |
> | **Stop rollback and notification fixes** | A failed `stop_server` rolled back to a rejected state; the Discord webhook was awaited inside the same `try`, so a rate limit undid a successful stop; and `idle_seconds` was read after the state reset, always reporting 0. |
> | **Crafty's own events** (`crafty_events:`) | The watcher holds the port while a server sleeps, and only polls every 30s, so anything it did not decide itself is an interval late — with the port in the wrong hands throughout. A server started from Crafty's UI dies with `FAILED TO BIND TO PORT`; a server stopped from it leaves the port unanswered, where a player sees a refused connection instead of the hibernating MOTD and cannot wake it. Listening to `start_server`, `stop_server`, `kill` and `crash_detected` closes both gaps. Off by default. |
> | **Tests** | `tests/` covers the whitelist, persistence, the transition graph, and the port-steal race — the last over a real socket. Run with `pytest tests/ -q`. |

---

## Features

- **Idle shutdown** — Stops servers via Crafty API when 0 players for a configurable duration
- **Wake-on-connect** — Binds to MC ports while servers are offline; shows a custom MOTD and kicks login attempts with a "starting…" message, then triggers a start via Crafty API
- **Multi-server** — Manage any number of Minecraft Java & Bedrock servers, each on a separate port
- **Bedrock Edition support** — UDP/RakNet proxy for Bedrock servers alongside Java TCP proxies
- **Health & metrics** — `/health`, `/status` (JSON), and `/metrics` (Prometheus) endpoints
- **Discord notifications** — Webhook alerts on server start, stop, and crash events
- **Config hot-reload** — Send SIGHUP to reload timeouts, MOTDs, and cooldowns without restart
- **Anti-flap** — Start grace, stop cooldown, and cycle-count-based flap guard
- **Wake-up whitelist** — Optionally refuse a start for players absent from the server's `whitelist.json`, so scanners never boot a JVM
- **State persistence** — Optionally keep idle countdowns across restarts, so a redeploy does not reset the clock
- **Crafty's own events** — Optionally receive Crafty's `start_server` / `stop_server` / `kill` / `crash_detected` webhooks, and hand the port over or take it back the moment a server changes hands, instead of a poll later
- **Minimal dependencies** — Python 3.11 + PyYAML only

## Requirements

- Crafty Controller 4.x with API v2 access
- A dedicated Crafty API user/role (see [Crafty API Setup](#crafty-api-setup))

---

## Deployment — Docker (Recommended)

### 1. Create your config

```bash
mkdir crafty-server-watcher && cd crafty-server-watcher
curl -O https://raw.githubusercontent.com/Soveticka/crafty-server-watcher/main/config.example.yaml
cp config.example.yaml config.yaml
nano config.yaml    # set your Crafty server UUIDs and ports
```

### 2. Create a `.env` file for the API token

```bash
echo "CRAFTY_API_TOKEN=your-token-here" > .env
chmod 600 .env
```

### 3. Create `docker-compose.yml`

```yaml
services:
  crafty-server-watcher:
    image: ghcr.io/Soveticka/crafty-server-watcher:latest
    container_name: crafty-server-watcher
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./config.yaml:/config/config.yaml:ro
    environment:
      - CRAFTY_API_TOKEN=${CRAFTY_API_TOKEN}
```

> **Why `network_mode: host`?** The service must bind directly to the MC server ports on your host so that players connect to the proxy when servers are hibernating.

### 4. Start

```bash
docker compose up -d
docker compose logs -f
```

### Updating (Docker)

```bash
docker compose pull
docker compose up -d
```

---

## Deployment — Manual

<details>
<summary>Click to expand manual deployment instructions</summary>

### Requirements

- Linux (tested on Debian 13 / Trixie)
- Python ≥ 3.11
- PyYAML (`pip install pyyaml`)

### Install

```bash
# 1. Clone
sudo git clone https://github.com/Soveticka/crafty-server-watcher.git /opt/crafty-server-watcher
cd /opt/crafty-server-watcher

# 2. Run installer (creates user, venv, directories, systemd service)
sudo bash install.sh

# 3. Configure
sudo nano /etc/crafty-server-watcher/config.yaml   # set server IDs & ports
sudo nano /etc/crafty-server-watcher/env            # set CRAFTY_API_TOKEN

# 4. Start
sudo systemctl start crafty-server-watcher
sudo systemctl status crafty-server-watcher

# 5. Logs
journalctl -u crafty-server-watcher -f
tail -f /var/log/crafty-server-watcher/service.log
```

### Updating (Manual)

```bash
cd /opt/crafty-server-watcher
sudo systemctl stop crafty-server-watcher
sudo git pull
sudo find . -type d -name __pycache__ -exec rm -rf {} +
sudo systemctl start crafty-server-watcher
```

If the systemd service file changed:

```bash
sudo cp systemd/crafty-server-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart crafty-server-watcher
```

</details>

---

## Configuration

See [`config.example.yaml`](config.example.yaml) for all available options.

### Crafty API Setup

1. Create a **dedicated Crafty user** (e.g., `auto-watcher`)
2. Create a **role** with only **Commands** permission on your managed servers
3. Assign the role to the user
4. Generate a long-lived API token for this user
5. Pass the token via the `CRAFTY_API_TOKEN` environment variable

### Server Mapping

Each server entry maps a listening port to a Crafty server UUID:

```yaml
servers:
  vanilla:
    crafty_server_id: "5adcec83-684c-4555-a7e9-9d913203d07e"
    listen_port: 25565
    idle_timeout_minutes: 10
    start_timeout_seconds: 180
```

The `crafty_server_id` can be found in the Crafty dashboard URL or via `GET /api/v2/servers`.

### Wake-up Whitelist

Any login attempt starts the server, and Internet-wide scanners find open Minecraft ports within hours. `access:` refuses the start for players the server would reject anyway — before the JVM boots.

```yaml
servers:
  vanilla:
    access:
      mode: whitelist       # "off" (default) or "whitelist"
      whitelist_file: "/servers/<uuid>/whitelist.json"
      allowed_players: []   # extra names, beyond the file
      deny_message: "§cThis server is private."
```

Pointing at the server's own `whitelist.json` keeps a single list: `/whitelist add` in Crafty is picked up automatically, without a restart. Mount the Crafty servers directory read-only so the watcher can see it:

```yaml
volumes:
  - /opt/docker-data/crafty/servers:/servers:ro
```

Two limits worth knowing:

- The player name comes from the Login Start packet, **before** Mojang authentication — it is a claim, not a proof. This stops scanners trying arbitrary names; it is not a security boundary.
- Java edition only. The Bedrock/RakNet handshake carries no player name for the proxy to inspect.

If the whitelist cannot be read, the watcher **fails open** (allows everyone) and logs an error, so a broken mount degrades to upstream behaviour instead of locking you out of your own servers.

### State Persistence

Idle countdowns live in memory. Set `state.file` to keep them across restarts:

```yaml
state:
  file: "/data/state.json"
```

Without it, every restart resets the clock — and a watcher restarted more often than `idle_timeout_minutes` can never reach the threshold, so servers stay up indefinitely. This is easy to hit under GitOps tooling that redeploys on a timer.

Timestamps are stored as wall-clock time and converted back on load, since `time.monotonic()` means nothing across processes. A snapshot is only adopted if it still matches what Crafty reports on the first poll, and snapshots older than 24 h are discarded.

### Starting and Stopping from the Crafty Console

While a server hibernates the watcher **holds its port** — that is how it answers
the MOTD and detects a player knocking. A start it did not trigger therefore
races it: Crafty spawns the JVM, the JVM asks for the port a few seconds later,
and the watcher is still sitting on it. The server dies with:

```
**** FAILED TO BIND TO PORT!
The exception was: ... bind(..) failed with error(-98): Address already in use
```

Polling cannot fix this. At the default 30s interval the watcher learns about the
start long after the JVM has given up. An event can: Crafty fires its
`start_server` webhook a few **milliseconds** after spawning the process, which
leaves the whole JVM startup — measured at 5s on a real server — to step aside.

The same race runs the other way when a server goes down. Between the stop and
the next poll — again up to 30s — **nobody is listening on the port at all**. A
player pinging gets a refused connection rather than the hibernating MOTD, and a
player connecting does not wake the server, which is the entire point of holding
the port. `stop_server`, `kill` and `crash_detected` close that gap the same way.

```yaml
crafty_events:
  enabled: true
  path: "/events/crafty"
  token: "a-long-random-secret"
```

Then, in Crafty: **Server → Config → Webhooks → New webhook**

| Field | Value |
|---|---|
| Type | `Discord` (any provider works — only the URL and body matter) |
| URL | `http://127.0.0.1:8095/events/crafty?token=a-long-random-secret` |
| Triggers | `start_server`, `stop_server`, `kill`, `crash_detected` — note the word order, it is `start_server`, not `server_start` |
| Body | `{"server_id": "{{ server_id }}", "event": "{{ event_type }}"}` |

| Event | What the watcher does | Discord |
|---|---|---|
| `start_server` | Steps off the port, moves to `STARTING` | Announced as a start from Crafty |
| `stop_server` | Moves to `STOPPED`, takes the port back | Announced as a stop from Crafty |
| `kill` | Same, named as a force kill | Announced as a force kill |
| `crash_detected` | Moves to `CRASHED`, takes the port back | Announced as a crash |

The receiver lives on the health server, so `health.enabled` must be true. A
start takes the same path as a player wake-up — stop listening, lock the port out
of the poll loop, move to `STARTING` — minus the deliberate 5s pause, which here
would eat into the margin. A stop is its mirror: clear the lockout, move to
`STOPPED`, and rebind. The rebind runs detached, because Crafty fires the event
when it *asks* for the stop and the JVM keeps the socket for as long as saving
the world takes.

An idle shutdown the watcher decided itself is announced once, not twice: the
event arrives while the state machine is still in `STOPPING`, which is how the
watcher recognises its own work.

Three things worth knowing:

- Crafty has no "custom" webhook provider, so the payload always arrives shaped
  for a chat service. The parser digs the rendered body out of whatever envelope
  it finds, and falls back to scanning for a known server id and event name.
- Every reply is `2xx`, even for events it ignores. Crafty calls
  `raise_for_status()` from inside the very call that started the server, so a
  refusal surfaces as an exception in Crafty's start path.
- The endpoint binds to `127.0.0.1`, but every container on the host network
  shares that localhost. Set a `token`.

---

## Architecture

Single Python asyncio daemon:

| Module | Purpose |
|---|---|
| `idle_monitor.py` | Polls Crafty API, drives per-server state machine |
| `proxy_listener.py` | Per-port TCP proxy (Java Edition fake MC protocol) |
| `bedrock_proxy.py` | Per-port UDP proxy (Bedrock Edition RakNet protocol) |
| `mc_protocol.py` | Java Edition protocol parsing (handshake, status, login) |
| `bedrock_protocol.py` | RakNet protocol helpers (ping/pong, connection reject) |
| `crafty_api.py` | Async Crafty API v2 client (stdlib `http.client`) |
| `server_state.py` | 7-state machine with timing/cooldown logic |
| `health_server.py` | HTTP server for `/health`, `/status`, `/metrics` |
| `crafty_events.py` | Reads Crafty's own webhook payloads (any provider) |
| `metrics.py` | Prometheus text exposition format generator |
| `webhook.py` | Discord/generic webhook notifications |
| `config.py` | YAML config loader and validation |
| `logger.py` | Rotating file + stderr logging |

### State Machine

```
UNKNOWN  → ONLINE / IDLE / STOPPED / CRASHED
ONLINE   → IDLE / STOPPED / CRASHED
IDLE     → ONLINE / STOPPING / STOPPED / CRASHED
STOPPING → ONLINE / IDLE / STOPPED / CRASHED
STOPPED  → ONLINE / IDLE / STARTING
STARTING → ONLINE / IDLE / STOPPED / CRASHED
CRASHED  → ONLINE / IDLE / STOPPED / STARTING
```

Every state has a way out, and the edges back to `IDLE` are the ones worth
knowing: a server that comes up with nobody on it is `IDLE`, not `ONLINE`, and
that is what starts the shutdown clock. `tests/test_state_machine.py` asserts
the graph directly, so this list and the code cannot drift apart.

## Security

- API token passed via environment variable, never in config files or logs
- Dedicated least-privilege Crafty user/role
- systemd sandboxing (manual deploy): `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, etc.
- Docker: minimal `python:3.11-slim` image, read-only config mount

## License

MIT
