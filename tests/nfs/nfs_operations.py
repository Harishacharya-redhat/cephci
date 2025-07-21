import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Thread
from time import sleep

import yaml

from ceph.waiter import WaitUntil
from cli.ceph.ceph import Ceph
from cli.cephadm.cephadm import CephAdm
from cli.exceptions import OperationFailedError
from cli.utilities.filesys import Mount, Unmount
from cli.utilities.packages import Package
from cli.utilities.utils import check_coredump_generated, get_ip_from_node, reboot_node
from utility.log import Log
from utility.retry import retry

log = Log(__name__)

ceph_cluster_obj = None
setup_start_time = None


class NfsCleanupFailed(Exception):
    pass


def setup_nfs_cluster(
    clients,
    nfs_server,
    port,
    version,
    nfs_name,
    nfs_mount,
    fs_name,
    export,
    fs,
    ha=False,
    vip=None,
    ceph_cluster=None,
    active_standby=False,
):
    # Get ceph cluter object and setup start time
    global ceph_cluster_obj
    global setup_start_time
    ceph_cluster_obj = ceph_cluster
    setup_start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    setup_start_time = datetime.strptime(setup_start_time, "%Y-%m-%d %H:%M:%S")

    # Step 1: Enable nfs
    installer_node = ceph_cluster.get_nodes("installer")[0]
    version_info = installer_node.exec_command(
        sudo=True, cmd="cephadm shell -- rpm -qa | grep nfs"
    )
    log.info("nfs info: %s", version_info)
    Ceph(clients[0]).mgr.module.enable(module="nfs", force=True)
    sleep(3)

    # Step 2: Create an NFS cluster
    Ceph(clients[0]).nfs.cluster.create(
        name=nfs_name,
        nfs_server=nfs_server,
        ha=ha,
        vip=vip,
        active_standby=active_standby,
    )
    sleep(3)

    # Step 3: Perform Export on clients
    export_list = []
    i = 0
    for client in clients:
        export_name = "{export}_{i}".format(export=export, i=i)
        Ceph(client).nfs.export.create(
            fs_name=fs_name, nfs_name=nfs_name, nfs_export=export_name, fs=fs
        )
        i += 1
        export_list.append(export_name)
        sleep(1)

    # Get the mount versions specific to clients
    mount_versions = _get_client_specific_mount_versions(version, clients)

    # Step 4: Perform nfs mount
    # If there are multiple nfs servers provided, only one is required for mounting
    if isinstance(nfs_server, list):
        nfs_server = nfs_server[0]
    if ha:
        nfs_server = vip.split("/")[0]  # Remove the port

    i = 0
    for version, clients in mount_versions.items():
        for client in clients:
            client.create_dirs(dir_path=nfs_mount, sudo=True)
            if mount_retry(
                clients, i, nfs_mount, version, port, nfs_server, export_name
            ):
                log.info("Mount succeeded on %s" % client.hostname)
            i += 1
            sleep(1)
    log.info("Mount succeeded on all clients")

    try:
        cmd_used = None
        services = [
            x["service_name"]
            for x in json.loads(Ceph(clients[0]).orch.ls(format="json"))
        ]
        # check nfs cluster and services are up
        if Ceph(clients[0]).nfs.cluster.ls():
            cmd_used = "ceph nfs cluster ls"
            log.info(
                "NFS cluster %s created successfully using %s"
                % (Ceph(clients[0]).nfs.cluster.ls()[0], cmd_used)
            )
        # verifying with orch cmd
        if [x for x in services if x.startswith("nfs")]:
            cmd_used = "ceph orch ls"
            log.info("NFS services are up and running from cmd %s" % cmd_used)

        if Ceph(clients[0]).execute("ps aux | grep nfs-ganesha")[0]:
            cmd_used = "ps aux | grep nfs-ganesha"
            log.info("NFS daemons are up and running verifying using %s" % cmd_used)
    except Exception as e:
        log.error(
            "Failed to verify nfs cluster and services %s cmd used: %s" % (e, cmd_used)
        )

    nfs_nodes = ceph_cluster.get_nodes("nfs")

    # Step 5: Enable nfs coredump to nfs nodes
    Enable_nfs_coredump(nfs_nodes)


def cleanup_cluster(clients, nfs_mount, nfs_name, nfs_export):
    """
    Clean up the cluster post nfs operation
    Steps:
        1. rm -rf of the content inside the mount folder --> rm -rf /mnt/nfs/*
        2. Unmount the volume
        3. rm -rf of the mount point
        4. delete export
        5. delete cluster
    Args:
        clients (ceph): Client nodes
        nfs_mount (str): nfs mount path
        nfs_name (str): nfs cluster name
        nfs_export (str): nfs export path
    """
    if not isinstance(clients, list):
        clients = [clients]

    # Check nfs coredump
    if ceph_cluster_obj:
        nfs_nodes = ceph_cluster_obj.get_nodes("nfs")
        coredump_path = "/var/lib/systemd/coredump"
        for nfs_node in nfs_nodes:
            if check_coredump_generated(nfs_node, coredump_path, setup_start_time):
                raise NfsCleanupFailed(
                    "Coredump generated post execution of the current test case"
                )

    # Wait until the rm operation is complete
    timeout, interval = 600, 10
    for client in clients:
        # Clear the nfs_mount, at times rm operation can fail
        # as the dir is not empty, this being an expected behaviour,
        # the solution is to repeat the rm operation.
        for w in WaitUntil(timeout=timeout, interval=interval):
            try:
                client.exec_command(
                    sudo=True, cmd=f"rm -rf {nfs_mount}/*", long_running=True
                )
                break
            except Exception as e:
                log.warning(f"rm operation failed, repeating!. Error {e}")
        if w.expired:
            raise NfsCleanupFailed(
                "Failed to cleanup nfs mount dir even after multiple iterations. Timed out!"
            )

        log.info("Unmounting nfs-ganesha mount on client:")
        sleep(3)
        if Unmount(client).unmount(nfs_mount):
            raise OperationFailedError(f"Failed to unmount nfs on {client.hostname}")
        log.info("Removing nfs-ganesha mount dir on client:")
        client.exec_command(sudo=True, cmd=f"rm -rf  {nfs_mount}")
        sleep(3)

    # Delete all exports
    for i in range(len(clients)):
        Ceph(clients[0]).nfs.export.delete(nfs_name, f"{nfs_export}_{i}")
    Ceph(clients[0]).nfs.cluster.delete(nfs_name)
    sleep(30)
    check_nfs_daemons_removed(clients[0])

    # Delete the subvolume
    for i in range(len(clients)):
        cmd = "ceph fs subvolume ls cephfs --group_name ganeshagroup"
        out = client.exec_command(sudo=True, cmd=cmd)
        json_string, _ = out
        data = json.loads(json_string)
        # Extract names of subvolume
        for item in data:
            subvol = item["name"]
            cmd = f"ceph fs subvolume rm cephfs {subvol} --group_name ganeshagroup"
            client.exec_command(sudo=True, cmd=cmd)

    # Delete the subvolume group
    cmd = "ceph fs subvolumegroup rm cephfs ganeshagroup --force"
    client.exec_command(sudo=True, cmd=cmd)


def setup_custom_nfs_cluster_multi_export_client(
    clients,
    nfs_server,
    port,
    version,
    nfs_name,
    fs_name,
    fs,
    nfs_mount,
    nfs_export,
    ha=False,
    vip=None,
    export_num=None,
    ceph_cluster=None,
):
    # Get ceph cluter object and setup start time
    global ceph_cluster_obj
    global setup_start_time
    ceph_cluster_obj = ceph_cluster
    setup_start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    setup_start_time = datetime.strptime(setup_start_time, "%Y-%m-%d %H:%M:%S")

    # Step 1: Enable nfs
    installer_node = ceph_cluster.get_nodes("installer")[0]
    version_info = installer_node.exec_command(
        sudo=True, cmd="cephadm shell -- rpm -qa | grep nfs"
    )
    log.info("nfs info: %s", version_info)
    Ceph(clients[0]).mgr.module.enable(module="nfs", force=True)
    sleep(3)

    # Step 2: Create an NFS cluster
    Ceph(clients[0]).nfs.cluster.create(
        name=nfs_name, nfs_server=nfs_server, ha=ha, vip=vip
    )
    sleep(3)

    # Step 3: Perform Export on clients
    client_export_mount_dict = exports_mounts_perclient(
        clients, nfs_export, nfs_mount, export_num
    )

    # Create the export and mount points for each client
    for client_num in range(len(clients)):
        for export_num in range(
            len(client_export_mount_dict[clients[client_num]]["export"])
        ):
            export_name = client_export_mount_dict[clients[client_num]]["export"][
                export_num
            ]
            mount_name = client_export_mount_dict[clients[client_num]]["mount"][
                export_num
            ]
            Ceph(clients[client_num]).nfs.export.create(
                fs_name=fs_name,
                nfs_name=nfs_name,
                nfs_export=export_name,
                fs=fs,
            )
            sleep(1)
            # Get the mount versions specific to clients
            mount_versions = _get_client_specific_mount_versions(version, clients)
            # Step 4: Perform nfs mount
            # If there are multiple nfs servers provided, only one is required for mounting
            if isinstance(nfs_server, list):
                nfs_server = nfs_server[0]
            if ha:
                nfs_server = vip.split("/")[0]  # Remove the port
            for version, clients in mount_versions.items():
                clients[client_num].create_dirs(dir_path=mount_name, sudo=True)
                if Mount(clients[client_num]).nfs(
                    mount=mount_name,
                    version=version,
                    port=port,
                    server=nfs_server,
                    export=export_name,
                ):
                    raise OperationFailedError(
                        "Failed to mount nfs on %s" % clients[client_num].hostname
                    )
                sleep(1)
        log.info("Mount succeeded on all clients")

    try:
        cmd_used = None
        services = [
            x["service_name"]
            for x in json.loads(Ceph(clients[0]).orch.ls(format="json"))
        ]
        # check nfs cluster and services are up
        if Ceph(clients[0]).nfs.cluster.ls():
            cmd_used = "ceph nfs cluster ls"
            log.info(
                "NFS cluster %s created successfully using %s"
                % (Ceph(clients[0]).nfs.cluster.ls()[0], cmd_used)
            )
        # verifying with orch cmd
        if [x for x in services if x.startswith("nfs")]:
            cmd_used = "ceph orch ls"
            log.info("NFS services are up and running from cmd %s" % cmd_used)

        if Ceph(clients[0]).execute("ps aux | grep nfs-ganesha")[0]:
            cmd_used = "ps aux | grep nfs-ganesha"
            log.info("NFS daemons are up and running verifying using %s" % cmd_used)
    except Exception as e:
        log.error(
            "Failed to verify nfs cluster and services %s cmd used: %s" % (e, cmd_used)
        )

    nfs_nodes = ceph_cluster.get_nodes("nfs")

    # Step 5: Enable nfs coredump to nfs nodes
    Enable_nfs_coredump(nfs_nodes)


def exports_mounts_perclient(clients, nfs_export, nfs_mount, export_num) -> dict:
    """
    Args:
        clients (list): List of client nodes
        nfs_export (str): NFS export path
        nfs_mount (str): NFS mount path
        export_num (int): Number of exports to create
    Returns:
        dict: Dictionary mapping each client to its corresponding exports and mounts
    """
    export_list = [f"{nfs_export}_{i}" for i in range(export_num)]
    mount_list = [f"{nfs_mount}_{i}" for i in range(export_num)]

    # Divide exports among clients
    num_clients = len(clients)
    exports_per_client = len(export_list) // num_clients
    remainder = len(export_list) % num_clients
    client_exports = []
    client_mounts = []
    start = 0

    for i in range(num_clients):
        end = start + exports_per_client + (1 if i < remainder else 0)
        client_exports.append(export_list[start:end])
        client_mounts.append(mount_list[start:end])
        start = end

    client_export_mount_dict = {}
    for i in range(len(clients)):
        client_export_mount_dict.update(
            {
                clients[i]: {
                    "export": client_exports[i],
                    "mount": client_mounts[i],
                }
            }
        )
    return client_export_mount_dict


def cleanup_custom_nfs_cluster_multi_export_client(
    clients, nfs_mount, nfs_name, nfs_export, export_num
):
    """
    Clean up the cluster post nfs operation
    """
    """
        Clean up the cluster post nfs operation
        Steps:
            1. rm -rf of the content inside the mount folder --> rm -rf /mnt/nfs/*
            2. Unmount the volume
            3. rm -rf of the mount point
            4. delete export
            5. delete cluster
        Args:
            clients (ceph): Client nodes
            nfs_mount (str): nfs mount path
            nfs_name (str): nfs cluster name
            nfs_export (str): nfs export path
        """
    if not isinstance(clients, list):
        clients = [clients]

    # Check nfs coredump
    if ceph_cluster_obj:
        nfs_nodes = ceph_cluster_obj.get_nodes("nfs")
        coredump_path = "/var/lib/systemd/coredump"
        for nfs_node in nfs_nodes:
            if check_coredump_generated(nfs_node, coredump_path, setup_start_time):
                raise NfsCleanupFailed(
                    "Coredump generated post execution of the current test case"
                )

    # Wait until the rm operation is complete
    timeout, interval = 600, 10

    client_export_mount_dict = exports_mounts_perclient(
        clients, nfs_export, nfs_mount, export_num
    )

    for client_num in range(len(clients)):
        for export_num in range(
            len(client_export_mount_dict[clients[client_num]]["export"])
        ):
            export_name = client_export_mount_dict[clients[client_num]]["export"][
                export_num
            ]
            mount_name = client_export_mount_dict[clients[client_num]]["mount"][
                export_num
            ]
            client = list(client_export_mount_dict.keys())[client_num]
            # Clear the nfs_mount, at times rm operation can fail
            # as the dir is not empty, this being an expected behaviour,
            # the solution is to repeat the rm operation.
            for w in WaitUntil(timeout=timeout, interval=interval):
                try:
                    client.exec_command(
                        sudo=True, cmd=f"rm -rf {mount_name}/*", long_running=True
                    )
                    break
                except Exception as e:
                    log.warning(f"rm operation failed, repeating!. Error {e}")
            if w.expired:
                raise NfsCleanupFailed(
                    "Failed to cleanup nfs mount dir even after multiple iterations. Timed out!"
                )
            log.info("Unmounting nfs-ganesha mount on client:")
            sleep(3)
            if Unmount(client).unmount(mount_name):
                raise OperationFailedError(
                    f"Failed to unmount nfs on {client.hostname}"
                )
            log.info("Removing nfs-ganesha mount dir on client:")
            client.exec_command(sudo=True, cmd=f"rm -rf  {mount_name}")
            sleep(3)

            # Delete all exports
            Ceph(clients[0]).nfs.export.delete(nfs_name, export_name)

    Ceph(clients[0]).nfs.cluster.delete(nfs_name)
    sleep(30)
    check_nfs_daemons_removed(clients[0])

    # Delete the subvolume
    for i in range(len(clients)):
        cmd = "ceph fs subvolume ls cephfs --group_name ganeshagroup"
        out = client.exec_command(sudo=True, cmd=cmd)
        json_string, _ = out
        data = json.loads(json_string)
        # Extract names of subvolume
        for item in data:
            subvol = item["name"]
            cmd = f"ceph fs subvolume rm cephfs {subvol} --group_name ganeshagroup"
            client.exec_command(sudo=True, cmd=cmd)

    # Delete the subvolume group
    cmd = "ceph fs subvolumegroup rm cephfs ganeshagroup --force"
    client.exec_command(sudo=True, cmd=cmd)


def _get_client_specific_mount_versions(versions, clients):
    # Identify the multi mount versions specific to clients
    version_dict = {}
    if not isinstance(versions, list):
        version_dict[versions] = clients
        return version_dict
    ctr = 0
    for entry in versions:
        ver = list(entry.keys())[0]
        count = list(entry.values())[0]
        version_dict[ver] = clients[ctr : ctr + int(count)]
        ctr = ctr + int(count)
    return version_dict


def perform_failover(nfs_nodes, failover_node, vip):
    # Trigger reboot on the failover node
    th = Thread(target=reboot_node, args=(failover_node,))
    th.start()

    # Validate any of the other nodes has got the VIP
    flag = False

    # Remove the port from vip
    if "/" in vip:
        vip = vip.split("/")[0]

    # Perform the check with a timeout of 60 seconds
    for w in WaitUntil(timeout=120, interval=5):
        for node in nfs_nodes:
            if node != failover_node:
                assigned_ips = get_ip_from_node(node)
                log.info(f"IP addrs assigned to node : {assigned_ips}")
                # If vip is assigned, set the flag and exit
                if vip in assigned_ips:
                    flag = True
                    log.info(f"Failover success, VIP reassigned to {node.hostname}")
        if flag:
            break
    if w.expired:
        raise OperationFailedError(
            "The failover process failed and vip is not assigned to the available nodes"
        )
    # Wait for the node to complete reboot
    th.join()


def Enable_nfs_coredump(nfs_nodes, conf_file="/etc/systemd/coredump.conf"):
    """nfs_coredump
    Args:
        nfs_nodes(obj): nfs server node
        conf_file: conf file path
    """
    if not isinstance(nfs_nodes, list):
        nfs_nodes = [nfs_nodes]

    for nfs_node in nfs_nodes:
        try:
            nfs_node.exec_command(
                sudo=True, cmd=f"echo Storage=external >> {conf_file}"
            )
            nfs_node.exec_command(
                sudo=True, cmd=f"echo DefaultLimitCORE=infinity >> {conf_file}"
            )
            nfs_node.exec_command(sudo=True, cmd="systemctl daemon-reexec")
        except Exception:
            raise OperationFailedError(f"failed enable coredump for {nfs_node}")


def get_nfs_pid_and_memory(nfs_nodes):
    """get nfs-ganesha pid and memory consumption(RSS)
    Args:
        nfs_nodes(obj): nfs server node
    Returns:
        nfs_server_info(dic): {"nfs server1": ["PID","RSS(MB)"], "nfs server2": ["PID","RSS(MB)"]}
    """
    nfs_server_info = {}
    if not isinstance(nfs_nodes, list):
        nfs_nodes = [nfs_nodes]

    for nfs_node in nfs_nodes:
        try:
            pid = nfs_node.exec_command(sudo=True, cmd="pgrep ganesha")[0].strip()
            rss = nfs_node.exec_command(sudo=True, cmd=f"ps -p {pid} -o rss=")[
                0
            ].strip()
            nfs_server_info[nfs_node.hostname] = [pid, rss]
        except Exception:
            raise OperationFailedError(
                f"failed get nfs process ID and rss for {nfs_node}"
            )
    return nfs_server_info


def permission(client, nfs_name, nfs_export, old_permission, new_permission):
    # Change export permissions to RO
    out = Ceph(client).nfs.export.get(nfs_name, f"{nfs_export}_0")
    client.exec_command(sudo=True, cmd=f"echo '{out}' > export.conf")
    client.exec_command(
        sudo=True,
        cmd=f'sed -i \'s/"access_type": "{old_permission}"/"access_type": "{new_permission}"/\' export.conf',
    )
    Ceph(client).nfs.export.apply(nfs_name, "export.conf")

    # Wait till the NFS daemons are up
    sleep(10)


def enable_v3_locking(installer, nfs_name, nfs_node, nfs_server_name):
    # Enable the NLM support for v3 Locking
    content = f"""service_type: nfs
service_id: {nfs_name}
placement:
    hosts:
        - {nfs_server_name}
spec:
    enable_nlm: true"""

    with open("ganesha.yaml", "w") as f:
        yaml.dump(content, f)
    log.info(content)

    # Adding the configurations into the ganesha.yaml file.
    cmd = f"echo '{content}' >> ganesha.yaml"
    CephAdm(installer).shell(cmd=cmd)

    # Mount the export file inside shell and apply changes
    cmd = (
        "--mount ganesha.yaml:/var/lib/ceph/ganesha.yaml -- "
        "ceph orch apply -i /var/lib/ceph/ganesha.yaml"
    )
    CephAdm(installer).shell(cmd=cmd)

    # Restart the NFS Ganesha service
    CephAdm(installer).ceph.orch.redeploy(service=f"nfs.{nfs_name}")

    # Wait till the NFS daemons are up
    sleep(10)

    # Start the rpc-statd service on server
    cmd = "sudo systemctl start rpc-statd"
    nfs_node.exec_command(cmd=cmd)


def getfattr(client, file_path, attribute_name=None):
    # Fetch the extended attribute for file or directory
    """
    Args:
    attribute_name (str): Specific attribute name to retrieve. If None, retrieves all attributes.
    file_path (str): Path to the file/dir whose extended attribute is to be retrieved.
    """
    cmd = f"getfattr -d {file_path}"
    if attribute_name:
        cmd += " -n user.{attribute_name}"
    out = client.exec_command(sudo=True, cmd=cmd)
    log.info(out)
    return out


def setfattr(client, file_path, attribute_name, attribute_value):
    """
    Sets the value of an extended attribute on a file using setfattr command.

    Args:
    - file_path (str): Path to the file/dir where the extended attribute is to be set.
    - attribute_name (str): Name of the extended attribute to set.
    - attribute_value (str): Value to set for the extended attribute.
    """
    cmd = f"setfattr -n user.{attribute_name} -v {attribute_value} {file_path}"
    out = client.exec_command(sudo=True, cmd=cmd)
    return out


def removeattr(client, file_path, attribute_name):
    """
    Remove the value of an extended attribute on a file.

    Args:
    - file_path (str): Path to the file/dir where the extended attribute needs tp be removed.
    - attribute_name (str): Name of the extended attribute to be removed.
    """
    cmd = f"setfattr -x user.{attribute_name} {file_path}"
    out = client.exec_command(sudo=True, cmd=cmd)
    return out


def check_nfs_daemons_removed(client):
    """
    Check if NFS daemons are removed.
    Wait until there are no NFS daemons listed by 'ceph orch ls'.
    """
    while True:
        try:
            cmd = "ceph orch ls | grep nfs"
            out = client.exec_command(sudo=True, cmd=cmd)

            if out:
                print("NFS daemons are still present. Waiting...")
                sleep(10)  # Wait before checking again
            else:
                print("All NFS daemons have been removed.")
                break
        except Exception as e:
            print(f"Unexpected error: {e}")
            break


def create_nfs_via_file_and_verify(installer_node, nfs_objects, timeout):
    """
    Create a temporary YAML file with NFS Ganesha configuration.
    Args:
        installer_node: The node where the NFS Ganesha configuration will be applied.
        nfs_objects: List of NFS Ganesha configuration objects.
    Returns:
        str: Path to the temporary YAML file.
    """
    temp_file = tempfile.NamedTemporaryFile(suffix=".yaml")
    spec_file = installer_node.remote_file(
        sudo=True, file_name=temp_file.name, file_mode="wb"
    )
    spec = yaml.dump_all(nfs_objects, sort_keys=False, indent=2).encode("utf-8")
    spec_file.write(spec)
    spec_file.flush()

    try:
        pos_args = []
        CephAdm(installer_node, mount="/tmp/").ceph.orch.apply(
            input=temp_file.name, check_ec=True, pos_args=pos_args
        )
        verify_nfs_ganesha_service(node=installer_node, timeout=timeout)
        log.info("NFS Ganesha spec file applied successfully.")
        return True
    except Exception as err:
        log.error(f"Failed to apply NFS Ganesha spec file: {err}")


def verify_nfs_ganesha_service(node, timeout):
    """
    Verify the status of NFS Ganesha service.
    Args:
        node: Installer Node.
    Returns:
        bool: True if the service is in the expected state, False otherwise.
    """
    interval = 5

    for w in WaitUntil(timeout=timeout, interval=interval):
        result = json.loads(
            CephAdm(node).ceph.orch.ls(format="json", service_type="nfs")
        )
        if all(x["status"]["running"] == x["status"]["size"] for x in result):
            log.info(
                "\n"
                + "=" * 30
                + "\n"
                + "NFS Ganesha service is up and running. Time taken : -- %s seconds \n"
                + "=" * 30,
                w._attempt * w.interval,
            )
            log.info("sleep(20)  # Allow some time for the service to stabilize")
            sleep(20)  # Allow some time for the service to stabilize
            return True
        else:
            log.info(
                "\n \n NFS Ganesha service is not running as expected, retrying...... "
                "Time remaining : -- %s seconds \n",
                timeout - (w._attempt * w.interval),
            )
            log.debug("Current status: %s", result)
    log.error("\n NFS Ganesha service is not running as expected.")
    if w.expired:
        raise OperationFailedError(
            "NFS daemons check failed Timeout expired. -- %s seconds" % timeout
        )


def delete_nfs_clusters_in_parallel(installer_node, timeout):
    """
    Delete NFS clusters in batch.
    Args:
        installer_node: The node where the NFS Ganesha configuration will be applied.
        nfs_objects: List of NFS Ganesha configuration objects.
    """
    clusters = CephAdm(installer_node).ceph.nfs.cluster.ls()
    with ThreadPoolExecutor(max_workers=None) as executor:
        futures = [
            executor.submit(
                CephAdm(installer_node).ceph.nfs.cluster.delete,
                cluster,
            )
            for cluster in clusters
        ]
        for future in futures:
            future.result()
    log.info("All NFS clusters deletion initiated.")
    # Check if any NFS Ganesha service is still running
    for w in WaitUntil(timeout=timeout, interval=5):
        result = json.loads(
            CephAdm(installer_node).ceph.orch.ls(format="json", service_type="nfs")
        )
        if all(x["status"]["running"] == 0 for x in result) or not result:
            log.info(
                "\n"
                + "=" * 30
                + "\n"
                + "All NFS Ganesha services are down. Time taken : -- %s seconds \n"
                + "=" * 30,
                w._attempt * w.interval,
            )
            return True
        else:
            log.error(
                "\n \n NFS Ganesha services are still running after deletion trying again...... "
                "Time remaining : -- %s seconds \n",
                timeout - (w._attempt * w.interval),
            )
            log.debug("Current status: %s", result)
    if w.expired:
        raise OperationFailedError(
            "NFS Ganesha services are still running after deletion. Timeout expired. -- %s seconds"
            % timeout
        )


@retry(OperationFailedError, tries=4, delay=5, backoff=2)
def mount_retry(
    clients, client_num, mount_name, version, port, nfs_server, export_name, ha=False
):
    if Mount(clients[client_num]).nfs(
        mount=mount_name,
        version=version,
        port=port,
        server=nfs_server,
        export=export_name,
    ):
        raise OperationFailedError("Failed to mount nfs on %s" % export_name.hostname)
    return True


def get_enctag(gklm_client, gkml_client_name, gklm_cert_alias, gklm_user, cert):
    try:
        log.info(
            f"Creating GKLM client '{gkml_client_name}' and assigning certificate '{gklm_cert_alias}'"
        )
        client_data = gklm_client.create_client(gkml_client_name)
        assaign_cert_data = gklm_client.assign_client_certificate(
            client_name=gkml_client_name, cert_pem=cert, alias=gklm_cert_alias
        )
        log.info(
            f"Created GKLM client {client_data} and assigned certificate {assaign_cert_data}"
        )
        symmetric_data = gklm_client.create_symmetric_key_object(
            number_of_objects=1,
            client_name=gkml_client_name,
            alias_prefix_name="AUT",
            cryptoUsageMask="Encrypt,Decrypt",
        )
        enctag = symmetric_data["id"]
        gklm_client.assign_users_to_generic_kmip_client(
            client_name=gkml_client_name, users=[gklm_user]
        )
        log.info(f"Created symmetric key object with UUID {enctag} in GKLM")
    except Exception as e:
        log.error(
            f"Failed to create GKLM client {gkml_client_name} and assign certificate {gklm_cert_alias}: {e}"
        )
        raise
    return enctag


def create_nfs_instance_for_byok(
    installer, nfs_node, nfs_name, kmip_host_list, rsa_key, cert, ca_cert
):
    try:
        log.info(f"Creating NFS Ganesha instance '{nfs_name}' with BYOK configuration")
        nfs_cluster_dict_for_orch = {
            "service_type": "nfs",
            "service_id": nfs_name,
            "placement": {"host_pattern": nfs_node.hostname},
            "spec": {
                "kmip_cert": "|\n" + cert.rstrip("\\n"),
                "kmip_key": "|\n" + rsa_key.rstrip("\\n"),
                "kmip_ca_cert": "|\n" + ca_cert.rstrip("\\n"),
                "kmip_host_list": [kmip_host_list],
            },
        }

        create_nfs_via_file_and_verify(
            installer_node=installer,
            nfs_objects=[nfs_cluster_dict_for_orch],
            timeout=300,
        )

        log.info("NFS Ganesha instance created successfully with BYOK configuration")
    except Exception as e:
        log.error(f"Failed to create NFS Ganesha instance with BYOK configuration: {e}")
        raise


def pre_requisite_for_gklm_get_ca(
    nfs_nodes, gklm_ip, gklm_password, gklm_hostname, gklm_rest_client
):
    """Set up prerequisites for GKLM interaction: copy SSH keys, ensure nodes can resolve each other,
    check for/re-export the CA certificate, and return the certificate contents.

    Args:
        nfs_nodes: List of node objects representing the NFS cluster.
        gklm_ip: IP address of the GKLM server.
        gklm_password: Root password for the GKLM server.
        gklm_hostname: Hostname of the GKLM server.
        gklm_rest_client: GKLM REST client object for certificate operations.

    Returns:
        String contents of the exported CA certificate.

    Raises:
        Exception: Propagates any failure during setup, with detailed log.
    """
    exe_node = nfs_nodes[0]
    log.info("Installing sshpass on execution node for non-interactive SSH usage")
    Package(exe_node).install("sshpass")

    try:
        # Ensure passwordless SSH from execution node to GKLM server
        log.info(f"Copying SSH key from execution node to GKLM server {gklm_ip}")
        cmd = f"sshpass -p {gklm_password} ssh-copy-id root@{gklm_ip}"
        Ceph(exe_node).execute(cmd)

        # For each NFS node, update its /etc/hosts with GKLM server's IP and hostname
        for node in nfs_nodes:
            log.info(
                f"Updating /etc/hosts on NFS node {node.hostname} with GKLM server info"
            )
            cmd = (
                f"gklm_ip={gklm_ip}; gklm_hostname={gklm_hostname}; "
                rf'sudo sed -i -e "/$gklm_hostname\>/d" -e "/^$gklm_ip\>/d" /etc/hosts && '
                f'echo "$gklm_ip $gklm_hostname" | sudo tee -a /etc/hosts'
            )
            out = Ceph(node).execute(sudo=True, cmd=cmd)
            log.info(
                f"Updated /etc/hosts in {node.hostname} with GKLM IP and hostname. Output: {out}"
            )

            # Update GKLM server's /etc/hosts with this NFS node's IP and hostname
            log.info(
                f"Updating /etc/hosts on GKLM server with NFS node {node.hostname} info"
            )
            cmd = (
                f"sshpass -p {gklm_password} ssh -o StrictHostKeyChecking=no root@{gklm_ip} "
                rf'"gklm_ip={node.ip_address}; gklm_hostname={node.hostname}; '
                rf'sudo sed -i -e "/$gklm_hostname\>/d" -e "/^$gklm_ip\>/d" /etc/hosts && '
                rf'echo "$gklm_ip $gklm_hostname" | sudo tee -a /etc/hosts"'
            )
            out = Ceph(exe_node).execute(sudo=True, cmd=cmd)
            log.info(
                f"Updated /etc/hosts in GKLM server with NFS node {node.hostname} IP and hostname. Output: {out}"
            )

        # Locate the UUID of the target SSL server certificate in GKLM
        log.info(
            "Locating target SSL server certificate (alias: self-signed-cert1) in GKLM"
        )
        certificate_uuid_to_export = [
            x["uuid"]
            for x in gklm_rest_client.list_certificates()
            if x.get("usage") == "SSLSERVER" and x.get("alias") == "self-signed-cert1"
        ][0]

        # Check if the exported certificate file exists on the GKLM server using SSH
        log.info(
            "Checking if exported certificate file exists on GKLM server at "
            "/opt/IBM/WebSphere/Liberty/products/sklm/data/export1/exportedCert"
        )
        file_check_cmd = (
            f"sshpass -p {gklm_password} ssh -o StrictHostKeyChecking=no root@{gklm_ip} "
            "'[ -f /opt/IBM/WebSphere/Liberty/products/sklm/data/export1/exportedCert ] && '\
                         'echo \"File exists\" || echo \"File does not exist\"'"
        )
        log.debug(f"Executing remote file check: {file_check_cmd}")
        is_cert_exists = Ceph(exe_node).execute(cmd=file_check_cmd)
        if isinstance(is_cert_exists, (list, tuple)) and len(is_cert_exists) >= 1:
            log.info(f"Remote file check result: {is_cert_exists[0]}")
        else:
            log.info(f"Remote file check returned unexpected output: {is_cert_exists}")

        # If the file does not exist, export the certificate from GKLM via REST API
        if is_cert_exists[0] == "File does not exist\n":
            log.info(
                "CA certificate not found on GKLM server; initiating export via REST API"
            )
            Ceph(exe_node).execute(
                cmd=f'sshpass -p {gklm_password} ssh -o StrictHostKeyChecking=no root@{gklm_ip} \
                                    "mkdir -p /opt/IBM/WebSphere/Liberty/products/sklm/data/export1"'
            )
            Ceph(exe_node).execute(
                sudo=True,
                cmd=f'sshpass -p {gklm_password} ssh -o StrictHostKeyChecking=no root@{gklm_ip} \
                                    "chmod 777 /opt/IBM/WebSphere/Liberty/products/sklm/data/export1"',
            )
            gklm_rest_client.export_certificate(
                uuid=certificate_uuid_to_export, file_name="export1/exportedCert"
            )
            log.info(
                f"Exported certificate with UUID {certificate_uuid_to_export} from GKLM server"
            )
        else:
            log.info(
                "CA certificate already available at the configured path; skipping export"
            )

        # Fetch the contents of the certificate from the GKLM server to return to caller
        log.info(
            "Attempting to read exported certificate file from GKLM server at "
            "/opt/IBM/WebSphere/Liberty/products/sklm/data/export1/exportedCert"
        )
        cert_fetch_cmd = (
            f"sshpass -p {gklm_password} ssh -o StrictHostKeyChecking=no root@{gklm_ip} "
            "'cat /opt/IBM/WebSphere/Liberty/products/sklm/data/export1/exportedCert'"
        )
        ca_cert = Ceph(exe_node).execute(cmd=cert_fetch_cmd)[0]
        log.info(f"CA certificate successfully fetched from GKLM server \n {ca_cert}")
        pass
        return ca_cert

    except Exception as e:
        log.error(f"Failed to setup prerequisites for GKLM: {e}")
        raise
