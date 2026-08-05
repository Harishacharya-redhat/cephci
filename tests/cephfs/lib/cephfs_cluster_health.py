"""
CephFS helpers for cluster health logging and crash detection.

Mirrors the NFS upgrade-test pattern, with check_nfs defaulting to False.
"""

from utility.log import Log

log = Log(__name__)


def init_cluster_health_check(ceph_cluster, config=None):
    """
    Create RadosOrchestrator and capture start timestamp for crash checks.

    Returns:
        tuple: (rados_obj, start_time) or (None, None) on failure.
    """
    try:
        from ceph.ceph_admin import CephAdmin
        from ceph.rados.core_workflows import RadosOrchestrator
        from ceph.rados.utils import get_cluster_timestamp

        # Only pass keys CephAdmin needs; suite config must not be spread in.
        cephadm_config = {
            k: config[k] for k in ("build", "rhbuild") if config and k in config
        }
        cephadm = CephAdmin(cluster=ceph_cluster, **cephadm_config)
        rados_obj = RadosOrchestrator(node=cephadm)
        start_time = get_cluster_timestamp(rados_obj.node)
        log.debug("Crash-check window started at %s", start_time)
        return rados_obj, start_time
    except Exception as e:
        log.warning("Failed to initialize cluster health check: %s", e)
        return None, None


def log_cluster_health_and_check_crashes(rados_obj, start_time, check_nfs=False):
    """
    Log cluster health and check for crashes since start_time.

    Returns:
        True if a crash was detected, False otherwise.
    """
    from ceph.rados.utils import get_cluster_timestamp

    if not rados_obj or not start_time:
        log.warning("Skipping health/crash check: rados_obj or start_time missing")
        return False

    # Best-effort health dump — must not skip the crash check on failure.
    try:
        rados_obj.log_cluster_health()
    except Exception as e:
        log.error("log_cluster_health() failed: %s", e)

    try:
        end_time = get_cluster_timestamp(rados_obj.node)
        log.debug(
            "Crash-check window completed. Start time: %s, End time: %s",
            start_time,
            end_time,
        )
        if rados_obj.check_crash_status(
            start_time=start_time,
            end_time=end_time,
            check_nfs=check_nfs,
        ):
            log.error("Crash detected during test window")
            return True
    except Exception as e:
        log.error("Crash status check failed: %s", e)
    return False
