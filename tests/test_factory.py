"""Factory registry, clone addresses and access control."""
import boa
import pytest

import hooks
from conftest import CHAIN, ZERO, fund

pytestmark = pytest.mark.skipif(CHAIN.chain_id != 1, reason="built on Ethereum pools")

USDC_USDT = "0x4f493B7dE8aAC7d55F71853688b1F7C8F0243C85"  # stableswap-ng
THREEPOOL = "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7"  # DAI/USDC/USDT
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


@pytest.fixture(scope="module")
def hook(factory):
    return hooks.create_hook(factory, USDC_USDT)


def test_predicted_address(factory, implementation):
    _, predicted = hooks.mine_salt(factory.address, implementation.address, THREEPOOL, 2, 0)
    hook = hooks.create_hook(factory, THREEPOOL, 2, 0)
    assert hook.address == predicted
    assert int(hook.address, 16) & hooks.ALL_HOOK_MASK == hooks.HOOK_FLAGS


def test_registry(factory, hook):
    assert factory.hook_count() == 1
    assert factory.hooks(0) == hook.address
    assert factory.get_hook(USDC_USDT, 0, 1) == factory.get_hook(USDC_USDT, 1, 0) == hook.address
    assert factory.get_hook(USDC_USDT, 0, 2) == ZERO
    key = factory.pool_key(hook.address)
    assert tuple(key) == (USDC, "0xdAC17F958D2ee523a2206206994597C13D831ec7", 0, 1, hook.address)
    assert hook.curve_pool() == USDC_USDT and hook.coin_indices() == (0, 1)
    assert hook.factory() == factory.address


def test_hook_created_event(factory):
    hook = hooks.create_hook(factory, THREEPOOL, 1, 2)
    # strict=False: the hook's approvals log token events other modules may not decode
    [ev] = [e for e in factory.get_logs(strict=False) if type(e).__name__ == "HookCreated"]
    key = factory.pool_key(hook.address)
    assert (ev.curve_pool, ev.hook, "0x" + ev.pool_id.hex()) == (THREEPOOL, hook.address, hooks.pool_id(key))
    assert (ev.currency0, ev.currency1, ev.i0, ev.i1, ev.kind) == (key[0], key[1], 1, 2, 0)


def test_one_hook_per_pair(factory, hook):
    salt, _ = hooks.mine_salt(factory.address, factory.IMPLEMENTATION(), USDC_USDT, 1, 0)
    with boa.reverts("hook exists"):
        factory.create_hook(USDC_USDT, 1, 0, salt)


def test_bad_salt(factory):
    good, _ = hooks.mine_salt(factory.address, factory.IMPLEMENTATION(), THREEPOOL, 0, 1)
    bad = (int.from_bytes(good, "big") + 1).to_bytes(32, "big")  # carries the flags by 1/16384 chance
    with boa.reverts("salt does not give hook flags"):
        factory.create_hook(THREEPOOL, 0, 1, bad)


@pytest.mark.parametrize("i,j,error", [(1, 1, "same coin"), (0, 8, "bad index"), (0, 3, "no such coin")])
def test_bad_coins(factory, i, j, error):
    with boa.reverts(error):
        factory.create_hook(THREEPOOL, i, j, b"\x00" * 32)


def test_initialized_once(hook, implementation):
    with boa.reverts("initialized"):
        hook.initialize(USDC_USDT, 0, 1, 0, ZERO, ZERO)
    with boa.reverts("initialized"):
        implementation.initialize(USDC_USDT, 0, 1, 0, ZERO, ZERO)


def test_only_factory_initializes_v4_pool(factory, hook, pool_manager):
    key = list(factory.pool_key(hook.address))
    key[3] = 10  # another tick spacing: a fresh v4 pool on the same hook
    with boa.reverts():
        pool_manager.initialize(tuple(key), 2**96)


def test_before_swap_only_pool_manager(factory, hook):
    with boa.reverts("not pool manager"):
        hook.beforeSwap(boa.env.eoa, factory.pool_key(hook.address), (True, -(10**6), 0), b"")


def test_add_liquidity_blocked(factory, hook):
    with boa.env.prank(CHAIN.pool_manager), boa.reverts("liquidity is in Curve"):
        hook.beforeAddLiquidity(boa.env.eoa, factory.pool_key(hook.address), (-10, 10, 10**18, b"\x00" * 32), b"")


def test_sweep(factory, hook, admin, trader):
    usdc = hooks.interface("IERC20").at(USDC)
    fund(trader, USDC, 5)
    usdc.transfer(hook.address, 5, sender=trader)
    boa.env.set_balance(hook.address, 7)
    with boa.reverts("not admin"):
        hook.sweep(USDC, admin, 5)
    hook.sweep(USDC, admin, 5, sender=admin)
    hook.sweep(ZERO, admin, 7, sender=admin)
    assert usdc.balanceOf(admin) == 5 and boa.env.get_balance(admin) == 7


def test_set_admin(factory, hook, admin):
    new = boa.env.generate_address()
    with boa.reverts("not admin"):
        factory.set_admin(new)
    factory.set_admin(new, sender=admin)
    assert factory.admin() == new
    with boa.reverts("not admin"):
        hook.sweep(USDC, admin, 0, sender=admin)
