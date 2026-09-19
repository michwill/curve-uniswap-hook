"""Deploy CurveHook at a flag-mined CREATE2 address and initialize its v4 pool.

    uv run python scripts/deploy.py           # dry run on a fork of NETWORK
    uv run python scripts/deploy.py --live    # real deployment from the KEYSTORE account
    uv run python scripts/deploy.py --live --rpc http://127.0.0.1:8545 --no-verify   # e.g. anvil
"""
import argparse
import getpass
import itertools
import json
import math
import os
import sys
import time
from pathlib import Path

import boa
import requests
import vyper
from eth_abi import encode
from eth_account import Account
from eth_hash.auto import keccak
from eth_utils import to_checksum_address

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from networks import ETHERSCAN_API_KEY, NETWORK  # noqa: E402

POOL_MANAGER = "0x000000000004444c5dc75cB358380D2e3dE08A90"
CREATE2_PROXY = "0x4e59b44847b379578588920cA78FbF26c0B4956C"  # Arachnid deterministic deployer
CURVE_POOL = "0x4f493B7dE8aAC7d55F71853688b1F7C8F0243C85"  # USDC/USDT stableswap-ng
TICK_SPACING = 1

# v4-core Hooks.sol permission bits, read from the low 14 bits of the hook address
BEFORE_INITIALIZE = 1 << 13
BEFORE_ADD_LIQUIDITY = 1 << 11
BEFORE_SWAP = 1 << 7
BEFORE_SWAP_RETURNS_DELTA = 1 << 3
HOOK_FLAGS = BEFORE_INITIALIZE | BEFORE_ADD_LIQUIDITY | BEFORE_SWAP | BEFORE_SWAP_RETURNS_DELTA
ALL_HOOK_MASK = (1 << 14) - 1

HOOK_SOURCE = "contracts/CurveHook.vy"  # relative to ROOT: this path ends up in verified sources
KEYSTORE = Path("~/.brownie/accounts/babe.json").expanduser()


def hook_deployer():
    os.chdir(ROOT)
    return boa.load_partial(HOOK_SOURCE)


def interface(name):
    return boa.load_vyi(str(ROOT / "interfaces" / f"{name}.vyi"))


def load_account(keystore: Path):
    with open(keystore) as f:
        encrypted = json.load(f)
    return Account.from_key(Account.decrypt(encrypted, getpass.getpass(f"Password for {keystore}: ")))


def ctor_args(owner, curve_pool=CURVE_POOL, i=0, j=1):
    return encode(
        ["address", "address", "uint256", "uint256", "address"],
        [POOL_MANAGER, curve_pool, i, j, str(owner)],
    )


def mine_salt(initcode: bytes, flags: int = HOOK_FLAGS) -> tuple[bytes, str]:
    """Find a salt whose CREATE2 address has exactly `flags` in its low 14 bits (~16k tries)."""
    prefix = b"\xff" + bytes.fromhex(CREATE2_PROXY[2:])
    code_hash = keccak(initcode)
    for n in itertools.count():
        salt = n.to_bytes(32, "big")
        addr = keccak(prefix + salt + code_hash)[12:]
        if int.from_bytes(addr[-2:], "big") & ALL_HOOK_MASK == flags:
            addr = to_checksum_address(addr)
            if not boa.env.get_code(addr):
                return salt, addr


def deploy_hook(owner, curve_pool=CURVE_POOL, i=0, j=1):
    deployer = hook_deployer()
    args = ctor_args(owner, curve_pool, i, j)
    salt, addr = mine_salt(deployer.compiler_data.bytecode + args)
    boa.env.raw_call(CREATE2_PROXY, data=salt + deployer.compiler_data.bytecode + args)
    hook = deployer.at(addr)
    assert hook.HOOK_FLAGS() == HOOK_FLAGS
    return hook, args


def pool_key(hook):
    return (hook.CURRENCY0(), hook.CURRENCY1(), 0, TICK_SPACING, hook.address)


def curve_sqrt_price_x96(hook) -> int:
    """v4 price (raw currency1 per raw currency0) quoted from Curve for one unit of currency0."""
    curve = interface("IStableSwapNG").at(hook.CURVE_POOL())
    dx = 10 ** interface("IERC20").at(hook.CURRENCY0()).decimals()
    dy = curve.get_dy(hook.I0(), hook.I1(), dx)
    return math.isqrt(dy * 2**192 // dx)


def initialize_pool(hook) -> int:
    pm = interface("IPoolManager").at(POOL_MANAGER)
    return pm.initialize(pool_key(hook), curve_sqrt_price_x96(hook))


def verify_etherscan(hook, args: bytes):
    std_json = hook_deployer().solc_json
    source = {k: std_json[k] for k in ("language", "sources", "settings")}
    api = "https://api.etherscan.io/v2/api"
    params = {"chainid": 1, "module": "contract", "apikey": ETHERSCAN_API_KEY}
    r = requests.post(api, params={**params, "action": "verifysourcecode"}, data={
        "codeformat": "vyper-json",
        "sourceCode": json.dumps(source),
        "contractaddress": hook.address,
        "contractname": f"{HOOK_SOURCE}:CurveHook",
        "compilerversion": f"vyper:{vyper.__version__}",
        "optimizationUsed": 1,
        "constructorArguments": args.hex(),
    }).json()
    if r["status"] != "1":
        print("Etherscan verification not submitted:", r["result"])
        return
    for _ in range(20):
        time.sleep(5)
        status = requests.get(api, params={**params, "action": "checkverifystatus", "guid": r["result"]}).json()
        if "Pending" not in status["result"]:
            print("Etherscan:", status["result"])
            return
    print("Etherscan verification still pending, guid", r["result"])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="send real transactions to --rpc")
    parser.add_argument("--no-verify", action="store_true", help="skip Etherscan verification")
    parser.add_argument("--rpc", default=NETWORK, help="RPC url (default: NETWORK from networks.py)")
    parser.add_argument("--keystore", type=Path, default=KEYSTORE, help=f"deployer keystore (default: {KEYSTORE})")
    opts = parser.parse_args()

    if opts.live:
        boa.set_network_env(opts.rpc)
        boa.env.add_account(load_account(opts.keystore), force_eoa=True)
    else:
        boa.fork(opts.rpc, block_identifier="latest")

    owner = boa.env.eoa
    hook, args = deploy_hook(owner)
    tick = initialize_pool(hook)
    pool_id = "0x" + keccak(encode(["(address,address,uint24,int24,address)"], [pool_key(hook)])).hex()
    print(f"hook:    {hook.address}  (owner {owner})")
    print(f"pool id: {pool_id}  (tick {tick})")
    print(f"key:     {pool_key(hook)}")

    if opts.live and not opts.no_verify:
        verify_etherscan(hook, args)
    elif not opts.live:
        print("dry run on a fork, nothing sent")


if __name__ == "__main__":
    main()
