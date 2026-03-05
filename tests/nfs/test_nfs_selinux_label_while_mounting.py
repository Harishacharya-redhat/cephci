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


def run(ceph_cluster, **kw):
    """Verify setting selinux context label while mounting NFS share
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
    dirname = "Directory"
    num_files = 20
    num_dirs = 10
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

        cmd = f"umount -l {nfs_mount}"
        clients[0].exec_command(sudo=True, cmd=cmd)

        cmd = f"mount -t nfs -o context=system_u:object_r:nfs_t:s0 {nfs_nodes[0].ip_address}:{nfs_export}_0 {nfs_mount}"
        clients[0].exec_command(sudo=True, cmd=cmd)
        set_client_mount_ownership(clients, nfs_mount, run_user)

        for i in range(1, num_files + 1):
            run_as_user(clients[0], f"touch {nfs_mount}/{filename}_{i}", run_user)

        for i in range(1, num_dirs + 1):
            run_as_user(clients[0], f"mkdir {nfs_mount}/{dirname}_{i}", run_user)
            run_as_user(clients[0], f"touch {nfs_mount}/{dirname}_{i}/dir{i}_{filename}", run_user)

        for i in range(1, num_files + 1):
            out = run_as_user(clients[0], f"ls -Z {nfs_mount}/{filename}_{i}", run_user)
            if "nfs_t" in out[0]:
                log.info(
                    f"selinux lable is set correctly for file {filename}_{i} :  {out[0]}"
                )
            else:
                raise OperationFailedError(
                    "Selinux label is not the same as the NFS mount point"
                )

        for i in range(1, num_dirs + 1):
            out = run_as_user(
                clients[0], f"ls -Z {nfs_mount}/{dirname}_{i}/dir{i}_{filename}", run_user
            )
            if "nfs_t" in out[0]:
                log.info(
                    f"selinux lable is set correctly for the file dir{i}_{filename} :  {out[0]}"
                )
            else:
                raise OperationFailedError(
                    "Selinux label is not the same as the NFS mount point"
                )
        return 0

    except Exception as e:
        log.error(
            f"Failed to verify the selinux label set on mount point NFS v4.2 : {e}"
        )
        cleanup_cluster(clients, nfs_mount, nfs_name, nfs_export)
        log.info("Cleaning up successful")
        return 1

    finally:
        log.info("Cleaning up")
        cleanup_cluster(clients, nfs_mount, nfs_name, nfs_export, nfs_nodes=nfs_node)
        log.info("Cleaning up successful")
