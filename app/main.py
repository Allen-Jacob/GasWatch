from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings
from app.database import Repository
from app.notifications.base import DisabledNotifier
from app.notifications.ntfy import NtfyNotifier
from app.providers import GasQuebecProvider
from app.service import GasWatchService


async def run() -> None:
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    repository = Repository(settings.database_path)
    repository.initialize()
    provider = GasQuebecProvider(settings.gasquebec_base_url, settings.provider_timeout_seconds)
    notifier = (
        NtfyNotifier(
            settings.ntfy_url,
            settings.ntfy_topic,
            settings.ntfy_token,
            settings.provider_timeout_seconds,
        )
        if settings.ntfy_enabled
        else DisabledNotifier()
    )
    service = GasWatchService(settings, repository, provider, notifier)
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    scheduler.add_job(
        service.collect,
        "interval",
        minutes=settings.price_check_interval_minutes,
        id="price-collection",
        max_instances=1,
        coalesce=True,
    )
    if settings.daily_report_enabled:
        hour, minute = map(int, settings.daily_report_time.split(":"))
        scheduler.add_job(
            service.send_daily_reports,
            CronTrigger(hour=hour, minute=minute, timezone=settings.tz),
            id="daily-report",
            max_instances=1,
            coalesce=True,
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    scheduler.start()
    try:
        await service.collect()
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await service.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
