"""
Reusable NFS v4 ACL helper class.

Wraps nfs4_setfacl / nfs4_getfacl operations and provides convenience
methods for ACL manipulation, user/group management, and verification
on remote NFS client nodes.

Usage
-----
    from tests.nfs.lib.nfs_acl import NfsAcl

    acl = NfsAcl(client, mount_point="/mnt/nfs")
    acl.install_acl_tools()
    acl.set_acl("f1", "A::1001:rw")
    entries = acl.get_acl("f1")
"""

from time import sleep

from utility.log import Log

log = Log(__name__)

_ACL_TOOLS_PKG = "nfs4-acl-tools"


class NfsAcl:
    """Helper class for NFSv4 ACL operations on a remote client node."""

    def __init__(self, client, mount_point, server=None, gpfs_path=None):
        self.client = client
        self.mount = mount_point
        self.server = server
        self.gpfs_path = gpfs_path

    # -- package management ---------------------------------------------------

    def install_acl_tools(self):
        """Install nfs4-acl-tools on the client node."""
        log.info("Installing %s on %s", _ACL_TOOLS_PKG, self.client.hostname)
        self.client.exec_command(
            sudo=True, cmd=f"yum install -y {_ACL_TOOLS_PKG}", long_running=True
        )

    # -- path helpers ---------------------------------------------------------

    def _full_path(self, relative_path):
        return f"{self.mount}/{relative_path}"

    def _server_path(self, relative_path):
        """Return the GPFS-native path on the server node, or None."""
        if self.gpfs_path:
            return f"{self.gpfs_path}/{relative_path}"
        return None

    # -- file / directory creation --------------------------------------------

    def create_file(self, name):
        """Create (or recreate) a regular file under the mount point."""
        path = self._full_path(name)
        log.info("Creating file %s", path)
        self.client.exec_command(sudo=True, cmd=f"rm -f {path} && touch {path}")

    def create_dir(self, name):
        """Create a directory under the mount point (with -p)."""
        path = self._full_path(name)
        log.info("Creating directory %s", path)
        self.client.exec_command(sudo=True, cmd=f"mkdir -p {path}")

    def write_file(self, name, content="hello"):
        """Write *content* into a file (creates it if absent)."""
        path = self._full_path(name)
        log.info("Writing to %s", path)
        self.client.exec_command(sudo=True, cmd=f"echo '{content}' > {path}")

    def remove(self, name, recursive=False):
        """Remove a file or directory."""
        flag = "-rf" if recursive else "-f"
        path = self._full_path(name)
        log.info("Removing %s", path)
        self.client.exec_command(sudo=True, cmd=f"rm {flag} {path}")

    def create_symlink(self, target, link_name):
        """Create a symbolic link *link_name* -> *target* (both relative to mount)."""
        target_path = f"../{target}" if not target.startswith("/") else target
        link_path = self._full_path(link_name)
        log.info("Creating symlink %s -> %s", link_path, target_path)
        self.client.exec_command(sudo=True, cmd=f"ln -sfn {target_path} {link_path}")

    def create_hardlink(self, src, dest):
        """Create a hard link *dest* pointing to *src*."""
        src_path = self._full_path(src)
        dest_path = self._full_path(dest)
        log.info("Creating hard link %s -> %s", dest_path, src_path)
        self.client.exec_command(sudo=True, cmd=f"ln {src_path} {dest_path}")

    def rename(self, old_name, new_name):
        """Rename / move a file."""
        old_path = self._full_path(old_name)
        new_path = self._full_path(new_name)
        log.info("Renaming %s -> %s", old_path, new_path)
        self.client.exec_command(sudo=True, cmd=f"mv {old_path} {new_path}")

    def inode(self, name):
        """Return the inode number of *name*."""
        path = self._full_path(name)
        out, _ = self.client.exec_command(
            sudo=True, cmd=f"ls -li {path} | awk '{{print $1}}'"
        )
        return out.strip()

    # -- ACL operations -------------------------------------------------------

    def get_acl(self, name):
        """Run nfs4_getfacl and return the output lines as a list."""
        path = self._full_path(name)
        log.info("Getting ACL for %s", path)
        out, _ = self.client.exec_command(sudo=True, cmd=f"nfs4_getfacl {path}")
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        log.info("ACL entries for %s: %s", path, lines)
        return lines

    @staticmethod
    def _expand_ace_perms(ace_spec):
        """
        Expand shorthand permission aliases in a comma-separated ACE spec.

        Spectrum Scale stores permission strings verbatim rather than expanding
        aliases.  Passing ``A::1001:rw`` stores only READ_DATA and WRITE_DATA,
        omitting READ_ATTRIBUTES (``t``) which is required to open a file,
        causing EPERM on real access.

        This method handles both single ACEs (``A::1001:rw``) and
        comma-separated multi-ACE specs (``D::2000:rw,A::1001:rw``).
        """
        alias_map = {"r": "rtcy", "w": "watcy", "a": "atcy", "x": "xtcy"}
        result = []
        for ace in ace_spec.split(","):
            ace = ace.strip()
            fields = ace.split(":", 3)
            if len(fields) != 4:
                result.append(ace)
                continue
            ace_type, ace_flags, ace_who, ace_perm = fields
            seen: set = set()
            expanded = []
            for ch in ace_perm:
                for bit in alias_map.get(ch, ch):
                    if bit not in seen:
                        seen.add(bit)
                        expanded.append(bit)
            result.append(":".join([ace_type, ace_flags, ace_who, "".join(expanded)]))
        return ",".join(result)

    @staticmethod
    def _ace_who(ace):
        parts = ace.split(":", 3)
        return parts[2] if len(parts) == 4 else None

    @staticmethod
    def _ensure_base_aces(ace_spec):
        """
        Guarantee that OWNER@, GROUP@, and EVERYONE@ are present, and order
        ACEs for GPFS/Ganesha's POSIX mode gate *and* correct NFSv4 evaluation.

        GPFS derives POSIX mode from OWNER@/GROUP@/EVERYONE@ before applying
        named-user ACEs; those three must be present or non-owner access is
        blocked entirely.

        EVERYONE@ typically includes read (``rtncy``).  That ACE is evaluated
        before trailing named-user ALLOWs, so users matched by EVERYONE@ would
        incorrectly get read unless explicit ``D::uid:...`` DENY entries appear
        **after** OWNER@ but **before** GROUP@/EVERYONE@.  This helper collects
        caller ``D::`` ACEs (for principals other than OWNER@/GROUP@/EVERYONE@)
        and places them in that slot; remaining caller ACEs follow GROUP@ and
        EVERYONE@.

        Example::

            set_acl("f1", f"D::{uid3}:rwatcy,A::{uid1}:rw")

        becomes roughly::

            A::OWNER@:rwatcy,D::uid3:rwatcy,A::GROUP@:rtncy,A::EVERYONE@:rtncy,A::uid1:rwatcy
        """
        SPECIAL = frozenset({"OWNER@", "GROUP@", "EVERYONE@"})
        parts = [p.strip() for p in ace_spec.split(",") if p.strip()]

        owner_aces = [a for a in parts if NfsAcl._ace_who(a) == "OWNER@"]
        group_aces = [a for a in parts if NfsAcl._ace_who(a) == "GROUP@"]
        everyone_aces = [a for a in parts if NfsAcl._ace_who(a) == "EVERYONE@"]
        denies = [
            a
            for a in parts
            if len(a) > 0
            and a[0] == "D"
            and NfsAcl._ace_who(a) not in SPECIAL
        ]
        other = [
            a
            for a in parts
            if a not in owner_aces
            and a not in group_aces
            and a not in everyone_aces
            and a not in denies
        ]

        out = []
        if owner_aces:
            out.append(owner_aces[0])
        else:
            out.append("A::OWNER@:rwatcy")

        out.extend(denies)

        if group_aces:
            out.append(group_aces[0])
        else:
            out.append("A::GROUP@:rtncy")

        if everyone_aces:
            out.append(everyone_aces[0])
        else:
            out.append("A::EVERYONE@:rtncy")

        out.extend(other)
        return ",".join(out)

    def set_acl(self, name, ace_spec):
        """Full-replace ACL using ``nfs4_setfacl -s``."""
        path = self._full_path(name)
        ace_spec = self._expand_ace_perms(ace_spec)
        ace_spec = self._ensure_base_aces(ace_spec)
        log.info("Setting ACL on %s: %s", path, ace_spec)
        self.client.exec_command(sudo=True, cmd=f"nfs4_setfacl -s '{ace_spec}' {path}")
        # region agent log - Hypothesis C: mmgetacl (GPFS native) vs nfs4_getfacl
        try:
            srv_path = self._server_path(name)
            if self.server and srv_path:
                try:
                    _mm_out, _mm_err = self.server.exec_command(
                        sudo=True,
                        cmd=f"/usr/lpp/mmfs/bin/mmgetacl {srv_path}",
                        check_ec=False,
                    )
                except Exception as _e:
                    _mm_out, _mm_err = "", str(_e)
                log.info(
                    "[DBG-8f239e] HypC mmgetacl(GPFS-native) for %s | ace_set=%s | mmgetacl=%r | mmgetacl_err=%r",
                    srv_path, ace_spec,
                    _mm_out.strip() if _mm_out else "",
                    _mm_err.strip() if _mm_err else "",
                )
        except Exception as _dbg_ex:
            log.warning("[DBG-8f239e] instrumentation error: %s", _dbg_ex)
        # endregion

    def add_acl(self, name, ace_spec):
        """Incrementally add an ACE using ``nfs4_setfacl -a``."""
        path = self._full_path(name)
        ace_spec = self._expand_ace_perms(ace_spec)
        log.info("Adding ACE to %s: %s", path, ace_spec)
        self.client.exec_command(sudo=True, cmd=f"nfs4_setfacl -a '{ace_spec}' {path}")

    def set_acl_from_file(self, name, spec_file_path):
        """Apply ACL from a spec file using ``nfs4_setfacl -S``."""
        path = self._full_path(name)
        log.info("Applying ACL from spec file %s to %s", spec_file_path, path)
        self.client.exec_command(
            sudo=True, cmd=f"nfs4_setfacl -S {spec_file_path} {path}"
        )

    def set_acl_from_file_expect_fail(self, name, spec_file_path):
        """
        Apply ACL from a spec file without raising on non-zero exit.

        Returns (stdout, stderr, exit_code).  ``exit_code`` may be None if the
        node client does not support the ``verbose`` execution path.
        """
        path = self._full_path(name)
        log.info(
            "Applying ACL from spec file %s to %s (expecting failure)",
            spec_file_path,
            path,
        )
        result = self.client.exec_command(
            sudo=True,
            cmd=f"nfs4_setfacl -S {spec_file_path} {path}",
            check_ec=False,
            verbose=True,
        )
        if isinstance(result, tuple) and len(result) == 4:
            out, err, exit_code, _duration = result
            return out, err, exit_code
        out, err = result
        return out, err, None

    def set_acl_recursive(self, name, ace_spec, follow_symlinks=None):
        """
        Recursively set ACL using ``nfs4_setfacl -R``.

        follow_symlinks: None  -> default behaviour
                         True  -> -L (follow symlinks)
                         False -> -P (skip symlinks)
        """
        path = self._full_path(name)
        flags = "-R"
        if follow_symlinks is True:
            flags += " -L"
        elif follow_symlinks is False:
            flags += " -P"
        ace_spec = self._expand_ace_perms(ace_spec)
        ace_spec = self._ensure_base_aces(ace_spec)
        log.info("Recursive set ACL on %s (flags=%s): %s", path, flags, ace_spec)
        self.client.exec_command(
            sudo=True, cmd=f"nfs4_setfacl {flags} -s '{ace_spec}' {path}"
        )

    def save_acl_to_file(self, name, dest_file):
        """Dump current ACL to a file via ``nfs4_getfacl name > dest``."""
        path = self._full_path(name)
        log.info("Saving ACL of %s to %s", path, dest_file)
        self.client.exec_command(sudo=True, cmd=f"nfs4_getfacl {path} > {dest_file}")

    # -- chmod helpers --------------------------------------------------------

    def chmod(self, name, mode):
        """Run chmod on a path."""
        path = self._full_path(name)
        log.info("chmod %s %s", mode, path)
        self.client.exec_command(sudo=True, cmd=f"chmod {mode} {path}")

    def get_mode(self, name):
        """Return the octal mode string (e.g. '0644') for a path."""
        path = self._full_path(name)
        out, _ = self.client.exec_command(sudo=True, cmd=f"stat -c '%a' {path}")
        return out.strip()

    # -- user / group management ----------------------------------------------

    @staticmethod
    def create_user(client, username, uid):
        """Create a local user with the given UID; no-op if exists."""
        log.info("Creating user %s (uid=%s) on %s", username, uid, client.hostname)
        client.exec_command(
            sudo=True,
            cmd=f"id -u {username} &>/dev/null || useradd -u {uid} {username}",
        )

    @staticmethod
    def create_group(client, groupname, gid):
        """Create a local group with the given GID; no-op if exists."""
        log.info("Creating group %s (gid=%s) on %s", groupname, gid, client.hostname)
        client.exec_command(
            sudo=True,
            cmd=f"getent group {groupname} &>/dev/null || groupadd -g {gid} {groupname}",
        )

    @staticmethod
    def add_user_to_group(client, username, groupname):
        """Add an existing user to a group."""
        log.info(
            "Adding user %s to group %s on %s", username, groupname, client.hostname
        )
        client.exec_command(sudo=True, cmd=f"usermod -aG {groupname} {username}")

    @staticmethod
    def delete_user(client, username):
        """Delete a user (ignore errors if missing)."""
        log.info("Deleting user %s on %s", username, client.hostname)
        client.exec_command(sudo=True, cmd=f"userdel -rf {username}", check_ec=False)

    @staticmethod
    def delete_group(client, groupname):
        """Delete a group (ignore errors if missing)."""
        log.info("Deleting group %s on %s", groupname, client.hostname)
        client.exec_command(sudo=True, cmd=f"groupdel {groupname}", check_ec=False)

    # -- command execution as a specific user ---------------------------------

    def run_as_user(self, username, cmd, check_ec=True):
        """Execute *cmd* as *username* via ``su - <user> -c '...'``."""
        log.info("Running as %s: %s", username, cmd)
        out, err = self.client.exec_command(
            sudo=True,
            cmd=f'su - {username} -c "{cmd}"',
            check_ec=check_ec,
        )
        return out, err

    # -- verification helpers -------------------------------------------------

    @staticmethod
    def _expand_perm_aliases(perms):
        """
        Expand shorthand NFSv4 permission aliases into their constituent bits.

        nfs4_setfacl accepts compact aliases; standard kernel NFS servers
        return the full bit-string via nfs4_getfacl, while Spectrum Scale
        returns the compact form verbatim.  Normalizing both sides before
        comparison makes ``rw`` == ``rwatcy``, ``a`` == ``atcy``, etc.

        Alias map (mirrors nfs4_setfacl shorthand definitions):
            r -> rtcy   (read-data + read-attrs + read-acl + sync)
            w -> watcy  (write-data + append + write-attrs + write-acl + sync)
            a -> atcy   (append + write-attrs + write-acl + sync)
            x -> xtcy   (execute + read-attrs + read-acl + sync)
        """
        alias_map = {"r": "rtcy", "w": "watcy", "a": "atcy", "x": "xtcy"}
        expanded = []
        for ch in perms:
            expanded.extend(alias_map.get(ch, ch))
        return expanded

    @staticmethod
    def _normalize_perm_set(perms):
        """Return a canonical, order-insensitive permission set."""
        return set(NfsAcl._expand_perm_aliases(perms))

    @staticmethod
    def _ace_fields(ace):
        """Parse ACE string into (type, flags, who, perms), or None."""
        parts = ace.split(":", 3)
        if len(parts) != 4:
            return None
        return parts[0], parts[1], parts[2], parts[3]

    @staticmethod
    def _ace_semantically_matches(actual_ace, expected_ace):
        """
        Match ACEs by identity fields and normalized permission/flag semantics.

        - ACE type (A/D) and who-field are compared exactly.
        - Flags are compared as unordered character sets so that Spectrum Scale
          reordering (e.g. ``nfd`` -> ``fdn``) does not cause false failures.
        - Permissions are normalized via ``_normalize_perm_set`` so compact
          aliases (``rw``) and expanded bit-strings (``rwatcy``) are equivalent.
        """
        actual = NfsAcl._ace_fields(actual_ace)
        expected = NfsAcl._ace_fields(expected_ace)
        if not actual or not expected:
            return False
        act_type, act_flags, act_who, act_perm = actual
        exp_type, exp_flags, exp_who, exp_perm = expected
        if act_type != exp_type:
            return False
        if set(act_flags) != set(exp_flags):
            return False
        if act_who != exp_who:
            return False
        return NfsAcl._normalize_perm_set(act_perm) == NfsAcl._normalize_perm_set(
            exp_perm
        )

    def _find_matching_ace(self, acl, expected_ace):
        """Return the first ACL entry that matches *expected_ace*, or None.

        Matching is attempted first as a fast substring check, then falls back
        to semantic comparison (order-insensitive flags + permission aliases).
        """
        for entry in acl:
            if expected_ace in entry:
                return entry
        for entry in acl:
            if self._ace_semantically_matches(entry, expected_ace):
                return entry
        return None

    def verify_acl_contains(self, name, expected_ace):
        """Assert that the ACL of *name* contains *expected_ace*."""
        acl = self.get_acl(name)
        matched = self._find_matching_ace(acl, expected_ace)
        if matched:
            log.info(
                "Verified ACL of %s contains '%s' (matched entry: '%s')",
                name,
                expected_ace,
                matched,
            )
        else:
            log.error(
                "ACL of %s does NOT contain '%s'. Current ACL: %s",
                name,
                expected_ace,
                acl,
            )
        return matched is not None

    def verify_acl_not_contains(self, name, unexpected_ace):
        """Assert that the ACL of *name* does NOT contain *unexpected_ace*."""
        acl = self.get_acl(name)
        matched = self._find_matching_ace(acl, unexpected_ace)
        if not matched:
            log.info("Verified ACL of %s does not contain '%s'", name, unexpected_ace)
        else:
            log.error(
                "ACL of %s unexpectedly contains '%s' (matched entry: '%s'). "
                "Current ACL: %s",
                name,
                unexpected_ace,
                matched,
                acl,
            )
        return matched is None

    def verify_acl_exact(self, name, expected_aces):
        """
        Verify the ACL matches the *expected_aces* list exactly
        (order-insensitive comparison).
        """
        acl = self.get_acl(name)
        if set(acl) == set(expected_aces):
            log.info("ACL of %s matches expected entries exactly", name)
            return True
        log.error(
            "ACL mismatch for %s. Expected: %s, Got: %s", name, expected_aces, acl
        )
        return False

    def verify_ace_count(self, name, expected_count):
        """Verify total number of ACE entries."""
        acl = self.get_acl(name)
        actual = len(acl)
        if actual == expected_count:
            log.info("ACE count for %s is %d as expected", name, expected_count)
            return True
        log.error(
            "ACE count mismatch for %s. Expected: %d, Got: %d",
            name,
            expected_count,
            actual,
        )
        return False

    def verify_access(self, username, file_path, operation="read", expect_success=True):
        """
        Verify a user can or cannot perform an operation on a file.

        operation: "read" | "write" | "append" | "delete"
        expect_success: True if the operation should succeed, False if it should fail.
        Returns True if result matches expectation.
        """
        full_path = self._full_path(file_path)
        if operation == "read":
            cmd = f"cat {full_path}"
        elif operation == "write":
            # Use dd instead of `echo > file`: bash does not propagate the
            # deferred NFS close-fd error back to stderr/exit-code for shell
            # output-redirections, so a silently-failed NFS write looks like
            # success.  dd explicitly reports close() errors to stderr as
            # "closing output file: Operation not permitted".
            cmd = f"bash -c 'printf test_data | dd of={full_path} conv=notrunc 2>&1'"
        elif operation == "append":
            cmd = f"bash -c 'printf test_data | dd of={full_path} oflag=append conv=notrunc 2>&1'"
        elif operation == "delete":
            cmd = f"rm {full_path}"
        else:
            raise ValueError(f"Unsupported operation: {operation}")

        out, err = self.run_as_user(username, cmd, check_ec=False)
        # region agent log - H-A post-fix: verify write actually lands on disk
        if operation in ("write", "append"):
            try:
                _sz_out, _ = self.client.exec_command(
                    sudo=True,
                    cmd=f"stat -c '%s' {full_path}",
                    check_ec=False,
                )
                log.info(
                    "[DBG-write-verify] op=%s user=%s path=%s "
                    "cmd_stdout=%r cmd_stderr=%r file_bytes_after=%s",
                    operation, username, full_path,
                    out.strip() if out else "",
                    err.strip() if err else "",
                    _sz_out.strip() if _sz_out else "unknown",
                )
            except Exception as _wv_ex:
                log.warning("[DBG-write-verify] stat check failed: %s", _wv_ex)
        # endregion
        # If err contains "Permission denied" or similar, the operation failed
        operation_succeeded = not (
            (out and (
                "denied" in out.lower()
                or "permission" in out.lower()
                or "not permitted" in out.lower()
            ))
            or (err and (
                "denied" in err.lower()
                or "permission" in err.lower()
                or "not permitted" in err.lower()
                or "cannot" in err.lower()
                or "no such" in err.lower()
            ))
        )
        if expect_success and operation_succeeded:
            log.info(
                "User %s successfully performed '%s' on %s (as expected)",
                username,
                operation,
                file_path,
            )
            return True
        elif not expect_success and not operation_succeeded:
            log.info(
                "User %s was denied '%s' on %s (as expected)",
                username,
                operation,
                file_path,
            )
            return True
        else:
            log.error(
                "Unexpected result for user %s operation '%s' on %s. "
                "Expected success=%s. stdout='%s' stderr='%s'",
                username,
                operation,
                file_path,
                expect_success,
                out,
                err,
            )
            return False

    # -- NFS service helpers --------------------------------------------------

    @staticmethod
    def restart_nfs_service(client, nfs_name):
        """Restart the NFS-Ganesha service via ceph orch."""
        log.info("Restarting NFS service %s", nfs_name)
        client.exec_command(
            sudo=True,
            cmd=f"ceph orch restart nfs.{nfs_name}",
        )
        sleep(15)
        log.info("NFS service %s restarted, waited 15s for stabilisation", nfs_name)

    @staticmethod
    def restart_upstream_nfs_service(server_node):
        """Restart NFS services on a Spectrum Scale / Ganesha server (installer node).

        basic-storage-scale.sh typically leaves nfs-ganesha active; fall back to other units.
        """
        log.info(
            "Restarting upstream NFS stack on %s", getattr(server_node, "hostname", server_node)
        )
        script = (
            "for u in nfs-ganesha ganesha nfs-server nfs-kernel-server; do "
            "if systemctl is-active --quiet \"$u\" 2>/dev/null; then "
            "systemctl restart \"$u\" && exit 0; fi; done; exit 0"
        )
        server_node.exec_command(sudo=True, cmd=script, check_ec=False, long_running=True)
        sleep(15)
        log.info(
            "Upstream NFS restart finished on %s, waited 15s for stabilisation",
            getattr(server_node, "hostname", server_node),
        )

    @staticmethod
    def remount_export(
        client,
        mount_point,
        nfs_server,
        export,
        version="4.1",
        port="2049",
        mount_type="nfs",
        remount_chown=None,
    ):
        """
        Unmount and remount an NFS export.

        mount_type: "nfs" (standard NFS), "kernel" (kernel NFS), "fuse" (ceph-fuse)
        remount_chown: optional \"user:group\" for chown on mount_point after mount;
            None skips chown (use for GPFS upstream clients without cephuser).
        """
        log.info(
            "Remounting %s on %s (mount_type=%s)", export, client.hostname, mount_type
        )
        client.exec_command(sudo=True, cmd=f"umount -f {mount_point}", check_ec=False)
        sleep(3)

        _, err = client.exec_command(
            sudo=True,
            cmd=f"mountpoint -q {mount_point}",
            check_ec=False,
        )

        if err:
            log.warning(
                "%s is still mounted after umount -f, retrying with lazy umount",
                mount_point,
            )
            client.exec_command(
                sudo=True,
                cmd=f"umount -l {mount_point}",
                check_ec=False,
            )
            sleep(3)
        log.info("Confirmed %s is unmounted on %s", mount_point, client.hostname)

        if mount_type == "fuse":
            from cli.utilities.filesys import FuseMount

            FuseMount(client).mount(
                client_hostname=client.hostname, mount_point=mount_point
            )
        else:
            client.exec_command(
                sudo=True,
                cmd=(
                    f"mount -t nfs -o vers={version},port={port} "
                    f"{nfs_server}:{export} {mount_point}"
                ),
            )
        if remount_chown:
            client.exec_command(sudo=True, cmd=f"chown {remount_chown} {mount_point}")
        log.info("Remount of %s completed on %s", export, client.hostname)

    # -- logging helpers ------------------------------------------------------

    @staticmethod
    def log_test_start(test_name):
        """Print a prominent banner marking the start of a sub-test."""
        banner = "\n" + "#" * 70 + "\n" + f"###  TEST START : {test_name}\n" + "#" * 70
        log.info(banner)

    @staticmethod
    def log_test_end(test_name, passed):
        """Print a prominent banner marking the end of a sub-test."""
        status = "PASSED" if passed else "FAILED"
        banner = (
            "\n"
            + "-" * 70
            + "\n"
            + f"###  TEST END   : {test_name} -> {status}\n"
            + "-" * 70
        )
        if passed:
            log.info(banner)
        else:
            log.error(banner)

    # -- bulk ACL generation --------------------------------------------------

    def generate_acl_file(self, dest_path, uid_start, uid_end, permission="r"):
        """
        Generate an ACL spec file with one ACE per UID in [uid_start, uid_end].

        For GPFS / Spectrum Scale over NFS, use :meth:`generate_gpfs_bulk_acl_file`
        instead: bare named-user lists are rejected by ``nfs4_setfacl -S`` unless
        OWNER@ / GROUP@ / EVERYONE@ (and explicit denies for verifier UIDs) are present.
        """
        log.info(
            "Generating ACL spec file %s for UIDs %d-%d",
            dest_path,
            uid_start,
            uid_end,
        )
        cmd = (
            f"seq {uid_start} {uid_end} | "
            f'awk \'{{print "A::"$1":{permission}"}}\' > {dest_path}'
        )
        self.client.exec_command(sudo=True, cmd=cmd)

    def generate_gpfs_bulk_acl_file(
        self,
        dest_path,
        uid_start,
        uid_end,
        named_perm="rxtcy",
        deny_uids=(4000, 5000),
        relative_entry="f1",
        include_file_directive=True,
    ):
        """
        Write an ``nfs4_setfacl -S`` spec that GPFS/Ganesha accepts for large ACL tests.

        If *include_file_directive* is true, the first line is ``# file: <nfs path>``
        (same form as ``nfs4_getfacl``).  Some stacks accept only ACE lines when the
        target path is already given on the ``nfs4_setfacl`` command line; set false
        to omit that line.

        Inserts ``A::OWNER@``, ``D::<uid>:rwatcy`` for each *deny_uids* (so those
        users are not granted read via ``EVERYONE@``), ``A::GROUP@:rtncy``,
        ``A::EVERYONE@:rtncy``, then one ``A::<uid>:<named_perm>`` per integer in
        ``[uid_start, uid_end]``.  *named_perm* should be an expanded NFSv4 mask
        (e.g. ``rxtcy``), not a short alias, for reliable ``-S`` applies.
        """
        log.info(
            "Generating GPFS bulk ACL file %s for UIDs %d-%d (deny_uids=%s, "
            "include_file_directive=%s)",
            dest_path,
            uid_start,
            uid_end,
            deny_uids,
            include_file_directive,
        )
        file_line = self._full_path(relative_entry)
        deny_q = " ".join(f"'D::{uid}:rwatcy'" for uid in deny_uids)
        if include_file_directive:
            header = (
                f"printf '%s\\n' '# file: {file_line}' 'A::OWNER@:rwatcy' {deny_q} "
                f"'A::GROUP@:rtncy' 'A::EVERYONE@:rtncy' > {dest_path}"
            )
        else:
            header = (
                f"printf '%s\\n' 'A::OWNER@:rwatcy' {deny_q} "
                f"'A::GROUP@:rtncy' 'A::EVERYONE@:rtncy' > {dest_path}"
            )
        cmd = (
            f"{header} && "
            f"seq {uid_start} {uid_end} | "
            f"awk -v p='{named_perm}' '{{print \"A::\"$1\":\"p}}' >> {dest_path}"
        )
        self.client.exec_command(sudo=True, cmd=cmd)

    @staticmethod
    def provision_passwd_uid_range(
        client, uid_lo, uid_hi, server_node=None, batch_size=128
    ):
        """
        Ensure every UID in ``[uid_lo, uid_hi]`` exists in ``/etc/passwd`` on the
        client and (if given) the GPFS installer node.

        Spectrum Scale resolves named-user NFSv4 ACEs against the server's passwd
        database; without entries, ``nfs4_setfacl -S`` often returns EINVAL.

        Uses ``useradd -M -u <uid> nfstest<uid>`` only when ``id -u <uid>`` has no
        mapping, so existing test users (e.g. ``u1500``) are left unchanged.
        """
        nodes = [client]
        if server_node is not None:
            nodes.append(server_node)
        for node in nodes:
            log.info(
                "Provisioning passwd UIDs %d-%d on %s (batch=%d)",
                uid_lo,
                uid_hi,
                node.hostname,
                batch_size,
            )
            for start in range(uid_lo, uid_hi + 1, batch_size):
                end = min(start + batch_size - 1, uid_hi)
                cmd = (
                    f"for u in $(seq {start} {end}); do "
                    f"if ! id -u \"$u\" &>/dev/null; then "
                    f"useradd -M -u \"$u\" \"nfstest$u\" 2>/dev/null || true; "
                    f"fi; done"
                )
                node.exec_command(
                    sudo=True,
                    cmd=cmd,
                    long_running=True,
                    check_ec=False,
                    timeout=900,
                )

    @staticmethod
    def cleanup_nfstest_passwd_uid_range(
        client, uid_lo, uid_hi, server_node=None, batch_size=256
    ):
        """Remove ``nfstest<uid>`` users created by :meth:`provision_passwd_uid_range`."""
        nodes = [client]
        if server_node is not None:
            nodes.append(server_node)
        for node in nodes:
            for start in range(uid_lo, uid_hi + 1, batch_size):
                end = min(start + batch_size - 1, uid_hi)
                cmd = (
                    f"for u in $(seq {start} {end}); do "
                    f"if id nfstest$u &>/dev/null; then userdel -rf nfstest$u 2>/dev/null || true; "
                    f"fi; done"
                )
                node.exec_command(
                    sudo=True,
                    cmd=cmd,
                    long_running=True,
                    check_ec=False,
                    timeout=600,
                )

    def timed_getfacl(self, name):
        """Run ``time nfs4_getfacl`` and return (acl_output, real_time_seconds)."""
        path = self._full_path(name)
        log.info("Timed nfs4_getfacl on %s", path)
        out, err = self.client.exec_command(
            sudo=True,
            cmd=f"{{ time nfs4_getfacl {path} ; }} 2>&1",
        )
        time_sec = None
        for line in out.splitlines():
            if line.startswith("real"):
                parts = line.split()
                if len(parts) >= 2:
                    t = parts[1]
                    if "m" in t:
                        mins, secs = t.split("m")
                        secs = secs.rstrip("s")
                        time_sec = float(mins) * 60 + float(secs)
                    else:
                        time_sec = float(t.rstrip("s"))
        log.info("nfs4_getfacl completed in %s seconds", time_sec)
        return out, time_sec

    # -- cleanup helper -------------------------------------------------------

    def cleanup_users_groups(self, users=None, groups=None):
        """Remove test users and groups created during ACL tests."""
        for user in users or []:
            self.delete_user(self.client, user)
        for group in groups or []:
            self.delete_group(self.client, group)

    def cleanup_test_files(self, *names):
        """Remove test files/dirs under the mount point."""
        for name in names:
            self.client.exec_command(
                sudo=True,
                cmd=f"rm -rf {self._full_path(name)}",
                check_ec=False,
            )
