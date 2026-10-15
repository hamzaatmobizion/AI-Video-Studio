"""Background polling: one job, every 5 s, never overlapping."""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.services.generation import GenerationService

log = logging.getLogger("app.scheduler")


def start(service: GenerationService) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(service.tick, "interval", seconds=5, id="generations", max_instances=1, coalesce=True)
    sched.start()
    log.info("scheduler started")
    return sched
