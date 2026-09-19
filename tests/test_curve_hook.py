import boa
import pytest
from eth_abi import encode
from eth_hash.auto import keccak

import deploy
from conftest import POOL_KEY_T, swap_exact_in, swap_exact_out

AMOUNTS = [1, 10**6, 1000 * 10**6, 100_000 * 10**6, 1_000_000 * 10**6]


def hook_events(router, name):
    return [e for e in router.get_logs(strict=False) if type(e).__name__ == name]


def curve_ij(hook, zero_for_one):
    return (hook.I0(), hook.I1()) if zero_for_one else (hook.I1(), hook.I0())


def min_dx(curve, i, j, dy):
    """Smallest input for which Curve's get_dy() (== exchange()) pays out at least dy."""
    def out(dx):
        return curve.get_dy(i, j, dx) if dx else 0
    # bracket out(lo) < dy <= out(hi) around get_dx(), which can be off either way
    guess = max(curve.get_dx(i, j, dy), 1)
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


def test_address_flags(hook):
    assert int(hook.address, 16) & deploy.ALL_HOOK_MASK == deploy.HOOK_FLAGS
    assert hook.HOOK_FLAGS() == deploy.HOOK_FLAGS


def test_currencies_sorted(hook, curve, tokens):
    usdc, usdt = tokens
    assert (hook.CURRENCY0(), hook.CURRENCY1()) == (usdc.address, usdt.address)
    assert curve.coins(hook.I0()) == usdc.address
    assert curve.coins(hook.I1()) == usdt.address


@pytest.mark.parametrize("zero_for_one", [True, False])
@pytest.mark.parametrize("amount", AMOUNTS)
def test_exact_input(hook, key, router, trader, tokens, curve, zero_for_one, amount):
    token_in, token_out = tokens if zero_for_one else tokens[::-1]
    expected = curve.get_dy(*curve_ij(hook, zero_for_one), amount)
    in_before, out_before = token_in.balanceOf(trader), token_out.balanceOf(trader)

    swap_exact_in(router, key, zero_for_one, amount, expected, trader)

    assert in_before - token_in.balanceOf(trader) == amount
    assert token_out.balanceOf(trader) - out_before == expected
    assert token_in.balanceOf(hook) == token_out.balanceOf(hook) == 0

    [ev] = hook_events(router, "CurveHookSwap")
    assert (ev.sender, ev.origin) == (router.address, trader)
    assert (ev.zeroForOne, ev.exactInput, ev.amountIn, ev.amountOut) == (zero_for_one, True, amount, expected)


@pytest.mark.parametrize("zero_for_one", [True, False])
@pytest.mark.parametrize("amount", AMOUNTS)
def test_exact_output(hook, key, router, trader, tokens, curve, zero_for_one, amount):
    token_in, token_out = tokens if zero_for_one else tokens[::-1]
    i, j = curve_ij(hook, zero_for_one)
    cheapest = min_dx(curve, i, j, amount)
    in_before, out_before = token_in.balanceOf(trader), token_out.balanceOf(trader)

    swap_exact_out(router, key, zero_for_one, amount, cheapest + 1, trader)

    paid = in_before - token_in.balanceOf(trader)
    assert token_out.balanceOf(trader) - out_before == amount
    assert cheapest <= paid <= cheapest + 1  # stops once the spare output is < 1 input wei
    # output Curve paid beyond `amount` stays in the hook: under one input wei's worth
    assert token_in.balanceOf(hook) == 0
    assert token_out.balanceOf(hook) <= 2

    [ev] = hook_events(router, "CurveHookSwap")
    assert (ev.zeroForOne, ev.exactInput, ev.amountIn, ev.amountOut) == (zero_for_one, False, paid, amount)


def test_hook_swap_event(hook, key, router, trader, pool_manager):
    swap_exact_in(router, key, True, 1000 * 10**6, 0, trader)
    [ev] = hook_events(router, "HookSwap")
    [cve] = hook_events(router, "CurveHookSwap")
    # poolId computed by the hook matches v4's PoolId
    assert ev.poolId == keccak(encode([POOL_KEY_T], [key]))
    assert ev.sender == router.address
    assert (ev.amount0, ev.amount1) == (cve.amountIn, -cve.amountOut)
    assert (ev.hookLPfeeAmount0, ev.hookLPfeeAmount1) == (0, 0)


@pytest.mark.parametrize("zero_for_one", [True, False])
def test_quoter(hook, key, router, trader, quoter, curve, tokens, zero_for_one):
    amount = 50_000 * 10**6
    token_in = tokens[0] if zero_for_one else tokens[1]
    amount_out, _ = quoter.quoteExactInputSingle((key, zero_for_one, amount, b""))
    assert amount_out == curve.get_dy(*curve_ij(hook, zero_for_one), amount)

    amount_in, _ = quoter.quoteExactOutputSingle((key, zero_for_one, amount, b""))
    before = token_in.balanceOf(trader)
    swap_exact_out(router, key, zero_for_one, amount, amount_in, trader)
    assert before - token_in.balanceOf(trader) == amount_in


def test_min_out_enforced_by_router(hook, key, router, trader, curve):
    expected = curve.get_dy(hook.I0(), hook.I1(), 10**9)
    with boa.reverts():
        swap_exact_in(router, key, True, 10**9, expected + 1, trader)


def test_before_swap_only_pool_manager(hook, key):
    with boa.reverts("not pool manager"):
        hook.beforeSwap(boa.env.eoa, key, (True, -(10**6), 0), b"")


def test_add_liquidity_blocked(hook, key):
    with boa.env.prank(deploy.POOL_MANAGER), boa.reverts("liquidity is in Curve"):
        hook.beforeAddLiquidity(boa.env.eoa, key, (-10, 10, 10**18, b"\x00" * 32), b"")


@pytest.mark.parametrize("change", ["fee", "currency", "dynamic_fee"])
def test_initialize_rejects_other_pools(hook, key, pool_manager, change):
    bad = list(key)
    bad[3] = 10  # fresh tickSpacing so the pool is not already initialized
    if change == "fee":
        bad[2] = 100
    elif change == "dynamic_fee":
        bad[2] = 0x800000
    else:
        bad[0] = "0x6B175474E89094C44Da98b954EedeAC495271d0F"  # DAI
    with boa.reverts():
        pool_manager.initialize(tuple(bad), 2**96)
    # the same key with the right currencies and fee is fine
    pool_manager.initialize((key[0], key[1], 0, 10, key[4]), 2**96)


def test_sweep(hook, owner, tokens):
    usdc, _ = tokens
    boa.deal(usdc, hook, 5)
    with boa.reverts("not owner"):
        hook.sweep(usdc, owner, 5)
    hook.sweep(usdc, owner, 5, sender=owner)
    assert usdc.balanceOf(owner) == 5
