"""Shared helpers: deploy the CurveHook implementation and factory, mine salts, create hooks."""
import getpass
import itertools
import json
import os
import sys
import time
import warnings
from pathlib import Path

import boa
import requests
import vyper
from boa.environment import Env
from boa.network import NetworkEnv
from boa.rpc import EthereumRPC, RPCError
from eth_abi import encode
from eth_account import Account
from eth_hash.auto import keccak
from eth_utils import to_checksum_address
from vyper.builtins.functions import eip1167_bytecode
from vyper.compiler.output import build_abi_output

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from chains import CHAINS, load_deployment, save_deployment  # noqa: E402, F401
from networks import ETHERSCAN_API_KEY  # noqa: E402

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
# boa casts every clone the factory creates to the implementation it forwards to
warnings.filterwarnings("ignore", message="casted bytecode does not match compiled bytecode")
# relative to ROOT: these paths end up in verified sources
HOOK_SOURCE = "contracts/CurveHook.vy"
FACTORY_SOURCE = "contracts/CurveHookFactory.vy"


class PinnedRPC(EthereumRPC):
    """
    boa's RPC client for load-balanced nodes (e.g. drpc), whose backends lag each other.

    It keeps `head`, the highest block any answer has shown to exist (latest blocks,
    receipts of our transactions), and never lets a read see an older chain:
    - state reads at "latest" are pinned to `head`, so the nonce read right after our
      transaction is mined cannot come from a backend that has not seen it;
    - a backend answering with a latest block below `head`, or not knowing a block at
      or below `head`, is lagging, and the read is asked again.
    Anything else, including a rejected transaction, is raised at once; sends are never retried.
    """
    BLOCK_PARAM = {"eth_getTransactionCount": 1, "eth_getBalance": 1, "eth_getCode": 1,
                   "eth_getStorageAt": 2, "eth_call": 1, "eth_getBlockByNumber": 0}
    LAGGING = ("unknown block", "header not found", "block not found")
    SENDS = {"eth_sendRawTransaction", "eth_sendTransaction"}
    RETRIES = 40  # half a second apart

    def __init__(self, url):
        super().__init__(url)
        self.head = 0

    def fetch(self, method, params):
        if method in self.SENDS:
            return super().fetch(method, params)
        return self._read(lambda requests: [EthereumRPC.fetch(self, *requests[0])], [(method, params)])[0]

    def fetch_multi(self, payloads):
        if any(method in self.SENDS for method, _ in payloads):
            return super().fetch_multi(payloads)
        return self._read(lambda requests: EthereumRPC.fetch_multi(self, requests), payloads)

    def _read(self, send, requests):
        for attempt in range(self.RETRIES):
            pinned = [self._pin(method, params) for method, params in requests]
            try:
                results = send(pinned)
            except RPCError as e:
                if attempt + 1 < self.RETRIES and self._lagging(e, pinned):
                    time.sleep(0.5)
                    continue
                raise
            if not any(self._behind(method, params, r) for (method, params), r in zip(pinned, results)):
                for (method, _), r in zip(pinned, results):
                    self._observe(method, r)
                return results
            time.sleep(0.5)
        raise RuntimeError(f"{self.name} stays behind block {self.head}")

    def _pin(self, method, params):
        i = self.BLOCK_PARAM.get(method)
        if i is None or method == "eth_getBlockByNumber" or not self.head or len(params) <= i or params[i] != "latest":
            return method, params
        return method, [*params[:i], hex(self.head), *params[i + 1:]]

    def _block(self, method, params):
        i = self.BLOCK_PARAM.get(method)
        if i is not None and len(params) > i and isinstance(params[i], str) and params[i].startswith("0x"):
            return int(params[i], 16)
        return None

    def _lagging(self, error, requests):
        # "unknown block" is only lag if every block asked about is one we know exists
        blocks = [self._block(method, params) for method, params in requests]
        return any(lag in str(error).lower() for lag in self.LAGGING) and \
            all(b <= self.head for b in blocks if b is not None)

    def _behind(self, method, params, result):
        if method == "eth_blockNumber":
            return int(result, 16) < self.head
        if method == "eth_getBlockByNumber":
            if result is None:  # a lagging backend answers null for a block it has not seen
                block = self._block(method, params)
                return block is not None and block <= self.head
            return params[0] == "latest" and int(result["number"], 16) < self.head
        return False

    def _observe(self, method, result):
        if method == "eth_blockNumber":
            block = int(result, 16)
        elif method == "eth_getBlockByNumber" and result:
            block = int(result["number"], 16)
        elif method == "eth_getTransactionReceipt" and result:
            block = int(result["blockNumber"], 16)
        else:
            return
        self.head = max(self.head, block)


def use_network(rpc_url):
    """Send real transactions through rpc_url."""
    boa.set_env(NetworkEnv(PinnedRPC(rpc_url)))


def use_fork(rpc_url):
    """Simulate on a fork of rpc_url's latest block."""
    env = Env()
    env.fork_rpc(PinnedRPC(rpc_url), block_identifier="latest")
    boa.set_env(env)


def contract(source):
    os.chdir(ROOT)
    return boa.load_partial(source)


def interface(name):
    return boa.load_vyi(str(ROOT / "interfaces" / f"{name}.vyi"))


def load_account(keystore: Path):
    with open(keystore) as f:
        encrypted = json.load(f)
    return Account.from_key(Account.decrypt(encrypted, getpass.getpass(f"Password for {keystore}: ")))


def factory_ctor_args(chain, implementation, admin):
    return [chain.pool_manager, implementation, chain.stableswap_ng_factory, chain.twocrypto_ng_factory, admin]


def deploy_factory(admin, chain=CHAINS[1]):
    implementation = contract(HOOK_SOURCE).deploy(chain.pool_manager)
    factory = contract(FACTORY_SOURCE).deploy(*factory_ctor_args(chain, implementation, admin))
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


ETHERSCAN_API = "https://api.etherscan.io/v2/api"


def etherscan(chain_id, **params):
    return requests.get(ETHERSCAN_API, timeout=30, params={"chainid": chain_id, "apikey": ETHERSCAN_API_KEY, **params}).json()


def is_verified(address, chain_id) -> bool:
    return bool(etherscan(chain_id, module="contract", action="getsourcecode", address=address)["result"][0]["SourceCode"])


def ctor_args_on_chain(address, source, chain_id) -> bytes:
    """Constructor arguments of a deployed contract: its creation code past the compiled init code."""
    creation = etherscan(chain_id, module="contract", action="getcontractcreation", contractaddresses=address)["result"][0]
    creation_code = bytes.fromhex(creation["creationBytecode"][2:])
    initcode = contract(source).compiler_data.bytecode
    assert creation_code.startswith(initcode), f"{address} was not compiled from {source} as it is now"
    return creation_code[len(initcode):]


def verify_etherscan(address, source, name, ctor_args: bytes, chain_id):
    std_json = contract(source).solc_json
    payload = {k: std_json[k] for k in ("language", "sources", "settings")}
    params = {"chainid": chain_id, "module": "contract", "apikey": ETHERSCAN_API_KEY}
    # Etherscan cannot find a contract for a while after it is mined
    for _ in range(12):
        r = requests.post(ETHERSCAN_API, params={**params, "action": "verifysourcecode"}, data={
            "codeformat": "vyper-json",
            "sourceCode": json.dumps(payload),
            "contractaddress": str(address),
            "contractname": f"{source}:{name}",
            "compilerversion": f"vyper:{vyper.__version__}",
            "optimizationUsed": 1,
            "constructorArguments": ctor_args.hex(),
        }).json()
        if r["status"] == "1" or "Unable to locate ContractCode" not in r["result"]:
            break
        print(f"{name}: Etherscan has not indexed {address} yet, retrying in 10s")
        time.sleep(10)
    if r["status"] != "1":
        print(f"{name}: Etherscan verification not submitted:", r["result"])
        return
    for _ in range(20):
        time.sleep(5)
        status = etherscan(chain_id, module="contract", action="checkverifystatus", guid=r["result"])
        if "Pending" not in status["result"]:
            print(f"{name}: Etherscan:", status["result"])
            return
    print(f"{name}: Etherscan verification still pending, guid", r["result"])
