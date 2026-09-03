"""
Validate monitor auth cipher policy is preserved across cephadm upgrade.

IBMCEPH-17962: upgrade must not add legacy ``aes`` to ``auth_allowed_ciphers``
when the cluster was configured with ``aes256k`` only. If ``aes`` was already
allowed before upgrade, it may remain; the policy must not change otherwise.
"""

import json
from json import loads

from ceph.ceph import CommandFailed
from cli.exceptions import OperationFailedError
from utility.log import Log

log = Log(__name__)

DEFAULT_BASELINE_FILE = "/home/cephuser/mon_auth_cipher_baseline.json"
INSECURE_AUTH_HEALTH_CODES = (
    "AUTH_INSECURE_KEYS_ALLOWED",
    "AUTH_INSECURE_KEYS_CREATABLE",
)
LEGACY_AES_CIPHER = "aes"


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


def _run_shell(installer, cmd):
    out, err = installer.exec_command(sudo=True, cmd=f"cephadm shell -- {cmd}")
    if err:
        log.warning("ceph command stderr: %s", err)
    return out


def _get_mon_auth_policy(installer):
    """Collect auth cipher fields from ``ceph mon dump --format json``."""
    mon_dump = loads(_run_shell(installer, "ceph mon dump --format json"))
    policy = {
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


def _assert_no_new_legacy_aes(baseline, current):
    before = set(baseline.get("auth_allowed_ciphers", []))
    after = set(current.get("auth_allowed_ciphers", []))
    if LEGACY_AES_CIPHER not in before and LEGACY_AES_CIPHER in after:
        raise OperationFailedError(
            "Upgrade added legacy 'aes' cipher to auth_allowed_ciphers "
            f"(IBMCEPH-17962). before={sorted(before)}, after={sorted(after)}"
        )


def _assert_health_if_secure_baseline(installer, baseline):
    if LEGACY_AES_CIPHER in baseline.get("auth_allowed_ciphers", []):
        log.info(
            "Baseline already allows legacy aes; skipping insecure-auth health check"
        )
        return

    health_detail = _run_shell(installer, "ceph health detail")
    for code in INSECURE_AUTH_HEALTH_CODES:
        if code in health_detail:
            raise OperationFailedError(
                f"Unexpected {code} after upgrade with aes256k-only baseline "
                f"(IBMCEPH-17962). health detail:\n{health_detail}"
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
        policy = _get_mon_auth_policy(installer)
        _write_baseline(installer, state_file, policy)
        log.info("Captured monitor auth cipher baseline before upgrade")
        return 0

    if phase != "validate":
        raise OperationFailedError(f"Unsupported phase: {phase}")

    baseline = _read_baseline(installer, state_file)
    current = _get_mon_auth_policy(installer)
    _assert_policy_unchanged(baseline, current)
    _assert_no_new_legacy_aes(baseline, current)
    _assert_health_if_secure_baseline(installer, baseline)
    log.info("Monitor auth cipher policy preserved after upgrade")
    return 0
