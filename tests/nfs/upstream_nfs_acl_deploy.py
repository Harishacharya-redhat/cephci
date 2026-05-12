"""
Spectrum Scale / NFS deployment stage for the GPFS upstream ACL suite.

Run this module as the **first** test in ``nfs-scale-acl-upstream.yaml`` so
``basic-storage-scale.sh`` executes once.  Later ACL modules should set
``skip_deployment: true`` in config so they only mount clients.
"""

from cli.exceptions import ConfigError
from tests.nfs.lib.upstream_gpfs_nfs_setup import setup_gpfs_nfs, teardown_gpfs_nfs
from utility.log import Log

log = Log(__name__)


def run(ceph_cluster, **kw):
    """Deploy Scale NFS on the installer and mount clients (suite bootstrap)."""
    config = dict(kw.get("config") or {})
    clients_all = ceph_cluster.get_nodes("client")
    no_clients = int(config.get("clients", "1"))
    if no_clients > len(clients_all):
        raise ConfigError("The test requires more clients than available")

    config["skip_deployment"] = False

    gpfs = None
    try:
        log.info(
            "\n"
            + "=" * 70
            + "\n"
            + "  NFS ACL SUITE — Spectrum Scale / NFS deployment\n"
            + "=" * 70
        )
        gpfs = setup_gpfs_nfs(ceph_cluster, config)
        log.info("NFS ACL suite deployment stage completed successfully")
        return 0
    except Exception as e:
        log.error("NFS ACL suite deployment failed: %s", e)
        return 1
    finally:
        if gpfs:
            teardown_gpfs_nfs(gpfs["clients"], gpfs["nfs_mount"])
            log.info("ACL deployment stage: unmount and mount-point cleanup done")
