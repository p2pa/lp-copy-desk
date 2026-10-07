# lp-copy-desk

A copy-trading desk for Uniswap v4 liquidity on Robinhood Chain. It finds wallets whose LP positions make money,
replays what copying them would have earned at your size and your delay, picks a squad from those replays, and then
copies the squad's new positions live: same pool, same range, your size. It exits when the leader exits.

Everything is driven from one command line, `./lpdesk`, and every command also speaks `--json`.

This code ran live with real money from 2026-09-28 to 2026-10-07 as part of a larger private bot. It is extracted here
whole, with its tests and the backtests behind every setting.

## How it did (honestly)

The live desk's own record, 09-28 to 10-07: **1,180 copies closed, net -$11.96** on a book of roughly $200-600.

| | |
|---|---|
| 09-29 to 10-01 | +$296 |
| 10-02 to 10-04 | -$345 (launch-pool dumps, before the launch rules below existed) |
| 10-05 to 10-07 | +$37 |

Copying works when the leaders are good and the pools are not launches. It loses when a pool is minutes old, when its
creator still holds the liquidity, or when the desk is the last buyer in a thin pool. Most of the rules in
`chains/robinhood/chain.example.toml` exist because one of those happened. Each rule carries the backtest that set it.
Treat the numbers in those comments as the desk's history, not as a promise.

## How it works

```
history store (copydesk/lphist)    every v4 swap, position and LP NFT on the chain, kept locally (~100 MB a day, 14 days)
        |
discovery (copydesk/lpfind)        every wallet that minted >= 7 positions over 7 days on >= 2 days
        |
replay (copydesk/lpcopy, lplive)   each wallet's positions copied at OUR size and OUR measured delay, with our gas and the
        |                          desk's live rules (the same code path the desk uses to decide)
score (copydesk/lpdb)              (mean - one standard error) x copies a day x trust (how well the replay reproduces the
        |                          wallet's own P&L); validated blind: its top 20 earned +1.16 %/copy the next day vs +0.08 %
pick (copydesk/lpopt)              mains (2), bench, filler, written into chain.toml [lp.follow]; repicked hourly
        |
the desk (copydesk/lpfollow)       a websocket on the PositionManager: a squad mint -> checks -> our mint in the same range
                                   -> exit when the leader's liquidity falls under half its peak
treasury (copydesk/treasury)       the only place USDG <-> ETH converts; keeps the gas band; sells dust
exit routing (core/exec/lp)        sells the token side wherever pays most: every hookless USDG / ETH pool of the token,
                                   quoted exactly, and Relay's quote beside them
```

The desk's checks before a copy include:
- pool age (no pools under 2 h)
- the launch creator still in
- a cold pool (last hour's volume z-score) and a price-flat pool
- a thin pool (our share of active liquidity)
- the entry swap's cost
- squad agreement for filler copies
- a token cool-off after losses
- per-token and per-wallet caps
- slot and capital room by tier (main > bench > filler; a main copy closes lower tiers' copies to make room)

## Install

```sh
git clone <this repo> lp-copy-desk && cd lp-copy-desk
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./lpdesk init        # writes chains/robinhood/chain.toml from the example (gitignored)
./lpdesk smoke       # every module's offline selftest
./lpdesk doctor      # what is still missing
```

Outside the repo, in `~/.config/lp-copy-desk/` (none of these are ever committed):
- `wallets.json`: `{"lp-1": {"address": "0x..", "key": "0x.."}}`, mode 600. Use a wallet that holds only what the desk may lose.
- `telegram.json` (optional): `{"token": "...", "chat_id": "..."}` for the desk's cards.
- `hypersync.token` (optional): an envio.dev HyperSync token, used by wallet forensics.
- `rpc.url` (optional): a keyed endpoint, read before the public ones in `chains/robinhood/rpc.toml`.

## First run

```sh
./lpdesk hist backfill 10        # build the history store (hours on public endpoints)
./lpdesk nightly --dry           # discover, replay, score: who would it pick?
./lpdesk pick --apply            # write the squad into chain.toml
./lpdesk run                     # the desk WITHOUT --go: it hears, decides and logs, signs nothing
LIVE_OK=yes ./lpdesk run --go    # live. Fund the wallet with USDG and a little ETH first
```

Then schedule it: `ops/crontab.example` has the minute ingest, the hourly repick, the nightly, the weekly checks, and
`ops/supervise.sh`, which restarts the desk if it dies.

## Commands

`./lpdesk help` lists them all. The ones you will use:

| | |
|---|---|
| `status` | every open copy checked against its leader and the chain, the record, the exit sales |
| `watch` | full screen and live |
| `log [N\|tokenId\|SYM\|0x..\|skips H\|errors H]` | the desk's own records, one copy at a time |
| `stop` / `resume` | no new copies (open ones still exit with their leaders) / copying again |
| `close <leader tokenId>` | close one copy through the desk's own exit |
| `treasury [resume]` | conversions, gas band, dust |
| `pick [--apply]`, `qc`, `find board` | who to follow |
| `wallet <addr>`, `copy <addr>`, `backtest <addr>` | one wallet: its P&L, and what copying it would have made |
| `tune`, `tune downside`, `tune recency` | the blind fold tests behind the desk's settings |

## Safety

- Nothing signs without `--go` AND `LIVE_OK=yes` in the environment.
- `./lpdesk stop` writes the desk's STOP file: no new copies. The desk keeps running so open copies still exit with their leaders.
- `stop_drawdown` pauses copying for `stop_pause_h` hours after a drawdown from the realized high-water mark.
- The fork tests run every write path against an anvil fork. The fork has a private chain id (real + 1,000,000), only local URLs, and throwaway keys, so a signature made there can never reach the real chain.

## Tests

```sh
./lpdesk smoke                     # offline
.venv/bin/python -m tests.copyfork # 37 asserts: entry, failed mint undo, ETH pools via the treasury, tiers, exits, the books to a cent
.venv/bin/python -m tests.exitfork # 7 asserts: a token sold through the best route (needs var/lp_heat.json from `hist ingest`)
.venv/bin/python -m tests.ladderfork  # Delta Liquidity ladders as one copy (needs the history store and a recent ladder; see CLAUDE.md)
```

The fork tests need foundry's `anvil` (`ANVIL=/path/to/anvil` if it is not in `~/.foundry/bin`).

## Layout

```
lpdesk, cli.py         the command line
copydesk/              the desk, the replay, the score, the pick, the history store, the treasury, the Telegram card
core/                  the slice of the engine the desk uses: chain config, RPC, v4 tick maths, LP execution
                       (approve, swap, mint, exit on PositionManager / UniversalRouter), signing, Telegram
discovery/, research/  wallet forensics (HyperSync) and pool features at entry
chains/robinhood/      chain.example.toml (every desk setting, with why), rpc.toml (public endpoints); var/ is state
docs/                  the research behind it: LPWALLETS (finding wallets), LPAGENT, LPML (a model that did not beat
                       copying), DELTA (managed ladders)
tests/                 the fork tests
ops/                   crontab.example, supervise.sh
```

## Scope

- **One chain:** Robinhood Chain (4663), Uniswap v4, USDG and native-ETH pools without hooks. Another chain needs its own `chains/<name>/` config and a look at the token / router assumptions in `core/exec/lp.py`.
- **Licence:** none chosen yet.
