"""Hold the live cluster so IBM Cloud nodes stay up for debug.

Do not merge this module to main. Debug-branch only.
"""

import time

from utility.log import Log

log = Log(__name__)


def run(ceph_cluster, **kw):
    """Sleep for the configured duration (default 6 hours)."""
    config = kw.get("config") or {}
    hours = float(config.get("hours", 6))
    seconds = int(config.get("seconds", hours * 3600))
    log.info(
        "Debug hold: sleeping %s seconds (%.1f hours) so IBM Cloud nodes stay up",
        seconds,
        seconds / 3600.0,
    )
    time.sleep(seconds)
    log.info("Debug hold complete")
    return 0
