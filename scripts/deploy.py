"""Deploy the CurveHook implementation and CurveHookFactory.

    uv run python scripts/deploy.py                         # dry run on a fork of Ethereum
    uv run python scripts/deploy.py --network robinhood     # dry run on a fork of Robinhood Chain
    uv run python scripts/deploy.py --network robinhood --live
    uv run python scripts/deploy.py --live --rpc http://127.0.0.1:8545 --no-verify   # e.g. anvil

A live deployment to a --network is recorded in deployments.json and verified on Etherscan
(scripts/verify.py retries a verification that did not go through). With --rpc it is neither:
an anvil fork reports the chain id it forks and would overwrite that chain's entry.
"""
import argparse

import boa
from eth_abi import encode

import chains
import hooks


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", default="ethereum", choices=sorted(chains.BY_NAME), help="default: ethereum")
    parser.add_argument("--rpc", help="another RPC for the network's chain, e.g. an anvil fork")
    parser.add_argument("--live", action="store_true", help="send real transactions")
    parser.add_argument("--no-verify", action="store_true", help="skip Etherscan verification")
    parser.add_argument("--keystore", type=hooks.Path, default=hooks.KEYSTORE,
                        help=f"deployer keystore (default: {hooks.KEYSTORE})")
    opts = parser.parse_args()
    rpc, chain = chains.resolve(opts.network, opts.rpc)

    if opts.live:
        hooks.use_network(rpc)
        boa.env.add_account(hooks.load_account(opts.keystore), force_eoa=True)
    else:
        hooks.use_fork(rpc)

    admin = boa.env.eoa
    implementation, factory = hooks.deploy_factory(admin, chain)
    print(f"{chain.name} (chain id {chain.chain_id})")
    print(f"implementation: {implementation.address}")
    print(f"factory:        {factory.address}  (admin {admin})")

    if not opts.live:
        print("dry run on a fork, nothing sent")
        return
    if opts.rpc:
        print("--rpc given: not recorded in deployments.json, not verified")
        return
    hooks.save_deployment(chain.chain_id, implementation=implementation.address, factory=factory.address)
    if not opts.no_verify:
        hooks.verify_etherscan(implementation.address, hooks.HOOK_SOURCE, "CurveHook",
                               encode(["address"], [chain.pool_manager]), chain.chain_id)
        hooks.verify_etherscan(factory.address, hooks.FACTORY_SOURCE, "CurveHookFactory", encode(
            ["address"] * 5, hooks.factory_ctor_args(chain, implementation.address, str(admin))), chain.chain_id)


if __name__ == "__main__":
    main()
