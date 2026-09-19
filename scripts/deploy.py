"""Deploy the CurveHook implementation and CurveHookFactory.

    uv run python scripts/deploy.py           # dry run on a fork of NETWORK
    uv run python scripts/deploy.py --live    # real deployment from the KEYSTORE account

A live deployment is recorded in deployments.json and verified on Etherscan
(scripts/verify.py retries a verification that did not go through).
"""
import argparse

import boa
from eth_abi import encode

import hooks
from networks import NETWORK


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="send real transactions to --rpc")
    parser.add_argument("--no-verify", action="store_true", help="skip Etherscan verification")
    parser.add_argument("--rpc", default=NETWORK, help="RPC url (default: NETWORK from networks.py)")
    parser.add_argument("--keystore", type=hooks.Path, default=hooks.KEYSTORE,
                        help=f"deployer keystore (default: {hooks.KEYSTORE})")
    opts = parser.parse_args()

    if opts.live:
        boa.set_network_env(opts.rpc)
        boa.env.add_account(hooks.load_account(opts.keystore), force_eoa=True)
    else:
        boa.fork(opts.rpc, block_identifier="latest")

    admin = boa.env.eoa
    implementation, factory = hooks.deploy_factory(admin)
    print(f"implementation: {implementation.address}")
    print(f"factory:        {factory.address}  (admin {admin})")

    if not opts.live:
        print("dry run on a fork, nothing sent")
        return
    if opts.rpc != NETWORK:
        # e.g. an anvil fork: it reports mainnet's chain id and would overwrite the real entry
        print("--rpc is not NETWORK: not recorded in deployments.json, not verified")
        return
    hooks.save_deployment(hooks.chain_id(opts.rpc), implementation=implementation.address, factory=factory.address)
    if not opts.no_verify:
        hooks.verify_etherscan(implementation.address, hooks.HOOK_SOURCE, "CurveHook", encode(["address"], [hooks.POOL_MANAGER]))
        hooks.verify_etherscan(factory.address, hooks.FACTORY_SOURCE, "CurveHookFactory", encode(
            ["address", "address", "address", "address", "address"],
            [hooks.POOL_MANAGER, implementation.address, hooks.STABLESWAP_NG_FACTORY, hooks.TWOCRYPTO_NG_FACTORY,
             str(admin)],
        ))


if __name__ == "__main__":
    main()
