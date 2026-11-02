"""Isolated real application host, with a graceful stdin shutdown for acceptance."""
import os
import sys
import threading
import faulthandler

import uvicorn

from code_agent.api.app import create_app

if os.environ.get("ACCEPTANCE_DUMP_STACKS") == "1":
    faulthandler.dump_traceback_later(15, repeat=True)
server = uvicorn.Server(uvicorn.Config(create_app(provider_name="offline"), host="127.0.0.1", port=int(os.environ["ACCEPTANCE_API_PORT"]), log_level="warning"))


def control():
    for line in sys.stdin:
        if line.strip() == "stop":
            server.should_exit = True
            return


threading.Thread(target=control, daemon=True).start()
server.run()
