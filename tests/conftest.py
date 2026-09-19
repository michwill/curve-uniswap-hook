import sys
from pathlib import Path

import boa
import pytest
from eth_abi import encode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import chains  # noqa: E402
import hooks  # noqa: E402

ZERO = "0x0000000000000000000000000000000000000000"
STETH = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
# set by pytest_configure for --network, before test modules are imported
CHAIN = UNIVERSAL_ROUTER = V4_QUOTER = None

# Universal Router command and v4-periphery Actions
V4_SWAP = 0x10
SWAP_EXACT_IN_SINGLE = 0x06
SWAP_EXACT_OUT_SINGLE = 0x08
SETTLE = 0x0B
SETTLE_ALL = 0x0C
TAKE_ALL = 0x0F
POOL_KEY_T = "(address,address,uint24,int24,address)"


def pytest_addoption(parser):
    parser.addoption("--network", default="ethereum", choices=sorted(chains.BY_NAME), help="chain to fork (default: ethereum)")


def pytest_configure(config):
    global CHAIN, UNIVERSAL_ROUTER, V4_QUOTER
    rpc, CHAIN = chains.resolve(config.getoption("network"))
    UNIVERSAL_ROUTER, V4_QUOTER = CHAIN.universal_router, CHAIN.v4_quoter
    hooks.use_fork(rpc)


@pytest.fixture(scope="session")
def admin():
    return boa.env.generate_address("admin")


@pytest.fixture(scope="session")
def deployment(admin):
    return hooks.deploy_factory(admin, CHAIN)


@pytest.fixture(scope="session")
def implementation(deployment):
    return deployment[0]


@pytest.fixture(scope="session")
def factory(deployment):
    return deployment[1]


@pytest.fixture(scope="session")
def router():
    return hooks.interface("IUniversalRouter").at(UNIVERSAL_ROUTER)


@pytest.fixture(scope="session")
def quoter():
    return hooks.interface("IV4Quoter").at(V4_QUOTER)


@pytest.fixture(scope="session")
def pool_manager():
    return hooks.interface("IPoolManager").at(CHAIN.pool_manager)


@pytest.fixture(scope="session")
def trader():
    trader = boa.env.generate_address("trader")
    boa.env.set_balance(trader, 10**24)
    return trader


@pytest.fixture(scope="session", autouse=True)
def session_fixtures_first(admin, deployment, implementation, factory, router, quoter, pool_manager, trader):
    # boa rolls fixture state back as a stack: a session fixture first set up inside a
    # module would sit above that module's fixtures and keep their state for the session
    pass


def balance(currency, owner):
    if currency == ZERO:
        return boa.env.get_balance(str(owner))
    return hooks.interface("IERC20").at(currency).balanceOf(owner)


def fund(trader, currency, amount):
    """Give the trader `amount` of currency and approve it to the Universal Router via Permit2."""
    if currency == ZERO:
        return
    token = hooks.interface("IERC20").at(currency)
    if currency == STETH:  # balances are shares: mint by staking
        hooks.interface("ILido").at(STETH).submit(ZERO, value=amount + 10, sender=trader)
    else:
        boa.deal(token, trader, token.balanceOf(trader) + amount, adjust_supply=False)
    token.approve(PERMIT2, 2**256 - 1, sender=trader)
    hooks.interface("IPermit2").at(PERMIT2).approve(currency, UNIVERSAL_ROUTER, 2**160 - 1, 2**48 - 1, sender=trader)


def swap(router, key, zero_for_one, exact_input, amount, limit, sender, prepay=False):
    """One v4 swap through the Universal Router.

    exact input: `amount` in, at least `limit` out; exact output: `amount` out, at most `limit` in.
    prepay settles the input before the swap, for tokens the PoolManager holds too little of.
    """
    currency_in, currency_out = (key[0], key[1]) if zero_for_one else (key[1], key[0])
    max_in = amount if exact_input else limit
    if CHAIN.hop_price:  # newer routers take a minimum output/input price per hop, 0 for none
        single = encode([f"({POOL_KEY_T},bool,uint128,uint128,uint256,bytes)"], [(key, zero_for_one, amount, limit, 0, b"")])
    else:
        single = encode([f"({POOL_KEY_T},bool,uint128,uint128,bytes)"], [(key, zero_for_one, amount, limit, b"")])
    action = SWAP_EXACT_IN_SINGLE if exact_input else SWAP_EXACT_OUT_SINGLE
    min_out = limit if exact_input else amount
    if prepay:
        actions = [SETTLE, action, TAKE_ALL, TAKE_ALL]
        params = [
            # two spare wei: rebasing coins arrive short; whatever is left is refunded below
            encode(["address", "uint256", "bool"], [currency_in, max_in + 2, True]),
            single,
            encode(["address", "uint256"], [currency_out, min_out]),
            encode(["address", "uint256"], [currency_in, 0]),  # refund what exact output did not use
        ]
    else:
        actions = [action, SETTLE_ALL, TAKE_ALL]
        params = [
            single,
            encode(["address", "uint256"], [currency_in, max_in]),
            encode(["address", "uint256"], [currency_out, min_out]),
        ]
    v4_input = encode(["bytes", "bytes[]"], [bytes(actions), params])
    value = max_in if currency_in == ZERO else 0
    router.execute(bytes([V4_SWAP]), [v4_input], 2**64, value=value, sender=sender)
