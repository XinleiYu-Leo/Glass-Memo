# Two versions of "10-frame MLP (n1652) vs pixel-motion baseline" on the 413-window eval set

| Version | Where computed | Scores used | Bootstrap | AP gain [95% CI], P(≤0) | AUC gain [95% CI], P(≤0) |
|---|---|---|---|---|---|
| A | Claude sandbox re-analysis of `runs/run_20261027_200303/eval_predictions.csv` (`paired_comparisons_sandbox_paired_comparisons_v3.json`) | MLP pool10 n1652, AP of seed-averaged scores (seeds 0–2), v3 features | 2,000 parent-video resamples, seed 11, **new draws for each comparison in sequence** | +0.066 [+0.001, +0.125], 0.022 | +0.066 [+0.024, +0.110], 0.001 |
| B | Windows `compare_v4.py` in `runs/run_v4_20261027_203618/comparison/comparison_v4.json` (`n1652/mlp/app`) | same head recipe, but video features **recomputed in v4** (appearance + difference images in mixed GPU batches → tiny numeric differences; seed0 AP 0.7404 vs 0.7418) | 2,000 parent-video resamples, seed 11, **one shared set of draws for all comparisons** | +0.0652 [+0.0002, +0.1241], 0.024 | +0.0652 [+0.0212, +0.1078], 0.001 |

Same eval windows (413), same metric code; the differences come from the feature recomputation and the bootstrap draw schedule.
Both say the gain is about +0.065 AP and borderline-significant (lower CI ≈ 0).

Correction: the progress report first written on 2026-10-27 printed "AP +0.066 [0.000, 0.124]", mixing A's point estimate
with B's interval. The online reports were corrected to "+0.065 [0.000, 0.124] (A: +0.066 [0.001, 0.125])"; a copy exported
to Google Docs before the fix still shows the mixed line.

Related raw numbers
- n1000 MLP vs motion: on eval_v1 (331, run 193623, sandbox) +0.053 [−0.010, +0.112], P 0.052; on eval 413 (sandbox v3) +0.076 [+0.011, +0.134], P 0.013; Windows v4 +0.0752 [+0.0102, +0.1352], P 0.0115.
- Fine-tune n1652 vs motion (v4): AP +0.0795 [+0.0147, +0.1425]; vs frozen main: +0.014 [−0.037, +0.060].
