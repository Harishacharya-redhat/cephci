import json
import time
import traceback

from pip._internal.exceptions import CommandError

from ceph.ceph import CommandFailed
from tests.cephfs.cephfs_upgrade.cluster_state import (
    log_pre_upgrade_cluster_state,
    parse_upgrade_status,
    wait_for_active_mdss,
    wait_for_upgrade_in_progress,
)
from tests.cephfs.cephfs_utilsV1 import FsUtils
from utility.log import Log
from utility.retry import retry

log = Log(__name__)


def run(ceph_cluster, **kw):
    """
    CEPH-83575628 - Perform active mds failures while upgrading
    Steps Performed:
    1. Check if upgrade in progress
    2. get active mds
    3. Fail active mds with interval for 2 min each
    4. Perform this till upgrade in progress
    5. Check if there are any crash occurred
    """
    try:
        fs_util = FsUtils(ceph_cluster)
        clients = ceph_cluster.get_ceph_objects("client")
        log.info("checking Pre-requisites")
        if not clients:
            log.info(
                f"This test requires minimum 1 client nodes.This has only {len(clients)} clients"
            )
            return 1
        client1 = clients[0]
        fs_name = "cephfs"
        retry_exec_command = retry(CommandFailed, tries=10, delay=30, backoff=1)(
            client1.exec_command
        )

        log.info("Wait for upgrade to start")
        if not wait_for_upgrade_in_progress(client1):
            raise CommandError("Upgrade did not start within the expected timeout")

        log_pre_upgrade_cluster_state(client1, fs_name=fs_name)

        start_time = time.time()
        while time.time() - start_time < 1800:
            out, rc = client1.exec_command(
                cmd="ceph orch upgrade status", sudo=True
            )
            upgrade_status = parse_upgrade_status(out)
            if not upgrade_status.get("in_progress"):
                log.info("Upgrade complete or not in progress: %s", upgrade_status)
                break

            mds_ls = fs_util.get_active_mdss(client1, fs_name=fs_name)
            for mds in mds_ls:
                out, rc = retry_exec_command(
                    cmd=f"ceph mds fail {mds}", client_exec=True
                )
                log.info(out)

                if not wait_for_active_mdss(client1, fs_name):
                    raise CommandError(
                        "Active MDS ranks did not recover after failing one MDS"
                    )
                time.sleep(120)
                out, rc = retry_exec_command(
                    cmd=f"ceph fs status {fs_name}", client_exec=True
                )
                log.info(f"Status of {fs_name}:\n {out}")
                out, rc = retry_exec_command(cmd="ceph -s -f json", client_exec=True)
                ceph_status = json.loads(out)
                log.info(f"Ceph status: {json.dumps(ceph_status, indent=4)}")
                if ceph_status["health"]["status"] == "HEALTH_ERR":
                    log.error("Ceph Health is NOT OK")
                    return 1

        out, rc = retry_exec_command(sudo=True, cmd="ceph crash ls")
        if out:
            raise CommandError(f"Found Crash while Upgrade {out}")
        return 0
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error(e)
        log.error(traceback.format_exc())
        return 1
    finally:
        pass
