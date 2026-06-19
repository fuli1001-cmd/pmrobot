from types import SimpleNamespace

import pytest

from utils.web3_compat import encode_contract_call, get_raw_transaction


class _FakeFunction:
    def __init__(self, encoded):
        self._encoded = encoded

    def _encode_transaction_data(self):
        return self._encoded


class _FakeFunctions:
    def approve(self, *args):
        return _FakeFunction(f"function:{args!r}")


class _FakeContract:
    functions = _FakeFunctions()

    def encodeABI(self, *args, **kwargs):
        raise AssertionError("function-level encoder should be preferred")


class _LegacyContract:
    def encodeABI(self, fn_name, args):
        return f"legacy:{fn_name}:{args!r}"


class _SnakeContract:
    def encode_abi(self, abi_element_identifier, args):
        return f"snake:{abi_element_identifier}:{args!r}"


def test_encode_contract_call_prefers_function_encoder():
    assert encode_contract_call(_FakeContract(), "approve", ["spender", 10]) == (
        "function:('spender', 10)"
    )


def test_encode_contract_call_accepts_legacy_encode_abi():
    assert encode_contract_call(_LegacyContract(), "approve", ["spender", 10]) == (
        "legacy:approve:['spender', 10]"
    )


def test_encode_contract_call_accepts_snake_case_encode_abi():
    assert encode_contract_call(_SnakeContract(), "approve", ["spender", 10]) == (
        "snake:approve:['spender', 10]"
    )


def test_get_raw_transaction_prefers_snake_case():
    signed = SimpleNamespace(raw_transaction=b"new", rawTransaction=b"old")

    assert get_raw_transaction(signed) == b"new"


def test_get_raw_transaction_accepts_camel_case():
    signed = SimpleNamespace(rawTransaction=b"old")

    assert get_raw_transaction(signed) == b"old"


def test_get_raw_transaction_requires_known_attribute():
    with pytest.raises(AttributeError):
        get_raw_transaction(SimpleNamespace())
