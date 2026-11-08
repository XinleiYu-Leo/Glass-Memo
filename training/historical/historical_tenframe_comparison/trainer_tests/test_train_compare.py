"""Offline tests only: fabricated labels/images/features are visibly synthetic."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("train_compare", ROOT / "package/tools/train_compare.py")
trainer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trainer)


def frozen_selection(protocol, old, reservation):
    exclusions = {
        "declared_train_parent_ids": old["declared_train_parents"],
        "declared_dev_parent_ids": old["declared_dev_parents"],
        "diagnostic_parent_ids": reservation["parent_ids"],
        "diagnostic_known_related_parent_ids": reservation["known_related_parent_ids"],
        "official_val_parent_ids": ["synthetic-holdout-val"],
        "official_test_parent_ids": ["synthetic-holdout-test"],
        "metadata_holdout_parent_ids": ["synthetic-holdout-meta"],
        "reserved_parent_ids": ["synthetic-holdout-val"],
        "reserved_clip_ids": [f"reserved-{i}" for i in range(60)],
    }
    blocked = sorted(set().union(*(set(v) for k, v in exclusions.items() if k != "reserved_clip_ids")))
    exclusions["excluded_parent_ids"] = blocked
    rows = []
    for i in range(1, 65):
        local = i if i <= 48 else i - 48
        split = "train" if i <= 48 else "dev"
        stratum = ("candidate_positive" if local <= (24 if split == "train" else 8) else
                   "candidate_hard_negative" if local <= (40 if split == "train" else 14) else "candidate_boundary")
        rows.append({"item_id": f"T{i:02d}", "video_uid": f"synthetic-video-{i}", "clip_uid": f"synthetic-clip-{i}",
                     "split": split, "stratum": stratum, "related_group_parent_ids": [f"synthetic-video-{i}"],
                     "target_start_sec": 1., "target_end_sec": 2.5,
                     "repeat_selected": i in (1, 25, 41, 49, 57, 63)})
    selection = {"schema": "robomemo-tenframe-selection-frozen-v1", "status": "selection_frozen_before_download_and_teacher",
                 "seed": 20261026, "protocol_sha256": trainer.digest(protocol), "synthetic_test": True,
                 "items": rows, "selection_rules": {"fixture_only": True}, "pool_audit": {"fixture_only": True},
                 "exclusions": exclusions, "repeat_item_ids": [r["item_id"] for r in rows if r["repeat_selected"]],
                 "source_files": {name: {"sha256": "0" * 64} for name in
                                  ("fho_lta_val.json", "fho_lta_test_unannotated.json", "vq_val.json", "vq_test_unannotated.json")},
                 "group_resolution": {"group_to_canonical_parent_ids": {}, "metadata_sha256": "0" * 64,
                                      "blocked_parent_ids_before_group_closure": blocked,
                                      "additional_blocked_parent_ids_from_group_closure": []}}
    reseal_selection(selection)
    return selection


def reseal_selection(selection):
    selection["selection_content_sha256"] = trainer.canonical_digest({
        "items": selection["items"], "rules": selection["selection_rules"],
        "pool_audit": selection["pool_audit"], "exclusions": selection["exclusions"]})


class InputValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PIL import Image
        cls.temporary = tempfile.TemporaryDirectory(prefix="tenframe-synthetic-validation-")
        cls.root = Path(cls.temporary.name)
        cls.protocol_dir = cls.root / "protocol"
        shutil.copytree(ROOT / "package/protocol", cls.protocol_dir)
        cls.protocol_path = cls.protocol_dir / "experiment.json"
        cls.protocol, old, reservation = trainer.validate_protocol(cls.protocol_path)
        cls.selection = frozen_selection(cls.protocol_path, old, reservation)
        cls.dataset = cls.root / "dataset"
        (cls.dataset / "student_frames").mkdir(parents=True)
        rows = []
        for i, source in enumerate(cls.selection["items"], 1):
            row = {key: copy.deepcopy(source[key]) for key in ("item_id", "split", "video_uid", "clip_uid", "related_group_parent_ids", "target_start_sec", "target_end_sec")}
            row["frames"] = []
            for j in range(10):
                relative = f"student_frames/T{i:02d}_f{j + 1:02d}.png"
                path = cls.dataset / relative
                Image.new("RGB", (224, 224), (i, j, (i + j) % 256)).save(path)
                row["frames"].append({"frame_id": f"f{j + 1:02d}", "path": relative, "sha256": trainer.digest(path),
                                      "requested_time_sec": 1 + j / 6, "actual_time_sec": 1 + j / 6,
                                      "source_frame_index": 30 + j * 5})
            rows.append(row)
        cls.manifest = {"schema": "robomemo-tenframe-dataset-v1", "synthetic_test": True,
                        "protocol_sha256": trainer.digest(cls.protocol_path), "items": rows}
        cls.labels = {"schema": "robomemo-tenframe-reviewed-labels-v1", "protocol_sha256": trainer.digest(cls.protocol_path), "items": []}
        for i, row in enumerate(rows):
            decision = "present" if i % 2 else "absent"
            cls.labels["items"].append({"item_id": row["item_id"], "split": row["split"], "status": "accepted", "label": i % 2,
                                        "teacher_decision": decision, "blind_review_decision": decision,
                                        "blind_review_id": "synthetic-review-fixture", "blind_review_saw_teacher": False,
                                        "repeat_decisions": [decision] if row["item_id"] in cls.selection["repeat_item_ids"] else [],
                                        "evidence_frame_ids": ["f01", "f10"] if i % 2 else ["f01"],
                                        "rationale": "Synthetic fixture only; not visual supervision.", "input_scope": "same_ten_224_pngs"})

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.s = copy.deepcopy(self.selection)
        self.m = copy.deepcopy(self.manifest)
        self.l = copy.deepcopy(self.labels)
        self.selection_path = self.root / "selection.json"
        self.labels_path = self.root / "labels.json"

    def persist(self):
        trainer.save_json(self.selection_path, self.s)
        self.m["selection_sha256"] = trainer.digest(self.selection_path)
        trainer.save_json(self.dataset / "manifest.json", self.m)
        self.l["dataset_manifest_sha256"] = trainer.digest(self.dataset / "manifest.json")
        trainer.save_json(self.labels_path, self.l)
        trainer.save_json(self.root / "independent_validation.json", {
            "schema": "robomemo-tenframe-export-validation-v1", "status": "passed", "synthetic_test": True,
            "selection_sha256": trainer.digest(self.selection_path), "protocol_sha256": trainer.digest(self.protocol_path),
            "dataset_manifest_sha256": trainer.digest(self.dataset / "manifest.json"),
            "independent_official_isolation_replay": True,
            "isolation": {"selected_independent_group_components": 64, "selected_parents": 64, "selected_overlap": 0,
                          "independent_seed_replay": {"seed": 20261026, "all_64_items_and_windows_reproduced": True,
                                                      "all_six_repeat_choices_reproduced": True}},
            "dataset": {"png_count": 640, "case_count": 64, "source_decode_verified": True,
                        "items": [{"item_id": f"T{i:02d}", "png_count": 10, "unique_source_frames": 10,
                                   "original_decode_verified": True} for i in range(1, 65)]}})

    def validate(self, allow_synthetic=True):
        self.persist()
        return trainer.validate_inputs(self.dataset, self.selection_path, self.labels_path, self.protocol_path,
                                       allow_synthetic=allow_synthetic)

    def test_valid_complete_synthetic_fixture(self):
        rows, provenance = self.validate()
        self.assertEqual(len(rows), 64)
        self.assertEqual(provenance["accepted_counts"]["train"]["accepted"], 48)
        self.assertTrue(provenance["synthetic_test"])

    def test_cli_mode_rejects_synthetic(self):
        with self.assertRaisesRegex(ValueError, "synthetic"):
            self.validate(allow_synthetic=False)

    def test_bad_protocol_and_manifest_hashes(self):
        self.m["protocol_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "protocol hash"):
            self.validate()
        self.m = copy.deepcopy(self.manifest)
        self.persist()
        self.l["dataset_manifest_sha256"] = "0" * 64
        trainer.save_json(self.labels_path, self.l)
        with self.assertRaisesRegex(ValueError, "dataset hash"):
            trainer.validate_inputs(self.dataset, self.selection_path, self.labels_path, self.protocol_path, allow_synthetic=True)

    def test_duplicate_review_rows_and_split_move(self):
        self.l["items"][1]["item_id"] = "T01"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.validate()
        self.l = copy.deepcopy(self.labels)
        self.l["items"][0]["split"] = "dev"
        with self.assertRaisesRegex(ValueError, "split changed"):
            self.validate()

    def test_related_source_overlap_rejected(self):
        self.s["group_resolution"]["group_to_canonical_parent_ids"] = {"synthetic-group": ["synthetic-video-1", "synthetic-video-49"]}
        for i in (0, 48):
            self.s["items"][i]["related_group_parent_ids"] = ["synthetic-video-1", "synthetic-video-49"]
        reseal_selection(self.s)
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.validate()

    def test_full_exclusion_union_and_closure_rejected(self):
        self.s["exclusions"]["excluded_parent_ids"].remove("synthetic-holdout-test")
        reseal_selection(self.s)
        with self.assertRaisesRegex(ValueError, "exclusion union"):
            self.validate()

    def test_independent_report_required_and_bound_to_dataset(self):
        self.persist()
        report_path = self.root / "independent_validation.json"
        report = trainer.read_json(report_path)
        report["dataset_manifest_sha256"] = "0" * 64
        trainer.save_json(report_path, report)
        with self.assertRaisesRegex(ValueError, "Independent validation hash"):
            trainer.validate_inputs(self.dataset, self.selection_path, self.labels_path, self.protocol_path, allow_synthetic=True)
        report_path.unlink()
        with self.assertRaises(FileNotFoundError):
            trainer.validate_inputs(self.dataset, self.selection_path, self.labels_path, self.protocol_path, allow_synthetic=True)

    def test_unknown_or_disagreement_cannot_be_binary(self):
        for key, value in (("teacher_decision", "unclear"), ("blind_review_decision", "present"), ("teacher_decision", "rule_unsure")):
            self.l = copy.deepcopy(self.labels)
            self.l["items"][0][key] = value
            with self.assertRaisesRegex(ValueError, "agreeing binary"):
                self.validate()

    def test_repeat_disagreement_unknown_and_missing(self):
        for repeats in (["present"], ["unclear"], []):
            self.l = copy.deepcopy(self.labels)
            self.l["items"][0]["repeat_decisions"] = repeats
            with self.assertRaisesRegex(ValueError, "repeat missing, unknown or disagrees"):
                self.validate()

    def test_seen_teacher_and_insufficient_evidence(self):
        self.l["items"][0]["blind_review_saw_teacher"] = True
        with self.assertRaisesRegex(ValueError, "blind"):
            self.validate()
        self.l = copy.deepcopy(self.labels)
        self.l["items"][1]["evidence_frame_ids"] = ["f10"]
        with self.assertRaisesRegex(ValueError, "before/after"):
            self.validate()

    def test_blank_template_and_missing_class_stop(self):
        for row in self.l["items"]:
            row.update(status="held", label=None, teacher_decision=None, blind_review_decision=None,
                       blind_review_id=None, repeat_decisions=[], evidence_frame_ids=[], rationale="")
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_ACCEPTED_LABELS"):
            self.validate()
        self.l = copy.deepcopy(self.labels)
        for row in self.l["items"]:
            if row["split"] == "dev" and row["label"] == 1:
                row.update(status="held", label=None)
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_ACCEPTED_LABELS: dev"):
            self.validate()

    def test_future_outside_repeated_or_offgrid_frame(self):
        for field, value, expected in (("actual_time_sec", 1.001, "Future"), ("actual_time_sec", .99, "out-of-window"),
                                       ("requested_time_sec", 1.01, "grid"), ("source_frame_index", 35, "distinct")):
            self.m = copy.deepcopy(self.manifest)
            self.m["items"][0]["frames"][0][field] = value
            with self.assertRaisesRegex(ValueError, expected):
                self.validate()

    def test_altered_frame_and_non_rgb_png(self):
        from PIL import Image
        path = self.dataset / "student_frames/T01_f01.png"
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"alteration")
            with self.assertRaisesRegex(ValueError, "image hash"):
                self.validate()
            Image.new("L", (224, 224), 20).save(path)
            self.m["items"][0]["frames"][0]["sha256"] = trainer.digest(path)
            with self.assertRaisesRegex(ValueError, "RGB 224"):
                self.validate()
        finally:
            path.write_bytes(original)

    def test_gate_failure_precedes_encoder_and_training(self):
        self.persist()
        # The production entry point must reject this synthetic marker before loading torch weights.
        args = argparse_args(self)
        with mock.patch.object(trainer, "load_encoder", side_effect=AssertionError("must not load")), \
             mock.patch.object(trainer, "extract_features", side_effect=AssertionError("must not extract")), \
             mock.patch.object(trainer, "fit_comparison", side_effect=AssertionError("must not fit")):
            with self.assertRaisesRegex(ValueError, "synthetic"):
                trainer.run(args)
        self.assertEqual(trainer.read_json(args.output / "status.json")["status"], "stopped")
        with self.assertRaisesRegex(ValueError, "already exists"):
            trainer.run(args)


def argparse_args(test):
    from argparse import Namespace
    return Namespace(dataset=test.dataset, selection=test.selection_path, labels=test.labels_path,
                     protocol=test.protocol_path, checkpoint=test.root / "nonexistent.pt", output=test.root / "refused-run", device="cpu")


class PairedCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import torch
        from torchvision.models import mobilenet_v3_small
        torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)
        torch.manual_seed(785)
        model = mobilenet_v3_small(weights=None)
        cls.encoder_state = torch.nn.Sequential(model.features, model.avgpool, torch.nn.Flatten(1)).state_dict()
        cls.features = torch.randn(32, 10, 576)
        cls.rows = [{"item_id": f"S{i:02d}", "split": "train" if i < 24 else "dev", "label": i % 2} for i in range(32)]

    def test_summary_shape_same_endpoints_and_initialization(self):
        import torch
        four, ten = (trainer.summarize(self.features, arm) for arm in trainer.ARMS)
        self.assertEqual(four.shape, (32, 1728))
        self.assertEqual(ten.shape, (32, 1728))
        self.assertTrue(torch.equal(four[:, 1152:], ten[:, 1152:]))
        self.assertFalse(torch.equal(four[:, :1152], ten[:, :1152]))
        head_a, head_b = trainer.make_head(566), trainer.make_head(566)
        self.assertEqual(trainer.state_digest(head_a.state_dict()), trainer.state_digest(head_b.state_dict()))
        self.assertEqual(sum(p.numel() for p in head_a.parameters()), 1729)

    def test_normalization_uses_only_train_population_and_floor(self):
        import torch
        train = torch.zeros(24, 1728)
        train[:, 0] = torch.arange(24)
        dev = torch.ones(8, 1728) * 1000
        tx, dx, mean, std = trainer.normalize_from_train(train, dev)
        _, _, mean2, std2 = trainer.normalize_from_train(train, dev * -99)
        self.assertTrue(torch.equal(mean, train.mean(0)))
        self.assertTrue(torch.equal(std, train.std(0, unbiased=False).clamp_min(.05)))
        self.assertTrue(torch.equal(mean, mean2) and torch.equal(std, std2))
        self.assertEqual(float(std[1]), float(torch.tensor(.05)))
        self.assertTrue(torch.allclose(tx.mean(0), torch.zeros(1728), atol=1e-6))
        self.assertTrue(float(dx[0, 1]) > 100)

    def test_all_six_final_runs_reload_exact_and_reported(self):
        import torch
        with tempfile.TemporaryDirectory(prefix="tenframe-synthetic-training-") as temporary:
            output = Path(temporary)
            before = trainer.state_digest(self.encoder_state)
            report = trainer.fit_comparison(self.features, self.rows, output, self.encoder_state,
                                            {"synthetic_test": True, "fixture_only": True}, synthetic_test=True)
            self.assertEqual(report["status"], "synthetic_test_complete")
            self.assertEqual(len(report["runs"]), 6)
            self.assertEqual(len(report["paired_differences"]), 3)
            self.assertEqual(before, trainer.state_digest(self.encoder_state))
            self.assertEqual(report["all_positive_baseline"]["dev"]["confusion"], {"tp": 4, "fp": 4, "fn": 0, "tn": 0})
            for seed in trainer.SEEDS:
                pair = [r for r in report["runs"] if r["seed"] == seed]
                self.assertEqual(pair[0]["initial_head_sha256"], pair[1]["initial_head_sha256"])
                for run in pair:
                    checkpoint = torch.load(output / run["checkpoint"], map_location="cpu", weights_only=True)
                    self.assertEqual(checkpoint["saved_epoch"], 40)
                    self.assertTrue(run["reload"]["exact_logits_equal"])
                    self.assertTrue(run["reload"]["encoder_hash_unchanged"])
                    self.assertEqual(checkpoint["encoder_sha256"], before)
                    history = trainer.read_json((output / run["checkpoint"]).parent / "history.json")
                    self.assertEqual([h["epoch"] for h in history], list(range(1, 41)))
                    self.assertFalse(any("dev" in h for h in history))
            for key, value in report["mean_paired_difference"].items():
                self.assertAlmostEqual(value, sum(row["dev"][key] for row in report["paired_differences"]) / 3)
            self.assertFalse(any("best" in str(path.name) for path in output.rglob("*")))

    def test_fixed_encoder_strict_load_and_freeze(self):
        encoder = trainer.frozen_encoder(self.encoder_state)
        self.assertFalse(encoder.training)
        self.assertFalse(any(p.requires_grad for p in encoder.parameters()))
        self.assertEqual(trainer.state_digest(encoder.state_dict()), trainer.state_digest(self.encoder_state))
        missing = dict(self.encoder_state)
        missing.pop(next(iter(missing)))
        with self.assertRaises(RuntimeError):
            trainer.frozen_encoder(missing)

    def test_review_archive_contains_only_complete_json_allowlist(self):
        import zipfile
        with tempfile.TemporaryDirectory(prefix="tenframe-synthetic-archive-") as temporary:
            output = Path(temporary)
            expected = ["results.json", "validated_inputs.json", "feature_extraction.json", "artifact_manifest.json", "status.json"]
            expected.extend(f"seed_{seed}_{arm}/{name}" for seed in trainer.SEEDS for arm in trainer.ARMS
                            for name in ("result.json", "predictions.json", "history.json"))
            for name in expected:
                path = output / name
                path.parent.mkdir(parents=True, exist_ok=True)
                trainer.save_json(path, {"synthetic_test": True, "fixture_file": name})
            (output / "shared_features.pt").write_bytes(b"must stay local")
            (output / "some_image.png").write_bytes(b"must stay local")
            (output / "seed_566_four_frame/final.pt").write_bytes(b"must stay local")
            result = trainer.make_review_archive(output)
            self.assertEqual(result["json_file_count"], 23)
            self.assertEqual(result["sha256"], trainer.digest(output / "results_for_review.zip"))
            self.assertTrue((output / "results_for_review.zip.sha256").read_text().startswith(result["sha256"]))
            with zipfile.ZipFile(result["path"]) as zipped:
                self.assertEqual(zipped.namelist(), sorted(expected))
                self.assertIsNone(zipped.testzip())
                for name in expected:
                    self.assertEqual(zipped.read(name), (output / name).read_bytes())
            with self.assertRaisesRegex(ValueError, "already exists"):
                trainer.make_review_archive(output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
