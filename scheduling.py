"""APScheduler owns all periodic scheduling; SQLite owns business state."""
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler


def start_scheduler(watcher, timezone):
    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(
        watcher.tick, trigger='interval', seconds=30, id='game-deals-check',
        next_run_time=datetime.now(timezone), coalesce=True,
        max_instances=1, misfire_grace_time=300, replace_existing=True)
    scheduler.start()
    return scheduler
