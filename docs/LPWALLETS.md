# Finding LP wallets worth copying on Robinhood (2026-09-28, in progress)

Goal (owner): wallets that are very active and win small, steadily - better than our LP desk - found on chain, to copy.

## Tools

- `copydesk/lpportfolio.py <address>` - an address's Uniswap v4 positions rebuilt from chain, laid next to LP Agent's view
  (`check` against `copydesk/fixtures/lpagent_0x78a1003e.json`, the owner's paste of app.lpagent.io for 0x78a1003e).
  Per position: pool + every liquidity change (PoolManager ModifyLiquidity rows with salt = tokenId: helper contracts cannot
  hide it), principal from the liquidity math at StateView.getSlot0(block-1), fees = the payout (PoolManager -> owner
  Transfers) minus that principal, zero-delta rows are fee collections, and REALIZED = USDG back + the owner's own sale of the
  returned token within ~10 min.
- `copydesk/lpwallets.py` - v3 (exact NPM amounts) per-wallet ranking; v3-only, running.

## How close we are to LP Agent (0x78a1003e, 12 historical positions)

- age: exact. invested (ETH at open, ETH/USD from the deepest v3 WETH/USDG pool): 12/12 within 0.0001 ETH.
- fee %: all 12 within ~0.07 pts (USD terms: fee_usd / invested_usd).
- PnL %: LP Agent's is ETH-denominated and values the returned token at a price that is not the pool's: #3353835 USDG/ERRAND
  shows **+8.00%**; at the pool price it is -7.22%; the wallet sold the 132,755 ERRAND 37 blocks later for 71.20 USDG, so the
  REAL round trip was 200.00 -> 182.82 USDG = **-8.59%**. Where the sale was found: NOSH +2.25 (LPA) vs +1.45 realized, FELS
  +0.35 vs -1.41, HYDX +0.90 vs -0.66, CPU +1.40 vs +2.88. LP Agent's PnL cannot be matched exactly where its price is wrong;
  the realized column is what a copier earns.

## 0x78a1003e, the wallet level (7 days, measured)

- USDG flows: -31,583 into positions (helper 0x56044b4f), +29,519 back (PoolManager), +6,799 token sales and -4,500 token buys
  through an aggregator (0x6e2a35a7 with executor 0x69a52a05); USDG 501.37 -> 543.55 reconciles to the cent. Gas ~0.012 ETH for
  942 txs.
- Net worth ~$865 -> ~$1,109 (LP Agent: 0.42 ETH): **+$244 / week, ~+28% on the book, every cost included**. LP Agent's
  "avg monthly profit 0.62 ETH" (~$380/week) overstates by ~55%.
- Pattern: one-sided USDG bids just below the price in trending memecoin/USDG v4 pools, $100-370 each, ~50 a day, held 3 min
  to 10 h, the returned token sold within minutes.

## `copydesk/lpwallet.py`: any address in seconds, reconciled (2026-09-28)

Replaces the per-event pricing of `lpportfolio.py` (minutes a week, archive-bound). One topic-indexed hypersync query of
every Transfer to or from the wallet carries the book (PositionManager NFT mints/burns included): 7 days in ~5 s, 30 days in
~20-50 s (the hypersync token is shared: 30 requests/min). `rank <addr..>` for many wallets.

What made it trustworthy, each found by a reconciliation that did not add up:
- A ladder (several NFTs in one mint tx) is ONE position; giving each NFT the whole deposit invented a -$10.5k day.
- DECREASE-to-zero without a burn closes a position; its returns are the wallet's PositionManager calls, decoded
  (`modifyLiquidities`, inside `multicall` too) to the tokenIds they name.
- Returned tokens are matched to the sales that happened (FIFO). A token-for-token swap moves the lot into the new token;
  a sale for native ETH is credited once and taken out of the native leg (it was counted twice).
- A Transfer log is a claim, the balance is the fact: INVOKER spam "sent" $931 that `balanceOf` says is 0. Lots are
  trimmed to the real balance at the pinned block.
- Every "now" read is pinned BY BLOCK HASH to the last block the ledger covers. blockmachine load-balances over a node
  that ignores the block number (0.121589 ETH for this wallet at every block from 44M to 70M); by hash it must error.
- Held tokens without a DexScreener price are marked at the wallet's own last trade price, not $0.

The guard: the wallet's own change (USDG/WETH cash of its txs + tokens held + open positions + native ETH at both ends) is
split into LP book, positions opened before the window, open uPnL, directional trades, skipped ladders and the native
leg. What is left is printed as UNEXPLAINED, and a report whose rest exceeds max($30, 0.1% of turnover) says
DOES NOT RECONCILE on its first line. 0x78a1003e: +$26 (7 d), +$19 (21 d), +$25 (30 d) on $34k-$122k turnover.

LP Agent, checked on receipts: tokenId 3367966 put 350 USDG in and got 349.999999 back, no fee, no token (LP Agent:
+1.87% fee, +2.51% P&L). Open #3376639 USDG/VAULT is out of range, all VAULT: value + fee - invested = -0.0257 ETH;
LP Agent prints +0.0257 (+22.69%, DPR 544%). Its invested, value, fee and range match ours; its P&L sign does not.

0x78a1003e (measured 2026-09-28 ~10:30 UTC):

| window | wallet P&L | LP book | directional | native leg (gas inside) | green LP days |
|---|---|---|---|---|---|
| 7 d | +$456 | +$491 | +$110 | -$100 | 8/8 |
| 21 d | +$592 | +$1,267 | -$20 | -$617 | 16/22 |
| 30 d | +$12 | +$1,773 | -$846 | -$769 | 22/31 |

The LP book is steady; the wallet gives it back in directional token bets (mostly the first 9 days) and in its native-ETH
leg (gas $394 plus token trades routed through ETH). Copying the LP legs alone is the question, not copying the wallet.

## Paper copy of 0x78a1003e (`copydesk/lpcopy.py`, 7 days to 2026-09-28)

Same pool, ticks and USD as each of its closed positions, entered and exited `d` blocks after it; fees swap by swap from the
pool's Swap logs at our share of active liquidity (our own liquidity added to the denominator); the token side sold at the
pool price minus the pool fee.

Validation (d = 0, its own liquidity) against its real realized P&L: correlation 0.956 over 147 positions, median error
$0.00, 105 of 147 within $1, but +$96 in total (model $508 vs real $413): the misses are the big fills, where selling the
returned token cost more than the pool fee (slippage). So copy numbers get the same 0.81 haircut.

| | model, before gas | x 0.81 |
|---|---|---|
| d = 0 (validation) | +$510 | +$413 (real) |
| copy +7 s | +$444 | ~+$360 |
| copy +30 s | +$457 | ~+$370 |

The edge does not decay with a 7-30 s lag: it is range choice, not speed. Unmodeled: our sale of the same returned token
right behind its own (both dump into one thin pool), and capital (~the same $500-1,000 it runs). Runtime is ~9-16 min, almost
all of it the hypersync pass over the pools' Swap logs (cached per address and window afterwards).

## The copy desk goes live (2026-09-28 15:14 UTC, owner's call)

The deep-LP program on robinhood is replaced by the copy desk (`copydesk/lpfollow.py`, supervised with `--go`):
- the old desk's crons (`lp daily`, `lp guard`) are commented `#STOPPED-2026-09-28`; `lp exit-all --go` closed its four
  positions (CEREBRO, 富贵, MONITOR, RAXOL: book value $147) into $145.33 USDG in lp-1, verify OK (0 NFTs);
- leader 0x78a1003e, $25 per copy, at most 4 open and $100 deployed (owner: start small); only positions the leader
  opens after the start are copied (the first poll sets the NFT cursor, #3388894);
- backups: kept outside the repo.

Backtest behind the size (7 days, copies 7 s late, the model's 0.79 validation ratio applied, our gas $0.058 a
cycle): $25 copies +$64/week, $50 +$137, $100 +$283 at $300 capital; 7/8 green days, worst day -$2 to -$5. A
fixed size overweights the leader's small positions (its own dollar-weighted return is ~1.2% of turnover, the
fixed-size copy ~1.9%), and slippage above ~$100 a copy is unmodeled.

Rollback: `touch chains/robinhood/var/lp/follow.STOP` (no new copies; open ones still exit with the leader), or stop the
daemon and uncomment the two cron lines.

### Backtest correction (same day): the fee share

lpcopy took our share of a swap's fee from the active liquidity the Swap log reports, which is AFTER the swap: a swap
that leaves the range reports the next tick's (often tiny) liquidity, and 0xa2161edc's model read +$26,135 against
+$419 real ($9,649 of fees on a 1-minute $966 position). The share now uses the larger of the liquidity before and
after the swap, capped at 1 (conservative). Recalibrated, 7 days:

| leader | real | model, no delay | copy +7 s (model) | verdict |
|---|---|---|---|---|
| 0x78a1003e | +$388 | +$362 (ratio 1.00) | +$334 | model conservative; the copy keeps 92% |
| 0xa2161edc | +$412 | +$894 (ratio 0.46) | +$362 | still 2.2x optimistic: $1,000+ positions held 1-5 min, often most of the active liquidity (just-in-time style); not copyable on this evidence |

0x78a1003e at the live size ($25 a copy, $100 capital): **+$48/week**, 6/8 green days, worst day -$3.39 (the first
table's +$64 used the inflated share). $50 copies: +$103 at $100 capital (9 skipped), +$105 at $300.
