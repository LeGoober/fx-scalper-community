# Judge calibration: Jev vs Laya vs ensemble vs code rules (2026-09-26)

**Question:** do the AI judges separate winning ICT setups from losing ones?

**Method** (`python -m app.research.jev_calibration --symbols OTC_NDX --sample 120 --laya-schemas 1`):
- **Setups:** 120 random NDX kill-zone setups with a tradeable plan, over the past 365 days. Each passed the structural trigger (raid → shift → displacement → first FVG).
- **Outcome:** simulated with strategy v3 execution on Rise/Fall 15m, the only NDX contract Deriv offers. All other filters are ignored.
- **Scoring:** the same code-computed facts go to each judge:
  - Jev (`jev-1.13.0`), 3 phrasings per question;
  - Laya (`convaiinnovations/laya`), primary phrasing only (~24 s per setup on CPU);
  - the ensemble (weighted Jev 0.6 / Laya 0.4, agreement factor, threshold 0.6);
  - the code fallback rules.
- **Metric:** "liked" means the judgment passed its gate. *Edge* is the mean R of liked setups minus the mean R of not-liked ones.

**Baseline** (all 120 setups): expectancy **−0.04R**, win rate 51.7%, 95% CI [−0.21, +0.13].

| Judgment | Judge | Liked (n) | E[R] liked | Not liked (n) | E[R] not liked | Edge |
|---|---|---:|---:|---:|---:|---:|
| Displacement quality | Jev | 107 | +0.00 | 13 | −0.43 | **+0.43** |
| | Laya | 120 | −0.04 | 0 | — | none (liked everything) |
| | Ensemble | 111 | −0.02 | 9 | −0.38 | +0.37 |
| | Code rule | 86 | **+0.14** (CI −0.05…+0.33) | 34 | −0.51 | **+0.65** |
| Daily draw on liquidity | Jev | 78 | −0.03 | 42 | −0.08 | +0.05 |
| | Laya | 60 | −0.01 | 60 | −0.08 | +0.06 |
| | Ensemble | 77 | −0.01 | 43 | −0.10 | +0.08 |
| | Code rule | 79 | −0.04 | 41 | −0.05 | +0.01 |
| Model alignment (flag) | Jev | 2 | — | 118 | — | too strict |
| | Laya | 120 | — | 0 | — | liked everything |
| **Ensemble decision** (execute) | Ensemble | 3 | −0.38 | 117 | −0.04 | executes almost nothing |

## Findings
1. **No configuration shows an edge.** Every liked group's 95% interval includes zero. The baseline is slightly negative after costs.
2. **Laya did not discriminate.** It passed 100% of setups on displacement and alignment, and split draw-on-liquidity 50/50 with negligible edge. Its loader warns that the checkpoint's confidence is uncalibrated. It also misread an obvious bearish description in spot checks (P = 0.17, where Jev gave 0.83).
3. **Jev's displacement judgment carries signal.** Setups it rated weak lost −0.43R, consistent with the earlier study. A **simple code rule** (body/ATR strength) separates better still.
4. **The draw-on-liquidity judgment adds nothing measurable**, for any judge.
5. **The ensemble as configured is unusable for execution.** It says "execute" on only 3 of 120 setups, because Laya/Jev disagreement lowers the agreement factor below the threshold.

## Recommendations
- **Set Laya's ensemble weight to 0 for decisions** (keep it as a logged second opinion), or try a fine-tuned or larger Laya on a GPU.
- **Rephrase `model_alignment`**, or drop it.
- Forward-test the only promising filter, *reject weak displacement*, on **paper**. Its liked group is +0.14R but not statistically significant.
- Don't tune thresholds on this same year of data. Freeze rules and test forward, or on an older untouched dataset.
