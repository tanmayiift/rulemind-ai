from __future__ import annotations

import asyncio
from pathlib import Path

from .scheduler import init_scheduler
from .storage import Storage

WORKER_HEALTH_PATH = Path("/tmp/rulemind-worker.ready")


async def heartbeat() -> None:
    while True:
        WORKER_HEALTH_PATH.write_text("ok\n", encoding="utf-8")
        await asyncio.sleep(30)


async def run_worker() -> None:
    storage = Storage()
    init_scheduler(storage)
    WORKER_HEALTH_PATH.write_text("ok\n", encoding="utf-8")
    await asyncio.gather(heartbeat(), asyncio.Event().wait())


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
