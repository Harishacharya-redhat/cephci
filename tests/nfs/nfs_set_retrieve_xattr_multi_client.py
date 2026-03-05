import threading
from time import sleep

from nfs_operations import (
    cleanup_cluster,
    get_nfs_run_user,
    getfattr,
    run_as_user,
    set_client_mount_ownership,
    setfattr,
    setup_nfs_cluster,
)

from cli.exceptions import ConfigError
from utility.log import Log

log = Log(__name__)


def run(ceph_cluster, **kw):
    """Verify multiple extended attribute on file with parallel access from 2 clients
    Args:
        **kw: Key/value pairs of configuration information to be used in the test.
    """
    config = kw.get("config")
    nfs_nodes = ceph_cluster.get_nodes("nfs")
    clients = ceph_cluster.get_nodes("client")
    port = config.get("port", "2049")
    version = config.get("nfs_version", "4.2")
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
    filename = "Testfile"

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

        # Mount the export on client 2 (admin)
        cmd = f"umount -l {nfs_mount}"
        clients[1].exec_command(sudo=True, cmd=cmd)

        cmd = f"mount -t nfs {nfs_nodes[0].ip_address}:{nfs_export}_0 {nfs_mount}"
        clients[1].exec_command(sudo=True, cmd=cmd)
        set_client_mount_ownership([clients[1]], nfs_mount, run_user)

        # Create a file on Mount point (as run_user when set)
        for i in range(1, 11):
            run_as_user(clients[0], f"touch {nfs_mount}/{filename}{i}", run_user)

        # Set multiple extended attribute on the file from client 1
        set_attributes = []
        for i in range(1, 11):
            thread_set = threading.Thread(
                target=setfattr,
                args=(
                    clients[0],
                    f"{nfs_mount}/{filename}{i}",
                    f"myattr{i}",
                    f"value{i}",
                ),
            )
            log.info(
                f"Successfully set the attribute 'myattr{i}' on file - {filename}{i}"
            )
            set_attributes.append(thread_set)
            thread_set.start()

        # Fetch the extended attribute on the file from client 2
        get_attributes = []
        for i in range(1, 11):
            sleep(2)
            thread_get = threading.Thread(
                target=getfattr, args=(clients[1], f"{nfs_mount}/{filename}{i}")
            )
            get_attributes.append(thread_get)
            thread_get.start()

        # Wait for all set attribute threads to complete
        for thread_set in set_attributes:
            thread_set.join()

        # Wait for all get attribute threads to complete
        for thread_get in get_attributes:
            thread_get.join()
        return 0
    except Exception as e:
        log.error(f"Failed to validate extended attribute with parallel clients : {e}")
        cleanup_cluster(clients, nfs_mount, nfs_name, nfs_export)
        log.info("Cleaning up successful")
        return 1

    finally:
        log.info("Cleaning up")
        cleanup_cluster(clients, nfs_mount, nfs_name, nfs_export, nfs_nodes=nfs_nodes)
        log.info("Cleaning up successful")
