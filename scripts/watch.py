"""Watch CurveHookSwap events of a deployed CurveHook: who routes through it.

    uv run python scripts/watch.py HOOK                         # history since deployment, then follow
    uv run python scripts/watch.py HOOK --from-block 26000000   # history from a given block
    uv run python scripts/watch.py HOOK --no-follow             # history only

Prints every swap, and a per-(origin, sender) summary after the history scan and on Ctrl-C.
"""
import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from eth_abi import decode
from eth_hash.auto import keccak
from eth_utils import to_checksum_address

sys.path.insert(0, str(Path(__file__).resolve().parent))
from networks import ETHERSCAN_API_KEY, NETWORK  # noqa: E402

TOPIC = "0x" + keccak(b"CurveHookSwap(address,address,bool,bool,uint256,uint256)").hex()
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

    def call(self, to, signature, output_type):
        data = self("eth_call", {"to": to, "data": "0x" + keccak(signature.encode())[:4].hex()}, "latest")
        return decode([output_type], bytes.fromhex(data[2:]))[0]


class Token:
    def __init__(self, rpc, address):
        self.symbol = rpc.call(address, "symbol()", "string")
        self.decimals = rpc.call(address, "decimals()", "uint8")

    def fmt(self, amount):
        return f"{amount / 10**self.decimals:,.{self.decimals}f} {self.symbol}"


def label(address):
    return KNOWN.get(address.lower(), address)


def creation_block(rpc, hook):
    r = requests.get("https://api.etherscan.io/v2/api", timeout=30, params={
        "chainid": 1, "module": "contract", "action": "getcontractcreation",
        "contractaddresses": hook, "apikey": ETHERSCAN_API_KEY,
    }).json()
    if r["status"] != "1":
        raise RuntimeError(f"Etherscan: {r['result']}")
    return int(r["result"][0]["blockNumber"])


def get_logs(rpc, hook, start, end):
    try:
        return rpc("eth_getLogs", {"address": hook, "topics": [TOPIC], "fromBlock": hex(start), "toBlock": hex(end)})
    except RuntimeError:
        if start == end:
            raise
        mid = (start + end) // 2
        return get_logs(rpc, hook, start, mid) + get_logs(rpc, hook, mid + 1, end)


class Watcher:
    def __init__(self, rpc, hook, start):
        self.rpc = rpc
        self.hook = hook
        self.tokens = (Token(rpc, rpc.call(hook, "CURRENCY0()", "address")),
                       Token(rpc, rpc.call(hook, "CURRENCY1()", "address")))
        self.last_block = start - 1  # last block fully scanned and printed
        self.block_times = {}
        # (origin, sender) -> [swaps, volume in per token symbol]
        self.stats = defaultdict(lambda: [0, defaultdict(int)])

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
        while self.last_block < end:
            chunk_end = min(self.last_block + CHUNK, end)
            logs = get_logs(self.rpc, self.hook, self.last_block + 1, chunk_end)
            # fetch all timestamps before printing anything, so a failure here prints no duplicates on retry
            for log in logs:
                self.timestamp(int(log["blockNumber"], 16))
            for log in logs:
                self.show(log)
            self.last_block = chunk_end

    def show(self, log):
        sender = to_checksum_address("0x" + log["topics"][1][-40:])
        origin = to_checksum_address("0x" + log["topics"][2][-40:])
        zero_for_one, exact_input, amount_in, amount_out = decode(
            ["bool", "bool", "uint256", "uint256"], bytes.fromhex(log["data"][2:])
        )
        token_in, token_out = self.tokens if zero_for_one else self.tokens[::-1]
        block = int(log["blockNumber"], 16)
        print(
            f"{self.timestamp(block)}  block {block}  tx {log['transactionHash']}\n"
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
    parser.add_argument("hook", help="CurveHook address")
    parser.add_argument("--from-block", type=int, help="default: hook deployment block (via Etherscan)")
    parser.add_argument("--no-follow", action="store_true", help="exit after the history scan")
    parser.add_argument("--interval", type=float, default=12, help="seconds between polls when following")
    parser.add_argument("--confirmations", type=int, default=3,
                        help="stay this many blocks behind the reported head (load-balanced nodes lag each other)")
    parser.add_argument("--rpc", default=NETWORK, help="RPC url (default: NETWORK from networks.py)")
    opts = parser.parse_args()

    rpc = RPC(opts.rpc)
    hook = to_checksum_address(opts.hook)
    start = opts.from_block if opts.from_block is not None else creation_block(rpc, hook)
    watcher = Watcher(rpc, hook, start)
    tokens = "/".join(t.symbol for t in watcher.tokens)
    print(f"CurveHook {hook} ({tokens}) from block {start}\n", flush=True)

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
