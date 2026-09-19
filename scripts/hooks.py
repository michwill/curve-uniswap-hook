"""Shared helpers: deploy the CurveHook implementation and factory, mine salts, create hooks."""
import getpass
import itertools
import json
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
from vyper.builtins.functions import eip1167_bytecode
from vyper.compiler.output import build_abi_output

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from networks import ETHERSCAN_API_KEY  # noqa: E402

POOL_MANAGER = "0x000000000004444c5dc75cB358380D2e3dE08A90"
STABLESWAP_NG_FACTORY = "0x6A8cbed756804B16E05E741eDaBd5cB544AE21bf"
TWOCRYPTO_NG_FACTORY = "0x98EE851a00abeE0d95D08cF4CA2BdCE32aeaAF7F"

# v4-core Hooks.sol permission bits, read from the low 14 bits of the hook address
BEFORE_INITIALIZE = 1 << 13
BEFORE_ADD_LIQUIDITY = 1 << 11
BEFORE_SWAP = 1 << 7
BEFORE_SWAP_RETURNS_DELTA = 1 << 3
HOOK_FLAGS = BEFORE_INITIALIZE | BEFORE_ADD_LIQUIDITY | BEFORE_SWAP | BEFORE_SWAP_RETURNS_DELTA
ALL_HOOK_MASK = (1 << 14) - 1

# CurveHook kind bits
KIND_CRYPTO = 1
KIND_RECEIVED = 2
KIND_GET_DX = 4

KEYSTORE = Path("~/.brownie/accounts/babe.json").expanduser()
DEPLOYMENTS = ROOT / "deployments.json"
# relative to ROOT: these paths end up in verified sources
HOOK_SOURCE = "contracts/CurveHook.vy"
FACTORY_SOURCE = "contracts/CurveHookFactory.vy"


def contract(source):
    os.chdir(ROOT)
    return boa.load_partial(source)


def interface(name):
    return boa.load_vyi(str(ROOT / "interfaces" / f"{name}.vyi"))


def load_account(keystore: Path):
    with open(keystore) as f:
        encrypted = json.load(f)
    return Account.from_key(Account.decrypt(encrypted, getpass.getpass(f"Password for {keystore}: ")))


def chain_id(rpc_url) -> int:
    r = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=30)
    return int(r.json()["result"], 16)


def load_deployment(chain_id: int) -> dict:
    if DEPLOYMENTS.exists():
        return json.loads(DEPLOYMENTS.read_text()).get(str(chain_id), {})
    return {}


def save_deployment(chain_id: int, **addresses):
    data = json.loads(DEPLOYMENTS.read_text()) if DEPLOYMENTS.exists() else {}
    data.setdefault(str(chain_id), {}).update(addresses)
    DEPLOYMENTS.write_text(json.dumps(data, indent=2) + "\n")


def deploy_factory(admin, stableswap_ng=STABLESWAP_NG_FACTORY, twocrypto_ng=TWOCRYPTO_NG_FACTORY):
    implementation = contract(HOOK_SOURCE).deploy(POOL_MANAGER)
    factory = contract(FACTORY_SOURCE).deploy(POOL_MANAGER, implementation, stableswap_ng, twocrypto_ng, admin)
    return implementation, factory


def proxy_initcode(implementation) -> bytes:
    """Init code of Vyper's create_minimal_proxy_to (EIP-1167 runtime, Vyper's own loader)."""
    loader, pre, post = eip1167_bytecode()
    return loader + pre + bytes.fromhex(str(implementation)[2:]) + post


def mine_salt(factory, implementation, pool, i, j, flags=HOOK_FLAGS) -> tuple[bytes, str]:
    """Salt for create_hook() whose clone address has exactly `flags` in its low 14 bits (~16k tries)."""
    lo, hi = sorted((i, j))
    # create_hook() hashes abi_encode(pool, lo, hi, salt) into the CREATE2 salt
    head = bytes(12) + bytes.fromhex(str(pool)[2:]) + lo.to_bytes(32, "big") + hi.to_bytes(32, "big")
    prefix = b"\xff" + bytes.fromhex(str(factory)[2:])
    code_hash = keccak(proxy_initcode(implementation))
    for n in itertools.count():
        salt = n.to_bytes(32, "big")
        addr = keccak(prefix + keccak(head + salt) + code_hash)[12:]
        if int.from_bytes(addr[-2:], "big") & ALL_HOOK_MASK == flags:
            return salt, to_checksum_address(addr)


def hook_at(address):
    # an ABI handle: the clone's own bytecode is the EIP-1167 forwarder, not CurveHook
    abi = build_abi_output(contract(HOOK_SOURCE).compiler_data)
    return boa.loads_abi(json.dumps(abi), name="CurveHook").at(address)


def create_hook(factory, pool, i=0, j=1):
    salt, _ = mine_salt(factory.address, factory.IMPLEMENTATION(), pool, i, j)
    return hook_at(factory.create_hook(pool, i, j, salt))


def pool_id(key) -> str:
    return "0x" + keccak(encode(["(address,address,uint24,int24,address)"], [tuple(key)])).hex()


def verify_etherscan(deployed, source, name, ctor_args: bytes):
    std_json = contract(source).solc_json
    payload = {k: std_json[k] for k in ("language", "sources", "settings")}
    api = "https://api.etherscan.io/v2/api"
    params = {"chainid": 1, "module": "contract", "apikey": ETHERSCAN_API_KEY}
    r = requests.post(api, params={**params, "action": "verifysourcecode"}, data={
        "codeformat": "vyper-json",
        "sourceCode": json.dumps(payload),
        "contractaddress": deployed.address,
        "contractname": f"{source}:{name}",
        "compilerversion": f"vyper:{vyper.__version__}",
        "optimizationUsed": 1,
        "constructorArguments": ctor_args.hex(),
    }).json()
    if r["status"] != "1":
        print(f"{name}: Etherscan verification not submitted:", r["result"])
        return
    for _ in range(20):
        time.sleep(5)
        status = requests.get(api, params={**params, "action": "checkverifystatus", "guid": r["result"]}).json()
        if "Pending" not in status["result"]:
            print(f"{name}: Etherscan:", status["result"])
            return
    print(f"{name}: Etherscan verification still pending, guid", r["result"])
