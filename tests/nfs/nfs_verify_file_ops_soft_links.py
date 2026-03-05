from threading import Thread

from nfs_operations import (
    cleanup_cluster,
    get_nfs_run_user,
    run_as_user,
    set_client_mount_ownership,
    setup_nfs_cluster,
)

from cli.exceptions import ConfigError, OperationFailedError
from utility.log import Log

log = Log(__name__)


def create_files(client, mount_point, file_count, run_user=None):
    for i in range(1, file_count + 1):
        try:
            run_as_user(
                client,
                f"dd if=/dev/urandom of={mount_point}/file{i} bs=1 count=1",
                run_user,
            )
        except Exception:
            raise OperationFailedError(f"failed to create file file{i}")


def create_soft_link(client, mount_point, file_count, run_user=None):
    for i in range(1, file_count + 1):
        try:
            run_as_user(
                client,
                f"ln -s {mount_point}/file{i} {mount_point}/link_file{i}",
                run_user,
            )
        except Exception:
            raise OperationFailedError(f"failed to create softlink file{i}")


def perform_lookups(client, mount_point, num_files, run_user=None):
    for _ in range(1, num_files):
        try:
            log.info(
                run_as_user(client, f"ls -laRt {mount_point}/", run_user)
            )
        except FileNotFoundError as e:
            error_message = str(e)
            if "No such file or directory" not in error_message:
                raise OperationFailedError("failed to perform lookups")
            log.warning(f"Ignoring error: {error_message}")
        except Exception:
            raise OperationFailedError("failed to perform lookups")


def run(ceph_cluster, **kw):
    """Verify create file, create soflink and lookups from nfs clients
    Args:
        **kw: Key/value pairs of configuration information to be used in the test.
    """
    config = kw.get("config")
    nfs_nodes = ceph_cluster.get_nodes("nfs")
    clients = ceph_cluster.get_nodes("client")

    port = config.get("port", "2049")
    version = config.get("nfs_version", "4.0")
    no_clients = int(config.get("clients", "2"))
    file_count = int(config.get("file_count", "10"))
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

        operations = [
            Thread(target=create_files, args=(clients[0], nfs_mount, file_count, run_user)),
            Thread(target=create_soft_link, args=(clients[1], nfs_mount, file_count, run_user)),
            Thread(target=perform_lookups, args=(clients[1], nfs_mount, file_count, run_user)),
        ]

        # start opertaion on each client
        for operation in operations:
            operation.start()

        # Wait for all operation to finish
        for operation in operations:
            operation.join()
        return 0

    except Exception as e:
        log.error(f"Error : {e}")
    finally:
        log.info("Cleaning up")
        cleanup_cluster(clients, nfs_mount, nfs_name, nfs_export, nfs_nodes=nfs_node)
        log.info("Cleaning up successfull")
