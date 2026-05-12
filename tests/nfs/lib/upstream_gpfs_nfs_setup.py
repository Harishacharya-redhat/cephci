"""Spectrum Scale (GPFS) NFS bootstrap and client mounts for upstream cephci tests."""

from os import environ
from time import sleep

from ceph.waiter import WaitUntil
from cli.exceptions import ConfigError, OperationFailedError
from cli.utilities.filesys import Mount, MountFailedError, Unmount
from utility.log import Log

log = Log(__name__)

_SERVER_CI_CMDS = [
    "rm -rf ci-tests/",
    "yum install -y git wget",
    "git clone https://github.com/aravindrrh/ci-tests; cd ci-tests; git checkout scale_downstream",
    "sh ci-tests/build_scripts/common/basic-storage-scale.sh",
]


def setup_gpfs_nfs(ceph_cluster, config):
    """
    Optionally deploy Scale NFS via ci-tests, then mount the export on clients.

    Environment:
        SKIP_DEPLOYMENT: if ``true``, skip server bootstrap (cluster already prepared).
        EXPORT_NAME: export path when not set in config (default ``/ibm/scale_volume``).

    Config keys:
        mount_point, nfs_export, port, nfs_version, clients, mount_type
        skip_deployment: if present (bool), overrides SKIP_DEPLOYMENT for this run.
            Use ``true`` on ACL tests after a suite-local deploy step; ``false`` or omit on the deploy step.

    Returns:
        dict with server, clients, nfs_mount, nfs_export, nfs_server_host, port, version, mount_type
    """
    conf = config or {}
    mount_point = conf.get("mount_point", "/mnt/nfs")
    nfs_export = conf.get("nfs_export") or environ.get("EXPORT_NAME", "/ibm/scale_volume")
    port = str(conf.get("port", "2049"))
    version = str(conf.get("nfs_version", "4.1"))
    no_clients = int(conf.get("clients", "1"))
    mount_type = conf.get("mount_type", "nfs")
    skip_deploy = environ.get("SKIP_DEPLOYMENT", "").lower() == "true"
    if "skip_deployment" in conf:
        sd = conf.get("skip_deployment")
        if isinstance(sd, str):
            skip_deploy = sd.strip().lower() in ("true", "1", "yes")
        else:
            skip_deploy = bool(sd)

    server = ceph_cluster.get_nodes("installer")[0]
    clients_all = ceph_cluster.get_nodes("client")
    if no_clients > len(clients_all):
        raise ConfigError("The test requires more clients than available")
    clients = clients_all[:no_clients]

    if not skip_deploy:
        log.info(
            "Deploying Spectrum Scale / NFS on installer node %s",
            server.hostname,
        )
        for cmd in _SERVER_CI_CMDS:
            rc = server.exec_command(
                cmd=cmd, sudo=True, long_running=True, timeout=7200
            )
            if rc != 0:
                raise OperationFailedError(
                    f"GPFS upstream server command failed (exit {rc}): {cmd}"
                )
    else:
        log.info("SKIP_DEPLOYMENT=true — skipping basic-storage-scale.sh")

    nfs_server_host = server.ip_address

    if mount_type != "nfs":
        raise ConfigError(f"Unsupported mount_type {mount_type}")

    for client in clients:
        client.exec_command(
            sudo=True,
            cmd="yum install -y nfs-utils || dnf install -y nfs-utils",
            long_running=True,
            check_ec=False,
        )
        client.exec_command(sudo=True, cmd=f"mkdir -p {mount_point}")
        client.exec_command(
            sudo=True, cmd=f"umount -f {mount_point}", check_ec=False
        )
        client.exec_command(
            sudo=True, cmd=f"umount -l {mount_point}", check_ec=False
        )
        try:
            Mount(client).nfs(
                mount=mount_point,
                version=version,
                port=port,
                server=nfs_server_host,
                export=nfs_export,
            )
        except MountFailedError as e:
            raise OperationFailedError(
                f"NFS mount failed on {client.hostname}: {e}"
            ) from e
        sleep(1)

    log.info(
        "GPFS NFS ready: %s:%s -> %s on %d client(s)",
        nfs_server_host,
        nfs_export,
        mount_point,
        len(clients),
    )

    return {
        "server": server,
        "clients": clients,
        "nfs_mount": mount_point,
        "nfs_export": nfs_export,
        "nfs_server_host": nfs_server_host,
        "port": port,
        "version": version,
        "mount_type": mount_type,
    }


def teardown_gpfs_nfs(clients, nfs_mount):
    """Remove data under the mount, unmount, and delete the mount point."""
    if not isinstance(clients, list):
        clients = [clients]
    timeout, interval = 600, 10
    for client in clients:
        for w in WaitUntil(timeout=timeout, interval=interval):
            try:
                client.exec_command(
                    sudo=True, cmd=f"rm -rf {nfs_mount}/*", long_running=True
                )
                break
            except Exception as e:
                log.warning("rm under %s failed, retrying: %s", nfs_mount, e)
        if w.expired:
            log.error("Timeout clearing %s on %s", nfs_mount, client.hostname)
        sleep(2)
        out = Unmount(client).unmount(nfs_mount)
        if out:
            log.warning("umount %s on %s returned: %s", nfs_mount, client.hostname, out)
        client.exec_command(sudo=True, cmd=f"rm -rf {nfs_mount}", check_ec=False)
        sleep(1)
