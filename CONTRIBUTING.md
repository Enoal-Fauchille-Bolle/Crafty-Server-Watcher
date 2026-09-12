# Contributing to Crafty Server Watcher

Thanks for considering a contribution! Here's how to get started.

## Development Setup

```bash
# Clone
git clone https://github.com/Soveticka/crafty-server-watcher.git
cd crafty-server-watcher

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# Install dependencies
pip install pyyaml

# Install dev tools
pip install ruff

# Enable the git hooks (once per clone)
.githooks/setup-hooks.sh      # Linux/macOS
# .githooks\setup-hooks.ps1   # Windows
```

## Git Hooks

The hooks in [.githooks/](.githooks/) are versioned with the code, but git does not
use them until `setup-hooks.sh` (or `setup-hooks.ps1`) runs
`git config core.hooksPath .githooks`. Undo it with
`git config --unset core.hooksPath`.

The `pre-commit` hook scans the staged changes for secrets with
[Betterleaks](https://github.com/betterleaks/betterleaks) and refuses the commit
when it finds one. Without Betterleaks on the `PATH`, it prints a warning and lets
the commit through, so install it once:

```bash
brew install betterleaks        # macOS, Linux
sudo dnf install betterleaks    # Fedora
```

On Windows, download `betterleaks_<version>_windows_x64.zip` from the
[releases](https://github.com/betterleaks/betterleaks/releases) and put
`betterleaks.exe` on the `PATH`.

If the scan flags something that is not a secret, such as a placeholder in
`config.example.yaml`, end that line with a `betterleaks:allow` comment:

```yaml
token: "example-token"  # betterleaks:allow
```

## Code Style

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
# Lint
ruff check .

# Auto-fix lint issues
ruff check --fix .

# Format
ruff format .
```

CI will block merges that fail lint or format checks.

## Making Changes

1. Fork the repo and create a feature branch from `main`:
   ```bash
   git checkout -b feature/my-feature
   ```
2. Make your changes
3. Run `ruff check .` and `ruff format .` before committing
4. Commit with a clear message:
   ```bash
   git commit -m "Add: brief description of change"
   ```
5. Push and open a PR against `main`

## Pull Request Labels

Label your PRs for automatic release note categorization:

| Label | Category |
|---|---|
| `feature`, `enhancement` | 🚀 Features |
| `bug`, `fix` | 🐛 Bug Fixes |
| `chore`, `maintenance` | 🧰 Maintenance |
| `docs` | 📖 Documentation |

## Project Structure

```
crafty_server_watcher/
├── __init__.py          # Package metadata
├── __main__.py          # Entry point, signal handling, asyncio loop
├── config.py            # YAML config loader and validation
├── crafty_api.py        # Async Crafty API v2 client
├── idle_monitor.py      # Polling loop and state transition logic
├── logger.py            # Rotating file + stderr logging
├── mc_protocol.py       # Minecraft Java protocol helpers
├── proxy_listener.py    # Per-port TCP proxy manager
└── server_state.py      # 7-state machine with timing logic
```

## Reporting Issues

When reporting bugs, please include:
- Python version (`python --version`)
- Deployment method (Docker or manual)
- Relevant log output
- Your `config.yaml` (with sensitive values redacted)

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
