"""Persistent queue and independent watchdog processes. No browser dependency."""

import argparse
import logging
import os
import signal
import time

from .config import settings
from .db import Store
from .gpu.scheduler import Scheduler

log = logging.getLogger("studio.worker")
running = True


def stop(*_):
    global running
    running = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["worker", "watchdog"], default="worker", nargs="?")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=2)
    args = parser.parse_args()
    from .logging_conf import configure_logging

    configure_logging()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    store = Store(settings.database_url)
    last_ccu_tick = 0.0
    while running:
        try:
            scheduler = Scheduler(store)
            result = scheduler.watchdog() if args.mode == "watchdog" else scheduler.tick()
            if args.mode == "worker" and os.getenv("STUDIO_ATELIER_ENABLED", "0") == "1":
                from .atelier_routes import auto_export_tick

                auto_export_tick(store)
            if args.mode == "worker" and time.time() - last_ccu_tick >= 60:
                # Balance snapshots and leaked-runtime detection: read-only, no CU cost.
                last_ccu_tick = time.time()
                try:
                    from .gpu.ccu import tick_ccu
                    from .gpu.repository import Repository

                    repo = Repository(store)
                    owner_ids = sorted(
                        {str(account["owner_id"]) for account in repo.list("accounts") if account.get("owner_id")}
                    )
                    for owner_id in owner_ids:
                        tick_ccu(repo, owner_id)
                except Exception as exc:  # noqa: BLE001 - never stop the queue for accounting
                    log.warning("ccu tick failed: %s", type(exc).__name__)
            if result.get("advanced"):
                log.info("job=%s state=%s", result.get("job_id"), result.get("state"))
        except Exception as exc:
            # Exception bodies and provider HTTP responses can contain credentials.
            log.error("worker tick failed: %s", type(exc).__name__)
        if args.once:
            break
        time.sleep(max(0.1, args.interval))


if __name__ == "__main__":
    main()
