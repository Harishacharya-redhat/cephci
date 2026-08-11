import concurrent.futures
import os
import re
import signal
import time
import traceback

from tests.cephfs.cephfs_mirroring.cephfs_mirroring_utils import CephfsMirroringUtils
from tests.cephfs.lib.cephfs_common_lib import CephFSCommonUtils
from utility.log import Log


class TestTimeout(Exception):
    pass


def _timeout_handler(signum, frame):
    raise TestTimeout("Test exceeded maximum allowed runtime of 3 hours")


log = Log(__name__)

log_base_dir = os.path.dirname(log.logger.handlers[0].baseFilename)
log_dir = f"{log_base_dir}/SnapDiff_Results/"


def run(ceph_cluster, **kw):
    """
    CEPH-83595260 - Performance evaluation of snapdiff feature using CephFS Mirroring after upgrading to RHCS 8.0
    """
    os.makedirs(log_dir, exist_ok=True)
    log.info(f"Log Dir : {log_dir}")

    max_runtime = 3 * 3600
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(max_runtime)
    # Initialize cleanup-scoped vars so finally never hits UnboundLocalError
    # when setup/mount fails early.
    config = kw.get("config", {})
    source_clients = None
    target_clients = None
    fs_util_ceph1 = None
    fs_mirroring_utils = None
    source_fs = None
    target_fs = None
    nfs_server = None
    nfs_name = None
    nfs_server_node = None
    target_user = None
    peer_uuid = None
    subvol_group_name = "subvolgroup_1"
    subvolume_names = ["subvol_1", "subvol_2", "subvol_3"]
    subvol_paths_without_uuid = {}
    mount_paths = {}
    export_binding = None
    io_dir_paths = {}
    try:
        ceph_cluster_dict = kw.get("ceph_cluster_dict")
        test_data = kw.get("test_data")
        fs_mirroring_utils = CephfsMirroringUtils(
            ceph_cluster_dict.get("ceph1"), ceph_cluster_dict.get("ceph2")
        )
        nfs_servers = ceph_cluster_dict.get("ceph1").get_ceph_objects("nfs")
        env = fs_mirroring_utils.prepare_env_snapdiff(
            config, ceph_cluster_dict, test_data
        )
        if not env:
            log.error("Failed to prepare environment for snapdiff testing.")
            return 1

        source_clients = env["source_clients"]
        target_clients = env["target_clients"]
        fs_util_ceph1 = env["fs_util_ceph1"]
        source_fs = env["source_fs"]
        target_fs = env["target_fs"]
        fs_mirroring_utils = env["fs_mirroring_utils"]
        cephfs_mirror_node = env["cephfs_mirror_node"]
        nfs_server = env["nfs_server"]
        nfs_name = env["nfs_name"]
        nfs_server_node = None
        for nfs_server_iter in nfs_servers:
            if nfs_server in nfs_server_iter.node.hostname:
                nfs_server_node = nfs_server_iter
        target_user = "mirror_remote_user_snap_diff_1"
        target_site = "remote_site_snap_diff_1"

        fs_mirroring_utils.deploy_cephfs_mirroring(
            source_fs,
            source_clients[0],
            target_fs,
            target_clients[0],
            target_user,
            target_site,
        )

        result_file = config.get("result_file")
        csv_file = f"{log_dir}/{result_file}"

        ceph_version_cmd = source_clients[0].exec_command(sudo=True, cmd="ceph version")
        log.info(f"Version : {ceph_version_cmd}")
        ceph_version_out = ceph_version_cmd[0].strip()
        log.info(f"Ceph Version: {ceph_version_out}")

        fs_mirroring_utils.initialize_csv_file_snapdiff(csv_file, ceph_version_out)

        log.info("Create Subvolumes for adding Data")

        fs_util_ceph1.create_subvolumegroup(
            source_clients[0], vol_name=source_fs, group_name=subvol_group_name
        )

        for subvol in subvolume_names:
            fs_util_ceph1.create_subvolume(
                source_clients[0],
                vol_name=source_fs,
                subvol_name=subvol,
                group_name=subvol_group_name,
            )

        def get_ganesha_pid(nfs_server_node):
            try:
                netstat_out, _ = nfs_server_node.exec_command(
                    sudo=True, cmd="netstat -anp | grep ganesha"
                )
                log.info(f"NFS netstat: {netstat_out}")
                pid_out, _ = nfs_server_node.exec_command(
                    sudo=True, cmd="pgrep ganesha"
                )
                log.info(f"NFS pgrep: {pid_out}")
                pid = pid_out.strip()
                # if pid is null, return None
                if not pid:
                    return None
                log.info(
                    f"ganesha process PID for {nfs_server_node.node.hostname}: {pid}"
                )
                return pid
            except Exception as e:
                log.error(f"Error getting ganesha process PID: {e}")
                return None

        if not fs_util_ceph1.wait_for_nfs_process(
            source_clients[0],
            nfs_name,
            timeout=60,
            desired_state="running",
        ):
            log.error(
                "NFS daemon %s is not running before mount; collecting debug logs",
                nfs_name,
            )
            nfs_nodes = ceph_cluster_dict.get("ceph1").get_ceph_objects("nfs")
            cephfs_common_utils = CephFSCommonUtils(ceph_cluster)
            try:
                cephfs_common_utils.nfs_debug_logs(
                    source_clients[0],
                    nfs_name,
                    log_dir,
                    nfs_nodes,
                    dump_output=True,
                )
            except Exception as log_ex:
                log.error("Failed to collect NFS container debug logs: %s", log_ex)
            raise Exception(
                f"NFS daemon {nfs_name} not running on {nfs_server}; refusing to mount"
            )

        ganesha_pid = get_ganesha_pid(nfs_server_node)
        if not ganesha_pid:
            log.error("Failed to get ganesha process PID")
        try:
            mount_paths, subvol_paths, export_binding = (
                fs_mirroring_utils.mount_subvolumes_snapdiff(
                    source_client=source_clients[0],
                    fs_util_ceph1=fs_util_ceph1,
                    default_fs=source_fs,
                    subvolume_names=subvolume_names,
                    subvol_group_name=subvol_group_name,
                    nfs_server=nfs_server,
                    nfs_name=nfs_name,
                )
            )
        except Exception as e:
            log.error(f"Error mounting subvolumes: {e}")
            if re.search(r"mount\.nfs:[\s\S]*Connection refused", str(e)):
                nfs_nodes = ceph_cluster_dict.get("ceph1").get_ceph_objects("nfs")
                cephfs_common_utils = CephFSCommonUtils(ceph_cluster)
                try:
                    cephfs_common_utils.nfs_debug_logs(
                        source_clients[0],
                        nfs_name,
                        log_dir,
                        nfs_nodes,
                        dump_output=True,
                    )
                except Exception as log_ex:
                    log.error("Failed to collect NFS container debug logs: %s", log_ex)
                ganesha_pid = get_ganesha_pid(nfs_server_node)
                if not ganesha_pid:
                    log.error("Failed to get ganesha process PID")
            raise Exception("Mount operation failed")
        log.info(f"Mount Paths : {mount_paths}")
        log.info(f"Sub Volume Paths : {subvol_paths}")

        if mount_paths == 1:
            log.error("Mounting of subvolumes failed")
            raise Exception("Mount operation failed")

        io_dir = "snapdiff_io_dir"
        io_dir_paths = {}
        for mount_type in ["kernel", "fuse", "nfs"]:
            mount_path = mount_paths[mount_type]
            subvol_base = subvol_paths[mount_type].split("/")[0] + "/"
            full_path = f"{mount_path}{subvol_base}{io_dir}"
            log.info(f"Creating I/O directory at: {full_path}")
            source_clients[0].exec_command(
                sudo=True,
                cmd=f"mkdir -p {full_path}",
            )
            io_dir_paths[mount_type] = full_path

        for mtype, path in subvol_paths.items():
            subvol_paths_without_uuid[mtype] = path.split("/")[0] + "/"
        log.info(f"Subvolume Paths without UUID: {subvol_paths_without_uuid}")

        log.info("Add paths for mirroring to remote location")
        for mount_type in ["kernel", "fuse", "nfs"]:
            subvol_path_without_uuid = subvol_paths_without_uuid[mount_type]
            fs_mirroring_utils.add_path_for_mirroring(
                source_clients[0],
                source_fs,
                f"/volumes/{subvol_group_name}/{subvol_path_without_uuid}",
            )

        fsid = fs_util_ceph1.get_fsid(source_clients[0])
        daemon_name = fs_mirroring_utils.get_daemon_name(source_clients[0])
        asok_file = fs_mirroring_utils.get_asok_file(
            cephfs_mirror_node, fsid, daemon_name
        )
        filesystem_id = fs_mirroring_utils.get_filesystem_id_by_name(
            source_clients[0], source_fs
        )
        peer_uuid = fs_mirroring_utils.get_peer_uuid_by_name(
            source_clients[0], source_fs
        )

        log.info(f"I/O directory paths for future use: {io_dir_paths}")

        overall_start = time.time()
        log.info("Starting file creation in all I/O directories...")

        cloud_type = config.get("cloud_type")
        num_of_files = config.get("num_of_files")
        file_size = config.get("file_size")

        generate_file_script = "generate_files_for_snapdiff.py"
        src_path = (
            f"tests/cephfs/cephfs_mirroring/snapdiff_scripts/{generate_file_script}"
        )
        dst_path = f"/root/{generate_file_script}"

        source_clients[0].upload_file(
            sudo=True,
            src=src_path,
            dst=dst_path,
        )

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [
                executor.submit(
                    fs_mirroring_utils.create_files_for_snapdiff,
                    source_clients[0],
                    path,
                    num_of_files,
                    file_size,
                    cloud_type,
                )
                for path in io_dir_paths.values()
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        overall_duration = time.time() - overall_start
        log.info(f"All file creation tasks completed in {overall_duration:.2f} seconds")

        log.info("Create Initial Snapshots, Validate the Sync, and Log Info")

        snap_suffix = "initial"
        client_types = {
            "kernel": "Kernel",
            "fuse": "Fuse",
            "nfs": "NFS",
        }

        snapshot_sync_info = {}

        # Create, Validate, and Log for each client type
        for ctype, label in client_types.items():
            snapshot_name = f"snap_{ctype[0]}_{snap_suffix}"

            # Create snapshot
            fs_mirroring_utils.create_snapshot_snapdiff(
                fs_util_ceph1,
                source_clients[0],
                mount_paths[ctype],
                subvol_paths_without_uuid[ctype],
                snapshot_name,
                source_fs,
                subvolume=True,
                subvol_name=subvol_paths_without_uuid[ctype].rstrip("/"),
                subvol_group=subvol_group_name,
            )

            # Validate snapshot sync
            sync_info = fs_mirroring_utils.validate_snapshot_sync(
                fs_mirroring_utils,
                cephfs_mirror_node,
                source_fs,
                snapshot_name,
                fsid,
                asok_file,
                filesystem_id,
                peer_uuid,
            )

            snapshot_sync_info[label] = sync_info

            # Log sync info
            if sync_info:
                log.info(
                    f"{label} Initial Snapshot Info - Name: {sync_info['snapshot_name']}, "
                    f"Duration: {sync_info['sync_duration']}, "
                    f"Time Stamp: {sync_info['sync_time_stamp']}, "
                    f"Snaps Synced: {sync_info['snaps_synced']}"
                )
                fs_mirroring_utils.log_snapshot_info_snapdiff(
                    f"{label} Initial", sync_info, csv_file
                )

        common_args = {
            "io_dir_paths": io_dir_paths,
            "source_clients": source_clients,
            "mount_paths": mount_paths,
            "subvol_paths_without_uuid": subvol_paths_without_uuid,
            "source_fs": source_fs,
            "subvol_group_name": subvol_group_name,
            "fs_mirroring_utils": fs_mirroring_utils,
            "cephfs_mirror_node": cephfs_mirror_node,
            "fsid": fsid,
            "asok_file": asok_file,
            "filesystem_id": filesystem_id,
            "peer_uuid": peer_uuid,
            "csv_file": csv_file,
        }

        modify_script = "modify_file_at_10_random_offsets.py"
        source_clients[0].upload_file(
            sudo=True,
            src=f"tests/cephfs/cephfs_mirroring/snapdiff_scripts/{modify_script}",
            dst="/root/modify_file_at_10_random_offsets.py",
        )

        # Run incremental snapshots
        # Run 4 write-mode snapshots
        fs_mirroring_utils.modify_and_create_snapshot_snapdiff(
            fs_util_ceph1,
            num_files=1,
            snap_suffix="w1",
            label_suffix="write_1",
            mode="write",
            **common_args,
        )
        fs_mirroring_utils.modify_and_create_snapshot_snapdiff(
            fs_util_ceph1,
            num_files=2,
            snap_suffix="w2",
            label_suffix="write_2",
            mode="write",
            **common_args,
        )
        fs_mirroring_utils.modify_and_create_snapshot_snapdiff(
            fs_util_ceph1,
            num_files=5,
            snap_suffix="w3",
            label_suffix="write_3",
            mode="write",
            **common_args,
        )
        fs_mirroring_utils.modify_and_create_snapshot_snapdiff(
            fs_util_ceph1,
            num_files=10,
            snap_suffix="w4",
            label_suffix="write_4",
            mode="write",
            **common_args,
        )

        # Run 4 read-mode snapshots
        fs_mirroring_utils.modify_and_create_snapshot_snapdiff(
            fs_util_ceph1,
            num_files=1,
            snap_suffix="r1",
            label_suffix="read_1",
            mode="read",
            **common_args,
        )
        fs_mirroring_utils.modify_and_create_snapshot_snapdiff(
            fs_util_ceph1,
            num_files=2,
            snap_suffix="r2",
            label_suffix="read_2",
            mode="read",
            **common_args,
        )
        fs_mirroring_utils.modify_and_create_snapshot_snapdiff(
            fs_util_ceph1,
            num_files=5,
            snap_suffix="r3",
            label_suffix="read_3",
            mode="read",
            **common_args,
        )
        fs_mirroring_utils.modify_and_create_snapshot_snapdiff(
            fs_util_ceph1,
            num_files=10,
            snap_suffix="r4",
            label_suffix="read_4",
            mode="read",
            **common_args,
        )

        # Run 4 remove-mode snapshots
        fs_mirroring_utils.modify_and_create_snapshot_snapdiff(
            fs_util_ceph1,
            num_files=1,
            snap_suffix="rm1",
            label_suffix="remove_1",
            mode="remove",
            **common_args,
        )
        fs_mirroring_utils.modify_and_create_snapshot_snapdiff(
            fs_util_ceph1,
            num_files=2,
            snap_suffix="rm2",
            label_suffix="remove_2",
            mode="remove",
            **common_args,
        )
        fs_mirroring_utils.modify_and_create_snapshot_snapdiff(
            fs_util_ceph1,
            num_files=5,
            snap_suffix="rm3",
            label_suffix="remove_3",
            mode="remove",
            **common_args,
        )
        fs_mirroring_utils.modify_and_create_snapshot_snapdiff(
            fs_util_ceph1,
            num_files=10,
            snap_suffix="rm4",
            label_suffix="remove_4",
            mode="remove",
            **common_args,
        )

        return 0
    except TestTimeout as e:
        log.error(f"Test timed out after {max_runtime}s: {e}")
        return 1
    except Exception as e:
        log.error(e)
        log.error(traceback.format_exc())
        return 1
    finally:
        signal.alarm(0)
        if config.get("cleanup", True) and (
            source_clients and fs_util_ceph1 and fs_mirroring_utils and source_fs
        ):
            try:
                if subvol_paths_without_uuid:
                    log.info("Delete the snapshots")
                    snap_suffixes = [
                        "initial",
                        "w1",
                        "w2",
                        "w3",
                        "w4",
                        "r1",
                        "r2",
                        "r3",
                        "r4",
                        "rm1",
                        "rm2",
                        "rm3",
                        "rm4",
                    ]
                    client_types = {
                        "kernel": "Kernel",
                        "fuse": "Fuse",
                        "nfs": "NFS",
                    }
                    for snap_suffix in snap_suffixes:
                        for ctype in client_types:
                            if ctype not in subvol_paths_without_uuid:
                                continue
                            snapshot_name = f"snap_{ctype[0]}_{snap_suffix}"
                            subvol_name = subvol_paths_without_uuid[ctype].rstrip("/")
                            try:
                                fs_util_ceph1.remove_snapshot(
                                    client=source_clients[0],
                                    vol_name=source_fs,
                                    subvol_name=subvol_name,
                                    snap_name=snapshot_name,
                                    validate=True,
                                    group_name=subvol_group_name,
                                    force=True,
                                )
                                log.info(
                                    f"Successfully removed snapshot: {snapshot_name} for {ctype}"
                                )
                            except Exception as cleanup_ex:
                                log.warning(
                                    "Failed to remove snapshot %s for %s: %s",
                                    snapshot_name,
                                    ctype,
                                    cleanup_ex,
                                )

                if mount_paths:
                    log.info("Unmount the paths")
                    for path in [
                        mount_paths.get("kernel"),
                        mount_paths.get("fuse"),
                        mount_paths.get("nfs"),
                    ]:
                        if not path:
                            continue
                        try:
                            source_clients[0].exec_command(
                                sudo=True, cmd=f"umount -l {path}", check_ec=False
                            )
                        except Exception as cleanup_ex:
                            log.warning("Failed to unmount %s: %s", path, cleanup_ex)

                if subvol_paths_without_uuid:
                    log.info("Remove paths used for mirroring")
                    for mount_type in ["kernel", "fuse", "nfs"]:
                        if mount_type not in subvol_paths_without_uuid:
                            continue
                        subvol_path_without_uuid = subvol_paths_without_uuid[mount_type]
                        try:
                            fs_mirroring_utils.remove_path_from_mirroring(
                                source_clients[0],
                                source_fs,
                                f"/volumes/{subvol_group_name}/{subvol_path_without_uuid}",
                            )
                        except Exception as cleanup_ex:
                            log.warning(
                                "Failed to remove mirror path for %s: %s",
                                mount_type,
                                cleanup_ex,
                            )

                if export_binding and nfs_name:
                    try:
                        fs_util_ceph1.remove_nfs_export(
                            source_clients[0], nfs_name, export_binding, validate=True
                        )
                    except Exception as cleanup_ex:
                        log.warning("Failed to remove NFS export: %s", cleanup_ex)

                if nfs_name:
                    try:
                        fs_util_ceph1.remove_nfs_cluster(
                            source_clients[0], nfs_name, validate=True
                        )
                    except Exception as cleanup_ex:
                        log.warning("Failed to remove NFS cluster: %s", cleanup_ex)

                if nfs_server_node:
                    try:
                        fsid = fs_util_ceph1.get_fsid(source_clients[0])
                        out, _ = nfs_server_node.exec_command(
                            sudo=True, cmd=f"ls -l /var/lib/ceph/{fsid}/nfs*/"
                        )
                        log.error(f"nfs files: {out}")
                    except Exception as e:
                        log.info(f"nfs files doesn't exist in nfs node: {e}")

                if target_fs and target_clients and target_user:
                    log.info("Destroy CephFS Mirroring setup.")
                    try:
                        cleanup_peer_uuid = peer_uuid
                        if not cleanup_peer_uuid:
                            cleanup_peer_uuid = (
                                fs_mirroring_utils.get_peer_uuid_by_name(
                                    source_clients[0], source_fs
                                )
                            )
                        if cleanup_peer_uuid:
                            fs_mirroring_utils.destroy_cephfs_mirroring(
                                source_fs,
                                source_clients[0],
                                target_fs,
                                target_clients[0],
                                target_user,
                                cleanup_peer_uuid,
                            )
                        else:
                            log.warning(
                                "Skipping destroy_cephfs_mirroring; peer_uuid unavailable"
                            )
                    except Exception as cleanup_ex:
                        log.warning("Failed to destroy mirroring setup: %s", cleanup_ex)

                log.info("Remove Subvolumes")
                for subvol in subvolume_names:
                    try:
                        fs_util_ceph1.remove_subvolume(
                            source_clients[0],
                            vol_name=source_fs,
                            subvol_name=subvol,
                            group_name=subvol_group_name,
                        )
                    except Exception as cleanup_ex:
                        log.warning(
                            "Failed to remove subvolume %s: %s", subvol, cleanup_ex
                        )

                log.info("Remove Subvolume Group")
                try:
                    fs_util_ceph1.remove_subvolumegroup(
                        source_clients[0],
                        vol_name=source_fs,
                        group_name=subvol_group_name,
                    )
                except Exception as cleanup_ex:
                    log.warning("Failed to remove subvolumegroup: %s", cleanup_ex)

                log.info("Delete the mounted paths")
                for mount_type in ["kernel", "fuse", "nfs"]:
                    path = io_dir_paths.get(mount_type)
                    if path:
                        try:
                            source_clients[0].exec_command(
                                sudo=True, cmd=f"rm -rf {path}", check_ec=False
                            )
                        except Exception as cleanup_ex:
                            log.warning(
                                "Failed to remove path %s: %s", path, cleanup_ex
                            )

                try:
                    fs_util_ceph1.remove_fs(
                        source_clients[0], source_fs, validate=False
                    )
                except Exception as cleanup_ex:
                    log.warning("Failed to remove source fs: %s", cleanup_ex)
                if target_clients and target_fs:
                    try:
                        fs_util_ceph1.remove_fs(
                            target_clients[0], target_fs, validate=False
                        )
                    except Exception as cleanup_ex:
                        log.warning("Failed to remove target fs: %s", cleanup_ex)
            except Exception as cleanup_ex:
                log.error("Snapdiff cleanup encountered an error: %s", cleanup_ex)
                log.error(traceback.format_exc())
        elif config.get("cleanup", True):
            log.warning("Skipping cleanup; snapdiff environment was not fully prepared")
