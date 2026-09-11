import json
import time

from utility.log import Log

log = Log(__name__)


def log_pre_upgrade_cluster_state(client, fs_name="cephfs"):
    """
    Log cluster health, max_mds, and full MDS map before upgrade starts.
    """
    out, _ = client.exec_command(sudo=True, cmd="ceph -s -f json")
    log.info("Pre-upgrade cluster status:\n%s", out)

    out, _ = client.exec_command(sudo=True, cmd="ceph health detail")
    log.info("Pre-upgrade cluster health detail:\n%s", out)

    out, _ = client.exec_command(sudo=True, cmd="ceph fs dump -f json")
    fs_dump = json.loads(out)
    log.info("Pre-upgrade ceph fs dump:\n%s", json.dumps(fs_dump, indent=2))

    for filesystem in fs_dump.get("filesystems", []):
        mdsmap = filesystem.get("mdsmap", {})
        if mdsmap.get("fs_name") == fs_name:
            log.info(
                "Pre-upgrade max_mds for %s: %s",
                fs_name,
                mdsmap.get("max_mds"),
            )
            break

    out, _ = client.exec_command(
        sudo=True, cmd=f"ceph fs status {fs_name} -f json"
    )
    fs_status = json.loads(out)
    log.info(
        "Pre-upgrade ceph fs status for %s:\n%s",
        fs_name,
        json.dumps(fs_status, indent=2),
    )


def get_max_mds(client, fs_name="cephfs"):
    out, _ = client.exec_command(sudo=True, cmd="ceph fs dump -f json")
    fs_dump = json.loads(out)
    for filesystem in fs_dump.get("filesystems", []):
        mdsmap = filesystem.get("mdsmap", {})
        if mdsmap.get("fs_name") == fs_name:
            return mdsmap.get("max_mds", 1)
    return 1


def parse_upgrade_status(out):
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        if "There are no upgrades in progress currently." in out:
            return {"in_progress": False}
    return {}


def wait_for_upgrade_in_progress(client, timeout=1800, poll_interval=30):
    """
    Wait until ceph orch upgrade reports in_progress=true.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        out, _ = client.exec_command(sudo=True, cmd="ceph orch upgrade status")
        status = parse_upgrade_status(out)
        log.info("Upgrade status while waiting to start: %s", status)
        if status.get("in_progress"):
            return True
        time.sleep(poll_interval)
    return False


def wait_for_active_mdss(
    client, fs_name="cephfs", max_wait_time=600, retry_interval=20
):
    """
    Wait until ranks 0 through max_mds-1 are active.
    """
    start_time = time.time()
    expected_active = get_max_mds(client, fs_name)
    log.info(
        "Waiting for %s active MDS ranks (max_mds=%s) on %s",
        expected_active,
        expected_active,
        fs_name,
    )
    while time.time() - start_time < max_wait_time:
        out, _ = client.exec_command(
            sudo=True, cmd=f"ceph fs status {fs_name} -f json"
        )
        log.info(out)
        parsed_data = json.loads(out)
        active_ranks = {
            mds.get("rank")
            for mds in parsed_data.get("mdsmap", [])
            if mds.get("state") == "active" and mds.get("rank") is not None
        }
        required_ranks = set(range(expected_active))
        if required_ranks.issubset(active_ranks):
            return True
        time.sleep(retry_interval)
    return False
