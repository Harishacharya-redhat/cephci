"""
Validate monitor auth cipher policy is preserved across cephadm upgrade.

IBMCEPH-17962: monitor ``auth_allowed_ciphers`` must not change across upgrade.
If legacy ``aes`` was allowed before upgrade it must remain; if it was absent it
must not be introduced. Any AUTH_INSECURE health warning fails validation.
"""

import json
from json import loads

from ceph.ceph import CommandFailed
from cli.exceptions import OperationFailedError
from utility.log import Log

log = Log(__name__)

DEFAULT_BASELINE_FILE = "/home/cephuser/mon_auth_cipher_baseline.json"
LEGACY_AES_CIPHER = "aes"
INSECURE_AUTH_HEALTH_MARKERS = (
    "AUTH_INSECURE_KEYS_ALLOWED",
    "AUTH_INSECURE_KEYS_CREATABLE",
    "AUTH_INSECURE_SERVICE_KEYS_ALLOWED",
    "AUTH_INSECURE_SERVICE_TICKETS",
    "AUTH_INSECURE_CLIENT_KEY_TYPE",
    "AUTH_INSECURE_SERVICE_KEY_TYPE",
    "insecure cipher",
)


def _normalize_cipher_token(value):
    """Return a cipher name from mon dump JSON (string or {name: ...} object)."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("name") or value.get("cipher")
    token = str(value).strip()
    return token or None


def _normalize_cipher_list(value):
    """Return a sorted list of cipher tokens from mon dump output."""
    if value is None:
        return []
    if isinstance(value, list):
        tokens = [_normalize_cipher_token(item) for item in value]
    else:
        tokens = [_normalize_cipher_token(item) for item in str(value).replace(",", " ").split()]
    return sorted({token for token in tokens if token})


def _policy_from_mon_dump_json(mon_dump):
    """Build normalized policy dict from ``ceph mon dump --format json``."""
    return {
        "auth_service_cipher": _normalize_cipher_token(
            mon_dump.get("auth_service_cipher")
        ),
        "auth_allowed_ciphers": _normalize_cipher_list(
            mon_dump.get("auth_allowed_ciphers")
        ),
        "auth_preferred_cipher": _normalize_cipher_token(
            mon_dump.get("auth_preferred_cipher")
        ),
    }


def _policy_from_mon_dump_text(mon_dump_text):
    """Parse cipher fields from plain ``ceph mon dump`` output."""
    policy = {
        "auth_service_cipher": None,
        "auth_allowed_ciphers": [],
        "auth_preferred_cipher": None,
    }
    for line in mon_dump_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("auth_service_cipher "):
            policy["auth_service_cipher"] = _normalize_cipher_token(
                stripped.split(None, 1)[1]
            )
        elif stripped.startswith("auth_allowed_ciphers "):
            policy["auth_allowed_ciphers"] = _normalize_cipher_list(
                stripped.split(None, 1)[1]
            )
        elif stripped.startswith("auth_preferred_cipher "):
            policy["auth_preferred_cipher"] = _normalize_cipher_token(
                stripped.split(None, 1)[1]
            )
    return policy


def _policy_is_empty(policy):
    return not any(
        (
            policy.get("auth_service_cipher"),
            policy.get("auth_allowed_ciphers"),
            policy.get("auth_preferred_cipher"),
        )
    )


def _run_shell(installer, cmd):
    out, err = installer.exec_command(sudo=True, cmd=f"cephadm shell -- {cmd}")
    if err:
        log.warning("ceph command stderr: %s", err)
    return out


def _read_mon_auth_policy(installer):
    """Read policy from mon dump JSON, falling back to plain text output."""
    mon_dump = loads(_run_shell(installer, "ceph mon dump --format json"))
    policy = _policy_from_mon_dump_json(mon_dump)
    if _policy_is_empty(policy):
        log.warning(
            "Monitor auth cipher fields missing from mon dump JSON; "
            "falling back to plain mon dump output"
        )
        text_dump = _run_shell(installer, "ceph mon dump")
        policy = _policy_from_mon_dump_text(text_dump)
    return policy


def _get_mon_auth_policy(installer, require_readable=False):
    """Collect auth cipher fields from ``ceph mon dump``."""
    policy = _read_mon_auth_policy(installer)
    if require_readable and _policy_is_empty(policy):
        raise OperationFailedError(
            "Unable to read monitor auth cipher policy from mon dump "
            "(IBMCEPH-17962). Ensure the cluster exposes auth_service_cipher, "
            "auth_allowed_ciphers, and auth_preferred_cipher."
        )
    log.info("Monitor auth cipher policy: %s", policy)
    return policy


def _write_baseline(installer, state_file, policy):
    remote_file = installer.remote_file(sudo=True, file_name=state_file, file_mode="w")
    remote_file.write(json.dumps(policy, indent=4))
    remote_file.write("\n")
    remote_file.flush()
    log.info("Saved monitor auth cipher baseline to %s", state_file)


def _read_baseline(installer, state_file):
    try:
        out, _ = installer.exec_command(sudo=True, cmd=f"cat {state_file}")
    except CommandFailed as exc:
        raise OperationFailedError(
            f"Missing baseline file {state_file}. Run capture phase before upgrade."
        ) from exc
    baseline = json.loads(out)
    baseline["auth_service_cipher"] = _normalize_cipher_token(
        baseline.get("auth_service_cipher")
    )
    baseline["auth_allowed_ciphers"] = _normalize_cipher_list(
        baseline.get("auth_allowed_ciphers")
    )
    baseline["auth_preferred_cipher"] = _normalize_cipher_token(
        baseline.get("auth_preferred_cipher")
    )
    return baseline


def _assert_policy_unchanged(baseline, current):
    for field in (
        "auth_service_cipher",
        "auth_allowed_ciphers",
        "auth_preferred_cipher",
    ):
        if baseline.get(field) != current.get(field):
            raise OperationFailedError(
                "Monitor auth cipher policy changed across upgrade "
                f"(IBMCEPH-17962). Field '{field}': "
                f"before={baseline.get(field)!r}, after={current.get(field)!r}"
            )


def _assert_legacy_aes_unchanged(baseline, current):
    """Legacy aes must be preserved: present before iff present after."""
    before = set(baseline.get("auth_allowed_ciphers", []))
    after = set(current.get("auth_allowed_ciphers", []))
    had_aes = LEGACY_AES_CIPHER in before
    has_aes = LEGACY_AES_CIPHER in after
    if had_aes and not has_aes:
        raise OperationFailedError(
            "Upgrade removed legacy 'aes' from auth_allowed_ciphers "
            f"(IBMCEPH-17962). before={sorted(before)}, after={sorted(after)}"
        )
    if not had_aes and has_aes:
        raise OperationFailedError(
            "Upgrade added legacy 'aes' to auth_allowed_ciphers "
            f"(IBMCEPH-17962). before={sorted(before)}, after={sorted(after)}"
        )


def _assert_no_insecure_auth_warnings(installer):
    """Fail on any insecure-auth health warning after upgrade."""
    health_detail = _run_shell(installer, "ceph health detail")
    matches = []
    for line in health_detail.splitlines():
        if any(marker in line for marker in INSECURE_AUTH_HEALTH_MARKERS):
            matches.append(line.strip())
    if matches:
        raise OperationFailedError(
            "Insecure auth cipher health warning(s) after upgrade "
            f"(IBMCEPH-17962):\n" + "\n".join(matches)
        )


def run(ceph_cluster, **kw):
    """
    Capture or validate monitor auth cipher policy around upgrade.

    Config:
        phase (str): ``capture`` before upgrade, ``validate`` after upgrade.
        state_file (str): JSON baseline path on installer (default below).
    """
    config = kw.get("config", {})
    phase = config.get("phase", "validate")
    state_file = config.get("state_file", DEFAULT_BASELINE_FILE)
    installer = ceph_cluster.get_nodes(role="installer")[0]

    if phase == "capture":
        policy = _read_mon_auth_policy(installer)
        policy["mon_auth_fields_readable"] = not _policy_is_empty(policy)
        if _policy_is_empty(policy):
            log.warning(
                "Mon auth cipher fields not exposed in mon dump (e.g. pre-tentacle); "
                "recording empty baseline — legacy aes treated as absent"
            )
        _write_baseline(installer, state_file, policy)
        log.info("Captured monitor auth cipher baseline before upgrade")
        return 0

    if phase != "validate":
        raise OperationFailedError(f"Unsupported phase: {phase}")

    baseline = _read_baseline(installer, state_file)
    current = _get_mon_auth_policy(installer, require_readable=True)

    if baseline.get("mon_auth_fields_readable"):
        _assert_policy_unchanged(baseline, current)
    else:
        log.info(
            "Pre-upgrade mon auth fields were unreadable; "
            "validating legacy aes preservation and health only"
        )

    _assert_legacy_aes_unchanged(baseline, current)
    _assert_no_insecure_auth_warnings(installer)
    log.info("Monitor auth cipher policy preserved after upgrade")
    return 0
