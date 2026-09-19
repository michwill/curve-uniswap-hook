import sys
from pathlib import Path

import boa
import pytest
from eth_abi import encode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import deploy  # noqa: E402
from networks import NETWORK  # noqa: E402

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
UNIVERSAL_ROUTER = "0x66a9893cC07D91D95644AEDD05D03f95e1dBA8Af"
V4_QUOTER = "0x52F0E24D1c21C8A0cB1e5a5dD6198556BD9E1203"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

# Universal Router command and v4-periphery Actions
V4_SWAP = 0x10
SWAP_EXACT_IN_SINGLE = 0x06
SWAP_EXACT_OUT_SINGLE = 0x08
SETTLE_ALL = 0x0C
TAKE_ALL = 0x0F
POOL_KEY_T = "(address,address,uint24,int24,address)"

boa.fork(NETWORK, block_identifier="latest")


@pytest.fixture(scope="session")
def owner():
    return boa.env.generate_address("owner")


@pytest.fixture(scope="session")
def hook(owner):
    hook, _ = deploy.deploy_hook(owner)
    deploy.initialize_pool(hook)
    return hook


@pytest.fixture(scope="session")
def key(hook):
    return deploy.pool_key(hook)


@pytest.fixture(scope="session")
def curve():
    return deploy.interface("IStableSwapNG").at(deploy.CURVE_POOL)


@pytest.fixture(scope="session")
def pool_manager():
    return deploy.interface("IPoolManager").at(deploy.POOL_MANAGER)


@pytest.fixture(scope="session")
def tokens():
    erc20 = deploy.interface("IERC20")
    return erc20.at(USDC), erc20.at(USDT)


@pytest.fixture(scope="session")
def router():
    return deploy.interface("IUniversalRouter").at(UNIVERSAL_ROUTER)


@pytest.fixture(scope="session")
def quoter():
    return deploy.interface("IV4Quoter").at(V4_QUOTER)


@pytest.fixture(scope="session")
def trader(tokens):
    trader = boa.env.generate_address("trader")
    permit2 = deploy.interface("IPermit2").at(PERMIT2)
    for token in tokens:
        boa.deal(token, trader, 10**7 * 10**6)
        token.approve(PERMIT2, 2**256 - 1, sender=trader)
        permit2.approve(token.address, UNIVERSAL_ROUTER, 2**160 - 1, 2**48 - 1, sender=trader)
    return trader


def swap_exact_in(router, key, zero_for_one, amount_in, min_out, sender):
    currency_in, currency_out = (key[0], key[1]) if zero_for_one else (key[1], key[0])
    params = [
        encode([f"({POOL_KEY_T},bool,uint128,uint128,bytes)"], [(key, zero_for_one, amount_in, min_out, b"")]),
        encode(["address", "uint256"], [currency_in, amount_in]),
        encode(["address", "uint256"], [currency_out, min_out]),
    ]
    _execute(router, bytes([SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL]), params, sender)


def swap_exact_out(router, key, zero_for_one, amount_out, max_in, sender):
    currency_in, currency_out = (key[0], key[1]) if zero_for_one else (key[1], key[0])
    params = [
        encode([f"({POOL_KEY_T},bool,uint128,uint128,bytes)"], [(key, zero_for_one, amount_out, max_in, b"")]),
        encode(["address", "uint256"], [currency_in, max_in]),
        encode(["address", "uint256"], [currency_out, amount_out]),
    ]
    _execute(router, bytes([SWAP_EXACT_OUT_SINGLE, SETTLE_ALL, TAKE_ALL]), params, sender)


def _execute(router, actions, params, sender):
    v4_input = encode(["bytes", "bytes[]"], [actions, params])
    router.execute(bytes([V4_SWAP]), [v4_input], 2**64, sender=sender)
