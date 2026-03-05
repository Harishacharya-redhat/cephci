from time import sleep

from nfs_operations import (
    cleanup_cluster,
    get_nfs_run_user,
    run_as_user,
    set_client_mount_ownership,
    setup_nfs_cluster,
)

from cli.exceptions import ConfigError
from cli.io.io import linux_untar
from utility.log import Log

log = Log(__name__)


def run(ceph_cluster, **kw):
    """Verify readdir ops
    Args:
        **kw: Key/value pairs of configuration information to be used in the test.
    """
    config = kw.get("config")
    nfs_nodes = ceph_cluster.get_nodes("nfs")
    clients = ceph_cluster.get_nodes("client")

    port = config.get("port", "2049")
    version = config.get("nfs_version", "4.0")
    no_clients = int(config.get("clients", "2"))
    run_user = get_nfs_run_user(config, kw.get("test_data"))

    # If the setup doesn't have required number of clients, exit.
    if no_clients > len(clients):
        raise ConfigError("The test requires more clients than available")

    clients = clients[:no_clients]  # Select only the required number of clients
    nfs_node = nfs_nodes[0]
    fs_name = "cephfs"
    nfs_name = "cephfs-nfs"
    nfs_export = "/export"
    nfs_mount = "/mnt/nfs"
    fs = "cephfs"
    nfs_server_name = nfs_node.hostname

    try:
        # Setup nfs cluster
        setup_nfs_cluster(
            clients,
            nfs_server_name,
            port,
            version,
            nfs_name,
            nfs_mount,
            fs_name,
            nfs_export,
            fs,
            ceph_cluster=ceph_cluster,
        )
        set_client_mount_ownership(clients, nfs_mount, run_user)

        # Linux untar on client 1 (writes to nfs_mount; root or run_user can write after chown)
        io = linux_untar(clients[0], nfs_mount)

        run_as_user(clients[1], f"ls -lart {nfs_mount}", run_user)
        run_as_user(clients[2], f"du -sh {nfs_mount}", run_user)
        run_as_user(clients[3], f"find {nfs_mount} -name *.txt", run_user)

        for th in io:
            th.join()

        run_as_user(clients[1], f"ls -lart {nfs_mount}", run_user)
        run_as_user(clients[2], f"du -sh {nfs_mount}", run_user)
        run_as_user(clients[3], f"find {nfs_mount} -name *.txt", run_user)
        return 0

    except Exception as e:
        log.error(f"Failed to validate read dir operations : {e}")
        return 1
    finally:
        log.info("Cleaning up")
        sleep(30)
        cleanup_cluster(clients, nfs_mount, nfs_name, nfs_export, nfs_nodes=nfs_node)
        log.info("Cleaning up successful")
