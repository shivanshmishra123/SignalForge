from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.domains.operations import CrawlOperations


class CrawlScheduler:
    def __init__(self, operations: CrawlOperations) -> None:
        self.operations = operations
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    def register_schedule(
        self,
        schedule_id: str,
        interval_minutes: int,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        self.scheduler.add_job(
            callback,
            trigger="interval",
            minutes=interval_minutes,
            id=schedule_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    async def start(self) -> None:
        self.scheduler.start()
        await self.operations.start_worker()

    async def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.operations.stop_worker()
