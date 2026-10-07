# LP Agent (lpagent.io): copy-LP on Robinhood, and what it means for us

Status: done 2026-09-28. Verdict: nothing to trade against; one LP-discovery test worth running with a free API key (section 4). Sources: lpagent.io, docs.lpagent.io (llms.txt, Robinhood integration, Robinhood copy
best practices), app.lpagent.io bundles (`tmp/lpa/`), and on-chain scans (`tmp/lpa/copyscan.py`).

## 1. What it is

A liquidity-provider toolkit: portfolio tracking, a leaderboard of "smart LP" wallets, one-click COPY LP, AI position
suggestions, and an open data API. Solana (Meteora DLMM / DAMM v2) first; **Robinhood Chain** (Uniswap v3 + v4) live; Base wired in
the app. Founder Thanh Le.

**How a copy runs on Robinhood** (docs, "Copy LP Best Practices on Robinhood Chain"):
- each user funds an **agent wallet**: an ordinary EOA held by **Turnkey** (EVM; Privy on Solana) under a signing policy that
  lets LP Agent's servers transact but not withdraw. Automation only acts on positions opened from that agent wallet.
- when a followed leader opens a position, the server **swaps the follower's ETH into the pool's two tokens through an
  aggregator with up to 7% slippage tolerance**, then adds liquidity (1% slippage). When the leader exits: remove, swap back to
  ETH. **Four txs per copy cycle**, plus TP / SL exits.
- copy size = copy % x the leader's position, capped by "Max Increase Amount"; the app suggests copy % = your net worth / the
  leader's. Advice: >= 0.08 ETH net worth, leaders with average hold >= 1 h.
- **fees**: 0% to open/close, **8% of claimed fees**.

**No LP Agent contracts.** Everything is off-chain automation signing from users' own EOAs straight into Uniswap's position
managers and an aggregator. The public API serves Robinhood READS only (positions, historical positions, pool discover, pool
positions, on-chain stats, top LPers); every transaction endpoint is Solana-only. The app's internal API (`api.lpagent.io/api/v1`:
`smart-lp`, `pools/discover`, `pools/{id}/top-lpers`, `pools/{id}/robinhood-detail`, `token/robinhood/swap`) sits behind a
Cloudflare challenge; the open API needs an `x-api-key` (free tier exists; top-LPers needs Premium). Their own warning: Robinhood
PnL is the liquidity leg only (the up-to-four swap legs are not booked) and open positions are not live-valued.

## 2. On-chain footprint (measured)

A copy mirrors the leader's position, so a copier adds liquidity to the SAME pool and the SAME tick range seconds later.
`tmp/lpa/copyscan.py` scanned every Uniswap v3 (NPM) and v4 (PoolManager) liquidity add on Robinhood for 2 days:

| reading | value |
|---|---|
| liquidity adds scanned | 228,057 |
| same pool + range by another wallet within 120 s | 16,953 (mostly coincidence: full range, UI presets) |
| excluding full range, leader->follower pairs seen >= 3 times | **984 pairs, 319 leaders, 332 followers** |
| follower lag in those pairs | p10 1 s, **p50 7 s**, p90 68 s (LP Agent: "settled in under 5 s" + confirmation) |
| follower size / leader size (liquidity) | p10 0.02, p50 0.63, p90 11 |
| strongest pairs | 0x0860cd45 -> 0xc23cf804 239 copies in 2 days (9 s); leader 0x5c132d27 with 7+ followers at 170+ copies each, some at 0-2 s (possibly one operator's own wallets rather than customers - indistinguishable on chain) |
| follower wallets: native ETH | p50 **$52**, p90 $1,054 |
| follower wallets: tx count | p50 1,524, p90 7,424 |
| leader wallets: native ETH | p50 $38, p90 $1,436 |

Not every pair is LP Agent (a leader with one-block followers may be a single operator), but the copy mechanic is visible and
its tickets are small and frequent.

## 3. Weaknesses

- **Followers pay a lot to copy**: four txs per cycle, an aggregator swap allowed 7% slippage each way, 8% of claimed fees, and
  leaders who flip every few minutes (0x0860cd45: 239 adds in 2 days). LP Agent's own docs say copies under ~0.08 ETH cannot
  earn back the round trip.
- **Reported PnL leaves out the swap legs** and does not live-value open positions: the leaderboard overstates what a copier
  keeps.
- **Custody**: Turnkey (EVM) / Privy (Solana) policy wallets; LP Agent's servers sign. A server or policy compromise acts on every
  agent wallet at once.
- **Copies are predictable** (same pool, same range, ~7 s later), but on an FCFS chain with no public mempool nothing can be
  inserted between a leader and its followers, and nobody should.

## 4. Verdicts

| Desk | Verdict |
|---|---|
| **Arb** | Nothing. Follower zaps are ~$50-wallet aggregator swaps routed across pools; no mempool to act on and nothing worth backrunning after gas |
| **LP: use their contracts** | There are none. Their Robinhood rails are Uniswap's position managers plus an aggregator, which our `core/exec/lp.py` already drives directly |
| **LP: free-ride the leaders** | The one open question. Copying ourselves skips the 8% fee and the 7% slippage allowance, but it is only worth it if a leader is net-profitable INCLUDING swap legs and holds for hours (our costs, like theirs, are per cycle). Not measured: needs per-position P&L of the leaders |
| **New desk** | No. A copy-LP service is a product business, and a copy-trading desk against these leaders fails on the same per-cycle costs LP Agent warns about |

## 5. Game plan

1. **Free API key** (docs.lpagent.io/api-key-dashboard, free tier): `GET /lp-positions/historical?owner=<leader>&chain=ROBINHOOD`
   for the 319 leaders found above (the top-LPers endpoint needs Premium; the leader list comes from our own scan instead).
2. Reconcile each leader's closed positions against chain (add the swap legs LP Agent leaves out), keep leaders whose net P&L
   is positive over >= 20 closed positions with average hold >= 6 h on pools >= $25k deep.
3. If any survive, test "mirror their NEW positions from our LP pot" as a paper trade in `lp` for two weeks before any capital
   moves. This is the "smart LP wallet tracker" backlog item from docs/APE.md, with a ready-made leader list.

