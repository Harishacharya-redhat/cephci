# NFS Test Coverage Review and Gap Analysis

## 1. Executive Summary
This report provides a comprehensive review of the NFS test coverage in the `cephci` repository, an analysis of the internal NFS workflows in Ceph, and a detailed list of identified gaps. While the current coverage is robust for the CephFS backend and core protocol compliance, significant gaps exist in RGW NFS integration, security (authentication/authorization), and advanced NFS v4.2 features.

## 2. Current Test Coverage Analysis
The current test suites are organized by release: Reef, Squid, and Tentacle.

### 2.1. Feature Coverage
- **Deployment & Orchestration:** Well-tested via `cephadm` and `ceph nfs cluster create`. Supports both HA (with VIP and Keepalived) and non-HA configurations.
- **Protocol Support:** Tests cover NFS v3, v4.0, v4.1, and v4.2. `pynfs` is integrated for deep protocol compliance verification.
- **Backends:**
    - **CephFS:** Extensively covered. Includes subvolume integration, quotas, QoS (Squid), and basic/advanced file operations.
    - **RGW:** Limited coverage. Primarily relies on legacy scripts outside the main `ceph nfs` CLI workflow.
- **High Availability (HA):**
    - VIP failover is tested.
    - Grace period management via gRPC (Tentacle) is covered.
    - Lock recovery after server reboot is tested.
- **Quality of Service (QoS):** Robust coverage in Squid, including PerShare, PerClient, and combined bandwidth/IOPS limits.
- **Security:**
    - Basic squash (root_squash, all_squash) and access types (RO/RW) are covered.
    - BYOK (Bring Your Own Key) using KMIP for encryption is tested (Squid).
    - SELinux context preservation and enforcement are tested.
- **Client Interoperability:** Includes tests for various Linux distributions and Windows clients.

### 2.2. Performance Benchmarking
- **SPECstorage:** Automated via `tests/nfs/nfs_spec_storage.py`. This tool is used to validate NFS performance under various workloads (e.g., SWBUILD) and scale. It ensures that the Ganesha gateway does not become a bottleneck for the Ceph backend.

## 3. NFS Internal Workflows in Ceph
Understanding these workflows is crucial for identifying deep-seated gaps.

### 3.1. Control Plane (Management)
1. **CLI/MGR:** Users interact with the `ceph nfs` CLI. The `mgr/nfs` module handles these requests.
2. **Configuration Storage:** Export configurations and cluster specs are stored as objects in a RADOS pool.
3. **Orchestration:** `cephadm` (or other orchestrators) deploys `nfs-ganesha` containers.
4. **Dynamic Update:** Ganesha instances use the `FSAL_CEPH` or `FSAL_RGW` to talk to Ceph and watch for configuration changes in RADOS, allowing for "live" export updates without daemon restarts.

### 3.2. Data Plane (I/O)
1. **Client Request:** NFS client sends an RPC request to the Ganesha daemon.
2. **FSAL Layer:** Ganesha's File System Abstraction Layer (FSAL) translates NFS requests into `libcephfs` or `librgw` calls.
3. **Ceph Interaction:** `libcephfs`/`librgw` communicates with MDS/OSD/RGW to fulfill the request.

### 3.3. Provisioning Workflow
1. **Export Creation:** When a user calls `ceph nfs export create cephfs`, the MGR module:
    - Validates the existence of the CephFS volume and path.
    - (Optionally) Creates a CephFS subvolume if the framework utility is used.
    - Generates a Ganesha EXPORT block and stores it in the RADOS configuration pool.
2. **Notification:** The Ganesha daemons, which are watching the RADOS objects, receive a notification of the new/updated object.
3. **Dynamic Loading:** Ganesha's `dbus` interface or its internal RADOS watcher triggers a config reload, making the new export immediately available to clients without service disruption.

### 4.1. RGW NFS Integration Gaps
- **Modern CLI:** There is a lack of tests for the `ceph nfs export create rgw` command. Most existing RGW NFS tests use a legacy approach that doesn't align with the current product direction of unified management.
- **S3/NFS Interop:** Missing tests for object/file interoperability (e.g., uploading an object via S3 and reading/modifying it via NFS, and vice versa).

### 4.2. Security and Authentication Gaps
- **Kerberos:** No coverage for Kerberos-authenticated NFS mounts (sec=krb5, krb5i, krb5p).
- **ID Mapping:** NFS v4 relies heavily on ID mapping (`nfsidmap`). Current tests mostly use numeric IDs or simple string matches, missing edge cases in complex LDAP/AD environments.
- **ACLs:** Limited testing of NFS v4 ACLs and their mapping to CephFS/RGW permissions.

### 4.3. Advanced NFS v4.2 Features
- **Server-Side Copy (SSC):** No tests for `copy_file_range` or other SSC mechanisms that allow copying data between exports without pulling it through the client.
- **Sparse Files:** Lack of tests for hole punching and seeking (`SEEK_HOLE`, `SEEK_DATA`).
- **Space Reservation:** Tests for `ALLOCATE` and `DEALLOCATE` (though some partial deallocation tests exist, they are not exhaustive).

### 4.4. Scale and Performance Gaps
- **Export Scale:** No tests for clusters with thousands of exports.
- **Client Scale:** Limited testing of high-concurrency scenarios (e.g., hundreds of active clients hitting the same Ganesha instance).
- **Grace Period Recovery Scale:** Verification of lock recovery performance when thousands of locks are held during a failover.

### 4.5. Reliability and Negative Testing
- **Network Jitter:** Testing Ganesha behavior when network latency to OSDs or MDS is high.
- **Partial Outages:** Behavior when one MDS in a multi-active MDS setup fails.
- **Disk Full:** Ganesha's response to `EDQUOT` or `ENOSPC` from the backend.

## 5. Proposed Framework Enhancements

### 5.1. Backend-Agnostic Export Utility
The current `cli/ceph/nfs/export/export.py` hardcodes CephFS subvolume creation. It should be refactored to:
- Support RGW backends.
- Allow providing a raw path without assuming a subvolume should be created.
- Support more export options like `sectype` and `pseudo` paths.

### 5.2. Unified Log Parsing
Enhance `nfs_log_parser` in `tests/nfs/nfs_operations.py` to automatically aggregate logs from all Ganesha instances in a cluster, making it easier to debug HA failover issues.

## 6. Recommended New Test Cases
1. **`test_nfs_rgw_export_cli.py`**: Verify RGW bucket export via `ceph nfs` CLI.
2. **`test_nfs_v4_ssc.py`**: Verify Server-Side Copy between two CephFS exports.
3. **`test_nfs_kerberos_setup.py`**: A complex test involving a KDC to verify secure mounts.
4. **`test_nfs_scale_exports.py`**: Script to create 1000+ exports and verify Ganesha stability.

## 7. NFS Protocol-Side Automations

The `cephci` framework utilizes several industry-standard tools for protocol-level verification, but there are opportunities for deeper automation.

### 7.1. Existing Protocol Automations
- **pynfs:** Automated via `tests/nfs/nfs_verify_pynfs.py`. It runs the Linux NFS team's protocol test suite against the Ceph NFS cluster.
- **NFStest:** Automated via `tests/nfs/nfs_run_nfslocktest_suite.py`. Specifically used for verifying POSIX and NFS locking semantics.
- **FIO:** Used in various scale and stress tests (e.g., `nfs_scale_with_multi_io.py`) to automate protocol-level I/O patterns and verify data integrity.

### 7.2. Protocol Automation Gaps
- **Interop Matrix Automation:** Lack of an automated way to run the same protocol tests across a matrix of client OSs (RHEL, Ubuntu, Windows, SLES) and NFS versions (3, 4.0, 4.1, 4.2) simultaneously.
- **Protocol Fuzzing:** No automated protocol fuzzing (e.g., using `AFL` or `Sulley`) to test the robustness of the Ganesha XDR decoding and RPC handling.
- **Network-Level Automation:** Missing automation for network-level disruptions (packet loss, reordering, fragmentation) specifically targeting the NFS RPC stream to verify protocol-level recovery.
- **Compliance Tracking:** Lack of automated extraction and trending of `pynfs` results over time to identify protocol regressions in newer Ceph/Ganesha releases.
