"""Read-only runtime probe. No installs, uploads, weights or training."""
import argparse
import importlib.util
import json
import platform
import time
from pathlib import Path


def probe():
    result = {"schema": "robomemo-contact-environment-probe-v1",
              "platform": platform.platform(), "python": platform.python_version(),
              "installs": 0, "uploads": 0, "training_runs": 0}
    result["optional_modules"] = {
        name: importlib.util.find_spec(name) is not None
        for name in ("torch", "torchvision", "PIL", "numpy", "ijson")}
    try:
        import torch
        import torchvision
        from torchvision.ops import nms, roi_align, roi_pool
        result.update(torch=torch.__version__, torchvision=torchvision.__version__,
                      torch_cuda_build=torch.version.cuda,
                      cuda_available=torch.cuda.is_available())
        devices = ["cpu"]
        if torch.cuda.is_available():
            result["gpu_names"] = [torch.cuda.get_device_name(i)
                                   for i in range(torch.cuda.device_count())]
            devices.append("cuda:0")
        result["operator_checks"] = {}
        for device in devices:
            check = {}
            try:
                boxes = torch.tensor([[0., 0., 4., 4.], [0., 0., 4., 4.],
                                      [8., 8., 12., 12.]], device=device)
                scores = torch.tensor([.9, .8, .7], device=device)
                keep = nms(boxes, scores, .5).cpu().tolist()
                check["nms_indices"] = keep
                assert keep == [0, 2], "Unexpected NMS result"
                feature = torch.arange(64., device=device).reshape(1, 1, 8, 8)
                rois = torch.tensor([[0., 0., 0., 7., 7.]], device=device)
                for name, op in (("roi_align", roi_align), ("roi_pool", roi_pool)):
                    output = op(feature, rois, output_size=(2, 2), spatial_scale=1.)
                    assert output.shape == (1, 1, 2, 2)
                    assert bool(torch.isfinite(output).all())
                    check[name] = output.detach().cpu().tolist()
                check["passed"] = True
            except Exception as exc:
                check.update(passed=False, error=f"{type(exc).__name__}: {exc}")
            result["operator_checks"][device] = check
        result["status"] = ("probe_completed" if all(
            check["passed"] for check in result["operator_checks"].values()
        ) else "operator_check_failed")
        result["caveat"] = "Passing these small standard operators does not establish full model compatibility."
    except Exception as exc:
        result.update(status="torch_probe_unavailable", error=f"{type(exc).__name__}: {exc}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    result = probe()
    result["seconds"] = round(time.monotonic() - started, 3)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("ENVIRONMENT_PROBE:", result["status"], flush=True)
    raise SystemExit(0 if result["status"] == "probe_completed" else 1)
