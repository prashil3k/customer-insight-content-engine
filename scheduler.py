import json
import logging
from apscheduler.schedulers.background import BackgroundScheduler
import config

logger = logging.getLogger("scheduler")
_scheduler = None
_log = []


def _get_settings() -> dict:
    if config.SETTINGS_PATH.exists():
        return json.loads(config.SETTINGS_PATH.read_text())
    return config.DEFAULT_SETTINGS


def log(msg: str):
    import time
    entry = {"ts": time.strftime("%H:%M:%S"), "msg": msg}
    _log.append(entry)
    if len(_log) > 200:
        _log.pop(0)
    logger.info(msg)


def get_log() -> list:
    return list(_log)


def run_insight_scan():
    log("Scheduler: starting insight scan...")
    new_insights = 0

    try:
        from modules.insight_extractor import process_watch_folder
        results = process_watch_folder(progress_cb=log)
        new_insights += len(results)
        log(f"Scheduler: watch folder scan done — {len(results)} new insights.")
    except Exception as e:
        log(f"Scheduler: watch folder scan error — {e}")

    # Grain REST poller
    try:
        import config as _cfg
        if _cfg.GRAIN_API_TOKEN:
            from modules.grain_connector import poll_grain
            processed = poll_grain(progress_cb=log)
            new_insights += len(processed)
            if processed:
                log(f"Scheduler: Grain poll done — {len(processed)} new calls processed.")
        else:
            log("Scheduler: Grain token not set — skipping REST poll.")
    except Exception as e:
        log(f"Scheduler: Grain poll error — {e}")

    # Sybill REST poller
    try:
        import config as _cfg
        if _cfg.SYBILL_API_TOKEN:
            from modules.sybill_connector import poll_sybill
            processed = poll_sybill(progress_cb=log)
            new_insights += len(processed)
            if processed:
                log(f"Scheduler: Sybill poll done — {len(processed)} new calls processed.")
        else:
            log("Scheduler: Sybill token not set — skipping.")
    except Exception as e:
        log(f"Scheduler: Sybill poll error — {e}")

    # Auto-generate topics if setting is on and new insights arrived
    try:
        settings = _get_settings()
        if settings.get("scheduler", {}).get("auto_topics_after_scan") and new_insights > 0:
            log(f"Scheduler: {new_insights} new insights — auto-generating topics...")
            count = settings.get("topic_gen_count", 5)
            from modules.topic_planner import generate_topics
            topics = generate_topics(num_topics=count, progress_cb=log)
            log(f"Scheduler: auto-generated {len(topics)} topic proposals from new insights.")
    except Exception as e:
        log(f"Scheduler: auto-topic generation error — {e}")


def run_topic_generation():
    log("Scheduler: generating new topic proposals...")
    try:
        settings = _get_settings()
        count = settings.get("topic_gen_count", 5)
        from modules.topic_planner import generate_topics
        topics = generate_topics(num_topics=count, progress_cb=log)
        log(f"Scheduler: generated {len(topics)} new topic proposals.")
    except Exception as e:
        log(f"Scheduler: topic generation error — {e}")


def start(app=None):
    global _scheduler
    if _scheduler is not None:
        return

    settings = _get_settings()
    sched_cfg = settings.get("scheduler", config.DEFAULT_SETTINGS["scheduler"])

    if not sched_cfg.get("enabled", True):
        log("Scheduler: disabled in settings.")
        return

    insight_hours = sched_cfg.get("insight_scan_hours", 6)
    topic_hours = sched_cfg.get("topic_gen_hours", 24)

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(run_insight_scan, "interval", hours=insight_hours, id="insight_scan", replace_existing=True)
    _scheduler.add_job(run_topic_generation, "interval", hours=topic_hours, id="topic_gen", replace_existing=True)
    _scheduler.start()
    log(f"Scheduler started: insights every {insight_hours}h, topics every {topic_hours}h.")


def stop():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log("Scheduler stopped.")


def trigger(job_id: str):
    import threading
    if job_id == "insight_scan":
        threading.Thread(target=run_insight_scan, daemon=True).start()
        return True
    elif job_id == "topic_gen":
        threading.Thread(target=run_topic_generation, daemon=True).start()
        return True
    elif job_id == "grain_poll":
        def _grain():
            try:
                from modules.grain_connector import poll_grain
                poll_grain(progress_cb=log)
            except Exception as e:
                log(f"Grain manual poll error — {e}")
        threading.Thread(target=_grain, daemon=True).start()
        return True
    elif job_id == "sybill_poll":
        def _sybill():
            try:
                from modules.sybill_connector import poll_sybill
                poll_sybill(progress_cb=log)
            except Exception as e:
                log(f"Sybill manual poll error — {e}")
        threading.Thread(target=_sybill, daemon=True).start()
        return True
    return False


def status() -> dict:
    settings = _get_settings()
    sched_cfg = settings.get("scheduler", config.DEFAULT_SETTINGS["scheduler"])
    return {
        "running": _scheduler is not None and _scheduler.running,
        "enabled": sched_cfg.get("enabled", True),
        "insight_scan_hours": sched_cfg.get("insight_scan_hours", 6),
        "topic_gen_hours": sched_cfg.get("topic_gen_hours", 24),
    }


def reschedule():
    global _scheduler
    if _scheduler:
        stop()
    start()
