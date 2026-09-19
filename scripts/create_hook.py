"""Create a CurveHook (and its Uniswap v4 pool) for a Curve pool.

    uv run python scripts/create_hook.py POOL               # coins 0 and 1, dry run on a fork
    uv run python scripts/create_hook.py POOL --coins 1 2   # another pair of a 3+ coin pool
    uv run python scripts/create_hook.py POOL --live        # real transaction from the KEYSTORE account
    uv run python scripts/create_hook.py POOL --network robinhood --live

Uses the network's factory in deployments.json; a dry run without one deploys a fresh factory on the fork.
"""
import argparse

import boa

import chains
import hooks


def describe(kind):
    return ", ".join([
        "uint256 indices" if kind & hooks.KIND_CRYPTO else "int128 indices",
        "exchange_received" if kind & hooks.KIND_RECEIVED else "classic exchange",
        "get_dx" if kind & hooks.KIND_GET_DX else "no get_dx",
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pool", help="Curve pool address")
    parser.add_argument("--coins", type=int, nargs=2, default=(0, 1), metavar=("I", "J"), help="Curve coin indices")
    parser.add_argument("--network", default="ethereum", choices=sorted(chains.BY_NAME), help="default: ethereum")
    parser.add_argument("--rpc", help="another RPC for the network's chain, e.g. an anvil fork")
    parser.add_argument("--live", action="store_true", help="send the transaction")
    parser.add_argument("--keystore", type=hooks.Path, default=hooks.KEYSTORE,
                        help=f"sender keystore (default: {hooks.KEYSTORE})")
    opts = parser.parse_args()
    rpc, chain = chains.resolve(opts.network, opts.rpc)

    deployment = chains.load_deployment(chain.chain_id)
    if opts.live:
        if "factory" not in deployment:
            parser.error(f"no {chain.name} factory in deployments.json: run scripts/deploy.py --live first")
        hooks.use_network(rpc)
        boa.env.add_account(hooks.load_account(opts.keystore), force_eoa=True)
    else:
        hooks.use_fork(rpc)

    if "factory" in deployment:
        factory = hooks.contract(hooks.FACTORY_SOURCE).at(deployment["factory"])
    else:
        _, factory = hooks.deploy_factory(boa.env.eoa, chain)
        print(f"deployed a fresh factory on the {chain.name} fork: {factory.address}")

    i, j = opts.coins
    existing = factory.get_hook(opts.pool, i, j)
    if existing != "0x0000000000000000000000000000000000000000":
        print(f"hook already exists: {existing}")
        return

    hook = hooks.create_hook(factory, opts.pool, i, j)
    key = factory.pool_key(hook.address)
    print(f"hook:    {hook.address}  ({describe(hook.kind())})")
    print(f"curve:   {hook.curve_pool()}  coins {hook.coin_indices()}")
    print(f"pool id: {hooks.pool_id(key)}")
    print(f"key:     {tuple(key)}")
    if not opts.live:
        print("dry run on a fork, nothing sent")


if __name__ == "__main__":
    main()
