from types import SimpleNamespace

import pytest

from utils.web3_compat import get_raw_transaction


def test_get_raw_transaction_prefers_snake_case():
    signed = SimpleNamespace(raw_transaction=b"new", rawTransaction=b"old")

    assert get_raw_transaction(signed) == b"new"


def test_get_raw_transaction_accepts_camel_case():
    signed = SimpleNamespace(rawTransaction=b"old")

    assert get_raw_transaction(signed) == b"old"


def test_get_raw_transaction_requires_known_attribute():
    with pytest.raises(AttributeError):
        get_raw_transaction(SimpleNamespace())
