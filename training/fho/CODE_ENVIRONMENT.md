# Environment used for the recorded runs (numbers only; no machine identifiers)

- Windows 11 + WSL2 Ubuntu 24.04 (glibc 2.39), Python 3.12.3, one NVIDIA RTX 5090 (CUDA 12.8 build).
- torch 2.10.0+cu128, torchvision 0.25.0+cu128, numpy, Pillow; ijson installed before the second inventory run; transformers installed before v5 (version not recorded).
- ffmpeg/ffprobe from the Ubuntu 24.04 apt package (6.1.x); the offline sandbox used for tests had 6.1.1-3ubuntu5.
- Every PowerShell launcher now reads the WSL Python path from the environment variable `ROBOMEMO_WSL_PYTHON` (default `python3`), e.g.
  `$env:ROBOMEMO_WSL_PYTHON = '/path/to/venv/bin/python'` before running.
- Default data layout (change with `--project` / `-Project`):
  `<PROJECT>/fho_transfer_v1/{selection, selection_v2, downloads/v2/clips, frames_v2, runs}` and Ego4D annotations at `<EGO4D_ROOT>/v2/annotations`.
- Suggested pip set for a fresh venv: `pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu128 && pip install numpy pillow ijson transformers`.
