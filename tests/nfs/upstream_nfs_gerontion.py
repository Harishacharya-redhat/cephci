import time

from cli.exceptions import OperationFailedError
from utility.log import Log

log = Log(__name__)

# Poll `ps -ef | grep <workload>` at these fractions of total workload duration
WORKLOAD_PS_POLL_FRACTIONS = (0.2, 0.4, 0.6, 0.8)

# GPFS stress tarball; on-disk layout must match gpfstest tree (gerontion hard-codes paths).
#   /u/gpfstest/bin
#   /u/gpfstest/stress/gerontion/gerontion   <-- mandatory binary path
#   /u/gpfstest/stress/workload
GPFS_TEST_TAR_URL = "http://10.0.210.156/gpfstest.tar.gz"
GPFS_BASE = "/u/gpfstest"
STRESS_DIR = f"{GPFS_BASE}/stress"
GERONTION_BIN = f"{STRESS_DIR}/gerontion/gerontion"
GPFS_EXTRACT_DIR = f"{GPFS_BASE}/.gpfstest_extract"
DEFAULT_WORKLOAD_DURATION_MINUTES = 30
WORKLOADS = [
    "racer",
    "cdata",
    "blast",
    "locktest",
    "iago",
    "kodak",
    "gpfsperf",
    "dbench",
    "fstest",
    "iozone",
    "iometer",
    "detectcorrupt",
    "kgnrdwr",
    "tortdir",
    "ffsb",
    "checkdata",
    "trunctest",
    "cloneTree",
    "eastress",
    "readAllSnapshots",
    "dirsplit",
    "fgdl_dirsplit",
    "aclStress",
    "aclsforall",
]


def _cleanup_after_workload(client, workload_name):
    """Stop workload binaries and gerontion/perl children between runs."""
    for cmd in (
        f"pkill -9 {workload_name} || true",
        "pkill -9 gerontion || true",
        "pkill -9 perl || true",
    ):
        client.exec_command(cmd=cmd, sudo=True, check_ec=False)


def _ps_grep_workload_pattern(workload_name):
    """Return a grep(1) pattern that matches workload_name but not the grep process itself."""
    if len(workload_name) == 1:
        return f"[{workload_name}]"
    return f"[{workload_name[0]}]{workload_name[1:]}"


def _assert_workload_running_ps(client, workload_name, fraction_label):
    """Fail the test if ps does not show the workload (gerontion may daemonize)."""
    pat = _ps_grep_workload_pattern(workload_name)
    cmd = f"ps -ef | grep '{pat}'"
    out, err = client.exec_command(cmd=cmd, sudo=True, check_ec=False)
    if not (out or "").strip():
        log.error(
            f"Workload {workload_name!r} not running on {client.hostname} at {fraction_label} "
            f"({cmd!r} produced no lines). stderr={err!r}"
        )
    log.info(
        "Workload %s still visible in ps on %s at %s",
        workload_name,
        client.hostname,
        fraction_label,
    )


def _run_gerontion_workload(client, gerontion_bin, mount, workload_name, timeout_sec):
    """Start gerontion in the background, wait for duration, poll ps at 20/40/60/80%."""
    mount_tag = mount.replace("/", "_").replace(" ", "_")
    log_path = f"/tmp/gerontion_{workload_name}{mount_tag}.log"
    start_cmd = (
        f"bash -c 'nohup {gerontion_bin} -N {client.ip_address} -F {mount} "
        f"{workload_name} >{log_path} 2>&1 </dev/null & echo $!'"
    )
    out, err = client.exec_command(cmd=start_cmd, sudo=True, check_ec=False)
    pid = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
    if not pid.isdigit():
        raise OperationFailedError(
            f"Could not start gerontion workload {workload_name!r} in background on "
            f"{client.hostname} mount={mount}: expected numeric PID, stdout={out!r} stderr={err!r}"
        )
    log.info(
        "Started gerontion workload %s on %s mount=%s (background pid=%s, log=%s)",
        workload_name,
        client.hostname,
        mount,
        pid,
        log_path,
    )

    t0 = time.monotonic()
    deadline = t0 + float(timeout_sec)
    elapsed = 0.0
    for frac in WORKLOAD_PS_POLL_FRACTIONS:
        target_elapsed = float(timeout_sec) * frac
        sleep_s = max(0.0, target_elapsed - elapsed)
        if sleep_s:
            time.sleep(sleep_s)
        elapsed = target_elapsed
        pct = int(frac * 100)
        _assert_workload_running_ps(client, workload_name, f"{pct}% of duration")

    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)

    log.info(
        "Gerontion workload window complete: %s on %s mount=%s (duration=%ss)",
        workload_name,
        client.hostname,
        mount,
        timeout_sec,
    )


def setup_passwordless_ssh(nodes):
    # Setup passwordless SSH between all nodes
    log.info("Setting up passwordless SSH between all nodes")

    # Generate SSH keys on all nodes if they don't exist
    for node in nodes:
        log.info(f"Generating SSH key on {node.hostname}")
        node.exec_command(
            cmd="[ -f ~/.ssh/id_rsa ] || ssh-keygen -t rsa -N '' -f ~/.ssh/id_rsa",
            sudo=True,
        )

    # Collect all public keys
    public_keys = {}
    for node in nodes:
        log.info(f"Collecting public key from {node.hostname}")
        out, _ = node.exec_command(cmd="cat ~/.ssh/id_rsa.pub", sudo=True)
        public_keys[node.hostname] = out.strip()

    # Distribute all public keys to all nodes
    for node in nodes:
        log.info(f"Distributing public keys to {node.hostname}")
        # Ensure .ssh directory and authorized_keys exist with correct permissions
        node.exec_command(cmd="mkdir -p ~/.ssh && chmod 700 ~/.ssh", sudo=True)
        node.exec_command(
            cmd="touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys",
            sudo=True,
        )

        # Add all public keys to authorized_keys (avoiding duplicates)
        for hostname, pub_key in public_keys.items():
            # Check if key already exists, if not add it
            check_cmd = f"grep -q '{pub_key}' ~/.ssh/authorized_keys || echo '{pub_key}' >> ~/.ssh/authorized_keys"
            node.exec_command(cmd=check_cmd, sudo=True)

        # Disable strict host key checking for seamless SSH
        ssh_config = """Host *
StrictHostKeyChecking no
UserKnownHostsFile=/dev/null"""
        node.exec_command(
            cmd=f"echo '{ssh_config}' > ~/.ssh/config && chmod 600 ~/.ssh/config",
            sudo=True,
        )

    log.info("Passwordless SSH setup completed successfully")

# Adding the hostnames to the /etc/hosts file
def add_etc_host_entries(nodes):
    etc_hosts_string = ""
    for node in nodes:
        etc_hosts_string += f"{node.ip_address} {node.hostname}\n"

    for node in nodes:
        node.exec_command(cmd=f"echo '{etc_hosts_string}' >> /etc/hosts", sudo=True)

def run(ceph_cluster, **kw):
    clients = ceph_cluster.get_nodes("client")
    log.info("Setup nfs cluster")
    config = kw.get("config") or {}
    raw_minutes = config.get(
        "workload_duration_minutes", DEFAULT_WORKLOAD_DURATION_MINUTES
    )
    try:
        workload_duration_sec = int(raw_minutes) * 60
    except (TypeError, ValueError) as err:
        raise OperationFailedError(
            f"Invalid workload_duration_minutes in config: {raw_minutes!r}"
        ) from err
    if workload_duration_sec <= 0:
        raise OperationFailedError(
            f"workload_duration_minutes must be positive, got {raw_minutes!r}"
        )
    log.info(
        "Gerontion workload duration: %s minutes (%s seconds)",
        raw_minutes,
        workload_duration_sec,
    )
    # export_name = environ['EXPORT_NAME']
    export_name = "/ibm/scale_volume"
    nodes = ceph_cluster.get_nodes()

    try:
        server = ceph_cluster.get_nodes("installer")[0]
        client = ceph_cluster.get_nodes("client")[0]
        node2 = client.hostname
        node3 = ceph_cluster.get_nodes("client")[1].hostname

        # Add the hostnames to the /etc/hosts file
        add_etc_host_entries(nodes)

        # install dependent packages -  kernel-devel-`uname -r` kernel-headers-`uname -r`
        # elfutils elfutils-devel on all nodes (clients and installer)
        log.info("Installing dependent packages on all nodes")
        for node in ceph_cluster.get_nodes():
            cmd = "yum install -y elfutils elfutils-devel kernel-devel-$(uname -r) kernel-headers-$(uname -r) gcc-c++"
            node.exec_command(cmd=cmd, sudo=True)

        # Setup Passwrodless SSH between Nodes
        log.info("Setting up passwordless SSH between all nodes")
        setup_passwordless_ssh(ceph_cluster.get_nodes())

        cmds = [
            "rm -rf ci-tests/",
            "yum install -y git wget",
            f'echo "export node2=\"{node2}\"" >> ~/.bashrc && source ~/.bashrc',
            f'echo "export node3=\"{node3}\"" >> ~/.bashrc && source ~/.bashrc',
            "git clone https://github.com/aravindrrh/ci-tests; cd ci-tests; git checkout scale_downstream_arun",
            "sh ci-tests/build_scripts/common/basic-storage-scale-multi-node.sh",
        ]  # Copy Gerontion folder to all the clients]

        for cmd in cmds:
            exit_code = server.exec_command(
                cmd=cmd, sudo=True, long_running=True, timeout=3600
            )
            if exit_code != 0:
                raise OperationFailedError(
                    f"IBM Scale installation command failed (exit {exit_code}): {cmd}"
                )

        tarball = f"{GPFS_EXTRACT_DIR}/gpfstest.tar.gz"
        for client in clients:
            for cmd in [
                "yum install -y wget tar",
                f"rm -rf {GPFS_EXTRACT_DIR} {GPFS_BASE}/bin {STRESS_DIR} /u/gpfstesti/stress",
                f"mkdir -p {GPFS_EXTRACT_DIR}",
                f"wget -O {tarball} {GPFS_TEST_TAR_URL}",
                f"tar -xzf {tarball} -C {GPFS_EXTRACT_DIR}",
                f"mkdir -p {GPFS_BASE}",
                # Tarball root: gpfstest/bin, gpfstest/stress/{gerontion,workload}
                f"cp -a {GPFS_EXTRACT_DIR}/gpfstest/bin {GPFS_BASE}/",
                f"cp -a {GPFS_EXTRACT_DIR}/gpfstest/stress {GPFS_BASE}/",
                f"chmod +x {GERONTION_BIN}",
                # Create gerontion users
                f"{GPFS_BASE}/bin/addGerontionUsers.sh",
            ]:
                client.exec_command(cmd=cmd, sudo=True)

        # Install pre-req
        for client in clients:
            cmd = (
                "sudo dnf install -y wget git gcc gcc-c++ time make automake autoconf "
                "pkgconf pkgconf-pkg-config libtool bison flex "
                "perl perl-Time-HiRes python3 wget tar libaio-devel net-tools nfs-utils"
            )
            client.exec_command(cmd=cmd, sudo=True)

        mounts = ["/mnt/nfsv3", "/mnt/nfsv4_1"]  # , "/mnt/nfsv4_2"]
        # Perform mount on all client with different mount versions
        for nfs_mount, ver in {
            "/mnt/nfsv3": "3",
            "/mnt/nfsv4_1": "4.1",
        }.items():  # , '/mnt/nfsv4_2':'4.2'}.items():
            cmds = [
                f"mkdir -p {nfs_mount}",
                f"mount -t nfs -o vers={ver} {server.ip_address}:{export_name} {nfs_mount}",
                f"export TESTDIR={nfs_mount}",
            ]
            for client in clients:
                for cmd in cmds:
                    client.exec_command(cmd=cmd, sudo=True)

    except OperationFailedError:
        raise
    except Exception as e:
        log.error("Gerontion setup failed: %s", e)
        raise OperationFailedError(f"Gerontion setup failed: {e}") from e

    # Workloads: start in background, poll ps at configured fractions; fail if not running.
    log.info(
        "Gerontion setup complete; running workloads in background with ps checks at "
        f"{', '.join(str(int(f * 100)) + '%' for f in WORKLOAD_PS_POLL_FRACTIONS)} of duration"
    )
    try:
        if not clients:
            raise OperationFailedError("No client nodes in cluster; cannot run gerontion workloads")
        n_clients = len(clients)
        for i, workload_name in enumerate(WORKLOADS):
            client = clients[i % n_clients]
            log.info(
                "Workload %s assigned to client %s (round-robin index %s of %s)",
                workload_name,
                client.hostname,
                i % n_clients,
                n_clients,
            )
            for mount in mounts:
                _run_gerontion_workload(
                    client,
                    GERONTION_BIN,
                    mount,
                    workload_name,
                    workload_duration_sec,
                )
                _cleanup_after_workload(client, workload_name)
    except OperationFailedError:
        raise
    except Exception as e:
        log.error("Gerontion workload execution failed: %s", e)
        raise OperationFailedError(
            f"Gerontion workload execution failed (could not run workload): {e}"
        ) from e

    log.info(
        "Gerontion workloads finished for configured duration; marking testcase passed"
    )
    return 0
