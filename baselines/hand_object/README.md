# 100DOH pretrained hand/object baseline

This is the **100K-only pretrained 100DOH** model from [hand_object_detector](https://github.com/ddshan/hand_object_detector), not the RoboMemo student or the ten-frame FHO model. It detects hands, contacted objects and contact categories. It does not predict completed take/put events or remember object locations.

The earlier pilot processed 120 images: 84 contained hand detections and 64 contained object detections. Those are output counts, not accuracy measurements. No pilot images, overlays or personal recordings are in this repository.

## Install and check

Use Python 3.12 and a compatible torch/torchvision pair. The earlier CPU test used torch 2.10.0 / torchvision 0.25.0. Install the appropriate platform build first; CUDA support in this wrapper has not been validated by this handoff.

From the repository root:

```bash
python -m pip install -r baselines/hand_object/requirements.txt
python models/fetch_pretrained.py 100doh
python baselines/hand_object/infer.py --smoke-test
```

The ~378 MB checkpoint is deliberately absent from GitHub and from the local project-weight archive. The explicit downloader verifies the pinned mirror file against SHA-256 `e7f57309da0536d4bb6c4cdf15ab53256790cf4486c6eef59cb8698a9c33bc43`. It uses a public university mirror recorded in [provenance.json](provenance.json). An independent digest from the original author was not available. Source URL, commit and mirror identity are recorded there.

`--smoke-test` checks analytic ROIAlign/NMS behavior, verifies the checkpoint hash, strictly loads 642 model tensors with `weights_only=True`, and processes one temporary synthetic square image. It makes no API calls and is not a real-data performance test.

## Own images

The legacy network uses a width/height convention in ROI clipping that requires square input in this wrapper. First letterbox your images:

```bash
python baselines/hand_object/prepare_images.py --images image_a.jpg image_b.jpg --output local-data/square_inputs
python baselines/hand_object/infer.py --images local-data/square_inputs/image_00000.png local-data/square_inputs/image_00001.png --output local-data/hand_output
```

Generated files contain the visual material you choose; keep private material out of Git. Prepared PNGs have generic names and no copied EXIF. `transforms.json` records scaling/padding. Boxes refer to square input pixels: subtract left/top padding, divide by `scale_xy`, and clamp to original bounds to map back.

Hands/objects use score threshold 0.5 and NMS 0.3, upstream BGR normalization, and internal short-side resize to 600. A small input is upscaled; that does not recover detail. Contact labels are no contact, self, other-person, portable-object and stationary-object contact. Association uses the upstream offset-nearest-object heuristic without distance rejection. A missing detection is **not** evidence of release; portable-object contact does not guarantee an associated object box. There is no tracking.

## Source and license

`vendor/hand_object_detector/` is an unmodified inference dependency subset of upstream commit `e6eec712a498ec7844b97893c8d012cea1a71e09`. Its MIT license/copyright remain in [LICENSE](vendor/hand_object_detector/LICENSE). Unused dataset loaders, legacy compiled operators, media, environments and Git history were omitted. The unused training logger was also omitted because its upstream source has a syntax error; none of the retained inference modules import it. `infer.py` supplies a torchvision ROIAlign/NMS bridge; analytic checks passed previously, but numerical equivalence against the original legacy CUDA binary was not established. No blanket license is assigned to the rest of this project or to datasets.
