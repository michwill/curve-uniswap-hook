"""List hooks and the Curve pools behind them, optionally with every swap.

    uv run python scripts/list_hooks.py                      # hooks of the factory in deployments.json
    uv run python scripts/list_hooks.py --factory FACTORY
    uv run python scripts/list_hooks.py HOOK [HOOK ...]      # just these, e.g. the first, standalone hook
    uv run python scripts/list_hooks.py --txs                # with the swaps through each hook
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_hash.auto import keccak
from eth_utils import to_checksum_address

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch  # noqa: E402
from networks import NETWORK  # noqa: E402

# names pools that have no name() of their own, like 3pool (mainnet)
METAREGISTRY = "0xF98B45FA17DE75FB1aD0e7aFD971b0ca00e379fC"
KIND_CRYPTO, KIND_RECEIVED, KIND_GET_DX = 1, 2, 4
CONFIRMATIONS = 3  # load-balanced nodes lag each other by a block or two


def try_call(rpc, to, signature, output_types, args=()):
    try:
        return rpc.call(to, signature, output_types, args)
    except (RuntimeError, DecodingError):
        return None


def pool_name(rpc, pool):
    name = try_call(rpc, pool, "name()", ["string"]) or try_call(rpc, METAREGISTRY, "get_pool_name(address)", ["string"], [pool])
    return name[0] if name else "?"


def describe(kind):
    if kind is None:
        return "standalone hook"
    return ", ".join([
        "uint256 indices" if kind & KIND_CRYPTO else "int128 indices",
        "exchange_received" if kind & KIND_RECEIVED else "classic exchange",
    ] + (["get_dx"] if kind & KIND_GET_DX else []))


def factory_hooks(rpc, factory):
    count = rpc.call(factory, "hook_count()", ["uint256"])[0]
    return [rpc.call(factory, "hooks(uint256)", ["address"], [n])[0] for n in range(count)]


def swaps_by_hook(rpc, hooks, start):
    end = int(rpc("eth_blockNumber"), 16) - CONFIRMATIONS
    swaps = defaultdict(list)
    for chunk_start in range(start, end + 1, watch.CHUNK):
        for log in watch.get_logs(rpc, hooks, chunk_start, min(chunk_start + watch.CHUNK - 1, end)):
            swaps[to_checksum_address(log["address"])].append(log)
    return swaps


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("hooks", nargs="*", help="hook addresses (default: every hook of the factory)")
    parser.add_argument("--factory", help="CurveHookFactory (default: the one in deployments.json)")
    parser.add_argument("--txs", action="store_true", help="also list the swaps through each hook")
    parser.add_argument("--from-block", type=int, help="where --txs starts (default: factory or hook deployment block)")
    parser.add_argument("--rpc", default=NETWORK, help="RPC url (default: NETWORK from networks.py)")
    opts = parser.parse_args()

    rpc = watch.RPC(opts.rpc)
    # the factory also resolves hooks given by address; hooks it does not know are standalone ones
    factory = to_checksum_address(opts.factory) if opts.factory else watch.default_factory(rpc)
    if not factory and not opts.hooks:
        parser.error("no factory in deployments.json: pass --factory or hook addresses")
    hooks = [to_checksum_address(h) for h in opts.hooks or factory_hooks(rpc, factory)]

    info = watch.Watcher(rpc, 0, factory)  # for the hooks' token pairs, timestamps and formatting
    swaps = {}
    if opts.txs and hooks:
        if opts.from_block is not None:
            start = opts.from_block
        else:
            start = min(watch.creation_block(h) for h in hooks) if opts.hooks else watch.creation_block(factory)
        swaps = swaps_by_hook(rpc, hooks, start)

    print(f"{'' if opts.hooks else 'factory ' + factory + ': '}{len(hooks)} hook{'' if len(hooks) == 1 else 's'}")
    for hook in hooks:
        token0, token1 = info.add(hook)
        pool = (try_call(rpc, hook, "curve_pool()", ["address"]) or try_call(rpc, hook, "CURVE_POOL()", ["address"]))[0]
        indices = try_call(rpc, hook, "coin_indices()", ["uint256", "uint256"]) or \
            tuple(try_call(rpc, hook, f"I{k}()", ["int128"])[0] for k in (0, 1))
        kind = try_call(rpc, hook, "kind()", ["uint256"])  # None for the standalone hook
        print(f"\n{hook}  {token0.symbol}/{token1.symbol}")
        print(f"    Curve pool  {to_checksum_address(pool)}  {pool_name(rpc, pool)}")
        print(f"    coins {indices[0]}, {indices[1]}  ({describe(kind[0] if kind else None)})")
        if factory:
            key = rpc.call(factory, "pool_key(address)", ["(address,address,uint24,int24,address)"], [hook])[0]
            if to_checksum_address(key[4]) == hook:
                print(f"    v4 pool id  0x{keccak(encode(['(address,address,uint24,int24,address)'], [key])).hex()}")
        if opts.txs:
            logs = swaps.get(hook, [])
            print(f"    swaps: {len(logs)}")
            for log in logs:
                print("      " + swap_line(info, hook, log))


def swap_line(info, hook, log):
    sender = to_checksum_address("0x" + log["topics"][1][-40:])
    origin = to_checksum_address("0x" + log["topics"][2][-40:])
    zero_for_one, exact_input, amount_in, amount_out = decode(
        ["bool", "bool", "uint256", "uint256"], bytes.fromhex(log["data"][2:])
    )
    token_in, token_out = info.pairs[hook] if zero_for_one else info.pairs[hook][::-1]
    block = int(log["blockNumber"], 16)
    return (
        f"{info.timestamp(block)}  tx {log['transactionHash']}  {'exact in ' if exact_input else 'exact out'}  "
        f"{token_in.fmt(amount_in)} -> {token_out.fmt(amount_out)}  origin {origin} via {watch.label(sender)}"
    )


if __name__ == "__main__":
    main()
