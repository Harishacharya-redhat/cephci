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
    """Verify selinux context label is preserved by moving files in different directories
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
    num_files = 5
    dir_name = "dir1"
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

        try:
            for i in range(1, num_files + 1):
                run_as_user(clients[0], f"touch {nfs_mount}/{filename}_{i}", run_user)
        except Exception:
            raise OperationFailedError(f"failed to create file {filename}_{i}")

        # Set the selinux label for files from client 1
        try:
            for i in range(1, num_files + 1):
                chcon_cmd = f"chcon -t public_content_t {nfs_mount}/{filename}_{i}"
                clients[0].exec_command(cmd=chcon_cmd, sudo=True)
        except Exception:
            raise OperationFailedError(
                f"failed to set/get the selinux label for file {filename}_{i}"
            )

        try:
            run_as_user(clients[0], f"mkdir {nfs_mount}/{dir_name}", run_user)
        except Exception:
            raise OperationFailedError(f"failed to create directory {dir_name} ")

        # Set the selinux label of the directory created from client 1
        try:
            chcon_cmd = f"chcon -t httpd_sys_content_t {nfs_mount}/{dir_name}"
            clients[0].exec_command(cmd=chcon_cmd, sudo=True)
        except Exception:
            raise OperationFailedError(f"failed to set the selinux context {dir_name} ")

        try:
            for i in range(1, num_files + 1):
                run_as_user(clients[0], f"touch {nfs_mount}/{dir_name}/newfile_{i}", run_user)
        except Exception:
            raise OperationFailedError(f"failed to create file newfile_{i}")

        try:
            for i in range(1, num_files + 1):
                out = run_as_user(
                    clients[1], f"ls -Z {nfs_mount}/{dir_name}/newfile_{i}", run_user
                )
                if "httpd_sys_content_t" in out[0]:
                    log.info(f"selinux lable is set correctly: {out[0]}")
                else:
                    raise OperationFailedError("Failed to set/get the selinux context")
        except Exception:
            raise OperationFailedError(
                f"failed to get the selinux context of file : {dir_name}/newfile_{i}"
            )

        # Move the files created on the NFS mount to the directory from client 2
        try:
            for i in range(1, num_files + 1):
                run_as_user(
                    clients[1],
                    f"mv {nfs_mount}/{filename}_{i} {nfs_mount}/{dir_name}",
                    run_user,
                )
        except Exception:
            raise OperationFailedError(
                f"failed to move the file inside directory: {filename}_{i}"
            )

        try:
            for i in range(1, num_files + 1):
                out = run_as_user(
                    clients[1],
                    f"ls -Z {nfs_mount}/{dir_name}/{filename}_{i}",
                    run_user,
                )
                if "public_content_t" in out[0]:
                    log.info(f"selinux lable is preserved: {out[0]}")
                else:
                    raise OperationFailedError(
                        "Unexpected : The selinux context is not preserved"
                    )
        except Exception:
            raise OperationFailedError(
                f"failed to get the selinux label for file {filename}_{i}"
            )
        return 0
    except Exception as e:
        log.error(f"Failed to set the selinux label on NFS v4.2 : {e}")
        cleanup_cluster(clients, nfs_mount, nfs_name, nfs_export)
        log.info("Cleaning up successful")
        return 1

    finally:
        log.info("Cleaning up")
        cleanup_cluster(clients, nfs_mount, nfs_name, nfs_export, nfs_nodes=nfs_node)
        log.info("Cleaning up successful")
