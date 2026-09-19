"""Verify the implementation and factory in deployments.json on Etherscan.

    uv run python scripts/verify.py                         # Ethereum
    uv run python scripts/verify.py --network robinhood

Skips what is already verified. Constructor arguments are read from the
creation transactions, so nothing about the deployment has to be repeated.
"""
import argparse

import chains
import hooks


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", default="ethereum", choices=sorted(chains.BY_NAME), help="default: ethereum")
    opts = parser.parse_args()
    chain = chains.BY_NAME[opts.network]

    deployment = chains.load_deployment(chain.chain_id)
    for key, source, name in [
        ("implementation", hooks.HOOK_SOURCE, "CurveHook"),
        ("factory", hooks.FACTORY_SOURCE, "CurveHookFactory"),
    ]:
        address = deployment.get(key)
        if not address:
            print(f"{name}: no {key} for {chain.name} in deployments.json")
        elif hooks.is_verified(address, chain.chain_id):
            print(f"{name}: {address} already verified")
        else:
            ctor_args = hooks.ctor_args_on_chain(address, source, chain.chain_id)
            hooks.verify_etherscan(address, source, name, ctor_args, chain.chain_id)


if __name__ == "__main__":
    main()
