# Delta Liquidity on Robinhood: what it is, where the value is, what we do about it

Status: done 2026-09-27. Verdict: nothing to build now; watch for Delta's own AMM (sections 4 and 6). Sources: deltaliquidity.app/docs + academy, the app's JS bundles,
the contracts' bytecode (selectors resolved against openchain), and every tx/log from hypersync
(`tmp/delta/onchain.py`, `tmp/delta/sels.py`). Blockscout sits behind a Cloudflare challenge and the contracts
are not on Sourcify, so no verified source was read. Everything below is marked measured / read from code / from docs.

## 1. What Delta is (from docs)

A UI and contract layer over existing Uniswap pools on Robinhood (v3, and v4 for orders). It creates no pools.
Three products:

| Product | What the user gets | Delta's cut | Contracts |
|---|---|---|---|
| **Pools / Ladders** | a "shaped" LP position (Spot / Curve / Bid-Ask), 1-40 bins = 1-40 separate Uniswap positions, minted into a Delta manager that holds the NFTs | 7.5% of fees CLAIMED (v8, v9); 1% on v1-v7 | DeltaLadderManager v1-v9, DeltaPositionBuilder v9 |
| **Stakes** | one vault per WETH pool: a wide Spot position, shares staked in a farm, fees paid out in WETH **streamed over 7 days** | 7.5% of claimed rewards | VaultFactory, VaultFarmFactory, DeltaZap |
| **Limit orders** | a single-sided position in ONE bin around a target market cap; fills as price trades through it; "closes automatically" when 100% filled | 1% of the FILLED principal (fees earned while filling go to the user) | DeltaRangeOrderManager |

Fees: 0 on deposit, withdraw, stake creation. No lockups, no exit penalty.

## 2. Contract map (read from bytecode)

| Contract | Address | Size | Surface (resolved selectors) |
|---|---|---|---|
| VaultFactory | 0x68EDc4948F60D21c4a7Dcbb8Ed4500cE6D0b153c | 14.8 kB | `createVault(pool)`, `allVaults`, `vaultOf`; the VAULT code is embedded: ERC-20 shares, `deposit(a0,a1,min0,min1,to)`, `withdraw(shares,min0,min1,to)`, `harvest()`, `flushPendingRewards()`, `pendingRewards()`, one v3 `positionId`, `tickLower/Upper`, `feeBps`/`treasury`, `farm` |
| VaultFarmFactory | 0x2bdA3FeB985d812a5932fe59eD4D8627BA3A10d1 | 10.4 kB | `createFarm(vault)`; the FARM: `stake`/`stakeFor`/`unstake`/`exit`/`emergencyUnstake`, `claim`, `compound(a,b)`, `notifyRewardAmount` (only `rewardNotifiers`), `rewardRate`, `periodFinish`, `rewardsDuration` (7 d), `accRewardPerShare`, `undistributed`, `claimFeeBps` |
| DeltaZap | 0xC0b8eC7589ee49c53305517bFd53BEd708392294 | 4.5 kB | one-coin -> balanced deposit through `swapRouter` (zapInToken/ETH/Both, zapOut per the bundle ABI) |
| DeltaRangeOrderManager | 0x24F4A03352e9fACd1005Aa35A7822c83A140F7f7 | 27.8 kB | v3 NPM **and** v4 PositionManager + StateView + Permit2; `orders(id)`, `nextOrderId`, `currentTick(id)`, `MAX_RUNGS`, `owed`/`claim`/`claimFor`, `feeBps`, `accruedProtocolFees`, `createPaused`. **No `keeper()`**, unlike every ladder manager. 18 selectors unresolved |
| DeltaLadderManager v9 | 0x83551A233f6D7b3cb4dEFBF55AbFD887C6811ce4 | 29.3 kB | `keeper`/`setKeeper`, `feeBps`, `owed`/`claim`, Merkl rewards (`MERKL_DISTRIBUTOR`, `claimMerklRewards`, `rewardsRoot`), `openPaused` |
| DeltaPositionBuilder v9 | 0x56A6e3a7Fe1cE9263Be6441DD6e4A587683e75e7 | 5.0 kB | mints the v3 positions for v9 ladders (`uniFactory`, `weth`) |
| DeltaLadderManager v1-v8 | see docs | 18-22 kB | same family: `keeper`, `depositV3(tokenId)`, `collectV3(tokenId)`, v3 + v4 |

Not in the docs, found in DefiLlama's adapter (`projects/delta-liquidity`): **RouterV3** 0x46dFEa430d1F069C129E26445319562e29f39C47,
**RouterV2** 0x75A361513Ccad1b1E9AAe33458764Df06001DC43 (Chainlink-automation style: `checkUpkeep`/`performUpkeep`, `keepers`,
routes with market-cap "milestones", cadence 600 s-30 d, oracle `feeds` with `FEED_MAX_AGE`, `MAX_SLIPPAGE_BPS` 1000, `fund`/
`fundETH`/`fundToken`, owner `emergencyWithdraw`) and the legacy **FarmFactoryV2** 0xc6F0E707574Fce5Da8B125edD9529DbAF985e62A (v2 LP farms).
The ladder managers emit `ManagedOpenV4(owner, poolId, tokenIds)`.

How the pieces fit:

```
user ──zap (DeltaZap: swap half via swapRouter)──> Vault (1 per WETH pool, full-range v3 NFT, ERC-20 shares)
                                                     │ harvest()  [anyone]  -> fees -> pendingRewards
                                                     │ flushPendingRewards() [gated] -> Farm.notifyRewardAmount
                                                     └> Farm (MasterChef + 7-day stream, rewardToken WETH) -> stakers claim / compound
user ──open ladder──> LadderManager v9 ──> PositionBuilder v9 mints 1-40 v3 positions (or v4 via PositionManager)
                          keeper (0xb1c2bbf8, 0xfb18421f): bookkeeping only (no swaps); 7.5% of claimed fees -> treasury
user ──limit order──> RangeOrderManager: 1 bin single-sided v3/v4 position, up to 20 rungs
                          settle 0x5a993221(orderId, bytes) by Delta's keeper 0xcb09010b, 1% of filled principal
treasury ──weekly──> buy DELTA on the open market, burn (80% of revenue, falling 5 pp/month to 60% by Jan 2027)
Router v2/v3 ──performUpkeep by keeper 0x8fbbf24a──> "liquidity-depth budgets" into pools (DORMANT since 07 Aug / 31 Aug)
```

## 3. On-chain activity (measured, hypersync: every tx to these contracts since block 15M, 70,469 txs)

| Contract | txs | ok | wallets | active | last 7 d |
|---|---|---|---|---|---|
| LadderManager v1-v9 | 60,210 | 82-98% | ~1,000 (v7 603, v8 459, v9 456) | 07 Aug -> now | v9 5,340 (the live version) |
| RouterV2 / V3 | 9,502 / 123 | 99-100% | keeper 0x8fbbf24a: 9,490 + 106 `performUpkeep` | 22 Jul -> 31 Aug / 07 Aug | 0 (dormant) |
| DeltaZap | 421 | 87% | 184 | 30 Jul -> now | 24 |
| RangeOrderManager | 166 | 100% | 22 | 23 Sep -> now (4 days old) | 166; 86 orders ever, 11 settled |
| VaultFactory / VaultFarmFactory | 20 / 20 | 100% | 16 | 30 Jul -> 25 Sep | 3 |

Ladder usage per day (all versions, txs / wallets): peak **09-07 2,930 / 273**, then 09-14 642/124, 09-20 249/57, 09-24 1,399/149,
09-27 315/72. Not growing today: the bet on Delta is a bet on the chain's next leg, not on a current trend.

**Stakes, live state (read 2026-09-27 ~20:00 UTC):** 20 vaults, 11 with a live stream. Every vault is a FULL-RANGE v3 position
(ticks +-887200). All live streams end 164.0 h from now: one keeper harvested + notified every farm together ~4 h earlier. Total
staked **18.1 ETH**, total stream **0.042 ETH/day for everyone**. APR 1-233%, but the high ones are tiny (0.49 ETH at 233%);
the DELTA/WETH stake (11.8 ETH) streams at 78%. Each vault owns 0.0001-5.5% of its pool's active liquidity. The vaults' `feeBps`
reads 100 (1%); the docs say 7.5% (on ladders). Farms' `claimFeeBps` = 0.

**Limit orders:** 86 ever, 11 settled, all by Delta's keeper `0xcb09010b` via `0x5a993221(orderId, bytes[1])`, minutes to hours
after the fill. Settling from a stranger address and from the keeper returns the SAME revert on every order (`0xc5a70202` on 75,
`0xb8f088ff` on the 11 settled), so settle is probably not caller-gated; none is settleable now, and the settler earns nothing.

**Who calls what (simulated with eth_call, nothing sent):** `vault.harvest()` - anyone. `flushPendingRewards()` - gated
(`0x1f2a2005`). Router `harvest`/`route` - keeper only (`0xf512b278`). Ladder keeper txs carry no swaps (bookkeeping events only).
A ladder open is 7-8 position mints; the only Delta-originated SWAP flow is DeltaZap (421 txs ever) and the weekly buyback.

**Size vs claims:** Delta reports ~$2M TVL, ~$3M fees claimed, 18k positions. DefiLlama's custodied basis is ~$26k (positions sit in
user wallets or are not counted). Third-party write-ups (FinTrender 2026-09-22) put on-chain burns at ~22 ETH (~$54k) against a
"$600k/month" revenue headline that monthlises one week.

## 4. Every angle, tested

| Idea | Mechanism | Measured | Verdict |
|---|---|---|---|
| **Fill limit orders with a flash loan / v4 flash accounting, pocket the difference** | A Delta order is not an order book entry: it is a 1-bin Uniswap position. It "fills" only when someone SWAPS through the pool at the pool's price. There is no fill function that pays the limit price. The only profit is a cross-pool gap, which is exactly what CycleArb already takes with v4 flash accounting and zero capital | 86 orders ever, ~$ small | Nothing new to build. Covered by the arb engine wherever the pool is indexed |
| **Settle filled orders (keeper desk)** | settle is probably callable by anyone | no reward to the caller, 11 settles in 4 days | No value |
| **Stake stream lag (LP desk)** | fees are streamed over 7 days to whoever is staked NOW, so a late staker shares fees earned before joining; memecoin volume decays, so last week's stream is usually richer than this week's | the whole stream is 0.042 ETH/day across all stakes | Real but tiny: taking half of every stream is ~0.02 ETH/day, with full-range IL on memecoins. Not worth capital |
| **Stakes as an LP product** | full-range v3 + 1% vault fee | our LP desk's +-100% positions beat full range (backtested) | Worse wrapper than what the LP desk already runs |
| **Router flow (arb)** | keeper-timed swaps into thin pools within 10% of an oracle | dormant since 31 Aug | Watch only: if it restarts, its `performUpkeep` is a predictable opener (cadence known) |
| **Weekly DELTA buyback (arb)** | a treasury buy of DELTA on the 1% v3 pool dislocates it against DELTA's other pools | ~22 ETH over ~7 weeks, ~3 ETH/week; the DELTA v3 pool is already an arb target (pools.json) and 10 DELTA pools are in the LP universe | Already covered by the desk. See burn log below |
| **Ladder liquidity (arb)** | 18k shaped positions = ordinary Uniswap liquidity, often Bid-Ask (thick at the edges, thin at price) | opens ~100-150 wallets/day | No special flow. Makes gaps near the current price slightly cheaper to move, which the engine already prices |
| **Delta's own AMM (in audit, per CMC/FinTrender)** | a new venue where liquidity migrates from Uniswap; new contract type, no rival bots on day one | not launched | **The one real opportunity.** First mover on a new venue is the classic arb edge: adapter + graph pools on launch day |
| **Discovery signal (LP desk)** | Delta's pool list ranks by 24h fees/TVL; `ManagedOpenV4` shows where Delta LPs pile in | - | Minor: our lpscreen measures the same fees/depth directly; a pile-in is a depth increase = lower share for us |

## 5. Weaknesses (design and trust, not exploits)

- **Stream lag transfers value from early to late stakers** (above). Economic, small, and documented as a feature.
- **Filled orders sit unsettled for minutes to hours** (settled by one keeper). A range order that is not withdrawn un-fills when
  price comes back: the user's "fill" is not locked in until the keeper acts.
- **Every parameter is owner-set**: `setFeeBps` up to `MAX_FEE_BPS`, `pause`, `setTreasury`, `sweepTokens`, router
  `emergencyWithdraw` (the routers' funded budgets), farm `recoverERC20`, `setRewardsDuration`. A trust assumption on a
  pseudonymous team, not a bug.
- **Zaps swap half the deposit** through a router with the user's slippage. Robinhood has no public mempool, so this is not
  sandwichable by outsiders, but it does move thin pools (an opener our engine may take).
- **Unverified contracts**: none of the sources is on Sourcify, and the explorer is behind a Cloudflare challenge. The Sherlock audit
  covers ladders v9 only.

## 6. Game plan

1. **Arb desk - nothing to build now.** Delta adds no swap flow of its own beyond zaps and the weekly buyback, and both land on
   pools the desk already carries or the graph overlay would pick up. The flash-fill idea reduces to the cross-pool arb the engine
   already runs.
2. **Watch for Delta's AMM (the one to be early on).** A watcher on Delta's deployer and ownership addresses: any new contract
   they deploy or any factory-style event pages us. On launch: read its swap interface, add a `venue` adapter
   (CycleArb already takes CALL hops, `exec contract allow-v2` style), add its pools to the graph overlay.
3. **LP desk - no Delta product beats our own positions.** Keep the `lpscreen` as is. Optional: use `ManagedOpenV4` pile-ins as a
   crowding warning on pools we hold.
4. **Revisit when** ladders pass ~500 wallets/day again, the routers restart, or the stake streams pass ~1 ETH/day total.
5. **Check separately (affects our desk, not Delta):** FinTrender says Robinhood's 90-day gas subsidy ends **2026-09-29**. Our
   revert cost ($0.005) and the $5/day gas cap assume today's gas price. Verify, and re-price the fast / predicted paths if it moves.

## Appendix: the DELTA burns (hypersync, Transfer to 0x..dead / 0x0)

| when (UTC) | from | DELTA |
|---|---|---|
| 09-08 Tue 03:06 | 0x266311a44e70363b3d168bcb38cf38cbf2ccc51f | 2,414,692 |
| 09-11 Fri 02:12 | same | 912,097 |
| 09-13 Sun 04:36 | same | 993,449 |
| 09-17 Thu 18:25 | same | 684,363 |
| 09-25 Fri 15:25 | same | 399,144 |

5.40M DELTA burned (0.54% of supply) by one treasury address, at irregular times (not a fixed weekday or hour), shrinking each
time. Not a schedule anyone can position ahead of; the buys that precede them land on pools the desk already carries.

Reproduce: `tmp/delta/onchain.py` (txs), `tmp/delta/sels.py` (selectors), `tmp/delta/farms.py` + `apr.py` (stakes),
`tmp/delta/an.py` (activity). `https://deltaliquidity.app/api/farms/topology` lists every vault/farm/pool.
