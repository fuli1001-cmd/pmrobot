"""Compatibility helpers for Web3/eth-account version differences."""


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
