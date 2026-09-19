"""The hooks live on mainnet (the factory in deployments.json), exercised on a fork of current state.

    uv run pytest tests/test_live_hooks.py -v
"""
from types import SimpleNamespace

import boa
import pytest
from vyper.builtins.functions import eip1167_bytecode

import hooks
from conftest import STETH, ZERO
# the swap checks of test_swaps, run here against the deployed hooks
from test_swaps import test_exact_input, test_exact_output, test_hook_swap_event, test_quoter  # noqa: F401

STATE_VIEW = "0x7fFE42C4a5DEeA5b0feC41C94C136Cf115597227"
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
