"""Background polling: one job, every 5 s, never overlapping; and the Scripts tab's run, every 10 s."""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.services.generation import GenerationService

log = logging.getLogger("app.scheduler")


def start(service: GenerationService, script_runs=None) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(service.tick, "interval", seconds=5, id="generations", max_instances=1, coalesce=True)
    if script_runs is not None:
        sched.add_job(script_runs.tick, "interval", seconds=10, id="script_runs", max_instances=1, coalesce=True)
    sched.start()
    log.info("scheduler started")
    return sched
