"""Per-chain addresses, --network resolution and deployments.json. No heavy imports."""
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import networks  # noqa: E402

DEPLOYMENTS = ROOT / "deployments.json"


@dataclass(frozen=True)
class Chain:
    name: str
    chain_id: int
    pool_manager: str
    stableswap_ng_factory: str  # pools it lists trade through exchange_received()
    twocrypto_ng_factory: str
    universal_router: str
    v4_quoter: str
    state_view: str
    hop_price: bool  # the Universal Router's swap params carry minHopPriceX36 (v4-periphery since 2025-11)


CHAINS = {c.chain_id: c for c in [
    Chain(
        name="ethereum",
        chain_id=1,
        pool_manager="0x000000000004444c5dc75cB358380D2e3dE08A90",
        stableswap_ng_factory="0x6A8cbed756804B16E05E741eDaBd5cB544AE21bf",
        twocrypto_ng_factory="0x98EE851a00abeE0d95D08cF4CA2BdCE32aeaAF7F",
        universal_router="0x66a9893cC07D91D95644AEDD05D03f95e1dBA8Af",
        v4_quoter="0x52F0E24D1c21C8A0cB1e5a5dD6198556BD9E1203",
        state_view="0x7fFE42C4a5DEeA5b0feC41C94C136Cf115597227",
        hop_price=False,
    ),
    Chain(
        name="robinhood",
        chain_id=4663,
        pool_manager="0x8366a39cc670b4001a1121b8f6a443a643e40951",
        stableswap_ng_factory="0x8271e06e5887fe5ba05234f5315c19f3ec90e8ad",
        twocrypto_ng_factory="0xe7fbd704b938cb8fe26313c3464d4b7b7348c88c",
        universal_router="0x8876789976decbfcbbbe364623c63652db8c0904",
        v4_quoter="0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
        state_view="0xf3334192d15450cdd385c8b70e03f9a6bd9e673b",
        hop_price=True,
    ),
]}
BY_NAME = {c.name: c for c in CHAINS.values()}


def network_url(name: str) -> str:
    urls = getattr(networks, "NETWORKS", {"ethereum": networks.NETWORK})
    if name not in urls:
        raise SystemExit(f"no RPC for network {name!r} in scripts/networks.py NETWORKS")
    return urls[name]


def chain_id(rpc_url) -> int:
    r = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=30)
    return int(r.json()["result"], 16)


def resolve(network="ethereum", rpc=None) -> tuple[str, Chain]:
    """RPC url and chain for --network, or for an --rpc override such as a fork of it."""
    url = rpc or network_url(network)
    cid = chain_id(url)
    if cid not in CHAINS:
        raise SystemExit(f"chain id {cid} of the RPC is not in scripts/chains.py CHAINS")
    chain = CHAINS[cid]
    if not rpc and chain.name != network:
        raise SystemExit(f"the {network} RPC in networks.py serves {chain.name} (chain id {cid})")
    return url, chain


def load_deployment(chain_id: int) -> dict:
    if DEPLOYMENTS.exists():
        return json.loads(DEPLOYMENTS.read_text()).get(str(chain_id), {})
    return {}


def save_deployment(chain_id: int, **addresses):
    data = json.loads(DEPLOYMENTS.read_text()) if DEPLOYMENTS.exists() else {}
    data.setdefault(str(chain_id), {}).update(addresses)
    DEPLOYMENTS.write_text(json.dumps(data, indent=2) + "\n")
