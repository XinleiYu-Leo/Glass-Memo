# Model artifacts

Source control contains the registry and readable model configs. The optional sibling `local-models` folder contains **all non-superseded saved project weights found in the previous engineering export**: 16 frozen-head packages, one fine-tuned seed, two original head archives, and the older reviewed-v2 checkpoint. Historical unsaved variants are documented rather than reconstructed or mislabeled.

The registry records the SHA-256, byte count and provenance of each file. To verify/install, from repository root:

```bash
python models/install_local.py --source ../local-models --verify-only
python models/install_local.py --source ../local-models
python training/fho/verify_models.py
```

Installed files go to ignored `models/local/`. The installer checks all files first and refuses to overwrite different files. NumPy is needed for head smoke checks; the install tool itself only uses Python's standard library. These models were learned from licensed research data; keep the optional archive separate and confirm applicable data/model redistribution terms before further distribution. No license for original project code or trained artifacts is invented here.

| Installed location | What it contains |
|---|---|
| `local/fho/models/vision_main_n1652_mlp_pool10` | Main saved 10-frame MLP, 3 seeds |
| `local/fho/models/n1652_mlp_pool4` | Four-frame ablation |
| `local/fho/models/n1000_mlp_pool{4,10}` | n1000 saved heads from corrected earlier run |
| `local/fho/models/logistic/` | n200/n500/n1000/n1652 × last/pool4/pool10 |
| `local/fho/models/finetune_mobilenet_n1652_seed0/` | Only saved fine-tuned seed, separate architecture |
| `local/fho/raw_head_weights/` | Original saved head archives for provenance checks |
| `local/historical/` | Earlier four-frame reviewed-v2 checkpoint, separate architecture |

`configs/` duplicates readable configs and prior export verification records so reviewers can inspect them without the binary archive. A past verification record is not a claim of a newly rerun dataset evaluation.

External pretrained weights are **not included**. Download explicitly when needed:

```bash
python models/fetch_pretrained.py imagenet
python models/fetch_pretrained.py 100doh
```

The helper checks exact file size and full SHA-256 before installation. MobileNet comes from the official [torchvision model host](https://download.pytorch.org/models/mobilenet_v3_small-047dcff4.pth). 100DOH is the original author's 100K-only architecture and checkpoint variant; see [upstream](https://github.com/ddshan/hand_object_detector) and [baseline provenance](../baselines/hand_object/provenance.json). The latter download uses a pinned public university mirror; its recorded LFS digest matched, but an independent digest from the original authors was unavailable. No download was performed while preparing this handoff.

Do not commit downloaded checkpoints, caches, model return ZIPs, or Ego4D data. Dataset source selections and per-sample labels/predictions are intentionally omitted; regenerate them locally with [the training instructions](../training/fho/README.md).
