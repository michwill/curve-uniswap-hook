# pragma version ~=0.4.3
# pragma optimize gas
"""
@title CurveHook
@notice Uniswap v4 hook that fills every swap of its v4 pool from a Curve
        stableswap-ng pool. The v4 pool never holds liquidity; it is only a
        routing entry point into Curve.
@dev Per swap: sync(out) -> take(in) straight into the Curve pool ->
     Curve exchange_received() pays the output straight into the PoolManager
     -> settle(). The returned BeforeSwapDelta consumes the whole
     amountSpecified, so v4's own AMM math runs on zero.
     The input is taken out of the PoolManager's existing balance before the
     swapper pays it in, so swap size is capped by what the PoolManager holds.
     Must be deployed at an address whose low 14 bits equal HOOK_FLAGS.
"""

struct PoolKey:
    currency0: address
    currency1: address
    fee: uint24
    tickSpacing: int24
    hooks: address

struct SwapParams:
    zeroForOne: bool
    amountSpecified: int256
    sqrtPriceLimitX96: uint160

struct ModifyLiquidityParams:
    tickLower: int24
    tickUpper: int24
    liquidityDelta: int256
    salt: bytes32


interface IPoolManager:
    def take(currency: address, to: address, amount: uint256): nonpayable
    def sync(currency: address): nonpayable
    def settle() -> uint256: payable

interface ICurvePool:
    def coins(i: uint256) -> address: view
    def get_dx(i: int128, j: int128, dy: uint256) -> uint256: view
    def get_dy(i: int128, j: int128, dx: uint256) -> uint256: view
    def exchange_received(i: int128, j: int128, dx: uint256, min_dy: uint256, receiver: address) -> uint256: nonpayable

interface IERC20:
    def transfer(to: address, amount: uint256) -> bool: nonpayable


# OpenZeppelin IHookEvents standard, for indexers: v4's own Swap event shows
# zero amounts because the hook fills the whole swap.
# Amounts are from the pool's side: positive = paid in, negative = paid out.
event HookSwap:
    poolId: indexed(bytes32)
    sender: indexed(address)
    amount0: int128
    amount1: int128
    hookLPfeeAmount0: uint128
    hookLPfeeAmount1: uint128

# Tracking: who routes through the hook.
event CurveHookSwap:
    sender: indexed(address)  # contract that called PoolManager.swap (router / bot)
    origin: indexed(address)  # tx.origin
    zeroForOne: bool
    exactInput: bool
    amountIn: uint256
    amountOut: uint256


# beforeInitialize | beforeAddLiquidity | beforeSwap | beforeSwapReturnDelta
HOOK_FLAGS: public(constant(uint256)) = (1 << 13) | (1 << 11) | (1 << 7) | (1 << 3)

MAX_HOOK_DATA: constant(uint256) = 1024
MAX_REFINE: constant(uint256) = 8

POOL_MANAGER: public(immutable(IPoolManager))
CURVE_POOL: public(immutable(ICurvePool))
CURRENCY0: public(immutable(address))
CURRENCY1: public(immutable(address))
OWNER: public(immutable(address))
# Curve coin indices of CURRENCY0 / CURRENCY1
I0: public(immutable(int128))
I1: public(immutable(int128))


@deploy
def __init__(pool_manager: IPoolManager, curve_pool: ICurvePool, i: uint256, j: uint256, owner: address):
    """
    @param i Curve index of one coin of the pair
    @param j Curve index of the other coin (order does not matter)
    @param owner Can sweep dust left over from exact-output swaps
    """
    assert i != j
    coin_i: address = staticcall curve_pool.coins(i)
    coin_j: address = staticcall curve_pool.coins(j)
    swap_order: bool = convert(coin_i, uint256) > convert(coin_j, uint256)

    POOL_MANAGER = pool_manager
    CURVE_POOL = curve_pool
    OWNER = owner
    CURRENCY0 = coin_j if swap_order else coin_i
    CURRENCY1 = coin_i if swap_order else coin_j
    I0 = convert(j if swap_order else i, int128)
    I1 = convert(i if swap_order else j, int128)


@internal
@pure
def _to_before_swap_delta(specified: int128, unspecified: int128) -> int256:
    # BeforeSwapDelta: specified in the upper 128 bits, unspecified in the lower 128
    low: int256 = convert(unspecified, int256)
    if low < 0:
        low += 2**128
    return convert(specified, int256) * 2**128 + low


@internal
@view
def _exact_out_dx(i: int128, j: int128, dy: uint256) -> uint256:
    # get_dx() is only an estimate (off by ~2.5e-6 either way on USDC/USDT), while
    # get_dy() matches exchange() exactly. Refine with get_dy() until dy is covered
    # with less than one input wei's worth of output to spare.
    dx: uint256 = max(staticcall CURVE_POOL.get_dx(i, j, dy), 1)
    best: uint256 = max_value(uint256)  # smallest dx seen that yields >= dy
    for _: uint256 in range(MAX_REFINE):
        y: uint256 = staticcall CURVE_POOL.get_dy(i, j, dx)
        if y >= dy:
            best = dx
            spare: uint256 = (y - dy) * dx // y
            if spare == 0:
                break
            dx -= spare
        else:
            if y == 0:  # dust amounts
                dx *= 2
            else:
                dx += (dy - y) * dx // y + 1
            if dx >= best:
                break
    assert best != max_value(uint256), "exact output not reached"
    return best


@external
@view
def beforeInitialize(sender: address, key: PoolKey, sqrtPriceX96: uint160) -> bytes4:
    assert key.currency0 == CURRENCY0 and key.currency1 == CURRENCY1, "wrong currencies"
    assert key.fee == 0, "fee must be 0"
    return method_id("beforeInitialize(address,(address,address,uint24,int24,address),uint160)", output_type=bytes4)


@external
def beforeAddLiquidity(
    sender: address, key: PoolKey, params: ModifyLiquidityParams, hookData: Bytes[MAX_HOOK_DATA]
) -> bytes4:
    raise "liquidity is in Curve"


@external
def beforeSwap(
    sender: address, key: PoolKey, params: SwapParams, hookData: Bytes[MAX_HOOK_DATA]
) -> (bytes4, int256, uint24):
    assert msg.sender == POOL_MANAGER.address, "not pool manager"

    exact_input: bool = params.amountSpecified < 0
    i: int128 = I0
    j: int128 = I1
    currency_in: address = CURRENCY0
    currency_out: address = CURRENCY1
    if not params.zeroForOne:
        i = I1
        j = I0
        currency_in = CURRENCY1
        currency_out = CURRENCY0

    amount_in: uint256 = 0
    amount_out: uint256 = 0
    extcall POOL_MANAGER.sync(currency_out)
    if exact_input:
        amount_in = convert(-params.amountSpecified, uint256)
        extcall POOL_MANAGER.take(currency_in, CURVE_POOL.address, amount_in)
        extcall CURVE_POOL.exchange_received(i, j, amount_in, 0, POOL_MANAGER.address)
        amount_out = extcall POOL_MANAGER.settle()
    else:
        amount_out = convert(params.amountSpecified, uint256)
        amount_in = self._exact_out_dx(i, j, amount_out)
        extcall POOL_MANAGER.take(currency_in, CURVE_POOL.address, amount_in)
        extcall CURVE_POOL.exchange_received(i, j, amount_in, amount_out, POOL_MANAGER.address)
        received: uint256 = extcall POOL_MANAGER.settle()
        # Dust over amount_out would be left as an unsettled hook delta: keep it here
        if received > amount_out:
            extcall POOL_MANAGER.take(currency_out, self, received - amount_out)

    signed_in: int128 = convert(amount_in, int128)
    signed_out: int128 = convert(amount_out, int128)

    if params.zeroForOne:
        log HookSwap(
            poolId=keccak256(abi_encode(key)), sender=sender,
            amount0=signed_in, amount1=-signed_out, hookLPfeeAmount0=0, hookLPfeeAmount1=0
        )
    else:
        log HookSwap(
            poolId=keccak256(abi_encode(key)), sender=sender,
            amount0=-signed_out, amount1=signed_in, hookLPfeeAmount0=0, hookLPfeeAmount1=0
        )
    log CurveHookSwap(
        sender=sender, origin=tx.origin, zeroForOne=params.zeroForOne, exactInput=exact_input,
        amountIn=amount_in, amountOut=amount_out
    )

    # Hook takes the input and owes the output: exact in -> (+in, -out), exact out -> (-out, +in)
    delta: int256 = 0
    if exact_input:
        delta = self._to_before_swap_delta(signed_in, -signed_out)
    else:
        delta = self._to_before_swap_delta(-signed_out, signed_in)

    return method_id("beforeSwap(address,(address,address,uint24,int24,address),(bool,int256,uint160),bytes)", output_type=bytes4), delta, 0


@external
def sweep(token: address, to: address, amount: uint256):
    assert msg.sender == OWNER, "not owner"
    assert extcall IERC20(token).transfer(to, amount, default_return_value=True)
