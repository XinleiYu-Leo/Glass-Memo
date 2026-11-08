# FHO weak-supervision pipeline

## Current scope

This is a causal **pick/place proxy classifier**, not an object identity tracker, a location memory system, or a validated event detector. A positive means an annotation-derived take/put completion in the preceding 1.5 seconds. Negatives are other annotated hand actions; idle/walking footage was not the negative class. These labels are weak supervision, not manually verified visual ground truth.

The packaged source covers the recorded stages, with no raw Ego4D files, selected labels, feature caches or per-sample predictions. The original files are untouched. Exact selections must be regenerated from your own authorized copy of the same annotation version and the preserved policy. Dataset IDs in exclusion policies are source identities used to prevent train/evaluation leakage, not account/device IDs.

## Environment

Recorded training: Windows 11, WSL2 Ubuntu 24.04, Python 3.12, RTX 5090, PyTorch 2.10.0+cu128 and torchvision 0.25.0+cu128, ffmpeg/ffprobe 6.1.x. NumPy and transformers exact training versions were not recorded; see [environment](CODE_ENVIRONMENT.md). CPU is sufficient for saved head checks, while training/feature extraction used the GPU. A different GPU/software stack can change floating point results.

From the repository root **inside WSL**:

```bash
python3 -m venv .venv-training
source .venv-training/bin/activate
python -m pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r training/fho/requirements.txt
# System prerequisite, if absent:
sudo apt install ffmpeg
```

Use an appropriate official CPU/macOS PyTorch build for CPU inspection instead of the CUDA index. The baseline versions above record the experiment; they are not a claim that every later library release is compatible. Audio v5 additionally requires transformers and its AST model download. No API is needed for these FHO training pipelines.

PowerShell launchers are retained for Windows users. Set `ROBOMEMO_WSL_PYTHON` to your WSL virtual environment's Python; the default is `python3`. The commands below use WSL paths. A Windows drive such as `D:\RoboMemoTraining` appears inside WSL as `/mnt/d/RoboMemoTraining`.

## Load existing heads first

Obtain the separate `local-models` folder from the project owner through the team's permitted channel; it is not in GitHub. From the repository root:

```bash
python models/install_local.py --source ../local-models
python training/fho/verify_models.py
```

The check runs all 16 saved logistic/MLP variants on synthetic features and checks their tensors against the original saved head archives. It does not replay dataset evaluation. To exercise the actual frozen encoder as well:

```bash
python models/fetch_pretrained.py imagenet
python training/fho/verify_models.py --encoder-checkpoint models/local/pretrained/mobilenet_v3_small_imagenet1k_v1.pth
```

For already preprocessed features:

```bash
python training/fho/apply_heads.py --model models/local/fho/models/vision_main_n1652_mlp_pool10 --features local-data/features.npz --output local-data/scores.csv
```

`features.npz` must contain `window_ids[N]` and finite `features[N,10,576]`. For the decoder's frame cache, use `--frames <cache-directory>` in place of `--features`; it expects one `<window_id>.npz` with `frames[10,224,224,3]` uint8 per window. The default encoder path is the file installed above. `--device cuda` is optional. The tool emits per-seed scores and their mean. A mean-score threshold was not calibrated in the original export; do not reuse a single seed's threshold for the ensemble.

There is **no raw-MP4 one-command inference wrapper** in this handoff. For raw recordings, first provide causal decision windows and decode them with the same input policy. Don't rename arbitrary frames or pass future frames as a valid model input.

Input policy: ten evenly spaced target times from `t−1.5` through `t`, newest decoded frame at/before each target; reject frames over 0.10 seconds stale. ffmpeg uses `-fps_mode passthrough`, RGB, bilinear letterbox to 224×224, padding `#7c7468`, ImageNet normalization. Frozen encoder outputs 576 dimensions/frame. Training-set frame standardization precedes concatenated temporal mean, max, last-minus-first and mean absolute frame difference; representation standardization then precedes the head. All four-frame/last-frame variants have separate saved statistics. See each model config for exact recipe.

## Regenerate data selections and train

These commands process your local authorized dataset; they can create dataset-derived assets and large caches. Keep them outside the source checkout. Do not upload their return archives or contact sheets automatically. Install the Ego4D CLI and configure your own access separately; this project contains no dataset credentials.

From repository root in WSL, set your own paths, then generate strict and relaxed candidate exports:

```bash
export ROBOMEMO_PROJECT=/mnt/d/RoboMemoTraining
export ROBOMEMO_ANNOTATIONS=/mnt/d/ego4d/v2/annotations
python training/fho/code/01_candidate_export_v1/export_candidates.py --annotations "$ROBOMEMO_ANNOTATIONS" --output "$ROBOMEMO_PROJECT/candidates_strict"
python training/fho/code/02_candidate_export_v2_relaxed/export_candidates_v2.py --annotations "$ROBOMEMO_ANNOTATIONS" --output "$ROBOMEMO_PROJECT/candidates_relaxed"
python training/fho/code/03_selection/select_windows.py --candidates "$ROBOMEMO_PROJECT/candidates_strict/fho_candidates.jsonl.gz" --output "$ROBOMEMO_PROJECT/fho_transfer_v1/selection"
python training/fho/code/03_selection/select_windows_v2.py --candidates_v2 "$ROBOMEMO_PROJECT/candidates_relaxed/fho_candidates_v2.jsonl.gz" --old_selection "$ROBOMEMO_PROJECT/fho_transfer_v1/selection" --output "$ROBOMEMO_PROJECT/fho_transfer_v1/selection_v2"
```

Export/selection directories must be new. The source annotations must include the split/related-video metadata required by `audit_annotations.py`; check its report before proceeding. Reproduction counts depend on using the original v2_1 data release. Do not weaken exclusions to reach a target count.

Download only selected clips under your own Ego4D account:

```bash
ego4d -o "$ROBOMEMO_PROJECT/fho_transfer_v1/downloads" --datasets clips --video_uid_file "$ROBOMEMO_PROJECT/fho_transfer_v1/selection/clips_to_download.txt"
ego4d -o "$ROBOMEMO_PROJECT/fho_transfer_v1/downloads" --datasets clips --video_uid_file "$ROBOMEMO_PROJECT/fho_transfer_v1/selection_v2/clips_to_download_v2_new.txt"
python training/fho/code/05_train_v3_extended/run_pipeline.py --project "$ROBOMEMO_PROJECT" --device cuda
```

Expected layout: `fho_transfer_v1/{selection,selection_v2,downloads/v2/clips,frames_v2,runs}` under the project path. The script validates its stage manifest before starting. `--fake-encoder` is exclusively a plumbing smoke test and cannot produce usable model/evaluation results.

| Stage directory | Purpose |
|---|---|
| `00_annotation_check` | Read-only annotation inventory and environment probe |
| `01_candidate_export_v1` | Strict source eligibility |
| `02_candidate_export_v2_relaxed` | Additional clips with eligible parents |
| `03_selection` | Fixed label policy, parent-group splits, nested learning curve |
| `04_train_v2_passthrough` | Corrected first selection, up to 1,000 windows |
| `05_train_v3_extended` | Main extended selection, 1,652 train windows |
| `06_train_v4_model_improvement` | Difference-image stream and partial fine-tune experiments |
| `07_audio_v5` | Audio-only and audiovisual experiments |

The v4/v5 scripts use their own project layout and arguments; read `--help` and the stage's included instructions before a long run. Audio and difference-stream training originally saved scores but **not all weights**. Those missing weights are not supplied or invented; see [missing artifacts](MODELS_MISSING_FILES.md).

## Recorded results and limitations

`results/` retains aggregate numerical evidence only. `run_20261027_192912` is explicitly superseded: CFR duplicate frames caused biased rejection. Use the corrected and extended runs.

- Extended evaluation: 413 usable windows from 97 parent videos. Selection had 414 windows; one was rejected by decoding checks.
- Ten-frame MLP mean per-seed AP: about 0.700/0.729/0.751/0.742 for 200/500/1,000/1,652 training windows. Pixel-motion AP about 0.678. AP is not percent accuracy.
- For n1652, AP of the seed-averaged scores differs from the mean of the three per-seed AP values. See [AP/CI reconciliation](MODELS_AP_CI_RECONCILIATION.md) before quoting gains.
- More data, partial fine-tuning and audio did not establish significant improvements over the main frozen model. Audio used a smaller subset, so its metrics cannot be compared directly with the full evaluation set.
- Evaluation labels are weak and not exhaustively visually checked. Repeated research decisions examined the same checks; this is development evidence, not a new untouched final benchmark.
- No demonstrated end-to-end object-location gain or reliable real-time phone operation follows from these numbers.

All saved model files are listed in `models/registry.json` at repository root. The optional fine-tuned checkpoint and historical four-frame model are distinct architectures; do not load them with `apply_heads.py`. Source/dependency notices are preserved in [third-party notes](CODE_THIRD_PARTY_NOTICES.md).
