# Third-party notices

No third-party source code or license-bearing third-party files are vendored in these packages; nothing third-party was removed.
The code depends on, or downloads at run time, the components below. Check each project's own license before redistribution.

| Component | Used for | License (per its project) |
|---|---|---|
| PyTorch 2.10.0 (+cu128 build) | feature extraction, fine-tuning | BSD-3-Clause style |
| torchvision 0.25.0 | MobileNetV3-Small architecture; ImageNet weights `IMAGENET1K_V1` downloaded by torchvision at run time | BSD-3-Clause (code); ImageNet-trained weights follow torchvision/ImageNet terms |
| NumPy, Pillow, ijson (optional) | numerics, images, streaming JSON | BSD-3-Clause / MIT-CMU (HPND) / BSD |
| Hugging Face `transformers` (version not recorded on the run machine) | AST audio encoder (v5 only) | Apache-2.0 |
| `MIT/ast-finetuned-audioset-10-10-0.4593` | AudioSet-pretrained audio embeddings, downloaded from Hugging Face at run time | see its model card (BSD-3-Clause listed there; verify) |
| FFmpeg / ffprobe 6.1.x (Ubuntu 24.04 package; exact build on the run machine not recorded) | video/audio decoding, invoked as an external binary | LGPL-2.1+ / GPL depending on build; not redistributed |
| Ego4D (FHO annotations, clips) | training/eval data and labels | Ego4D License Agreement. **No Ego4D frames, audio, features or annotation exports are included.** Selection files contain only public Ego4D IDs, times, derived binary labels and FHO structured noun strings; every collaborator needs their own Ego4D license to obtain the data. Check whether the Ego4D terms allow sharing models trained on it before external release. |
