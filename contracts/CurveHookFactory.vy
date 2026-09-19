# pragma version ~=0.4.3
# pragma optimize gas
"""
@title CurveHookFactory
@notice Creates CurveHook clones, one per (Curve pool, coin pair), each at a
        CREATE2 address carrying the v4 hook permission flags, initializes the
        Uniswap v4 pool behind each and records them.
@dev The salt that yields the flag bits is mined off-chain (scripts/hooks.py).
     It is bound to the pool and coin pair, so a mined salt cannot be used to
     take the address meant for another pool. Hooks are fully determined by
     (pool, coin pair), so whoever creates one first creates the same hook.
"""

struct PoolKey:
    currency0: address
    currency1: address
    fee: uint24
    tickSpacing: int24
    hooks: address


interface IPoolManager:
    def initialize(key: PoolKey, sqrtPriceX96: uint160) -> int24: nonpayable

interface ICurveHook:
    def initialize(curve_pool: address, i0: uint256, i1: uint256, kind: uint256, currency0: address, currency1: address): nonpayable

interface IStableswapNGFactory:
    def get_n_coins(pool: address) -> uint256: view
    def get_pool_asset_types(pool: address) -> DynArray[uint8, 8]: view

interface ITwocryptoNGFactory:
    def get_coins(pool: address) -> address[2]: view


event HookCreated:
    curve_pool: indexed(address)
    hook: indexed(address)
    pool_id: indexed(bytes32)
    currency0: address
    currency1: address
    i0: uint256  # Curve index of currency0
    i1: uint256  # Curve index of currency1
    kind: uint256

event SetAdmin:
    admin: address


# beforeInitialize | beforeAddLiquidity | beforeSwap | beforeSwapReturnDelta
HOOK_FLAGS: public(constant(uint256)) = (1 << 13) | (1 << 11) | (1 << 7) | (1 << 3)
ALL_HOOK_MASK: constant(uint256) = (1 << 14) - 1

# CurveHook kind bits
KIND_CRYPTO: constant(uint256) = 1
KIND_RECEIVED: constant(uint256) = 2
KIND_GET_DX: constant(uint256) = 4

# Curve's sentinel for native ETH in coins(); v4 uses the zero address
NATIVE: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
REBASING: constant(uint8) = 2  # stableswap-ng asset type

TICK_SPACING: public(constant(int24)) = 1
MIN_SQRT_PRICE: constant(uint256) = 4295128739
MAX_SQRT_PRICE: constant(uint256) = 1461446703485210103287273052203988822378723970342

POOL_MANAGER: public(immutable(IPoolManager))
IMPLEMENTATION: public(immutable(address))
STABLESWAP_NG_FACTORY: public(immutable(IStableswapNGFactory))
TWOCRYPTO_NG_FACTORY: public(immutable(ITwocryptoNGFactory))

admin: public(address)
hook_count: public(uint256)
hooks: public(HashMap[uint256, address])
pool_key: public(HashMap[address, PoolKey])  # by hook
hook_for: HashMap[address, HashMap[uint256, HashMap[uint256, address]]]  # pool -> lower index -> higher index


@deploy
def __init__(
    pool_manager: IPoolManager,
    implementation: address,
    stableswap_ng_factory: IStableswapNGFactory,
    twocrypto_ng_factory: ITwocryptoNGFactory,
    admin: address,
):
    """
    @param stableswap_ng_factory, twocrypto_ng_factory Pools they list get
           exchange_received(); pass empty on chains without them
    @param admin Can sweep dust from hooks
    """
    POOL_MANAGER = pool_manager
    IMPLEMENTATION = implementation
    STABLESWAP_NG_FACTORY = stableswap_ng_factory
    TWOCRYPTO_NG_FACTORY = twocrypto_ng_factory
    self.admin = admin
    log SetAdmin(admin=admin)


@internal
@view
def _static(target: address, data: Bytes[100]) -> (bool, uint256):
    # Payable fallbacks answer unknown selectors with success and no data
    ok: bool = False
    res: Bytes[32] = b""
    ok, res = raw_call(target, data, max_outsize=32, is_static_call=True, revert_on_failure=False)
    if not ok or len(res) != 32:
        return False, 0
    return True, abi_decode(res, uint256)


@internal
@view
def _coin(pool: address, i: uint256) -> address:
    ok: bool = False
    coin: uint256 = 0
    ok, coin = self._static(pool, concat(method_id("coins(uint256)"), abi_encode(i)))
    if not ok:
        # the oldest pools index coins with int128
        ok, coin = self._static(pool, concat(method_id("coins(int128)"), abi_encode(i)))
    assert ok and coin != 0, "no such coin"
    return convert(convert(coin, uint160), address)


@internal
@view
def _decimals(coin: address) -> uint256:
    if coin == NATIVE:
        return 18
    ok: bool = False
    decimals: uint256 = 0
    ok, decimals = self._static(coin, method_id("decimals()"))
    if not ok or decimals > 36:
        return 18
    return decimals


@internal
@view
def _kind(pool: address) -> uint256:
    ok: bool = False
    unused: uint256 = 0
    ok, unused = self._static(pool, method_id("gamma()"))
    if ok:
        if TWOCRYPTO_NG_FACTORY.address != empty(address):
            if (staticcall TWOCRYPTO_NG_FACTORY.get_coins(pool))[0] != empty(address):
                return KIND_CRYPTO | KIND_RECEIVED
        return KIND_CRYPTO
    if STABLESWAP_NG_FACTORY.address != empty(address):
        if staticcall STABLESWAP_NG_FACTORY.get_n_coins(pool) > 0:
            # exchange_received() is disabled in pools holding rebasing coins
            if REBASING not in staticcall STABLESWAP_NG_FACTORY.get_pool_asset_types(pool):
                return KIND_RECEIVED
    return 0


@internal
@pure
def _sqrt_price_x96(dx: uint256, dy: uint256) -> uint160:
    # sqrt(dy / dx) * 2**96 without overflowing at large prices, clamped to v4's range
    price: uint256 = 0
    if dy < 2**64:
        price = isqrt(dy * 2**192 // dx)
    else:
        price = isqrt(dy * 2**64 // dx) * 2**64
    return convert(min(max(price, MIN_SQRT_PRICE), MAX_SQRT_PRICE - 1), uint160)


@internal
@view
def _hook_address(pool: address, i: uint256, j: uint256) -> address:
    return self.hook_for[pool][min(i, j)][max(i, j)]


@view
@external
def get_hook(pool: address, i: uint256, j: uint256) -> address:
    """
    @notice Hook for coins i and j of a Curve pool, in either order
    """
    return self._hook_address(pool, i, j)


@external
def create_hook(pool: address, i: uint256, j: uint256, salt: bytes32) -> address:
    """
    @notice Create the hook for coins i and j of a Curve pool and initialize its v4 pool
    @param salt Mined off-chain so the clone address carries HOOK_FLAGS
    """
    assert i != j, "same coin"
    lo: uint256 = min(i, j)
    hi: uint256 = max(i, j)
    assert hi < 8, "bad index"
    assert self.hook_for[pool][lo][hi] == empty(address), "hook exists"

    # v4 sorts currencies by address, with native ETH (the zero address) first
    coin0: address = self._coin(pool, lo)
    coin1: address = self._coin(pool, hi)
    i0: uint256 = lo
    i1: uint256 = hi
    if coin0 != NATIVE and (coin1 == NATIVE or convert(coin0, uint256) > convert(coin1, uint256)):
        coin1 = coin0
        coin0 = self._coin(pool, hi)
        i0 = hi
        i1 = lo
    currency0: address = empty(address) if coin0 == NATIVE else coin0
    currency1: address = coin1
    assert currency0 != currency1, "same coin"

    kind: uint256 = self._kind(pool)
    ok: bool = False
    unused: uint256 = 0
    dx: uint256 = 10 ** self._decimals(coin0)
    dy: uint256 = 0
    get_dy: Bytes[4] = method_id("get_dy(int128,int128,uint256)")
    get_dx: Bytes[4] = method_id("get_dx(int128,int128,uint256)")
    if kind & KIND_CRYPTO != 0:
        get_dy = method_id("get_dy(uint256,uint256,uint256)")
        get_dx = method_id("get_dx(uint256,uint256,uint256)")
    ok, dy = self._static(pool, concat(get_dy, abi_encode(i0, i1, dx)))
    assert ok and dy > 0, "no quote"
    ok, unused = self._static(pool, concat(get_dx, abi_encode(i0, i1, dy)))
    if ok:
        kind |= KIND_GET_DX

    hook: address = create_minimal_proxy_to(IMPLEMENTATION, salt=keccak256(abi_encode(pool, lo, hi, salt)))
    assert convert(hook, uint256) & ALL_HOOK_MASK == HOOK_FLAGS, "salt does not give hook flags"
    extcall ICurveHook(hook).initialize(pool, i0, i1, kind, currency0, currency1)

    key: PoolKey = PoolKey(currency0=currency0, currency1=currency1, fee=0, tickSpacing=TICK_SPACING, hooks=hook)
    extcall POOL_MANAGER.initialize(key, self._sqrt_price_x96(dx, dy))

    self.hook_for[pool][lo][hi] = hook
    self.pool_key[hook] = key
    n: uint256 = self.hook_count
    self.hooks[n] = hook
    self.hook_count = n + 1
    log HookCreated(
        curve_pool=pool, hook=hook, pool_id=keccak256(abi_encode(key)),
        currency0=currency0, currency1=currency1, i0=i0, i1=i1, kind=kind
    )
    return hook


@external
def set_admin(admin: address):
    assert msg.sender == self.admin, "not admin"
    self.admin = admin
    log SetAdmin(admin=admin)
