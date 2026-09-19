"""Watch CurveHookSwap events: who routes through the hooks.

    uv run python scripts/watch.py                          # every hook of the factory in deployments.json
    uv run python scripts/watch.py --factory FACTORY        # every hook of another factory
    uv run python scripts/watch.py HOOK [HOOK ...]          # just these hooks
    uv run python scripts/watch.py --from-block 26000000    # history from a given block
    uv run python scripts/watch.py --no-follow              # history only

History starts at the factory's (or the hooks') deployment block unless --from-block is
given, then new blocks are followed and hooks the factory creates are picked up as they appear.
Prints every swap, and a per-(origin, sender) summary after the history scan and on Ctrl-C.
"""
import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from eth_abi import decode, encode
from eth_hash.auto import keccak
from eth_utils import to_checksum_address

sys.path.insert(0, str(Path(__file__).resolve().parent))
from networks import ETHERSCAN_API_KEY, NETWORK  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOPIC = "0x" + keccak(b"CurveHookSwap(address,address,bool,bool,uint256,uint256)").hex()
ZERO = "0x0000000000000000000000000000000000000000"
KNOWN = {
    "0x66a9893cc07d91d95644aedd05d03f95e1dba8af": "UniversalRouter",
}
CHUNK = 10_000  # blocks per eth_getLogs request; halved on node errors


class RPC:
    def __init__(self, url):
        self.url = url
        self.session = requests.Session()
        self.id = 0

    def __call__(self, method, *params):
        self.id += 1
        payload = {"jsonrpc": "2.0", "id": self.id, "method": method, "params": list(params)}
        r = self.session.post(self.url, json=payload, timeout=60).json()
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r["result"]

    def call(self, to, signature, output_types, args=()):
        arg_types = signature[signature.index("(") + 1:-1]
        data = keccak(signature.encode())[:4] + (encode(arg_types.split(","), list(args)) if args else b"")
        result = self("eth_call", {"to": to, "data": "0x" + data.hex()}, "latest")
        return decode(output_types, bytes.fromhex(result[2:]))


class Token:
    def __init__(self, rpc, address):
        if address == ZERO:
            self.symbol, self.decimals = "ETH", 18
        else:
            self.symbol = rpc.call(address, "symbol()", ["string"])[0]
            self.decimals = rpc.call(address, "decimals()", ["uint8"])[0]

    def fmt(self, amount):
        # integer arithmetic: a float garbles 18-decimal amounts (0.3 ETH -> 0.299999999999999989)
        whole, fraction = divmod(amount, 10**self.decimals)
        digits = f".{fraction:0{self.decimals}d}" if self.decimals else ""
        return f"{whole:,}{digits} {self.symbol}"


def label(address):
    return KNOWN.get(address.lower(), address)


def creation_block(address):
    r = requests.get("https://api.etherscan.io/v2/api", timeout=30, params={
        "chainid": 1, "module": "contract", "action": "getcontractcreation",
        "contractaddresses": address, "apikey": ETHERSCAN_API_KEY,
    }).json()
    if r["status"] != "1":
        raise RuntimeError(f"Etherscan: {r['result']}")
    return int(r["result"][0]["blockNumber"])


def get_logs(rpc, addresses, start, end):
    try:
        return rpc("eth_getLogs", {"address": addresses, "topics": [TOPIC], "fromBlock": hex(start), "toBlock": hex(end)})
    except RuntimeError:
        if start == end:
            raise
        mid = (start + end) // 2
        return get_logs(rpc, addresses, start, mid) + get_logs(rpc, addresses, mid + 1, end)


class Watcher:
    def __init__(self, rpc, start, factory=None, hooks=()):
        self.rpc = rpc
        self.factory = factory
        self.last_block = start - 1  # last block fully scanned and printed
        self.pairs = {}  # hook -> (token0, token1)
        self.factory_hooks = 0
        self.tokens = {}
        self.block_times = {}
        # (origin, sender) -> [swaps, volume in per token symbol]
        self.stats = defaultdict(lambda: [0, defaultdict(int)])
        for hook in hooks:
            self.add(hook)

    def token(self, address):
        if address not in self.tokens:
            self.tokens[address] = Token(self.rpc, address)
        return self.tokens[address]

    def add(self, hook):
        hook = to_checksum_address(hook)
        key = None
        if self.factory:
            key = self.rpc.call(self.factory, "pool_key(address)", ["(address,address,uint24,int24,address)"], [hook])[0]
        if key and to_checksum_address(key[4]) == hook:
            currencies = key[:2]
        else:  # not from the factory: the first, single-pool hook
            currencies = [self.rpc.call(hook, f"CURRENCY{k}()", ["address"])[0] for k in (0, 1)]
        self.pairs[hook] = tuple(self.token(to_checksum_address(c)) for c in currencies)
        return self.pairs[hook]

    def refresh(self):
        """Pick up hooks the factory created since the last look."""
        if not self.factory:
            return
        count = self.rpc.call(self.factory, "hook_count()", ["uint256"])[0]
        for n in range(self.factory_hooks, count):
            hook = self.rpc.call(self.factory, "hooks(uint256)", ["address"], [n])[0]
            token0, token1 = self.add(hook)
            print(f"hook {to_checksum_address(hook)} ({token0.symbol}/{token1.symbol})", flush=True)
        self.factory_hooks = count

    def timestamp(self, block):
        if block not in self.block_times:
            header = self.rpc("eth_getBlockByNumber", hex(block), False)
            if header is None:  # this backend has not seen the block yet
                raise RuntimeError(f"block {block} not found")
            ts = int(header["timestamp"], 16)
            self.block_times[block] = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return self.block_times[block]

    def scan(self, end):
        """Scan (last_block, end]. Advances last_block per chunk, so a failed chunk is retried whole."""
        self.refresh()
        while self.last_block < end:
            chunk_end = min(self.last_block + CHUNK, end)
            logs = get_logs(self.rpc, list(self.pairs), self.last_block + 1, chunk_end) if self.pairs else []
            # fetch all timestamps before printing anything, so a failure here prints no duplicates on retry
            for log in logs:
                self.timestamp(int(log["blockNumber"], 16))
            for log in logs:
                self.show(log)
            self.last_block = chunk_end

    def show(self, log):
        hook = to_checksum_address(log["address"])
        sender = to_checksum_address("0x" + log["topics"][1][-40:])
        origin = to_checksum_address("0x" + log["topics"][2][-40:])
        zero_for_one, exact_input, amount_in, amount_out = decode(
            ["bool", "bool", "uint256", "uint256"], bytes.fromhex(log["data"][2:])
        )
        token_in, token_out = self.pairs[hook] if zero_for_one else self.pairs[hook][::-1]
        block = int(log["blockNumber"], 16)
        print(
            f"{self.timestamp(block)}  block {block}  tx {log['transactionHash']}\n"
            f"    {self.pairs[hook][0].symbol}/{self.pairs[hook][1].symbol} hook {hook}\n"
            f"    origin {origin}  via {label(sender)}\n"
            f"    {'exact in ' if exact_input else 'exact out'}  "
            f"{token_in.fmt(amount_in)} -> {token_out.fmt(amount_out)}",
            flush=True,
        )
        stat = self.stats[(origin, sender)]
        stat[0] += 1
        stat[1][token_in.symbol] += amount_in / 10**token_in.decimals

    def summary(self):
        total = sum(s[0] for s in self.stats.values())
        print(f"\nswaps: {total}, distinct (origin, sender): {len(self.stats)}")
        for (origin, sender), (swaps, volume) in sorted(self.stats.items(), key=lambda kv: -kv[1][0]):
            vol = ", ".join(f"{v:,.2f} {sym}" for sym, v in volume.items())
            print(f"  {swaps:>6}  origin {origin}  via {label(sender)}  in: {vol}")
        print(flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("hooks", nargs="*", help="hook addresses (default: every hook of the factory)")
    parser.add_argument("--factory", help="CurveHookFactory (default: the one in deployments.json)")
    parser.add_argument("--from-block", type=int, help="default: factory or hook deployment block (via Etherscan)")
    parser.add_argument("--no-follow", action="store_true", help="exit after the history scan")
    parser.add_argument("--interval", type=float, default=12, help="seconds between polls when following")
    parser.add_argument("--confirmations", type=int, default=3,
                        help="stay this many blocks behind the reported head (load-balanced nodes lag each other)")
    parser.add_argument("--rpc", default=NETWORK, help="RPC url (default: NETWORK from networks.py)")
    opts = parser.parse_args()

    rpc = RPC(opts.rpc)
    factory = opts.factory
    if not factory and not opts.hooks:
        sys.path.insert(0, str(ROOT / "scripts"))
        from hooks import load_deployment
        factory = load_deployment(int(rpc("eth_chainId"), 16)).get("factory")
        if not factory:
            parser.error("no factory in deployments.json: pass --factory or hook addresses")
    factory = factory and to_checksum_address(factory)

    if opts.from_block is not None:
        start = opts.from_block
    else:
        start = creation_block(factory) if factory else min(creation_block(h) for h in opts.hooks)
    watcher = Watcher(rpc, start, factory, opts.hooks)
    print(f"{'factory ' + factory if factory else 'hooks'} from block {start}\n", flush=True)

    history_done = False
    try:
        while True:
            try:
                watcher.scan(int(rpc("eth_blockNumber"), 16) - opts.confirmations)
            except (RuntimeError, requests.RequestException) as e:
                print(f"rpc error, retrying from block {watcher.last_block + 1}: {e}", file=sys.stderr, flush=True)
            else:
                if not history_done:
                    history_done = True
                    watcher.summary()
                    if opts.no_follow:
                        return
                    print(f"following from block {watcher.last_block + 1} ...", flush=True)
            time.sleep(opts.interval)
    except KeyboardInterrupt:
        watcher.summary()


if __name__ == "__main__":
    main()
