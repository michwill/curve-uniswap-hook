"""Verify the implementation and factory in deployments.json on Etherscan.

    uv run python scripts/verify.py

Skips what is already verified. Constructor arguments are read from the
creation transactions, so nothing about the deployment has to be repeated.
"""
import hooks
from networks import NETWORK


def main():
    deployment = hooks.load_deployment(hooks.chain_id(NETWORK))
    for key, source, name in [
        ("implementation", hooks.HOOK_SOURCE, "CurveHook"),
        ("factory", hooks.FACTORY_SOURCE, "CurveHookFactory"),
    ]:
        address = deployment.get(key)
        if not address:
            print(f"{name}: no {key} in deployments.json")
        elif hooks.is_verified(address):
            print(f"{name}: {address} already verified")
        else:
            hooks.verify_etherscan(address, source, name, hooks.ctor_args_on_chain(address, source))


if __name__ == "__main__":
    main()
