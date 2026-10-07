# CLAUDE.md

Guidance for an agent working in this repo. Read README.md first for what the desk does and how it did.

## What this is

The copy LP desk: it copies chosen wallets' Uniswap v4 LP positions on Robinhood Chain at our own size, and exits when they
exit. Extracted on 2026-10-07 from a larger private bot (hook_bot), where it ran live from 09-28. Module paths are kept as
they were (`core/`, `copydesk/`), so a module's docstring may still mention `hookbot <chain> lp ...`. Here that command is
`./lpdesk ...`.

## Commands

```
./lpdesk smoke          offline selftests - run after any change
./lpdesk doctor         what the install is missing
./lpdesk status --json  the desk, machine-readable (every command takes --json; stdout is then only the JSON)
./lpdesk help           everything
```

Three test levels:
- **smoke:** offline.
- **fork:** `.venv/bin/python -m tests.copyfork`; anvil, throwaway keys, private chain id.
- **live:** `run --go` with `LIVE_OK=yes`.

A change to anything that signs (core/exec, copydesk/lpfollow, copydesk/treasury) passes copyfork before it ships.

## Rules (they cost money to learn)

1. **Nothing changes what the desk takes, sizes, keeps or closes without a backtest that beats live.** Crash, accounting and fail-safe fixes are exempt; they need only the selftest and the fork test. The procedure:
   - The replay must SEE the effect first. If it cannot, for example our own entry swap's fee and impact, model it in `copydesk/lpcopy` / `lplive` before testing, or the backtest lies.
   - A pool or copy feature must first predict what volatility and the cold-pool rule leave (research/lpfeat, the residual test).
   - Add it as a variant in `lpopt.downside` or a knob in `lpopt.tune`, then run `./lpdesk tune downside --days 9`: the whole desk, live rules, measured gas, take rate and trust, one continuous run booked by close day.
   - The bar: a better mean AND better on >= 2/3 of days AND a worst day no worse, and the neighbouring thresholds agree. Changes to the pick also pass `.venv/bin/python -m tests.pickworld` (every scenario, twice in a row).
   - Then the knob goes in chain.toml with its numbers in a comment. The weekly `tune downside` re-judges it; when it stops beating live, it comes out.
2. **Name followed wallets by their ROLE:** main (`leader`), bench or filler. "leader" is only the generic word for the wallet a copy follows.
3. **Keys never enter the repo or the context.** They live in `~/.config/lp-copy-desk/wallets.json` (mode 600). Do not print, copy or commit them. `.gitignore` covers `chain.toml`, `var/`, wallets, tokens.
4. **One signer per wallet.** Two processes on one key race for nonces. Never run a second `run --go` on the same wallet.
5. **Stop is not kill.** `./lpdesk stop` means no new copies; open copies must still exit with their leaders, so the process keeps running. To end the process, kill it by PID (SIGTERM is graceful: the entry or exit in progress finishes and is saved). `ops/supervise.sh` restarts it within a minute unless `ops/PAUSE` exists.
6. **A setting's comment is its evidence.** When you change a knob in `chains/robinhood/chain.toml`, write the backtest numbers beside it, the way the others are written.

## Where things are

- `copydesk/lpfollow.py`: the live desk (`Follow`).
  - entry checks: `Follow.room` (tiers), `token_block`, the cold / flat / thin / launch / entry-cost skips
  - the websocket listener
  - exits
  - restart-safety: write-ahead intent, `reconcile` adopts an NFT minted before a crash
- `copydesk/treasury.py`: USDG <-> ETH, the gas band, dust.
- `copydesk/lphist.py`: the history store (`chains/robinhood/var/lphist.db`), ingest / backfill / prune, and the heat table (`var/lp_heat.json`) that the cold-pool rule and the exit routing read.
- `copydesk/lpcopy.py`, `lplive.py`: replaying copies (`lplive` applies the desk's live rules).
- `copydesk/lpdb.py`: the wallet store and the copy score. `lpfind.py`: discovery, nightly, repick. `lpopt.py`: pick, tune, simulators. `lpqc.py`: quality control.
- `core/exec/lp.py`: every on-chain LP action (approve via Permit2, V4 swap, mint, decrease / burn / take, the exit router, Relay).
- `core/lpbook.py`: the LP account / wallet helpers.
- `core/exec/tx.py`: the only signer.
- `core/exec/refuel.py`: gas top-up.
- State lives in `chains/robinhood/var/lp/`: `follow.json` (the book), `follow.jsonl` (every event), `trace.jsonl` (every send), `follow.STOP`.

## Known gaps

- **`tests/ladderfork.py`** replays wallet 0x620a's recent Delta Liquidity ladder transactions. On 2026-10-07 none of its last 40 could mint at current prices, so the test fails at setup, in the original repo too. Point `LP` at a wallet with recent ladders, or rebuild the test around a synthetic ladder.
- **`tests/pickworld.py`** passes 17 of 18 scenarios: `drift` (a second pick run must not change the squad) fails the same way in the original repo (2026-10-07). It is the pick's stability rule, worth fixing before trusting `repick`'s churn.
- **`tests/exitfork.py`** needs `var/lp_heat.json`, which `./lpdesk hist ingest` writes.
- **The arb machinery's leftovers:** `core/` also carries code the copy desk never calls. `core/fmt.py`'s arb cards and `core/events.py`'s full catalogue came along with the modules the desk imports. Leave them, or cut them with a selftest run after.
