"""Compatibility helpers for Web3/eth-account version differences."""


def encode_contract_call(contract, fn_name, args):
    """Encode contract calldata across web3.py versions."""
    contract_functions = getattr(contract, "functions", None)
    function_factory = getattr(contract_functions, fn_name, None)
    if function_factory is not None:
        contract_function = function_factory(*args)
        encoder = getattr(contract_function, "_encode_transaction_data", None)
        if encoder is not None:
            return encoder()

    encode_abi = getattr(contract, "encodeABI", None)
    if encode_abi is not None:
        return encode_abi(fn_name=fn_name, args=args)

    encode_abi = getattr(contract, "encode_abi", None)
    if encode_abi is not None:
        try:
            return encode_abi(fn_name, args=args)
        except TypeError:
            return encode_abi(abi_element_identifier=fn_name, args=args)

    raise AttributeError(
        f"Contract cannot encode function call {fn_name!r}; unsupported web3.py API"
    )


def get_raw_transaction(signed_transaction):
    """Return raw signed transaction bytes across eth-account versions."""
    raw_transaction = getattr(signed_transaction, "raw_transaction", None)
    if raw_transaction is not None:
        return raw_transaction

    raw_transaction = getattr(signed_transaction, "rawTransaction", None)
    if raw_transaction is not None:
        return raw_transaction

    raise AttributeError(
        "Signed transaction has neither raw_transaction nor rawTransaction"
    )
