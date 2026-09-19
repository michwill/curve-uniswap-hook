# pragma version ~=0.4.3
# pragma optimize gas
"""
@title CurveHook
@notice Uniswap v4 hook that fills every swap of its v4 pool from a Curve
        pool. The v4 pool never holds liquidity; it is only a routing entry
        point into Curve.
@dev Deployed once as an implementation. CurveHookFactory clones it (EIP-1167)
     at flag-mined CREATE2 addresses, one clone per (Curve pool, coin pair),
     and initializes the single v4 pool each clone serves.

     Every swap is filled in beforeSwap and the returned BeforeSwapDelta
     consumes the whole amountSpecified, so v4's own AMM math runs on zero.
     Two ways to trade with Curve, chosen per pool by the factory:
     - received (stableswap-ng without rebasing coins, twocrypto-ng): the
       input goes from the PoolManager straight into the Curve pool and
       exchange_received() pays the output straight into the PoolManager.
     - classic (everything else): the hook takes the input, calls exchange()
       with an allowance, and forwards whatever balance it received, since
       legacy pools return nothing from exchange().
     The input is taken out of the PoolManager's existing balance before the
     swapper pays it in, so for tokens the PoolManager holds little of,
     routers have to settle before swapping.
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

interface IERC20:
    def balanceOf(owner: address) -> uint256: view
    def approve(spender: address, amount: uint256) -> bool: nonpayable
    def transfer(to: address, amount: uint256) -> bool: nonpayable

interface IFactory:
    def admin() -> address: view


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

# kind bits, set by the factory
KIND_CRYPTO: public(constant(uint256)) = 1  # uint256 coin indices, else int128
KIND_RECEIVED: public(constant(uint256)) = 2  # has exchange_received()
KIND_GET_DX: public(constant(uint256)) = 4  # has get_dx()

MAX_HOOK_DATA: constant(uint256) = 1024
MAX_REFINE: constant(uint256) = 12
ADDRESS_MASK: constant(uint256) = 2**160 - 1

POOL_MANAGER: public(immutable(IPoolManager))

# curve pool | i0 << 160 | i1 << 168 | kind << 176, one slot for the hot path
config: uint256
factory: public(address)


@deploy
def __init__(pool_manager: IPoolManager):
    POOL_MANAGER = pool_manager
    # lock the implementation itself; clones start with an empty factory
    self.factory = msg.sender


@payable
@external
def __default__():
    # native ETH from PoolManager.take() and from Curve pools paying ETH
    pass


@external
def initialize(curve_pool: address, i0: uint256, i1: uint256, kind: uint256, currency0: address, currency1: address):
    """
    @notice Called once by the factory right after cloning
    @param i0 Curve index of currency0
    @param i1 Curve index of currency1
    @param currency0 v4 currency0 (empty for native ETH)
    """
    assert self.factory == empty(address), "initialized"
    self.factory = msg.sender
    self.config = convert(curve_pool, uint256) | (i0 << 160) | (i1 << 168) | (kind << 176)
    if kind & KIND_RECEIVED == 0:
        for currency: address in [currency0, currency1]:
            if currency != empty(address):
                assert extcall IERC20(currency).approve(curve_pool, max_value(uint256), default_return_value=True)


@view
@external
def curve_pool() -> address:
    return convert(self.config & ADDRESS_MASK, address)


@view
@external
def coin_indices() -> (uint256, uint256):
    """
    @notice Curve coin indices of currency0 and currency1
    """
    return (self.config >> 160) & 255, (self.config >> 168) & 255


@view
@external
def kind() -> uint256:
    return self.config >> 176


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
def _balance(currency: address) -> uint256:
    if currency == empty(address):
        return self.balance
    return staticcall IERC20(currency).balanceOf(self)


@internal
@view
def _get_dy(pool: address, kind: uint256, i: uint256, j: uint256, dx: uint256) -> uint256:
    # small indices encode the same as int128 and uint256; only the selector differs
    selector: Bytes[4] = method_id("get_dy(int128,int128,uint256)")
    if kind & KIND_CRYPTO != 0:
        selector = method_id("get_dy(uint256,uint256,uint256)")
    return abi_decode(raw_call(pool, concat(selector, abi_encode(i, j, dx)), max_outsize=32, is_static_call=True), uint256)


@internal
@view
def _exact_out_dx(pool: address, kind: uint256, i: uint256, j: uint256, dy: uint256) -> uint256:
    # get_dx() is only an estimate (off by ~2.5e-6 either way on USDC/USDT), and
    # pools without it start from the reverse quote, off by about two fees.
    # get_dy() matches exchange() exactly: refine with it until dy is covered
    # with less than one input wei's worth of output to spare.
    dx: uint256 = 0
    if kind & KIND_GET_DX != 0:
        selector: Bytes[4] = method_id("get_dx(int128,int128,uint256)")
        if kind & KIND_CRYPTO != 0:
            selector = method_id("get_dx(uint256,uint256,uint256)")
        dx = abi_decode(raw_call(pool, concat(selector, abi_encode(i, j, dy)), max_outsize=32, is_static_call=True), uint256)
    else:
        dx = self._get_dy(pool, kind, j, i, dy)
    dx = max(dx, 1)

    best: uint256 = max_value(uint256)  # smallest dx seen that yields >= dy
    for _: uint256 in range(MAX_REFINE):
        y: uint256 = self._get_dy(pool, kind, i, j, dx)
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


@internal
def _exchange(pool: address, kind: uint256, i: uint256, j: uint256, dx: uint256, min_dy: uint256, eth: uint256):
    if kind & KIND_RECEIVED != 0:
        selector: Bytes[4] = method_id("exchange_received(int128,int128,uint256,uint256,address)")
        if kind & KIND_CRYPTO != 0:
            selector = method_id("exchange_received(uint256,uint256,uint256,uint256,address)")
        ret: Bytes[32] = raw_call(pool, concat(selector, abi_encode(i, j, dx, min_dy, POOL_MANAGER.address)), max_outsize=32)
    else:
        selector: Bytes[4] = method_id("exchange(int128,int128,uint256,uint256)")
        if kind & KIND_CRYPTO != 0:
            selector = method_id("exchange(uint256,uint256,uint256,uint256)")
        # legacy pools return nothing: the caller measures balances
        ret: Bytes[32] = raw_call(pool, concat(selector, abi_encode(i, j, dx, min_dy)), max_outsize=32, value=eth)


@internal
def _pay(currency: address, amount: uint256) -> uint256:
    # sync(currency) was called before the Curve exchange.
    # Returns what the PoolManager counted: rebasing coins arrive a wei or two short.
    if currency == empty(address):
        return extcall POOL_MANAGER.settle(value=amount)
    assert extcall IERC20(currency).transfer(POOL_MANAGER.address, amount, default_return_value=True)
    return extcall POOL_MANAGER.settle()


@external
@view
def beforeInitialize(sender: address, key: PoolKey, sqrtPriceX96: uint160) -> bytes4:
    # the factory initializes the one v4 pool this hook serves
    assert sender == self.factory, "only factory"
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

    config: uint256 = self.config
    pool: address = convert(config & ADDRESS_MASK, address)
    kind: uint256 = config >> 176
    i: uint256 = (config >> 160) & 255
    j: uint256 = (config >> 168) & 255
    currency_in: address = key.currency0
    currency_out: address = key.currency1
    if not params.zeroForOne:
        i = j
        j = (config >> 160) & 255
        currency_in = key.currency1
        currency_out = key.currency0

    exact_input: bool = params.amountSpecified < 0
    amount_in: uint256 = 0
    amount_out: uint256 = 0
    if exact_input:
        amount_in = convert(-params.amountSpecified, uint256)
    else:
        amount_out = convert(params.amountSpecified, uint256)
        if kind & KIND_RECEIVED != 0:
            amount_in = self._exact_out_dx(pool, kind, i, j, amount_out)
        else:
            # legacy get_dy() can overstate exchange() by a wei: aim a wei higher
            amount_in = self._exact_out_dx(pool, kind, i, j, amount_out + 1)

    extcall POOL_MANAGER.sync(currency_out)
    if kind & KIND_RECEIVED != 0:
        extcall POOL_MANAGER.take(currency_in, pool, amount_in)
        self._exchange(pool, kind, i, j, amount_in, amount_out, 0)
        received: uint256 = extcall POOL_MANAGER.settle()
        if exact_input:
            amount_out = received
        elif received > amount_out:
            # dust over amount_out would be left as an unsettled hook delta: keep it here
            extcall POOL_MANAGER.take(currency_out, self, received - amount_out)
    else:
        extcall POOL_MANAGER.take(currency_in, self, amount_in)
        # rebasing coins can arrive a wei or two short
        dx: uint256 = min(amount_in, self._balance(currency_in))
        balance_out: uint256 = self._balance(currency_out)
        eth: uint256 = 0
        if currency_in == empty(address):
            eth = dx
        self._exchange(pool, kind, i, j, dx, amount_out, eth)
        received: uint256 = self._balance(currency_out) - balance_out
        if exact_input:
            amount_out = self._pay(currency_out, received)
        else:
            # anything over amount_out stays here as dust
            assert self._pay(currency_out, amount_out) == amount_out, "rebasing output"

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
def sweep(currency: address, to: address, amount: uint256):
    """
    @notice Recover dust left over from exact-output swaps (empty currency = ETH)
    """
    assert msg.sender == staticcall IFactory(self.factory).admin(), "not admin"
    if currency == empty(address):
        send(to, amount)
    else:
        assert extcall IERC20(currency).transfer(to, amount, default_return_value=True)
