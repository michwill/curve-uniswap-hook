# Curve pools behind Uniswap v4 hooks

A Uniswap v4 hook that fills every swap of its v4 pool from a Curve pool. The v4
pool never holds liquidity: it is a routing entry point into Curve, so anything
that trades v4 (routers, aggregators, arbitrage bots) can reach Curve liquidity
through it.

`CurveHookFactory` creates one hook per (Curve pool, coin pair) as an EIP-1167
clone of `CurveHook`, at a CREATE2 address carrying the v4 hook permission
flags, and initializes the v4 pool behind it. Creation is permissionless: a
hook is fully determined by its pool and coin pair, so whoever creates it
first creates the same hook.

## How a swap works

The hook fills the whole swap in `beforeSwap` (`beforeSwapReturnDelta`), so v4's
own AMM math runs on zero. How it trades with Curve is detected per pool when
the hook is created:

| Pools | Mode |
|---|---|
| stableswap-ng without rebasing coins, twocrypto-ng | **received**: the input goes from the PoolManager straight into the Curve pool, and `exchange_received()` pays the output straight into the PoolManager |
| everything else: legacy stableswap and crypto pools, tricrypto-ng, stableswap-ng holding rebasing coins | **classic**: the hook takes the input, calls `exchange()` with an allowance, and forwards the balance it received (legacy pools return nothing from `exchange()`) |

Crypto pools (with `gamma()`) are called with `uint256` coin indices, stable
pools with `int128`. Curve's native ETH (`0xEeee…`) becomes v4's native
currency. Exact-output swaps start from `get_dx()` where the pool has it (else a
reverse `get_dy()`) and are refined with `get_dy()`, which matches `exchange()`.

Every swap emits `CurveHookSwap(sender, origin, zeroForOne, exactInput,
amountIn, amountOut)` for tracking (`sender` is the contract that called
`PoolManager.swap`, `origin` is `tx.origin`), plus OpenZeppelin's standard
`HookSwap` for indexers, since v4's own `Swap` event shows zero amounts.

## Setup

```
uv sync
cp scripts/networks.example.py scripts/networks.py   # then fill in NETWORK and ETHERSCAN_API_KEY
uv run pytest                                        # fork tests against NETWORK
```

`NETWORK` is an Ethereum mainnet RPC. Transactions are signed with the brownie
keystore `~/.brownie/accounts/babe.json` (password prompt); pass `--keystore`
to use another one. Private keys are never read from the environment.

## Deploy the factory

```
uv run python scripts/deploy.py           # dry run on a fork of NETWORK
uv run python scripts/deploy.py --live    # deploy
```

This deploys the `CurveHook` implementation and `CurveHookFactory` (admin: the
deployer), verifies both on Etherscan and records the addresses in
`deployments.json`, which the other scripts read. Commit that file.

## Add pools

```
uv run python scripts/create_hook.py POOL                # coins 0 and 1, dry run on a fork
uv run python scripts/create_hook.py POOL --coins 1 2    # another pair of a 3+ coin pool
uv run python scripts/create_hook.py POOL --live         # create it
```

The script mines the CREATE2 salt that gives the clone its hook flags (about
16k tries, well under a second), calls `create_hook(pool, i, j, salt)`, and
prints the hook, its swap mode and the v4 pool key and id. The factory creates
the v4 pool (currencies sorted, fee 0, tick spacing 1) at Curve's current price.

A dry run without a factory in `deployments.json` deploys a fresh one on the
fork first, so pools can be tried before anything is deployed.

On chain: `get_hook(pool, i, j)` (coins in either order), `hook_count()`,
`hooks(n)`, `pool_key(hook)`, and a `HookCreated` event per hook.

## Watch activity

```
uv run python scripts/list_hooks.py          # hooks with their Curve pools and v4 pool ids
uv run python scripts/list_hooks.py --txs    # plus every swap through each hook
uv run python scripts/watch.py               # print swaps as they happen, with a per-origin summary
```

Both default to the factory in `deployments.json`; pass `--factory ADDR` or hook
addresses to look elsewhere. `watch.py` picks up hooks the factory creates while
it runs, and stays a few blocks behind the head (`--confirmations`) because
load-balanced nodes lag each other.

## Limitations

- **The input comes out of the PoolManager's own balance** before the swapper
  pays it in. For tokens the PoolManager holds little of, routers have to
  settle the input before the swap, and the v4 Quoter cannot simulate the swap.
- **Rebasing coins (stETH)** work for exact input only. They arrive a wei or two
  short, so exact output reverts, and stETH input has to be paid in before the
  swap with a couple of spare wei (the Universal Router's `SETTLE_ALL` pays the
  exact debt and falls short).
- **Legacy pools' `get_dy()`** can overstate `exchange()` by a wei, so classic
  exact-output swaps aim a wei higher.
- **Uniswap's own routing will not find these pools**: its router skips v4 pools
  without liquidity or TVL, and hooks returning deltas need allowlisting.
  Traders that simulate hooks can use them.
- **No v4 fee**: the Curve pool's fee is the only one. Exact-output swaps can
  leave dust in the hook, which the factory admin can `sweep()`.

Gas through the Universal Router, 1000 USDC -> USDT: about 258k via a received
hook (stableswap-ng) and 261k via a classic one (3pool), against 164k and 139k
calling those Curve pools directly.

## Layout

```
contracts/CurveHook.vy          hook implementation, cloned per pool and coin pair
contracts/CurveHookFactory.vy   creates, initializes and records hooks
scripts/hooks.py                shared helpers: deployment, salt mining, Etherscan verification
scripts/deploy.py               deploy the implementation and factory
scripts/create_hook.py          create a hook for a Curve pool
scripts/list_hooks.py           list hooks, pools and swaps
scripts/watch.py                follow swaps live
tests/                          fork tests on real pools of every Curve family
```

## Deployments (Ethereum mainnet)

The factory's addresses go in `deployments.json` once deployed.

Before the factory, a single standalone hook (commit `e0b0a17`) was deployed
for the USDC/USDT stableswap-ng pool `0x4f493B7dE8aAC7d55F71853688b1F7C8F0243C85`:
hook `0x9A70b6cd21Be86B3AC68A55c956bBb1769Db2888`, v4 pool id
`0x25a8bef6e779bc0b14a0252c0efb21fe3600ebdc0ff6d3f22731c205233f1365`.
`watch.py` and `list_hooks.py` still read it when given its address.
