from cli.exceptions import OperationFailedError
from utility.log import Log

log = Log(__name__)


def run (ceph_cluster, **kw):
    config = kw.get("config")
    nfs_mount = config.get("mount_point", "/mnt/nfs")
    clients = ceph_cluster.get_nodes("client")
    port = config.get("port", "2049")
    version = config.get("nfs_version", "4.0")
    nfs_nodes = ceph_cluster.get_nodes("installer")
    nfs_node = nfs_nodes[0]
    fs_name = "cephfs"
    nfs_name = "cephfs-nfs"
    nfs_export = "/export"
    fs = "cephfs"
    nfs_server_name = nfs_node.hostname

    log.info("Setup nfs cluster")
    try:
        config = kw.get("config")
        server = ceph_cluster.get_nodes("installer")[0]

        cmds = ["rm -rf ci-tests/",
                "dnf -y install git wget gcc nfs-utils time make",
                "git clone https://github.com/aravindrrh/ci-tests; cd ci-tests; git checkout scale_downstream",
                "sh ci-tests/build_scripts/common/basic-storage-scale.sh"]

        for cmd in cmds:
            exit_code = server.exec_command(
                cmd=cmd, sudo=True, long_running=True, timeout="notimeout"
            )
            if exit_code != 0:
                raise OperationFailedError(
                    f"FIO server command failed (exit {exit_code}): {cmd}"
                )

        # Perform mount on client
        cmds = ["mkdir -p /mnt/nfsv3",
                f"mount -t nfs -o vers=3 {server.ip_address}:/ibm/scale_volume /mnt/nfsv3",
                "mkdir -p /mnt/nfsv4",
                f"mount -t nfs -o vers=4 {server.ip_address}:/ibm/scale_volume /mnt/nfsv4"
                ]

        for cmd in cmds:
            clients[0].exec_command(cmd=cmd, sudo=True)
            clients[1].exec_command(cmd=cmd, sudo=True)

        cmd = "yum install -y fio"
        clients[0].exec_command(cmd=cmd, sudo=True)

        fio_job_content = """[global]
ioengine=libaio
direct=1
runtime=60
time_based
size=100M
directory=/mnt/nfsv3
group_reporting
numjobs=10

[seqwrite]
rw=write
bs=1M

[seqread]
rw=read
bs=1M

[randrw]
rw=randrw
rwmixread=70
bs=4k
numjobs=4"""
        filepath = "/mnt/nfsv3/ganesha_test.fio"
        cmd = f"cat <<'EOF' > {filepath}\n{fio_job_content}\nEOF"
        clients[0].exec_command(cmd=cmd, sudo=True)

        # Run on V3
        log.info("Running FIO on V3")
        output = "/mnt/nfsv3/fio_results.json"
        cmd = f"fio {filepath} --output={output} --output-format=json"
        out, _ = clients[0].exec_command(cmd=cmd, sudo=True, timeout=10400)
        log.info("Execution complted on v3 mount")
        cmd = f"cat {output}"
        out, _ = clients[0].exec_command(cmd=cmd, sudo=True)

        fio_job_content = """[global]
ioengine=libaio
direct=1
runtime=60
time_based
size=100M 
directory=/mnt/nfsv4
group_reporting
numjobs=10

[seqwrite]
rw=write
bs=1M

[seqread]
rw=read
bs=1M

[randrw]
rw=randrw
rwmixread=70
bs=4k
numjobs=10"""
        filepath = "/mnt/nfsv4/ganesha_test.fio"
        cmd = f"cat <<'EOF' > {filepath}\n{fio_job_content}\nEOF"
        clients[0].exec_command(cmd=cmd, sudo=True)

        # Run on V4
        log.info("Running FIO on V4")
        output = "/mnt/nfsv4/fio_results.json"
        cmd = f"fio {filepath} --output={output} --output-format=json"
        out, _ = clients[0].exec_command(cmd=cmd, sudo=True, timeout=10400)
        log.info("Execution complted on v4 mount")
        cmd = f"cat {output}"
        out, _ = clients[0].exec_command(cmd=cmd, sudo=True)

    except OperationFailedError:
        raise
    except Exception as e:
        log.error("FIO run failed: %s", e)
        raise OperationFailedError(f"FIO run failed: {e}") from e
    finally:
        pass
    return 0
