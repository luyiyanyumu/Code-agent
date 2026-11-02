"""Verify the shipped source build before marking an installation ready."""

import asyncio
import sys

from .runtime import prepare_runtime


def main():
    if sys.argv[1:] != ["--prepare"]:
        raise SystemExit("usage: python -m code_agent.harness --prepare")
    asyncio.run(prepare_runtime())
    print("[code-agent] DeepSeek Harness 本地源码内核已校验，无需下载运行依赖。")


if __name__ == "__main__":
    main()
