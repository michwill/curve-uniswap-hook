"""The hooks live on mainnet (the factory in deployments.json), exercised on a fork of current state.

    uv run pytest tests/test_live_hooks.py -v
"""
from types import SimpleNamespace

import boa
import pytest
from eth_abi import encode
from vyper.builtins.functions import eip1167_bytecode

import hooks
from conftest import SETTLE_ALL, STETH, TAKE_ALL, V4_SWAP, ZERO, balance, fund
# the swap checks of test_swaps, run here against the deployed hooks
from test_swaps import test_exact_input, test_exact_output, test_hook_swap_event, test_quoter  # noqa: F401

STATE_VIEW = "0x7fFE42C4a5DEeA5b0feC41C94C136Cf115597227"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
WBTC = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"
USDC_CRVUSD = "0x4DEcE678ceceb27446b35C672dC7d61F30bAD69E"  # coins USDC, crvUSD
YB_WBTC = "0x313698667d7FDD6789a9BC70821309ff891E729A"  # coins crvUSD, WBTC
SWAP_EXACT_IN = 0x07
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
FACTORY = hooks.load_deployment(1).get("factory")


def symbol(currency):
    return "ETH" if currency == ZERO else hooks.interface("IERC20").at(currency).symbol()


def live_hooks():
    if not FACTORY:
        return []
    factory = hooks.contract(hooks.FACTORY_SOURCE).at(FACTORY)
    hooks_ = [factory.hooks(n) for n in range(factory.hook_count())]
    return [(h, "/".join(symbol(c) for c in factory.pool_key(h)[:2]) + " " + h[:6]) for h in hooks_]


LIVE = live_hooks()


@pytest.fixture(scope="module")
def live_factory():
    return hooks.contract(hooks.FACTORY_SOURCE).at(FACTORY)


@pytest.fixture(scope="module", params=[h for h, _ in LIVE], ids=[name for _, name in LIVE])
def case(request, live_factory):
    hook = hooks.hook_at(request.param)
    key = tuple(live_factory.pool_key(hook.address))
    kind = hook.kind()
    curve = hooks.interface("ICurveCrypto" if kind & hooks.KIND_CRYPTO else "IStableSwapNG").at(hook.curve_pool())
    return SimpleNamespace(hook=hook, key=key, kind=kind, curve=curve, indices=hook.coin_indices(),
                           rebasing=STETH in key[:2])


def test_deployment(case, live_factory):
    hook, key = case.hook, case.key
    assert int(hook.address, 16) & hooks.ALL_HOOK_MASK == hooks.HOOK_FLAGS
    # an EIP-1167 forwarder to the factory's implementation, initialized by the factory
    _, pre, post = eip1167_bytecode()
    assert boa.env.get_code(hook.address) == pre + bytes.fromhex(live_factory.IMPLEMENTATION()[2:]) + post
    assert hook.factory() == live_factory.address

    # registered, and the v4 currencies are the Curve coins it trades
    i0, i1 = case.indices
    assert live_factory.get_hook(hook.curve_pool(), i0, i1) == hook.address
    coins = [hooks.interface("IStableSwapNG").at(hook.curve_pool()).coins(i) for i in (i0, i1)]
    assert list(key[:2]) == [ZERO if c == NATIVE else c for c in coins]
    assert key[2:4] == (0, 1) and key[4] == hook.address

    # the v4 pool exists and holds no liquidity of its own
    state = hooks.interface("IStateView").at(STATE_VIEW)
    pool_id = bytes.fromhex(hooks.pool_id(key)[2:])
    sqrt_price, *_ = state.getSlot0(pool_id)
    assert sqrt_price > 0
    assert state.getLiquidity(pool_id) == 0


@pytest.fixture(scope="module")
def route(live_factory):
    """USDC -> crvUSD -> WBTC through two live hooks, a crvUSD sale the PoolManager alone could not fund."""
    first, second = live_factory.get_hook(USDC_CRVUSD, 0, 1), live_factory.get_hook(YB_WBTC, 0, 1)
    if ZERO in (first, second):
        pytest.skip("the route's hooks are not deployed")
    return first, second


def test_multihop_exact_input_past_the_cap(route, quoter, router, trader):
    # the first hop has Curve pay crvUSD into the PoolManager, so the second can take more than v4 holds
    held = balance(CRVUSD, hooks.POOL_MANAGER)
    amount = 3 * held // 10**12 + 10**6  # USDC worth about three times v4's crvUSD
    first_pool = hooks.interface("IStableSwapNG").at(USDC_CRVUSD)
    second_pool = hooks.interface("ICurveCrypto").at(YB_WBTC)
    crvusd = first_pool.get_dy(0, 1, amount)
    assert crvusd > held
    # a legacy get_dy() can overstate by a wei; exchange_received() also swaps crvUSD strayed into the pool
    strayed = max(balance(CRVUSD, YB_WBTC) - second_pool.balances(0), 0)
    least, most = second_pool.get_dy(0, 1, crvusd - 1), second_pool.get_dy(0, 1, crvusd + strayed)

    path = [(CRVUSD, 0, 1, route[0], b""), (WBTC, 0, 1, route[1], b"")]
    quoted, _ = quoter.quoteExactInput((USDC, path, amount))
    assert least <= quoted <= most

    # paid after the swap, as the Universal Router does
    fund(trader, USDC, amount)
    before = balance(WBTC, trader)
    params = encode(["(address,(address,uint24,int24,address,bytes)[],uint128,uint128)"], [(USDC, path, amount, quoted)])
    v4_input = encode(["bytes", "bytes[]"], [bytes([SWAP_EXACT_IN, SETTLE_ALL, TAKE_ALL]), [
        params, encode(["address", "uint256"], [USDC, amount]), encode(["address", "uint256"], [WBTC, quoted])]])
    router.execute(bytes([V4_SWAP]), [v4_input], 2**64, sender=trader)
    assert balance(WBTC, trader) - before == quoted


def test_multihop_exact_output_is_capped(route, quoter):
    # exact-output paths run their last hop first: crvUSD -> WBTC must take crvUSD before any came in
    held = balance(CRVUSD, hooks.POOL_MANAGER)
    second_pool = hooks.interface("ICurveCrypto").at(YB_WBTC)
    path = [(USDC, 0, 1, route[0], b""), (CRVUSD, 0, 1, route[1], b"")]
    if held // 2:
        quoter.quoteExactOutput((WBTC, path, second_pool.get_dy(0, 1, held // 2)))
    with boa.reverts():
        quoter.quoteExactOutput((WBTC, path, second_pool.get_dy(0, 1, 2 * held)))
