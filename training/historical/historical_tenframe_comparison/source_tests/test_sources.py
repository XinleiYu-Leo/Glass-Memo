"""Synthetic-only tests. Every generated source/dataset lives in deleted temp dirs.
Run with a Python containing PyAV and Pillow. No network, model, or real data.
"""
from __future__ import annotations
import contextlib
import copy
import csv
from fractions import Fraction
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import zipfile

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'package/tools'))
import select_sources as selector
import export_sources as exporter
import verify_sources as verifier
import prepare_sources as prepare
import av
from PIL import Image


def uid(n):
    return str(uuid.UUID(int=n))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def fixture(root):
    annotations = root / 'annotations'; annotations.mkdir()
    protocol = root / 'protocol'; protocol.mkdir()
    for name in ('experiment.json', 'source_exclusions.json', 'development_reservation.json', 'actual_model_instructions.txt'):
        shutil.copy2(BASE / 'package/protocol' / name, protocol / name)
    old = json.loads((protocol / 'source_exclusions.json').read_text())
    diagnostic = json.loads((protocol / 'development_reservation.json').read_text())
    videos = {v: {'video_uid': v, 'is_grouped': False} for v in old['declared_train_parents'] + old['declared_dev_parents'] + diagnostic['known_related_parent_ids']}
    metadata_clips, manifest, actions = [], [], []
    verbs = [selector.TAKE, selector.PUT, 'wipe', 'mix', 'hold_(support,_grip,_grasp)', 'open']
    nouns = ['cup', 'door']
    for category, verb in enumerate(verbs):
        for n in range(40 if category < 2 else 12):
            parent, clip = uid(1000 + category * 100 + n), uid(10000 + category * 100 + n)
            videos[parent] = {'video_uid': parent, 'is_grouped': False}
            metadata_clips.append({'clip_uid': clip, 'video_uid': parent, 'video_start_sec': 0, 'video_end_sec': 6})
            manifest.append({'exported_clip_uid': clip, 'parent_video_uid': parent, 'parent_start_sec': 0, 'parent_end_sec': 6, 's3_path': f's3://synthetic-no-network/{clip}.mp4', 'benchmarks': 'FHO', 'size_bytes': 1})
            actions.append({'video_uid': parent, 'clip_uid': clip, 'action_idx': 0, 'clip_parent_start_sec': 0,
                            'clip_parent_end_sec': 6, 'action_clip_start_sec': 2, 'action_clip_end_sec': 4,
                            'action_clip_start_frame': 60, 'action_clip_end_frame': 120,
                            'interval_start_sec': 0, 'interval_end_sec': 6, 'verb': verb,
                            'verb_label': category, 'noun': 'door' if category == 5 else 'cup',
                            'noun_label': 1 if category == 5 else 0})
    val, test, meta_holdout = uid(5000), uid(5001), uid(5002)
    for parent in (val, test, meta_holdout):
        videos[parent] = {'video_uid': parent, 'is_grouped': False}
    videos[meta_holdout]['split_custom'] = 'test'
    groups = {'grp-old': [old['declared_train_parents'][0], uid(1000)],
              'grp-diag': [diagnostic['parent_ids'][0], uid(1001)],
              'grp-diag-nested': ['grp-diag', uid(1002)],
              'grp-selected': [uid(1003), uid(1004)],
              'grp-val': [val, uid(1100)], 'grp-test': [test, uid(1101)],
              'grp-meta': [meta_holdout, uid(1102)]}
    videos.update({g: {'video_uid': g, 'is_grouped': True, 'group_video_uids': members} for g, members in groups.items()})
    reserved = []
    for n in range(60):
        clip = uid(20000 + n); reserved.append(clip)
        metadata_clips.append({'clip_uid': clip, 'video_uid': val, 'video_start_sec': 0, 'video_end_sec': 6})
        manifest.append({'exported_clip_uid': clip, 'parent_video_uid': val, 'parent_start_sec': 0, 'parent_end_sec': 6, 's3_path': f's3://synthetic-no-network/{clip}.mp4', 'benchmarks': 'VQ', 'size_bytes': 1})
    reserved_dir = root / 'reserved60'; reserved_dir.mkdir()
    for clip in reserved:
        (reserved_dir / (clip + '.mp4')).write_bytes(b'SYNTHETIC RESERVED ID PLACEHOLDER; NEVER DECODED')
    write(annotations / 'vq_val.json', {'videos': [{'video_uid': val, 'clips': [{'clip_uid': c} for c in reserved]}]})
    write(annotations / 'vq_test_unannotated.json', {'videos': [{'video_uid': test, 'clips': []}]})
    write(annotations / 'fho_lta_val.json', {'clips': [{'video_uid': val}]})
    write(annotations / 'fho_lta_test_unannotated.json', {'clips': [{'video_uid': test}]})
    write(annotations / 'sta_val.json', {'info': {'video_metadata': {'grp-val': {}}}})
    write(annotations / 'fho_lta_train.json', {'split': 'train', 'clips': actions})
    write(annotations / 'fho_lta_taxonomy.json', {'verbs': verbs, 'nouns': nouns})
    write(root / 'ego4d.json', {'videos': list(videos.values()), 'clips': metadata_clips})
    with (root / 'manifest.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=manifest[0].keys()); w.writeheader(); w.writerows(manifest)
    return SimpleNamespace(project=root, output=root / 'output', annotations=annotations,
                           metadata=root / 'ego4d.json', manifest=root / 'manifest.csv',
                           reserved_clips=reserved_dir, exclusions=protocol / 'source_exclusions.json',
                           reservation=protocol / 'development_reservation.json', protocol=protocol / 'experiment.json')


def make_video(path, *, step=1, frames=180, origin=0):
    with av.open(str(path), 'w') as out:
        stream = out.add_stream('mpeg4', rate=30)
        stream.width = 160; stream.height = 90; stream.pix_fmt = 'yuv420p'
        stream.time_base = Fraction(1, 30); stream.codec_context.time_base = Fraction(1, 30)
        for n in range(frames):
            image = Image.new('RGB', (160, 90), ((n * 7) % 256, (n * 3) % 256, n % 256))
            frame = av.VideoFrame.from_image(image)
            frame.pts = origin + n * step; frame.time_base = Fraction(1, 30)
            for packet in stream.encode(frame): out.mux(packet)
        for packet in stream.encode(): out.mux(packet)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='SYNTHETIC_ONLY_tenframe_')
        self.root = Path(self.temp.name)
        self.args = fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def freeze(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(selector.run(self.args), 0)
        self.args.selection = self.args.output / 'selection_frozen.json'
        return json.loads(self.args.selection.read_bytes())

    def test_exclusions_groupclosure_fixed_splits_repeat_quotas_and_independent_replay(self):
        selection = self.freeze()
        self.assertEqual(verifier.validate_selection(selection)['repeats'], 6)
        self.assertTrue(verifier.verify_isolation(self.args, selection)['independent_seed_replay']['all_64_items_and_windows_reproduced'])
        blocked = set(selection['exclusions']['excluded_parent_ids'])
        self.assertTrue({uid(1000), uid(1001), uid(1002), uid(1100), uid(1101), uid(1102)} <= blocked)
        self.assertFalse({i['video_uid'] for i in selection['items']} & blocked)
        train = set().union(*(set(i['related_group_parent_ids']) for i in selection['items'] if i['split'] == 'train'))
        dev = set().union(*(set(i['related_group_parent_ids']) for i in selection['items'] if i['split'] == 'dev'))
        self.assertFalse(train & dev)
        self.args.output = self.root / 'same_seed_second_freeze'
        second = self.freeze()
        self.assertEqual(selection['items'], second['items'])
        self.assertEqual(selection['selection_content_sha256'], second['selection_content_sha256'])

    def test_insufficient_pool_freezes_stop_without_relaxation_or_reselection(self):
        data = json.loads((self.args.annotations / 'fho_lta_train.json').read_text())
        data['clips'] = [r for r in data['clips'] if r['verb'] != 'mix']
        write(self.args.annotations / 'fho_lta_train.json', data)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(selector.run(self.args), 2)
        selection = json.loads((self.args.output / 'selection_frozen.json').read_bytes())
        with self.assertRaisesRegex(ValueError, 'insufficient'):
            verifier.validate_selection(selection)
        with self.assertRaisesRegex(ValueError, 'refusing overwrite'):
            selector.run(self.args)

    def test_reserved60_mapping_and_unknown_group_fail_closed(self):
        metadata = json.loads(self.args.metadata.read_text())
        metadata['videos'].append({'video_uid': 'grp-invalid', 'is_grouped': True, 'group_video_uids': [uid(999999), uid(1000)]})
        write(self.args.metadata, metadata)
        with self.assertRaisesRegex(ValueError, 'absent from metadata'):
            selector.run(self.args)

    def test_causal_predecessor_exact_pixels_and_mutation_rejection(self):
        selection = self.freeze(); row = selection['items'][0]
        video = self.root / 'source.mp4'; make_video(video)
        dataset = self.root / 'dataset'; dataset.mkdir()
        result = exporter.export_one(row, video, dataset, exporter.load_preprocessing())
        item = {k: row[k] for k in ('item_id', 'split', 'video_uid', 'clip_uid', 'related_group_parent_ids', 'target_start_sec', 'target_end_sec')}
        item.update(result)
        self.assertEqual(verifier.verify_one(item, row, dataset, video)['unique_source_frames'], 10)
        mutated = copy.deepcopy(item); mutated['frames'][1]['source_frame_index'] += 1
        with self.assertRaisesRegex(ValueError, 'nearest causal'):
            verifier.verify_one(mutated, row, dataset, video)
        mutated = copy.deepcopy(item); mutated['frames'][0]['actual_time_sec'] = row['target_start_sec'] - .1
        with self.assertRaisesRegex(ValueError, 'source time'):
            verifier.verify_one(mutated, row, dataset, video)
        image_path = dataset / item['frames'][0]['path']
        Image.new('RGB', (224,224), (0,0,0)).save(image_path)
        with self.assertRaisesRegex(ValueError, 'PNG hash'):
            verifier.verify_one(item, row, dataset, video)

    def test_before_target_repeated_and_truncated_sources_rejected(self):
        selection = self.freeze(); row = copy.deepcopy(selection['items'][0])
        # Deliberately put a target endpoint off the low-fps source's PTS grid.
        row.update(decision_tick_sixths=20, target_end_sec=20/6, target_start_sec=11/6)
        video = self.root / 'sparse.mp4'; make_video(video, step=12, frames=18)
        with self.assertRaisesRegex(ValueError, 'predecessor before target'):
            exporter.export_one(row, video, self.root / 'sparse_dataset', exporter.load_preprocessing())
        row.update(decision_tick_sixths=21, target_end_sec=3.5, target_start_sec=2.0)
        with self.assertRaisesRegex(ValueError, 'distinct source'):
            exporter.export_one(row, video, self.root / 'repeat_dataset', exporter.load_preprocessing())
        short = self.root / 'short.mp4'; make_video(short, frames=75)
        with self.assertRaisesRegex(ValueError, 'distinct source|ended before target'):
            exporter.export_one(row, short, self.root / 'short_dataset', exporter.load_preprocessing())

    def test_atomic_write_resume_no_overwrite(self):
        path = self.root / 'artifact.json'
        path.with_name(path.name + '.pending').write_bytes(b'interrupted partial output')
        exporter.write_once(path, b'complete frozen bytes')
        path.with_name(path.name + '.pending').write_bytes(b'complete frozen bytes')
        exporter.write_once(path, b'complete frozen bytes')
        with self.assertRaisesRegex(ValueError, 'refusing overwrite'):
            exporter.write_once(path, b'changed bytes')
        self.assertEqual(path.read_bytes(), b'complete frozen bytes')
        self.assertFalse(path.with_name(path.name + '.pending').exists())

    def test_interrupted_freeze_stops_before_any_download(self):
        self.freeze()
        (self.args.output / 'exclusion_audit.json').unlink()
        fake_cli = self.root / 'official_cli_DO_NOT_EXECUTE'; fake_cli.write_text('synthetic-only placeholder')
        with patch.object(prepare, 'OFFICIAL_CLI', fake_cli), patch.object(prepare.subprocess, 'run', side_effect=AssertionError('network must not run')), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'publication interrupted'):
                prepare.run(self.args)

    def test_full64_export_independent_verify_pack_and_safe_resume_without_network(self):
        selection = self.freeze()
        source = self.root / 'SYNTHETIC_ONLY.mp4'; make_video(source)
        clip_dir = self.args.output / 'downloads/ego4d/v2/clips'; clip_dir.mkdir(parents=True)
        for item in selection['items']:
            shutil.copyfile(source, clip_dir / (item['clip_uid'] + '.mp4'))
        fake_cli = self.root / 'official_cli_DO_NOT_EXECUTE'; fake_cli.write_text('synthetic-only placeholder')
        calls = []
        real_run = prepare.subprocess.run
        def fake_download(command, **kwargs):
            if str(command[0]) == str(fake_cli):
                calls.append(command)
                return SimpleNamespace(returncode=0)
            return real_run(command, **kwargs)
        with patch.object(prepare, 'OFFICIAL_CLI', fake_cli), patch.object(prepare, 'MAX_ZIP_BYTES', 200000), patch.object(prepare.subprocess, 'run', fake_download), contextlib.redirect_stdout(io.StringIO()):
            prepare.run(self.args)
            hashes = {p.name: exporter.digest(p) for p in self.args.output.glob('return_*.zip')}
            prepare.run(self.args)
        # Existing64 filenames with no receipt MUST still invoke official completion check.
        self.assertEqual(len(calls), 1)
        self.assertEqual(hashes, {p.name: exporter.digest(p) for p in self.args.output.glob('return_*.zip')})
        report = json.loads((self.args.output / 'independent_validation.json').read_text())
        self.assertTrue(report['independent_official_isolation_replay'])
        self.assertTrue(report['dataset']['source_decode_verified'])
        self.assertEqual(report['dataset']['png_count'], 640)
        seen = []
        self.assertGreater(len(list(self.args.output.glob('return_images_part*.zip'))), 1)
        for path in self.args.output.glob('return_images_part*.zip'):
            self.assertLessEqual(path.stat().st_size, 200000)
            with zipfile.ZipFile(path) as z:
                self.assertIsNone(z.testzip())
                for name in z.namelist():
                    self.assertTrue(name.endswith('.png') or name.startswith('dataset/teacher_manifest_part'))
                    self.assertFalse(name.endswith('.mp4'))
                    if name.endswith('.json'):
                        document = json.loads(z.read(name))
                        for item in document['items']:
                            self.assertEqual(set(item), {'item_id', 'target_start_sec', 'target_end_sec', 'frames'})
                            seen.append(item['item_id'])
        self.assertEqual(sorted(seen), [f'T{n:02d}' for n in range(1,65)])
        with zipfile.ZipFile(self.args.output / 'return_metadata.zip') as z:
            self.assertIn('independent_validation.json', z.namelist())
            self.assertIn('dataset/manifest.json', z.namelist())
            self.assertFalse(any(n.endswith('.mp4') for n in z.namelist()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
