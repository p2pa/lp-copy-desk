# LPML: can a model LP better than copying the squad? (2026-10-01 → 02)

**Verdict: do not deploy.** Built on its own, the model-wallet doesn't earn a squad slot. Its score is **−$28/day**, against
the filler bar of +$10/day. When it fills the squad's idle capital on the repo's own desk simulator, it **loses $5–24/day in
every fold**. Three of the four intervals sit at or below zero. Copying v3 LPs into v4 also loses, out of sample: **−$1.40 per
$100**, 95% interval [−2.35, −0.72]. **The holdout is still sealed and unread.** No model has earned the one run.

Code: `research/lpml/`, tests: `tests/test_lpml.py` (789 asserts). Commits `6b310e0` → `acd4a55`.

## 1. What was asked, and how it changed

1. **First question:** copy fewer, better leader mints. An entry model chose which leader mints to copy, plus ranges and exits.
2. **Rebuilt question (the owner's premise):** a **model-wallet** that doesn't depend on the squad. It picks its own pools,
   ranges and holds, is judged on its own win rate, and fills the squad's idle capital ("keep capital at a max in use").

Both are reported. (2) is the headline.

## 2. Data and biases

- **Store and pools:** 10.4 days of v4 swaps (9.07M) and 101k copyable pools; a median of 1,534 pools an hour clear $5k of
  24 h volume.
- **The store is v4-only.** On 2026-10-01, v3 was 24% of Robinhood's LP mints: 9,933 of 42,083. Half of that went through
  one third-party manager, `0x1195c074` (not Delta). Delta's v3 ladders were 7% of it. Nothing on v3 is visible to the desk,
  the replay or this study, except the dedicated v3 scan in section 8.
- **Recorded squad:** only from 2026-09-28 15:14. On 09-28/29 it was the hand-picked 0x78a1, not the rule's choice
  (J = 0 against the trailing re-pick). The live pick is re-derived trailing (`livepick.py`: the live functions with the clock
  pinned). It names no squad before 09-25, because QC needs 3+ green days.
- **Launch mints:** pools with no prior swap can't be priced, and are excluded.

## 3. Labels and the simulator gate (Gate 2)

**The label:** a $100 position, at our measured lag, with every cost: entry swap, exit impact, gas, and the ETH round trip.
It closes at its exit or the 12 h stop. The 12 h stop is what makes a 12 h purge gap possible.

**The metric, defined:** bias = |mean eᵢ| ÷ mean aᵢ, each position weighed equally.
- eᵢ = (model out − paid out) ÷ model in
- aᵢ = |paid − in| ÷ in

The denominator is small: mean |P&L| is 8.4%, median 0.83%. On the decomposition sample:
- the 4.6% of rows with |e| ≥ 50% carry 109% of the numerator,
- without them the bias is 0.03,
- dollar-weighted, it's 0.078–0.09.

**The diagnosis, hypothesis by hypothesis:**

| hypothesis | outcome |
|---|---|
| double counting | rejected: 0 duplicate rows; fees drive only 15% of the tail |
| liquidity modified mid-life | rejected: tail 10.9% vs rest 21% |
| position-id re-use | rejected: tokenIds are minted once |
| dilution (large share of the pool) | rejected: the tail rate is highest at the *smallest* shares |
| price at close / exit-block order | rejected: the tail is unchanged with the exit-index fix |
| tick rounding | rejected as the cause: the overstatement is principal quantity, not fees |
| **paid in another token / netted in the exit tx** | **confirmed**: v4 flash accounting nets the payout when the exit tx also **swaps** (60% of the tail, 0% of reconciled rows) or **re-mints** (39%) |
| concentration | yes: 6 wallets hold 87% of the tail, 10 tokens 70% |

**The fix was the yardstick, not the simulator.** The repo's payout counted only transfers to the owner: 205 of 237 traced
exits paid another address. An observability rule went in, declared before the one re-run and decided from receipts only:
an exit that swaps or re-mints can't judge the simulator, and our own exits never do either.

**The re-run,** on a fresh sample (seed 5, 160 wallets): bias **0.096 ≤ 0.10, a pass**. Interval 0.007–0.456, so a fragile pass.

## 4. Model-wallet results (development data, walk-forward, 4 one-day folds)

**Base rates:** every (range × hold) cell loses on average. Symmetric ranges average −$4.5 to −$5.3 per $100, with 3–12%
winners. Bids average −$0.3 to −$1.5; 78–93% never fill.

| policy | positions/day | $/day | squad score |
|---|---|---|---|
| **SMALL model** (pool, LP activity, token, timing) | 14 | **+3.25** (+34 −57 +21 +15) | **−28.4** |
| full features | 11 | −30.6 | −66.3 |
| simple 3 features | 15 | −18.0 | −44.0 |
| top 24 h volume + best fixed cell | 119 | −20.8 | −35.0 |
| random row, random cell | 24 | −68.5 | −118.9 |

**Ablations:** dropping the pool group costs $94/day, so pool state carries the signal. Token and timing add nothing.

**Calibration:** ECE 0.04–0.42 on the acted cells, poor.

**The role test** on `lpopt.simulate_tiers` (live rules; the wallet as a fourth, lowest tier):

| fold | squad alone $/day | with the wallet | capital deployed | gain/day [95% CI] |
|---|---|---|---|---|
| 1 | +85.0 | +70.9 | 74% → 81% | −13.6 [−27.3, −3.4] |
| 2 | +5.0 | −19.2 | 90% → 93% | −24.2 [−72.7, 0.0] |
| 3 | +115.5 | +105.6 | 85% → 89% | −9.5 [−26.9, −0.1] |
| 4 | −90.8 | −95.9 | 83% → 87% | −4.6 [−14.4, +1.2] |

**Audit:** point-in-time has 0 violations on all rows. The model-wallet's permuted-label check and dynamic exit test were
not run (interrupted by the owner). The copy-entry audit (below) passed every check.

## 5. Copy-entry results (the first question)

No entry model beats copying the live pick's squad: the squad makes **+$39.5/day** on the desk, the models −7.5 to −37.

The leaders carry what ranking edge there is: leader-aware PR-AUC 0.493 vs 0.439 leader-blind, against base rates of
0.35–0.43. The audit passed: permuted labels collapse, point-in-time is clean, no fold overlap.

## 6. The payout-attribution bug (live trust): backtest

The bug: the live trust measure counts only transfers to the owner. Correcting it **changes the picks** (mains differ on 28
of 39 re-picks, J 0.65) but shows **no evidence of more money**: −$7.3/day, interval [−56, +40]. Don't ship it as a pick
change on this evidence.

## 7. Experiments

`research/lpml/experiments.jsonl` holds every configuration run, with fold results. The model-wallet ran 7 feature
configurations × 4 folds; the copy-entry study ran 11 × 4. The best of many is inflated: read the fold spread, not the max.

## 8. v3 (the REVENUE question)

- **REVENUE's main pool is v3** (REVENUE/WETH, 0.01%), invisible to the desk. On v4, its 2.66% REVENUE/USDG pool had 14 LPs,
  all with negative copy scores or undiscovered. Our own $100 position there paid +$18–35 in the first ~3 hours and lost
  $29–94 afterwards.
- **Delta v3 ladders, 7 days:** 171 closed, **−$605 on $275k** (before gas). Only 2 of 9 repeat LPs clear the bar, and
  barely.
- **v3 → v4 mirror, out of sample:** 54 v3 LPs were picked on days 1–2. On days 3–4 their own v3 return was **−0.30%**, and
  mirroring them into v4 returned **−$1.40 per $100** [−2.35, −0.72]. Everyone else: −$9.59. The selection carries a little;
  the result is still negative.

## 9. What would change the verdict

- **More history,** for the model-wallet: the store holds 10 days. Run the shadow log and re-judge on new days, never on the
  holdout.
- **Imitation features,** not yet built: "where trailing-good LPs mint", at the pool level. The pool group carries the only
  signal found.
- **Relay exits,** from the desk notes. If they execute as quoted, every copy's exit improves by about 4% of its token side,
  and that changes all of the above.
