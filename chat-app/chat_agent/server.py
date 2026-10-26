"""Run the standalone service without loading any coding application config."""
import argparse
import logging
import re
from pathlib import Path

import uvicorn

from .api import create_app
from .config import Config


class RedactQueryTokens(logging.Filter):
    """WebSocket query credentials must not appear in the default server logs."""
    def filter(self, record):
        def redact(value):
            return re.sub(r"([?&]token=)[^&\s\"]+", r"\1[redacted]", value) if isinstance(value, str) else value
        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact(value) for key, value in record.args.items()}
        return True


def main():
    parser = argparse.ArgumentParser(description="Standalone Chat-agent HTTP + WebSocket service")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--static-dir", type=Path, default=None, help="Serve a built frontend (e.g. dist)")
    args = parser.parse_args()
    config = Config.from_env()
    if args.home:
        config.home = args.home.expanduser().resolve()
    host = args.host or config.host
    port = config.port if args.port is None else args.port
    if not 1 <= port <= 65535:
        parser.error("Port must be between 1 and 65535")
    if host not in {"127.0.0.1", "localhost", "::1"} and not config.access_token:
        parser.error("Set CHAT_AGENT_ACCESS_TOKEN before binding a non-loopback address")
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addFilter(RedactQueryTokens())
    uvicorn.run(create_app(config, static_dir=args.static_dir), host=host, port=port,
                ws_max_size=int(config.max_upload_bytes * 4 / 3) + 64_000)


if __name__ == "__main__":
    main()
