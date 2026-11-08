import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import audit_annotations as audit


class LimitedBytes(io.BytesIO):
    def read(self, n=-1):
        if n < 0 or n > audit.CHUNK:
            raise AssertionError("unbounded read")
        return super().read(min(n, 17))


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def action(verb="take_(pick,_grab,_get)", **updates):
    row = {"uid": "action-1", "structured_verb": verb, "is_valid_action": True, "is_partial": False, "is_rejected": False, "is_invalid_annotation": False,
           "start_frame": 100, "end_frame": 340, "clip_start_frame": 0, "clip_end_frame": 240,
           "start_sec": 100 / 30, "end_sec": 340 / 30, "clip_start_sec": 0, "clip_end_sec": 8,
           "critical_frames": {"pre_frame": 100, "contact_frame": 110, "pnr_frame": 120, "post_frame": 130},
           "clip_critical_frames": {"pre_frame": 0, "contact_frame": 10, "pnr_frame": 20, "post_frame": 30},
           "frames": [{"frame_type": "pnr_frame", "boxes": [{"object_type": "left_hand", "structured_noun": None}, {"object_type": "object_of_change", "structured_noun": "cup"}]}],
           "narration_text": "PRIVATE NARRATION MUST NEVER BE EXPORTED"}
    row.update(updates)
    return row


class ParserTests(unittest.TestCase):
    def test_chunk_boundaries_unicode_escape_and_arrays(self):
        payload = {"first": [0, -1.25e10, False, None, "你好😀 escaped\\\"value"], "empty": {}}
        events = list(audit.BoundedJSON(LimitedBytes(json.dumps(payload, ensure_ascii=False).encode())).events())
        self.assertIn(("first.item", "string", payload["first"][-1]), events)
        self.assertEqual(events[-1], ("", "end_map", None))
        self.assertEqual(sum(e == "number" for _, e, _ in events), 2)

    def test_rejects_malformed_and_truncated_json(self):
        for payload in (b'{"a": [1,2', b'{"x":true,}', b'{} {}', b'[NaN]', b'[01]', b'"unterminated'):
            with self.subTest(payload=payload), self.assertRaises((ValueError, json.JSONDecodeError)):
                list(audit.BoundedJSON(LimitedBytes(payload)).events())

    def test_token_memory_bound(self):
        with self.assertRaisesRegex(ValueError, "limit|oversized"):
            list(audit.BoundedJSON(io.BytesIO(json.dumps("x" * (audit.TOKEN_LIMIT + 1)).encode())).events())


class InventoryTests(unittest.TestCase):
    def test_coordinate_validity_and_missing_contact_not_negative(self):
        row = action(critical_frames={"pre_frame": 100, "pnr_frame": -1, "post_frame": 999}, is_partial=True)
        metric = audit.action_metrics(row)
        self.assertEqual(metric["critical_frames/contact_frame/missing"], 1)
        self.assertEqual(metric["critical_frames/pnr_frame/invalid_frame"], 1)
        self.assertEqual(metric["critical_frames/post_frame/outside_annotation_context"], 1)
        self.assertEqual(metric["status/not_confirmed_complete_and_valid"], 1)
        self.assertFalse(any("negative_label" in k for k in metric))

    def test_group_closure_is_transitive(self):
        actual = audit.closure({"A"}, {"G1": ["A", "B"], "G2": ["B", "C"]})
        self.assertEqual(actual, {"A", "B", "C", "G1", "G2"})

    def test_wrong_or_empty_main_schema_is_not_a_zero_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fho_main.json"
            for value in ({}, {"videos": []}, {"videos": [{"video_uid": "A", "annotated_intervals": []}]}, {"videos": [{"video_uid": "A", "annotated_intervals": [{"clip_uid": "C", "narrated_actions": []}]}]}):
                write_json(path, value)
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "unexpected/empty schema"):
                    audit.inspect_main(path, {}, set(), {}, set(), {}, "stdlib", 8)

    def test_main_field_order_and_source_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fho_main.json"
            # IDs are deliberately after child arrays; no ordering assumptions.
            videos = [{"annotated_intervals": [{"narrated_actions": [action()], "redacted": False, "clip_uid": "clip-A"}], "video_uid": "A"},
                      {"annotated_intervals": [{"narrated_actions": [action("put_(place,_leave,_drop)")], "clip_uid": "clip-B", "redacted": False}], "video_uid": "B"},
                      {"annotated_intervals": [{"narrated_actions": [action()], "clip_uid": "clip-C", "redacted": False}], "video_uid": "C"}]
            write_json(path, {"videos": videos})
            index = {"clip-" + p: {"splits": {"train"}, "parents": {p}} for p in "ABC"}
            result = audit.inspect_main(path, index, {"B", "H"}, {"G": ["C", "H"]}, {"clip-A", "clip-B", "clip-C"}, {}, "stdlib", 8)
            self.assertEqual(result["counts"]["all/all"]["actions"], 3)
            self.assertEqual(result["counts"]["eligible_train_and_local/all"]["actions"], 1)
            self.assertEqual(result["counts"]["eligibility_reasons"]["excluded_parent_or_related_group"], 2)
            self.assertNotIn("PRIVATE NARRATION", json.dumps(result))
            self.assertEqual(result["examples"][0]["action"]["structured_object_labels"], ["cup"])

    def test_end_to_end_and_no_source_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            annotations = root / "ego4d/v2/annotations"
            annotations.mkdir(parents=True)
            clips = annotations.parent / "clips"
            clips.mkdir()
            (clips / "clip-A.mp4").write_bytes(b"file-presence-only-not-a-decoded-video")
            write_json(root / "ego4d/ego4d.json", {"videos": [{"video_uid": "A"}, {"video_uid": "H", "split_fho": "val"}]})
            write_json(annotations / "fho_lta_taxonomy.json", {"verbs": ["take"], "nouns": ["cup"]})
            for split, name in (("train", "fho_lta_train.json"), ("val", "fho_lta_val.json"), ("test", "fho_lta_test_unannotated.json")):
                write_json(annotations / name, {"split": split, "clips": [{"video_uid": "A" if split == "train" else "H", "clip_uid": "clip-A" if split == "train" else "clip-H", "action_clip_start_sec": 0, "action_clip_end_sec": 8, "verb": "take", "noun": "cup", "verb_label": 0, "noun_label": 0}]})
            for name in ("vq_val.json", "vq_test_unannotated.json"):
                write_json(annotations / name, {"videos": [{"video_uid": "H"}]})
            write_json(annotations / "fho_main.json", {"videos": [{"video_uid": "A", "annotated_intervals": [{"clip_uid": "clip-A", "redacted": False, "narrated_actions": [action()]}]}]})
            policy = root / "policy.json"
            write_json(policy, {"excluded_parent_ids": []})
            before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
            output = root / "result"
            command = [sys.executable, str(Path(audit.__file__)), "--annotations", str(annotations), "--project", str(root / "project"), "--output", str(output), "--policy", str(policy), "--parser", "stdlib"]
            process = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            result = json.loads((output / "annotation_inventory.json").read_text())
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["fho_main"]["counts"]["eligible_train_and_local/all"]["actions"], 1)
            self.assertEqual(result["local_clips"]["unique_nonempty_clip_ids"], 1)
            self.assertTrue(all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in before.items()))
            self.assertEqual(result["files_read"][str(annotations / "fho_main.json")]["sha256"], before[str(annotations / "fho_main.json")])
            again = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0, "must not overwrite a previous report")

    def test_streaming_large_scalar_array_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.json"
            with path.open("w") as f:
                f.write("[")
                for i in range(14000):
                    f.write(("," if i else "") + json.dumps("x" * 2000))
                f.write("]")
            code = "import audit_annotations as a,json,resource,sys; n=sum(1 for p,e,v in a.stream(sys.argv[1],{},'stdlib') if e=='string'); r=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss; print(json.dumps({'n':n,'rss_mib':r/(1048576 if sys.platform=='darwin' else 1024)}))"
            p = subprocess.run([sys.executable, "-c", code, str(path)], cwd=Path(audit.__file__).parent, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            result = json.loads(p.stdout.splitlines()[-1])
            self.assertEqual(result["n"], 14000)
            self.assertLess(result["rss_mib"], 80, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
