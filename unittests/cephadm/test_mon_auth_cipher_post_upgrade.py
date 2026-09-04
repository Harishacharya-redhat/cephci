import pytest

from cli.exceptions import OperationFailedError
from tests.cephadm.test_mon_auth_cipher_post_upgrade import (
    _assert_legacy_aes_unchanged,
    _normalize_cipher_list,
    _normalize_cipher_token,
    _policy_from_mon_dump_json,
    _policy_from_mon_dump_text,
    _policy_is_empty,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("aes256k", "aes256k"),
        ({"name": "aes256k", "value": 2}, "aes256k"),
        ({"cipher": "aes"}, "aes"),
        (None, None),
        ("  ", None),
    ],
)
def test_normalize_cipher_token(value, expected):
    assert _normalize_cipher_token(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ([{"name": "aes256k", "value": 2}], ["aes256k"]),
        ([{"name": "aes", "value": 1}, {"name": "aes256k", "value": 2}], ["aes", "aes256k"]),
        ("aes, aes256k", ["aes", "aes256k"]),
        (None, []),
    ],
)
def test_normalize_cipher_list(value, expected):
    assert _normalize_cipher_list(value) == expected


def test_policy_from_mon_dump_json_tentacle_format():
    mon_dump = {
        "auth_service_cipher": {"name": "aes256k", "value": 2},
        "auth_allowed_ciphers": [
            {"name": "aes256k", "value": 2},
        ],
        "auth_preferred_cipher": {"name": "aes256k", "value": 2},
    }
    assert _policy_from_mon_dump_json(mon_dump) == {
        "auth_service_cipher": "aes256k",
        "auth_allowed_ciphers": ["aes256k"],
        "auth_preferred_cipher": "aes256k",
    }


def test_policy_from_mon_dump_text():
    mon_dump_text = """
epoch 3
fsid a0283f18-a777-11f1-ab9f-0201059f49b7
auth_epoch 0
auth_service_cipher aes256k
auth_allowed_ciphers aes, aes256k
auth_preferred_cipher aes256k
0: [v2:10.0.0.1:3300/0,v1:10.0.0.1:6789/0] mon.a
"""
    assert _policy_from_mon_dump_text(mon_dump_text) == {
        "auth_service_cipher": "aes256k",
        "auth_allowed_ciphers": ["aes", "aes256k"],
        "auth_preferred_cipher": "aes256k",
    }


def test_policy_is_empty():
    assert _policy_is_empty(
        {
            "auth_service_cipher": None,
            "auth_allowed_ciphers": [],
            "auth_preferred_cipher": None,
        }
    )
    assert not _policy_is_empty(
        {
            "auth_service_cipher": None,
            "auth_allowed_ciphers": ["aes256k"],
            "auth_preferred_cipher": None,
        }
    )


def test_legacy_aes_unchanged_rejects_added_aes():
    baseline = {"auth_allowed_ciphers": ["aes256k"]}
    current = {"auth_allowed_ciphers": ["aes", "aes256k"]}
    with pytest.raises(OperationFailedError, match="added legacy 'aes'"):
        _assert_legacy_aes_unchanged(baseline, current)


def test_legacy_aes_unchanged_rejects_removed_aes():
    baseline = {"auth_allowed_ciphers": ["aes", "aes256k"]}
    current = {"auth_allowed_ciphers": ["aes256k"]}
    with pytest.raises(OperationFailedError, match="removed legacy 'aes'"):
        _assert_legacy_aes_unchanged(baseline, current)


def test_legacy_aes_unchanged_allows_stable_policy():
    baseline = {"auth_allowed_ciphers": ["aes256k"]}
    current = {"auth_allowed_ciphers": ["aes256k"]}
    _assert_legacy_aes_unchanged(baseline, current)

    baseline = {"auth_allowed_ciphers": ["aes", "aes256k"]}
    current = {"auth_allowed_ciphers": ["aes", "aes256k"]}
    _assert_legacy_aes_unchanged(baseline, current)
