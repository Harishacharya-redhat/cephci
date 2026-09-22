from threading import Thread
from time import monotonic, sleep

from nfs_operations import (
    cleanup_cluster,
    init_cluster_health_check,
    log_cluster_health_and_check_crashes,
    setup_nfs_cluster,
)

from cli.exceptions import ConfigError, OperationFailedError
from cli.utilities.utils import perform_lookups
from utility.log import Log

log = Log(__name__)

COPY_WORK_SUBDIR = "cephci_copy_test"
WAIT_REMOTE_PATH_TIMEOUT_S = 120


def _wait_remote_path(client, path, sudo=False, timeout_s=WAIT_REMOTE_PATH_TIMEOUT_S):
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        out, _ = client.exec_command(
            sudo=sudo,
            cmd=f"test -e {path} && echo ok",
            check_ec=False,
        )
        if "ok" in out:
            return
        sleep(1)
    raise OperationFailedError(f"timed out waiting for {path} on {client.hostname}")


def create_copy_files(mount_point, num_files, client1, client2, sudo=False):
    for i in range(1, num_files + 1):
        src = f"{mount_point}/file{i}"
        try:
            client1.exec_command(
                sudo=sudo,
                cmd=f"dd if=/dev/urandom of={src} bs=1 count=1",
            )
            _wait_remote_path(client2, src, sudo=sudo)
            client2.exec_command(
                sudo=sudo,
                cmd=f"cp {src} {mount_point}/copyfile{i}",
            )
        except Exception as e:
            log.error(f"Failed to create/copy file{i}: {e}")
            raise OperationFailedError(f"failed to perform operation on file{i}: {e}")


def create_copy_dirs(mount_point, num_dirs, client1, client2, sudo=False):
    for i in range(1, num_dirs + 1):
        src = f"{mount_point}/dir{i}"
        try:
            client1.exec_command(
                sudo=sudo,
                cmd=f"mkdir {src}",
            )
            _wait_remote_path(client2, src, sudo=sudo)
            client2.exec_command(
                sudo=sudo,
                cmd=f"cp -r {src} {mount_point}/copydir{i}",
            )
        except Exception as e:
            log.error(f"Failed to create/copy directory dir{i}: {e}")
            raise OperationFailedError(
                f"failed to perform operation on directory dir{i}: {e}"
            )


def run(ceph_cluster, **kw):
    """Test copy of files and directories on NFS mount
    Args:
        **kw: Key/value pairs of configuration information to be used in the test.
    """
    config = kw.get("config")
    nfs_nodes = ceph_cluster.get_nodes("nfs")
    clients = ceph_cluster.get_nodes("client")

    port = config.get("port", "2049")
    version = config.get("nfs_version")
    num_files = config.get("num_files")
    num_dirs = config.get("num_dirs")
    no_clients = int(config.get("clients", "3"))
    sudo = config.get("sudo", False)
    nfs_name = "cephfs-nfs"
    nfs_mount = "/mnt/nfs"
    nfs_export = "/export"
    nfs_server_name = nfs_nodes[0].hostname
    fs_name = "cephfs"

    # If the setup doesn't have required number of clients, exit.
    if no_clients > len(clients):
        raise ConfigError("The test requires more clients than available")

    clients = clients[:no_clients]  # Select only the required number of clients
    rados_obj, start_time = init_cluster_health_check(ceph_cluster, config)

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
            fs_name,
            ceph_cluster=ceph_cluster,
            enable_rdma=config.get("enable_rdma", False),
            rdma_port=config.get("rdma_port"),
        )
        work_mount = f"{nfs_mount}/{COPY_WORK_SUBDIR}"
        clients[0].exec_command(
            sudo=sudo,
            cmd=f"mkdir -p {work_mount} && chmod a+rwx {work_mount}",
        )

        # Create files and dirs from client 1 and copy files and dirs from client 2
        client1 = clients[0]
        client2 = clients[1]
        lookup_iterations = min(int(num_files) + int(num_dirs), 50)
        operations = [
            Thread(
                target=create_copy_files,
                args=(work_mount, num_files, client1, client2, sudo),
            ),
            Thread(
                target=create_copy_dirs,
                args=(work_mount, num_dirs, client1, client2, sudo),
            ),
            Thread(
                target=perform_lookups,
                args=(clients[2], work_mount, lookup_iterations),
                kwargs={"sudo": sudo, "recursive": False},
            ),
        ]

        # start operation on each client
        for operation in operations:
            operation.start()

        # Wait for all operation to finish with timeout
        for idx, operation in enumerate(operations):
            operation.join(timeout=300)
            if operation.is_alive():
                op_names = ["create_copy_files", "create_copy_dirs", "perform_lookups"]
                log.error(f"Thread {idx} ({op_names[idx]}) did not complete in 300s")
                raise OperationFailedError(f"Thread {idx} hung")

        log.info("Successfully completed the copy tests for files and dirs")
        return 0

    except (ConfigError, OperationFailedError) as e:
        log.error(f"Failed to complete file copy operations: {e}")
        return 1
    except Exception as e:
        log.error(f"Unexpected error during file copy test: {e}")
        return 1
    finally:
        crash_detected = log_cluster_health_and_check_crashes(rados_obj, start_time)
        log.info("Cleaning up")
        cleanup_cluster(
            clients, nfs_mount, nfs_name, nfs_export, nfs_nodes=nfs_nodes[0]
        )
        log.info("Cleaning up successful")
        if crash_detected:
            return 1
