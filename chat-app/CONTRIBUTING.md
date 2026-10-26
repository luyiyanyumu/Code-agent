# Contributing

Use Python 3.11+ and Node.js 20.19+ (or 22.12+). This directory is the complete
project: development and CI must not require the parent coding workspace.

```sh
python -m venv .venv
# Activate .venv using your shell's activation command.
python -m pip install -e '.[dev]'
python -m pytest
npm ci
npm test
npm run build
```

Use a temporary CHAT_AGENT_HOME and the offline provider in tests. Never send
fixtures to a live model account or use a contributor's personal database.
Changes to event types should preserve reconnect replay, cancellation and
historical UI rendering. File tools must remain scoped to their session.

Submit focused changes with the problem, resulting behavior and relevant
validation. Contributions are offered under Apache-2.0, as described in LICENSE.
