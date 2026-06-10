from os import environ

from cli.exceptions import OperationFailedError
from utility.log import Log

log = Log(__name__)


def run (ceph_cluster, **kw):
    clients = ceph_cluster.get_nodes("client")
    log.info("Setup nfs cluster")
    skip_deployment = environ['SKIP_DEPLOYMENT']
    export_name = environ['EXPORT_NAME']

    try:
        server = ceph_cluster.get_nodes("installer")[0]

        if skip_deployment == "true":
            log.info("Skipping installation and deployment")
        else:
            cmds = ["rm -rf ci-tests/",
                    "yum install -y git wget",
                    "git clone https://github.com/aravindrrh/ci-tests; cd ci-tests; git checkout scale_downstream",
                    "sh ci-tests/build_scripts/common/basic-storage-scale.sh"]
            for cmd in cmds:
                exit_code = server.exec_command(
                    cmd=cmd, sudo=True, long_running=True,
                )
                if exit_code != 0:
                    raise OperationFailedError(
                        f"Iozone server command failed (exit {exit_code}): {cmd}"
                    )

        # Install pre-req
        cmd = "sudo dnf install -y wget git gcc gcc-c++ time make automake autoconf " \
              "pkgconf pkgconf-pkg-config libtool bison flex " \
              "perl perl-Time-HiRes python3 wget tar libaio-devel net-tools nfs-utils"
        clients[0].exec_command(cmd=cmd, sudo=True)

        cmd = "wget http://www.iozone.org/src/current/iozone3_506.tar;" \
              "tar xvf iozone3_506.tar;cd iozone3_506/src/current/;make;make linux"
        clients[0].exec_command(cmd=cmd, sudo=True)

        # Perform mount on client
        for nfs_mount, ver in {'/mnt/nfsv3':'3', '/mnt/nfsv4':'4'}.items():
            cmds = [f"mkdir -p {nfs_mount}",
                    f"mount -t nfs -o vers={ver} {server.ip_address}:{export_name} {nfs_mount}"
                    ]
            for cmd in cmds:
                clients[0].exec_command(cmd=cmd, sudo=True)
            # Perform mount on client
            io_params = {
                "file_name": None,  # default None, else list of filenames required to test ['file1','file2']
                "io_type": ["all"],
                "file_size": "auto",  # mention alternate size in as 64k/64m/64g for KB/MB/GB
                "max_file_size": "5g",
                "reclen": "auto",  # mention alternate size as 64k/64m/64g for KB/MB/GB
                "cpu_use_report": True,  # to report cpu use by each test
                "spreadsheet": True,  # to copy stats to spreadsheet
                "throughput_test": False,
                "threads": 2,  # to be used when throughput_test is True
                "ext_opts": None,  # other options as "-C -e -K",default is 'None'
            }
            iozone_path = "/root/iozone3_506/src/current/iozone"
            iozone_cmd = f"{iozone_path} -a"
            if io_params["throughput_test"]:
                iozone_cmd = f"{iozone_path} -t {io_params['threads']}"
            if io_params["cpu_use_report"]:
                iozone_cmd += " -+u"
            if io_params["spreadsheet"]:
                iozone_cmd += f" -b {nfs_mount}/iozone_report.xls"
            if "all" not in io_params["io_type"]:
                for io_type in io_params["io_type"]:
                    iozone_cmd += f" -i {io_type}"
            if "auto" not in io_params["file_size"]:
                iozone_cmd += f" -s {io_params['file_size']}"
            elif io_params["file_name"] is None:
                iozone_cmd += f" -g {io_params['max_file_size']}"
            if "auto" not in io_params["reclen"]:
                iozone_cmd += f" -r {io_params['reclen']}"
            if io_params.get("ext_opts"):
                iozone_cmd += f" {io_params['ext_opts']}"
            if io_params.get("file_names"):
                thread_len = len(io_params["file_names"])
                iozone_cmd += f" -t {thread_len} -F "
                for file_name in io_params["file_names"]:
                    io_path = f"{nfs_mount}/{file_name}"
                    iozone_cmd += f" {io_path}"

            cmd = f"cd {nfs_mount};{iozone_cmd}"
            exit_code = clients[0].exec_command(
                sudo=True,
                cmd=cmd,
                long_running=True,
                timeout=7200
            )
            if exit_code != 0:
                log.error(
                    "Iozone client command failed with exit code %s: %s",
                    exit_code, cmd,
                )
                raise OperationFailedError(
                    f"Iozone client command failed (exit {exit_code}): {cmd}"
                )

    except OperationFailedError:
        raise
    except Exception as e:
        log.error("Iozone setup/run failed: %s", e)
        raise OperationFailedError(f"Iozone setup/run failed: {e}") from e
    finally:
        pass
    return 0
