"""Swaps through hooks on real Curve pools of every family, via the Universal Router."""
from types import SimpleNamespace

import pytest

import hooks
from conftest import STETH, ZERO, balance, fund, swap

C, R, D = hooks.KIND_CRYPTO, hooks.KIND_RECEIVED, hooks.KIND_GET_DX
POOLS = [
    # name, Curve pool, coin indices, expected kind
    ("stableswap-ng USDC/USDT", "0x4f493B7dE8aAC7d55F71853688b1F7C8F0243C85", 0, 1, R | D),
    ("twocrypto-ng crvUSD/WBTC", "0x313698667d7fdd6789a9bc70821309ff891e729a", 0, 1, C | R | D),
    ("tricrypto-ng WBTC/WETH", "0x7f86bf177dd4f3494b841a37e810a34dd56c829b", 1, 2, C | D),
    ("3pool USDC/USDT", "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7", 1, 2, 0),
    ("tricrypto2 USDT/WETH", "0xD51a44d3FaE010294C616388b506AcdA1bfAAE46", 0, 2, C),
    ("factory-crypto WETH/rETH", "0x0f3159811670c117c372428d4e69ac32325e4d0f", 0, 1, C),
    ("crvUSD factory USDC/crvUSD", "0x4dece678ceceb27446b35c672dc7d61f30bad69e", 0, 1, D),
    ("steth ETH/stETH", "0xDC24316b9AE028F1497c275EB9192a3Ea0f67022", 0, 1, 0),
    ("stableswap-ng pxETH/stETH (rebasing)", "0x6951bdc4734b9f7f3e1b74afebc670c736a0edb6", 0, 1, D),
]


@pytest.fixture(scope="module", params=POOLS, ids=[p[0] for p in POOLS])
def case(request, factory):
    name, pool, i, j, kind = request.param
    hook = hooks.create_hook(factory, pool, i, j)
    key = tuple(factory.pool_key(hook.address))
    curve = hooks.interface("ICurveCrypto" if kind & C else "IStableSwapNG").at(pool)
    return SimpleNamespace(hook=hook, key=key, kind=kind, curve=curve, indices=hook.coin_indices(),
                           rebasing=STETH in key[:2])


def sides(case, zero_for_one):
    """(currency in, currency out, Curve index in, Curve index out)"""
    (c0, c1), (i0, i1) = case.key[:2], case.indices
    return (c0, c1, i0, i1) if zero_for_one else (c1, c0, i1, i0)


def prepay_needed(currency, amount):
    # The hook takes the input out of the PoolManager's own balance before the swapper pays.
    # A rebasing coin also has to be prepaid with spare wei: SETTLE_ALL pays exactly the debt
    # and the coin arrives a wei short.
    return currency == STETH or (currency != ZERO and balance(currency, hooks.POOL_MANAGER) < 2 * amount)


def hook_events(router, name):
    return [e for e in router.get_logs(strict=False) if type(e).__name__ == name]


def min_dx(curve, i, j, dy):
    """Smallest input for which Curve's get_dy() (== exchange()) pays out at least dy."""
    def out(dx):
        return curve.get_dy(i, j, dx) if dx else 0
    guess = max(curve.get_dy(j, i, dy), 1)
    step = 1
    if out(guess) >= dy:
        while out(max(guess - step, 0)) >= dy:
            step *= 2
        lo, hi = max(guess - step, 0), guess
    else:
        while out(guess + step) < dy:
            step *= 2
        lo, hi = guess, guess + step
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if out(mid) >= dy else (mid, hi)
    return hi


def test_kind(case):
    assert case.hook.kind() == case.kind


@pytest.mark.parametrize("zero_for_one", [True, False])
def test_exact_input(case, router, trader, zero_for_one):
    currency_in, currency_out, i, j = sides(case, zero_for_one)
    amount = case.curve.balances(i) // 1000
    fund(trader, currency_in, amount + 10)
    expected = case.curve.get_dy(i, j, amount)
    in_before, out_before = balance(currency_in, trader), balance(currency_out, trader)

    swap(router, case.key, zero_for_one, True, amount, 0, trader, prepay_needed(currency_in, amount))

    paid = in_before - balance(currency_in, trader)
    got = balance(currency_out, trader) - out_before
    tolerance = 3 if case.rebasing else 0  # stETH transfers round down a wei or two
    assert abs(paid - amount) <= tolerance
    # legacy pools' get_dy() can overstate exchange() by a wei
    assert expected - 1 - tolerance <= got <= expected

    [ev] = hook_events(router, "CurveHookSwap")
    assert (ev.sender, ev.origin, ev.zeroForOne, ev.exactInput) == (router.address, trader, zero_for_one, True)
    assert ev.amountIn == amount and got <= ev.amountOut <= expected


@pytest.mark.parametrize("zero_for_one", [True, False])
def test_exact_output(case, router, trader, zero_for_one):
    if case.rebasing:
        pytest.skip("rebasing coins arrive a wei short, so exact output is not supported")
    currency_in, currency_out, i, j = sides(case, zero_for_one)
    amount = case.curve.get_dy(i, j, case.curve.balances(i) // 1000) // 2
    cheapest = min_dx(case.curve, i, j, amount)
    fund(trader, currency_in, 2 * cheapest + 10)
    in_before, out_before = balance(currency_in, trader), balance(currency_out, trader)

    swap(router, case.key, zero_for_one, False, amount, 2 * cheapest, trader, prepay_needed(currency_in, cheapest))

    paid = in_before - balance(currency_in, trader)
    assert balance(currency_out, trader) - out_before == amount
    # stops once the spare output is < 1 input wei; classic pools aim an output wei higher
    aimed = amount if case.kind & R else amount + 1
    assert cheapest <= paid <= min_dx(case.curve, i, j, aimed) + 1
    [ev] = hook_events(router, "CurveHookSwap")
    assert (ev.exactInput, ev.amountIn, ev.amountOut) == (False, paid, amount)


def test_hook_swap_event(case, router, trader):
    currency_in, _, i, j = sides(case, True)
    amount = case.curve.balances(i) // 1000
    fund(trader, currency_in, amount + 10)
    swap(router, case.key, True, True, amount, 0, trader, prepay_needed(currency_in, amount))
    [ev] = hook_events(router, "HookSwap")
    [tracked] = hook_events(router, "CurveHookSwap")
    assert "0x" + ev.poolId.hex() == hooks.pool_id(case.key)
    assert (ev.amount0, ev.amount1) == (tracked.amountIn, -tracked.amountOut)


@pytest.mark.parametrize("zero_for_one", [True, False])
def test_quoter(case, quoter, zero_for_one):
    currency_in, _, i, j = sides(case, zero_for_one)
    amount = case.curve.balances(i) // 1000
    if prepay_needed(currency_in, amount) or case.rebasing:
        pytest.skip("the Quoter can only simulate inputs the PoolManager already holds")
    quoted, _ = quoter.quoteExactInputSingle((case.key, zero_for_one, amount, b""))
    expected = case.curve.get_dy(i, j, amount)
    # legacy pools' get_dy() can overstate exchange() by a wei
    assert quoted == expected if case.kind & R else expected - 1 <= quoted <= expected
