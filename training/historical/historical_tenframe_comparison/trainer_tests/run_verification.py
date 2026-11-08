"""Run offline fabricated-data tests and one real-encoder/synthetic-image smoke.

This never fits on real data, reads prior diagnostic images or calls any API.
"""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SPEC = importlib.util.spec_from_file_location("train_compare", ROOT / "package/tools/train_compare.py")
trainer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trainer)


def main():
    started = time.perf_counter()
    log_path = HERE / "verification_log.txt"
    with log_path.open("w", encoding="utf-8") as stream, redirect_stdout(stream), redirect_stderr(stream):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.discover(str(HERE), pattern="test_*.py"))
        import torch
        from PIL import Image
        torch.set_num_threads(1)
        checkpoint = ROOT.parent / "new-source24-return-20261026/checkpoint_replay/pick_place_reviewed_best.pt"
        encoder = trainer.load_encoder(checkpoint, "cpu")
        before = trainer.state_digest(encoder.state_dict())
        with tempfile.TemporaryDirectory(prefix="tenframe-synthetic-image-smoke-") as temporary:
            path = Path(temporary) / "synthetic_solid_color.png"
            Image.new("RGB", (224, 224), (30, 90, 140)).save(path)
            sha = trainer.digest(path)
            # Explicit core-only seam: repeat the sole synthetic PNG to prove encoding deduplicates it.
            features, extraction = trainer.extract_features([
                {"frames": [{"sha256": sha} for _ in range(10)], "resolved_images": [str(path)] * 10}
            ], encoder, "cpu")
            assert features.shape == (1, 10, 576)
            assert extraction["unique_images_encoded_once"] == 1
            assert before == trainer.state_digest(encoder.state_dict())
        smoke = {"status": "passed", "synthetic_image_only": True, "real_video_inputs_used": 0,
                 "checkpoint_sha256": trainer.digest(checkpoint), "encoder_sha256": before,
                 "encoder_loaded_strictly_with_weights_none": True, "encoder_unchanged": True,
                 "unique_synthetic_images_encoded": 1, "features_shape": [1, 10, 576]}
    report = {"schema": "robomemo-tenframe-trainer-offline-verification-v1",
              "status": "passed" if result.wasSuccessful() else "failed",
              "created_utc": datetime.now(timezone.utc).isoformat(), "tests_run": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
              "duration_seconds": time.perf_counter() - started,
              "six_synthetic_fits_only": True, "real_training_runs": 0, "teacher_calls": 0,
              "real_encoder_synthetic_image_smoke": smoke,
              "trainer_sha256": trainer.digest(ROOT / "package/tools/train_compare.py"),
              "tests_sha256": trainer.digest(HERE / "test_train_compare.py"),
              "log_sha256": trainer.digest(log_path)}
    trainer.save_json(HERE / "verification_report.json", report)
    print(f"OFFLINE_VERIFICATION {report['status']}: {result.testsRun} tests; real training=0; teacher calls=0")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
