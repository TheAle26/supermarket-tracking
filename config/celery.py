"""
config/celery.py
================
Celery app + Beat schedule:

    03:00/15:00 dispatch_frequent      -> API/HTTP stores
    every 6h   dispatch_headless       -> browser-based stores
    05:30 ART  mark_stale_out_of_stock -> flag vanished listings as OOS
    06:00 ART  prune_price_history     -> archive + drop out-of-window partitions

Times are early-morning ART (America/Argentina/Buenos_Aires) when store traffic
is lowest — kinder to anti-bot systems and to our proxy budget.
"""

import os
import sys
from pathlib import Path

from celery import Celery
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent
APPS_DIR = BASE_DIR / "apps"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("price_compare")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.timezone = "America/Argentina/Buenos_Aires"
app.conf.beat_schedule = {
    "frequent-api-scrape": {
        "task": "scraping.dispatch_frequent",
        # Carrefour / Chango Más: twice daily, 12 hours apart.
        "schedule": crontab(hour="3,15", minute=0),
    },
    "headless-scrape": {
        "task": "scraping.dispatch_headless",
        "schedule": crontab(minute=15, hour="*/6"),
    },
    "mark-stale-oos": {
        "task": "scraping.mark_stale_out_of_stock",
        "schedule": crontab(hour=5, minute=30),
    },
    "prune-history": {
        # Runs the management command via a thin task wrapper (see below).
        "task": "scraping.prune_history",
        "schedule": crontab(hour=6, minute=0),
    },
}


@app.task(name="scraping.prune_history")
def prune_history():
    """Beat-friendly wrapper around the retention management command."""
    from django.core.management import call_command

    call_command("prune_price_history", retain_days=90)
