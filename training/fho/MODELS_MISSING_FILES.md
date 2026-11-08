# What is not in these packages and why

## Never saved by the code (exists nowhere; reproducible by re-running the deterministic pipeline)
- v4 option A heads (app+diff, diff-only, app re-fit) — `train_eval_v4.py` writes scores, not weights.
- v5 audio heads (all audio / video+audio models) — `train_eval_audio.py` writes scores, not weights.
- v4 fine-tune: only n1652 seed0 was saved; seeds 1–2 and the n1000 seed0 model were not.
- MLP heads for n200/n500 (any run) and for n1000 in run_20261027_200303 (only logistic saved there; the n1000 MLP comes from run_20261027_193623 on identical windows/features).
Logistic heads are exactly reproducible; MLP heads are reproducible with the same seeds on the same features (numpy, fixed steps).

## Deliberately excluded
- Ego4D frames (`frames_v2` cache), video features (`features*.npz`, `video_features.npz`), audio windows and audio embeddings
  (`audio_windows.npz`, `audio_features.npz`), local contact sheets, candidate/annotation exports, inventories with raw paths.
- Home recordings, photos, credentials, private resume archives.
- The superseded first run's weights (biased sample); its metrics are kept under `runs/SUPERSEDED_run_20261027_192912` for audit.

## Downloaded at run time (not trained here)
- torchvision MobileNetV3-Small `IMAGENET1K_V1`; Hugging Face `MIT/ast-finetuned-audioset-10-10-0.4593`.

## On the Windows machine only
- All runs listed here were uploaded in full and every trained weight file they contain is included (SHA-256 checked).
  Any run made after run_v5_audio_20261028_001822, if one exists, is not covered: use `RoboMemo_windows_share_export` to
  collect and hash-check what is on disk.

## Not recorded
- Exact `transformers` and `numpy` versions on the run machine; exact ffmpeg build there.
