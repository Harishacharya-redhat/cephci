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


def create_file(count, mount_point, filename, client, run_user=None):
    log.info(f"Creating file of : {count}G")
    run_as_user(
        client,
        f"dd if=/dev/urandom of={mount_point}/{filename} bs=1G count={count}",
        run_user,
    )


def verify_disk_usage(client, mount_point, size, filename=None, run_user=None):
    if filename:
        cmd = f"du -sh {mount_point}/{filename}"
    else:
        cmd = f"du -sh {mount_point}"
    out = run_as_user(client, cmd, run_user)
    size_str = out[0].strip().split()[0]
    numeric_part = size_str.rstrip("G")
    size_rounded = f"{int(float(numeric_part))}G"
    if size_rounded == size:
        log.info(f"File created with correct space: {size_rounded}")
    else:
        raise OperationFailedError(
            f"File '{filename}' took incorrect space utilization. Expected: {size}, Actual: {size_rounded}"
        )


def run(ceph_cluster, **kw):
    """Verify the space allocation with different file sizes
    Args:
        **kw: Key/value pairs of configuration information to be used in the test.
    """
    config = kw.get("config")
    nfs_nodes = ceph_cluster.get_nodes("nfs")
    clients = ceph_cluster.get_nodes("client")
    port = config.get("port", "2049")
    version = config.get("nfs_version", "4.2")
    no_clients = int(config.get("clients", "2"))
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
    run_user = get_nfs_run_user(config, kw.get("test_data"))

    try:
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

        files_and_sizes = [("file1", "1G"), ("file2", "2G"), ("file3", "3G")]

        for filename, size in files_and_sizes:
            create_file(
                count=int(size[:-1]),
                mount_point=nfs_mount,
                filename=filename,
                client=clients[0],
                run_user=run_user,
            )
            verify_disk_usage(
                client=clients[0],
                mount_point=nfs_mount,
                filename=filename,
                size=size,
                run_user=run_user,
            )

        verify_disk_usage(
            client=clients[0], mount_point=nfs_mount, size="6G", run_user=run_user
        )
        return 0
    except Exception as e:
        log.error(f"Failed to  verify the space allocation test on NFS v4.2 : {e}")
        cleanup_cluster(clients, nfs_mount, nfs_name, nfs_export)
        log.info("Cleaning up successful")
        return 1

    finally:
        log.info("Cleaning up")
        cleanup_cluster(clients, nfs_mount, nfs_name, nfs_export, nfs_nodes=nfs_node)
        log.info("Cleaning up successful")
