import re
from time import sleep

from cli.ceph.ceph import Ceph
from cli.exceptions import ConfigError, OperationFailedError
from cli.utilities.filesys import Mount
from tests.nfs.nfs_operations import cleanup_cluster
from tests.nfs.test_nfs_qos_on_cluster_level_enablement import enable_disable_qos_for_cluster
from utility.log import Log

log = Log(__name__)


def capture_copy_details(client, nfs_mount,file_name, size="50"):
    """
    Captures the output of the dd command executed remotely.
    :param nfs_mount: The NFS mount path where the file will be created.
    :param size: The size of the file to create (default is "100M").
    :return: A tuple containing (stdout, stderr) from the dd command.
    """
    cmd  = f"touch {nfs_mount}/{file_name}"
    client.exec_command(
        sudo=True,
        cmd=cmd,
    )

    # cmd = f"dd if=/dev/urandom of={nfs_mount}/{file_name} bs={size}M count=1 > {nfs_mount}/out.txt 2>&1"
    cmd = f"dd if=/dev/urandom of={nfs_mount}/{file_name} bs={size}M count=1"
    data = client.exec_command(
        sudo=True,
        cmd=cmd
    )

    if re.search(r'(\d+\.\d+ MB/s)$', data[1]):
        speed = re.findall(r'(\d+\.\d+ MB/s)$', data[1])[0]
        log.info(f"File created successfully on {client.hostname}")
        return speed
    else:
        raise OperationFailedError(f"Failed to run dd command on {client.hostname}")

def enable_disable_qos_for_export(
    enable_flag: bool,
    ceph_export_nfs_obj,
    cluster_name: str,
    qos_type: str = None,
    nfs_name = str,
    export = str,
    **qos_parameters,
) -> None:
    # Common validation
    if enable_flag and not qos_type:
        raise ValueError("qos_type is required when enabling QoS")

    operation_key = "enable" if enable_flag else "disable"

    try:
        if enable_flag:
            ceph_export_nfs_obj.qos.enable(
                qos_type=qos_type,
                nfs_name=nfs_name,
                export=export,
                **qos_parameters,)
        else:
            ceph_export_nfs_obj.qos.disable(cluster_id=nfs_name, export=export)
        qos_data = ceph_export_nfs_obj.qos.get(nfs_name=nfs_name, export=export)
    except Exception as e:
        raise OperationFailedError(f"Failed to {operation_key} QoS for export {export} in cluster {cluster_name} : {str(e)}")


def run(ceph_cluster, **kw):
    """Verify QoS operations on NFS cluster"""
    config = kw.get("config")
    clients = ceph_cluster.get_nodes("client")
    nfs_nodes = ceph_cluster.get_nodes("nfs")
    cluster_name = config["cluster_name"]
    port = config.get("port", "2049")
    version = config.get("nfs_version", "4.2") # Select only the required number of clients
    fs_name = "cephfs"
    nfs_name = "cephfs-nfs"
    nfs_export = "/export"
    nfs_mount = "/mnt/nfs"
    fs = "cephfs"
    cluster_bw = config['cluster_bw'][0]
    export_bw = config['export_bw'][0]
    if not nfs_nodes:
        raise OperationFailedError("No NFS nodes found in cluster")

    nfs_node = nfs_nodes[0]
    client = clients[0]
    qos_type = config.get("qos_type", [])
    ceph_nfs_client = Ceph(client).nfs

    try:
        # Create NFS cluster
        ceph_nfs_client.cluster.create(
            name=cluster_name, nfs_server=nfs_node.hostname, ha=False
        )
        sleep(3)

        # Get cluster name reliably
        nfs_cluster_info = ceph_nfs_client.cluster.ls()
        if not nfs_cluster_info:
            raise OperationFailedError("No NFS clusters found")
        cluster_name_created = nfs_cluster_info[0]
        if cluster_name_created != cluster_name:
            raise OperationFailedError("NFS cluster was not created as user parameter")

        # Process QoS operations
        for qos in qos_type:
            # Enable QoS with parameters
            enable_disable_qos_for_cluster(
                enable_flag=True,
                ceph_cluster_nfs_obj=ceph_nfs_client.cluster,
                cluster_name=cluster_name,
                qos_type=qos,
                **{
                    k: cluster_bw[k]
                    for k in [
                        "max_export_write_bw",
                        "max_export_read_bw",
                        "max_client_write_bw",
                        "max_client_read_bw",
                    ]
                    if k in cluster_bw
                },
            )

            # create nfs export
            ceph_nfs_client.export.create(
                fs_name=fs_name, nfs_name=nfs_name, nfs_export=nfs_export, fs=fs
            )

            export_data = ceph_nfs_client.export.get(nfs_name=nfs_name, nfs_export=nfs_export)
            if not export_data:
                raise OperationFailedError("Failed to create nfs export")

            # mount the nfs export
            client.create_dirs(dir_path=nfs_mount, sudo=True)
            if Mount(client).nfs(
                mount=nfs_mount,
                version=version,
                port=port,
                server=nfs_node.hostname,
                export=nfs_export,
            ):
                raise OperationFailedError(f"Failed to mount nfs on {client.hostname}")

            speed = capture_copy_details(client, nfs_mount, "sample.txt")

            if (float(re.findall(r"\d+", cluster_bw["max_export_write_bw"])[0]) >
                    float(re.findall(r"\d+\.\d+", speed)[0])):

                log.info(f"Test passed: QoS {qos} enabled successfully in cluster level"
                         f" transfer speed is {speed} and max_export_write_bw is {cluster_bw['max_export_write_bw']}")

            enable_disable_qos_for_export(enable_flag=True,
                                          ceph_export_nfs_obj=ceph_nfs_client.export,
                                          cluster_name=cluster_name,
                                          qos_type=qos,
                                          nfs_name= nfs_name,
                                          export = nfs_export,
                                          **{
                                              k: export_bw[k]
                                              for k in [
                                                  "max_export_write_bw",
                                                  "max_export_read_bw",
                                                  "max_client_write_bw",
                                                  "max_client_read_bw",
                                              ]
                                              if k in export_bw
                                          },
                                          )

            client.create_dirs(dir_path=nfs_mount, sudo=True)
            if Mount(client).nfs(
                    mount=nfs_mount,
                    version=version,
                    port=port,
                    server=nfs_node.hostname,
                    export=nfs_export,
            ):
                raise OperationFailedError(f"Failed to mount nfs on {client.hostname}")

            speed = capture_copy_details(client, nfs_mount, "sample.txt")

            if (float(re.findall(r"\d+", export_bw["max_export_write_bw"])[0]) >
                    float(re.findall(r"\d+\.\d+", speed)[0])):
                log.info(f"Test passed: QoS {qos} enabled successfully in export level"
                         f" transfer speed is {speed} and max_export_write_bw is "
                         f"{export_bw['max_export_write_bw']}")
            else:
                raise OperationFailedError(f"Test failed: QoS {qos} enabled successfully in export level")


        enable_disable_qos_for_export(enable_flag=False, nfs_name=nfs_name, export=nfs_export,
                                      ceph_export_nfs_obj=ceph_nfs_client.export,cluster_name=cluster_name)

        enable_disable_qos_for_cluster(enable_flag=False,ceph_cluster_nfs_obj=ceph_nfs_client.cluster,
                                       cluster_name=cluster_name)

        return 0
    except (ConfigError, OperationFailedError, RuntimeError) as e:
        log.error(f"Test failed: {e}")
        return 1
    finally:
        log.info("Cleanup in progress <Finally block>")
        log.debug(f"deleting NFS cluster {cluster_name}")
        cleanup_cluster(clients, nfs_mount, nfs_name, nfs_export)
