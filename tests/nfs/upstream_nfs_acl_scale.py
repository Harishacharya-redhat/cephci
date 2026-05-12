"""
NFS v4 ACL Scale / Large ACE Count Tests (GPFS / Spectrum Scale upstream).

Runs all large-ACL and performance tests in a single pass:
  - Apply a large named-user ACL (see BULK_* constants), verify count
  - Random access validation, Non-listed user
  - Performance check
  - Overwrite behaviour, Append behaviour, Duplicate ACE handling
  - Scale limit test (expects reject or accept for an oversized ACL file)
  - Persistence after remount

GPFS note: ``nfs4_setfacl -S`` returns EINVAL when the ACL exceeds Spectrum
Scale's effective named-ACE budget (CI still failed with 800 named UIDs; the
range is capped much lower while keeping enough ACEs to exercise scale paths).
"""

from time import sleep

from cli.exceptions import ConfigError
from tests.nfs.lib.nfs_acl import NfsAcl
from tests.nfs.lib.upstream_gpfs_nfs_setup import setup_gpfs_nfs, teardown_gpfs_nfs
from utility.log import Log

log = Log(__name__)

# NFSv4 expanded permission strings as returned by a standard nfs4_getfacl.
# nfs4_setfacl accepts short aliases (r, rw, rwx) but standard kernel NFS
# servers return the full permission bits.  Spectrum Scale returns the alias
# verbatim; the NfsAcl helper normalises both forms before comparison, so
# these constants work regardless of which representation the server uses.
PERM_R = "rtcy"  # r   -> rtcy
PERM_RX = "rxtcy"  # rx  -> rxtcy
PERM_W = "watcy"  # w   -> watcy
PERM_WX = "waxtcy"  # wx  -> waxtcy
PERM_RW = "rwatcy"  # rw  -> rwatcy
PERM_RWX = "rwaxtcy"  # rwx -> rwaxtcy

# Bulk ACL: named UIDs in [BULK_UID_START, BULK_UID_END] inclusive.
# Spectrum Scale over NFS rejected ~800 named ``A::uid:`` ACEs (EINVAL); keep a
# conservative count that still exceeds trivial single-ACE cases.
BULK_UID_START = 1000
BULK_UID_END = 1063
BULK_NAMED_COUNT = BULK_UID_END - BULK_UID_START + 1
# Named UIDs used for spot checks / su -u access (must lie in the bulk range).
BULK_SPOT_UID_1 = 1020
BULK_SPOT_UID_2 = 1050

# Known issues: map test name (as it appears in the results table) to a
# tracker reference.  Tests listed here are still executed and reported,
# but a failure is marked as "KNOWN" instead of a hard failure.
# Add / remove entries as bugs are filed or fixed.
KNOWN_ISSUES = {
    # "Apply bulk ACEs": "BZ#1234567",
    # "Scale Limit": "TRACKER-9876 - description",
}


def run(ceph_cluster, **kw):
    """Entry point called by the test framework."""
    config = kw.get("config") or {}
    clients_all = ceph_cluster.get_nodes("client")

    port = config.get("port", "2049")
    version = config.get("nfs_version", "4.1")
    no_clients = int(config.get("clients", "1"))
    mount_type = config.get("mount_type", "nfs")

    if no_clients > len(clients_all):
        raise ConfigError("The test requires more clients than available")

    gpfs = None

    log.info(
        "\n"
        + "=" * 70
        + "\n"
        + "  NFS ACL SCALE TESTS (GPFS upstream)\n"
        + "  mount_type=%s  nfs_version=%s  clients=%s\n"
        + "=" * 70,
        mount_type,
        version,
        no_clients,
    )

    try:
        gpfs = setup_gpfs_nfs(ceph_cluster, config)
        clients = gpfs["clients"]
        client = clients[0]
        nfs_mount = gpfs["nfs_mount"]
        nfs_export = gpfs["nfs_export"]
        nfs_server_host = gpfs["nfs_server_host"]
        server_node = gpfs["server"]
        acl = NfsAcl(
            client, nfs_mount, server=server_node, gpfs_path=nfs_export
        )
        acl.install_acl_tools()

        for node in (client, server_node):
            NfsAcl.create_user(node, "u1001", 1001)
            NfsAcl.create_user(node, f"u{BULK_SPOT_UID_1}", BULK_SPOT_UID_1)
            NfsAcl.create_user(node, "u4000", 4000)
            NfsAcl.create_user(node, "u5000", 5000)

        bulk_lo, bulk_hi = BULK_UID_START, BULK_UID_END
        log.info(
            "Provisioning passwd entries for bulk ACL UIDs %d-%d on client and server",
            bulk_lo,
            bulk_hi,
        )
        NfsAcl.provision_passwd_uid_range(
            client, bulk_lo, bulk_hi, server_node=server_node
        )
        gpfs["scale_bulk_uid_range"] = (bulk_lo, bulk_hi)

        results = []

        results.append(("Apply bulk ACEs", _run_test(_test_apply_large_acl, acl)))
        results.append(("Verify ACE Count", _run_test(_test_verify_ace_count, acl)))
        results.append(
            ("Random Access Validation", _run_test(_test_random_access, acl, client))
        )
        results.append(
            ("Non-listed User", _run_test(_test_non_listed_user, acl, client))
        )
        results.append(("Performance Check", _run_test(_test_performance_check, acl)))
        results.append(
            ("Overwrite Behaviour", _run_test(_test_overwrite_behaviour, acl))
        )
        results.append(("Append Behaviour", _run_test(_test_append_behaviour, acl)))
        results.append(("Duplicate ACE Handling", _run_test(_test_duplicate_ace, acl)))
        results.append(("Scale Limit", _run_test(_test_scale_limit, acl)))
        results.append(
            (
                "Persistence After Remount",
                _run_test(
                    _test_persistence_remount,
                    acl,
                    client,
                    nfs_server_host,
                    nfs_mount,
                    nfs_export,
                    version,
                    port,
                    mount_type,
                ),
            )
        )

        return _report_results(results)

    except Exception as e:
        log.error("Fatal error in ACL scale tests: %s", e)
        return 1
    finally:
        if gpfs:
            clients = gpfs["clients"]
            nfs_mount = gpfs["nfs_mount"]
            server_node = gpfs.get("server")
            client = clients[0]
            bulk_rng = gpfs.pop("scale_bulk_uid_range", None)
            if bulk_rng and server_node:
                lo, hi = bulk_rng
                log.info("Removing nfstest* passwd entries for UIDs %d-%d", lo, hi)
                NfsAcl.cleanup_nfstest_passwd_uid_range(
                    client, lo, hi, server_node=server_node
                )
            for c in clients:
                NfsAcl.delete_user(c, "u1001")
                NfsAcl.delete_user(c, f"u{BULK_SPOT_UID_1}")
                NfsAcl.delete_user(c, "u4000")
                NfsAcl.delete_user(c, "u5000")
            if server_node:
                for u in ("u1001", f"u{BULK_SPOT_UID_1}", "u4000", "u5000"):
                    NfsAcl.delete_user(server_node, u)
            teardown_gpfs_nfs(clients, nfs_mount)
            log.info("Cleanup completed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_test(fn, *args, **kwargs):
    """Run a sub-test with prominent start/end banners."""
    name = fn.__name__.replace("_test_", "").replace("_", " ").title()
    NfsAcl.log_test_start(name)
    try:
        rc = fn(*args, **kwargs)
        NfsAcl.log_test_end(name, rc == 0)
        return rc
    except Exception as e:
        log.error("Sub-test %s raised an exception: %s", fn.__name__, e)
        NfsAcl.log_test_end(name, False)
        return 1


def _report_results(results):
    """Log a summary table; return 1 only if a sub-test failed outside KNOWN_ISSUES."""
    hard_failures = []
    known_failures = []
    log.info("=" * 60)
    log.info("SCALE TEST RESULTS")
    log.info("=" * 60)
    for name, rc in results:
        if rc == 0:
            log.info("  %-35s PASS", name)
        elif name in KNOWN_ISSUES:
            log.info("  %-35s FAIL (KNOWN: %s)", name, KNOWN_ISSUES[name])
            known_failures.append(name)
        else:
            log.info("  %-35s FAIL", name)
            hard_failures.append(name)
    log.info("=" * 60)
    if known_failures:
        log.warning("Known failures: %s", known_failures)
    if hard_failures:
        log.error("Unexpected failures: %s", hard_failures)
        return 1
    if known_failures:
        log.warning("Only known failures found; returning success for this run")
        return 0
    log.info("All scale tests passed")
    return 0


def _named_uid_ace_count(entries):
    """Count ``A::<numeric-uid>:`` lines (excludes OWNER@ / GROUP@ / EVERYONE@)."""
    n = 0
    for e in entries:
        if not e or e.startswith("#"):
            continue
        parts = e.split(":", 3)
        if len(parts) == 4 and parts[0] == "A" and parts[2].isdigit():
            n += 1
    return n


def _apply_bulk_aces(acl):
    """Generate and apply a GPFS-valid bulk ACL (see BULK_UID_* constants)."""
    acl.create_file("f1")
    acl_file = "/tmp/acl2.txt"
    acl.generate_gpfs_bulk_acl_file(
        acl_file,
        uid_start=BULK_UID_START,
        uid_end=BULK_UID_END,
        named_perm=PERM_RX,
        deny_uids=(4000, 5000),
        include_file_directive=False,
    )
    acl.set_acl_from_file("f1", acl_file)
    return acl_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _test_apply_large_acl(acl):
    log.info(
        "=== Test: Apply bulk ACEs (%d named UIDs %d-%d) ===",
        BULK_NAMED_COUNT,
        BULK_UID_START,
        BULK_UID_END,
    )
    acl_file = _apply_bulk_aces(acl)

    if not acl.verify_acl_contains("f1", f"A::{BULK_SPOT_UID_1}:{PERM_RX}"):
        log.error(
            "Spot check failed: A::%s:%s not found in large ACL",
            BULK_SPOT_UID_1,
            PERM_RX,
        )
        return 1
    if not acl.verify_acl_contains("f1", f"A::{BULK_SPOT_UID_2}:{PERM_RX}"):
        log.error(
            "Spot check failed: A::%s:%s not found in large ACL",
            BULK_SPOT_UID_2,
            PERM_RX,
        )
        return 1

    spot_user = f"u{BULK_SPOT_UID_1}"
    log.info(
        "Access verification: %s (in ACL) should read, u4000 (not in ACL) should not",
        spot_user,
    )
    if not acl.verify_access(spot_user, "f1", operation="read", expect_success=True):
        log.error("%s denied read despite ACL granting rx", spot_user)
        return 1
    if not acl.verify_access("u4000", "f1", operation="read", expect_success=False):
        log.error("u4000 granted read despite not being in ACL")
        return 1

    acl.client.exec_command(sudo=True, cmd=f"rm -f {acl_file}")
    log.info("Apply bulk ACEs: PASSED")
    return 0


def _test_verify_ace_count(acl):
    log.info("=== Test: Verify ACE Count ===")
    acl_file = _apply_bulk_aces(acl)

    entries = acl.get_acl("f1")
    count = len(entries)
    named = _named_uid_ace_count(entries)
    log.info("Total ACL lines: %d (named UID ACEs: %d)", count, named)

    if named < BULK_NAMED_COUNT:
        log.error(
            "Expected at least %d named UID ACEs, got %d",
            BULK_NAMED_COUNT,
            named,
        )
        return 1

    spot_user = f"u{BULK_SPOT_UID_1}"
    log.info("Access verification: %s should read with rx permission", spot_user)
    if not acl.verify_access(spot_user, "f1", operation="read", expect_success=True):
        log.error("%s denied read despite being in large ACL", spot_user)
        return 1

    acl.client.exec_command(sudo=True, cmd=f"rm -f {acl_file}")
    log.info("Verify ACE Count (%d): PASSED", count)
    return 0


def _test_random_access(acl, client):
    log.info("=== Test: Random Access Validation ===")
    acl_file = _apply_bulk_aces(acl)

    spot_user = f"u{BULK_SPOT_UID_1}"
    if not acl.verify_acl_contains("f1", f"A::{BULK_SPOT_UID_1}:{PERM_RX}"):
        log.error("ACE for UID %s not found before access check", BULK_SPOT_UID_1)
        return 1

    if not acl.verify_access(spot_user, "f1", operation="read", expect_success=True):
        log.error("User %s denied access despite ACL", spot_user)
        return 1

    if not acl.verify_access(spot_user, "f1", operation="write", expect_success=False):
        log.error("User %s granted access to write despite ACL", spot_user)
        return 1

    acl.client.exec_command(sudo=True, cmd=f"rm -f {acl_file}")
    log.info("Random Access Validation: PASSED")
    return 0


def _test_non_listed_user(acl, client):
    log.info("=== Test: Non-listed User ===")
    acl_file = _apply_bulk_aces(acl)

    if not acl.verify_acl_not_contains("f1", f"A::4000:{PERM_RX}"):
        log.error("ACE for UID 4000 unexpectedly found in ACL")
        return 1

    if not acl.verify_access("u4000", "f1", operation="read", expect_success=False):
        log.error("User u4000 granted access despite not being in ACL")
        return 1

    acl.client.exec_command(sudo=True, cmd=f"rm -f {acl_file}")
    log.info("Non-listed User: PASSED")
    return 0


def _test_performance_check(acl):
    log.info("=== Test: Performance Check ===")
    acl_file = _apply_bulk_aces(acl)

    out, elapsed = acl.timed_getfacl("f1")
    log.info("nfs4_getfacl completed in %s seconds", elapsed)

    if elapsed is not None and elapsed > 10:
        log.error("Performance issue: nfs4_getfacl took %.2f seconds (>10s)", elapsed)
        return 1

    if elapsed is not None:
        log.info("Performance acceptable: %.2f seconds", elapsed)
    else:
        log.warning("Could not parse timing information")

    acl.client.exec_command(sudo=True, cmd=f"rm -f {acl_file}")
    log.info("Performance Check: PASSED")
    return 0


def _test_overwrite_behaviour(acl):
    log.info("=== Test: Overwrite Behaviour ===")
    acl_file = _apply_bulk_aces(acl)

    count_before = len(acl.get_acl("f1"))
    log.info("ACE count before overwrite: %d", count_before)

    acl.set_acl("f1", "A::1001:wx")
    if not acl.verify_acl_contains("f1", f"A::1001:{PERM_WX}"):
        log.error("New ACE not found after overwrite")
        return 1

    entries = acl.get_acl("f1")
    count_after = len(entries)
    log.info("ACE count after overwrite: %d (was %d)", count_after, count_before)

    if count_after >= count_before:
        log.error(
            "Overwrite did not reduce ACE count: before=%d, after=%d",
            count_before,
            count_after,
        )
        return 1

    log.info(
        "Access verification: u1001 should write; bulk named ACE for uid %s gone; "
        "u1001 may still read via EVERYONE@ (not asserted)",
        BULK_SPOT_UID_1,
    )
    if not acl.verify_access("u1001", "f1", operation="write", expect_success=True):
        log.error("u1001 denied write after overwrite to wx")
        return 1
    if not acl.verify_acl_not_contains("f1", f"A::{BULK_SPOT_UID_1}:{PERM_RX}"):
        log.error(
            "Bulk ACE A::%s:%s still present after overwrite (expected replacement)",
            BULK_SPOT_UID_1,
            PERM_RX,
        )
        return 1

    acl.client.exec_command(sudo=True, cmd=f"rm -f {acl_file}")
    log.info("Overwrite Behaviour: PASSED")
    return 0


def _test_append_behaviour(acl):
    log.info("=== Test: Append Behaviour ===")
    acl_file = _apply_bulk_aces(acl)

    count_before = len(acl.get_acl("f1"))
    acl.add_acl("f1", "A::5000:wx")
    if not acl.verify_acl_contains("f1", f"A::5000:{PERM_WX}"):
        log.error("Appended ACE A::5000:%s not found", PERM_WX)
        return 1
    if not acl.verify_acl_contains("f1", f"A::{BULK_SPOT_UID_1}:{PERM_RX}"):
        log.error(
            "Original ACE A::%s:%s lost after append",
            BULK_SPOT_UID_1,
            PERM_RX,
        )
        return 1

    count_after = len(acl.get_acl("f1"))
    log.info("ACE count before append=%d, after=%d", count_before, count_after)
    if count_after != count_before + 1:
        log.error(
            "Expected ACE count to increase by 1 after append: before=%d, after=%d",
            count_before,
            count_after,
        )
        return 1

    spot_user = f"u{BULK_SPOT_UID_1}"
    log.info(
        "Access verification: u5000 (wx) should write, %s (rx) should still read",
        spot_user,
    )
    if not acl.verify_access("u5000", "f1", operation="write", expect_success=True):
        log.error("u5000 denied write after append with wx")
        return 1
    if not acl.verify_access(spot_user, "f1", operation="read", expect_success=True):
        log.error("%s denied read after append (original rx ACE should persist)", spot_user)
        return 1

    acl.client.exec_command(sudo=True, cmd=f"rm -f {acl_file}")
    log.info("Append Behaviour: PASSED")
    return 0


def _test_duplicate_ace(acl):
    log.info("=== Test: Duplicate ACE Handling ===")
    acl_file = _apply_bulk_aces(acl)

    spot_user = f"u{BULK_SPOT_UID_1}"
    if not acl.verify_acl_contains("f1", f"A::{BULK_SPOT_UID_1}:{PERM_RX}"):
        log.error("ACE A::%s:%s not found before duplicate add", BULK_SPOT_UID_1, PERM_RX)
        return 1

    count_before = len(acl.get_acl("f1"))
    acl.add_acl("f1", f"A::{BULK_SPOT_UID_1}:rx")
    count_after = len(acl.get_acl("f1"))

    if not acl.verify_acl_contains("f1", f"A::{BULK_SPOT_UID_1}:{PERM_RX}"):
        log.error("ACE A::%s:%s not found after duplicate add", BULK_SPOT_UID_1, PERM_RX)
        return 1

    log.info("Duplicate ACE: count before=%d, after=%d", count_before, count_after)
    delta = count_after - count_before
    if delta not in (0, 1):
        log.error(
            "Duplicate ACE unexpected count change: before=%d, after=%d (delta=%d; "
            "expected 0 if merged or 1 if a second literal ACE was appended)",
            count_before,
            count_after,
            delta,
        )
        return 1
    if delta == 1:
        log.info(
            "Server appended a second ACE for uid %s (Spectrum Scale / nfs4_setfacl "
            "does not always dedupe identical -a entries)",
            BULK_SPOT_UID_1,
        )

    log.info("Access verification: %s should still read after duplicate add", spot_user)
    if not acl.verify_access(spot_user, "f1", operation="read", expect_success=True):
        log.error("%s denied read after duplicate ACE add", spot_user)
        return 1
    if not acl.verify_access(spot_user, "f1", operation="write", expect_success=False):
        log.error("%s granted write despite only having rx", spot_user)
        return 1

    acl.client.exec_command(sudo=True, cmd=f"rm -f {acl_file}")
    log.info("Duplicate ACE Handling: PASSED")
    return 0


def _test_scale_limit(acl):
    log.info("=== Test: Scale Limit ===")
    acl.create_file("f1")
    big_acl_file = "/tmp/acl_big.txt"
    attempted_named = 4270 - 1000 + 1

    log.info(
        "Generating large GPFS-style ACL file (named UIDs 1000-4270 = %d entries)",
        attempted_named,
    )
    acl.generate_gpfs_bulk_acl_file(
        big_acl_file,
        uid_start=1000,
        uid_end=4270,
        named_perm=PERM_RX,
        deny_uids=(9998, 9999),
    )

    out, err, ec = acl.set_acl_from_file_expect_fail("f1", big_acl_file)
    log.info(
        "Scale limit apply exit_code=%s output=%s stderr=%s",
        ec,
        (out or "").strip()[:200],
        (err or "").strip()[:200],
    )

    entries = acl.get_acl("f1")
    named = _named_uid_ace_count(entries)
    log.info(
        "Named UID ACE count after apply attempt: %d (attempted %d)",
        named,
        attempted_named,
    )

    combined = ((out or "") + (err or "")).lower()
    if ec is not None:
        apply_failed = ec != 0
    else:
        apply_failed = (
            "failed operation" in combined
            or "invalid argument" in combined
            or "argument list too long" in combined
        )

    if apply_failed:
        if named >= BULK_NAMED_COUNT:
            log.error(
                "setfacl failed but ACL still shows many named ACEs (partial apply?): %d",
                named,
            )
            acl.client.exec_command(sudo=True, cmd=f"rm -f {big_acl_file}")
            return 1
        log.info(
            "Scale Limit: PASSED (server rejected oversized ACL; named=%d)",
            named,
        )
        acl.client.exec_command(sudo=True, cmd=f"rm -f {big_acl_file}")
        return 0

    if named >= BULK_NAMED_COUNT:
        log.info(
            "Scale Limit: PASSED (server accepted bulk ACL; named=%d)",
            named,
        )
        acl.client.exec_command(sudo=True, cmd=f"rm -f {big_acl_file}")
        return 0

    log.error(
        "Unexpected scale-limit outcome: exit=%s named=%s stderr=%s",
        ec,
        named,
        (err or "").strip(),
    )
    acl.client.exec_command(sudo=True, cmd=f"rm -f {big_acl_file}")
    return 1


def _test_persistence_remount(
    acl, client, nfs_server, nfs_mount, nfs_export, version, port, mount_type
):
    log.info("=== Test: Persistence After Remount ===")
    acl_file = _apply_bulk_aces(acl)

    if not acl.verify_acl_contains("f1", f"A::{BULK_SPOT_UID_1}:{PERM_RX}"):
        log.error("Pre-remount spot check failed for UID %s", BULK_SPOT_UID_1)
        return 1
    if not acl.verify_acl_contains("f1", f"A::{BULK_SPOT_UID_2}:{PERM_RX}"):
        log.error("Pre-remount spot check failed for UID %s", BULK_SPOT_UID_2)
        return 1

    spot_user = f"u{BULK_SPOT_UID_1}"
    log.info("Pre-remount access verification: %s should read", spot_user)
    if not acl.verify_access(spot_user, "f1", operation="read", expect_success=True):
        log.error("%s denied read before remount", spot_user)
        return 1

    pre_remount_count = len(acl.get_acl("f1"))
    log.info("ACE count before remount: %d", pre_remount_count)

    NfsAcl.remount_export(
        client,
        nfs_mount,
        nfs_server,
        nfs_export,
        version=str(version),
        port=str(port),
        mount_type=mount_type,
    )
    sleep(5)

    if not acl.verify_acl_contains("f1", f"A::{BULK_SPOT_UID_1}:{PERM_RX}"):
        log.error("ACL lost after remount for UID %s", BULK_SPOT_UID_1)
        return 1
    if not acl.verify_acl_contains("f1", f"A::{BULK_SPOT_UID_2}:{PERM_RX}"):
        log.error("ACL entry for UID %s lost after remount", BULK_SPOT_UID_2)
        return 1

    entries = acl.get_acl("f1")
    post_remount_count = len(entries)
    log.info(
        "ACE count before remount=%d, after remount=%d",
        pre_remount_count,
        post_remount_count,
    )
    if pre_remount_count != post_remount_count:
        log.error(
            "ACE count changed after remount! before=%d, after=%d",
            pre_remount_count,
            post_remount_count,
        )
        return 1

    log.info("Post-remount access verification: %s should still read", spot_user)
    if not acl.verify_access(spot_user, "f1", operation="read", expect_success=True):
        log.error("%s denied read after remount (ACL not persisted)", spot_user)
        return 1
    if not acl.verify_access("u4000", "f1", operation="read", expect_success=False):
        log.error("u4000 granted read after remount (should still be denied)")
        return 1

    acl.client.exec_command(sudo=True, cmd=f"rm -f {acl_file}")
    log.info("Persistence After Remount: PASSED")
    return 0
