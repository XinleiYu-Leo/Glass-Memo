"""Package reviewed observations and original timestamp evidence; no inference or upload."""
import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reviewed', type=Path, required=True)
    parser.add_argument('--source', type=Path, action='append', required=True,
                        help='Original recording directory; repeat for additional recording sessions.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    review = json.loads(args.reviewed.read_text())
    output = args.output.resolve()
    for name in ['evidence', 'diagnostics', 'review']:
        (output / 'data' / name).mkdir(parents=True, exist_ok=True)
    dataset = {
        'schema_version': 1, 'title': 'RoboMemo · 找物品',
        'indexed_at': datetime.now(timezone.utc).isoformat(),
        'objects': review.get('objects', [
            {'id': 'keys', 'label_zh': '钥匙', 'aliases': ['钥匙', '鑰匙', '钥匙串', '钥匙扣', 'key', 'keys', 'keyring', 'keychain']},
            {'id': 'wallet', 'label_zh': '钱包', 'aliases': ['钱包', '錢包', '皮夹', '皮夾', 'wallet', 'purse']},
        ]),
        'limitations': review['review_method']['coverage_limitations'],
        'clips': [], 'observations': [],
        'provenance': 'codex_visual_review', 'ground_truth_status': 'user_ground_truth_not_provided',
        'review_method': review['review_method'],
    }
    rows_by_clip = {}
    clip_data = sorted(review['clips'], key=lambda c: c['clip_order'])
    labels = ['01 · High@15', '02 · High@7', '03 · Medium@15', '04 · High@15']
    for i, clip in enumerate(clip_data):
        cid = clip['clip_id']
        sources = [folder for folder in args.source if (folder / f'{cid}.json').is_file()]
        if len(sources) != 1:
            raise ValueError(f'Expected exactly one source directory for {cid}; found {len(sources)}')
        source = sources[0]
        metadata_path = source / f'{cid}.json'
        csv_path = source / f'{cid}_frames.csv'
        metadata = json.loads(metadata_path.read_text())
        if metadata.get('recordingStatus') != 'completed' or metadata.get('diagnosticsStatus') != 'complete':
            raise ValueError(f'Incomplete clip: {cid}')
        with csv_path.open(newline='') as file:
            rows = list(csv.DictReader(file))
        if len(rows) != metadata['receivedFrames']:
            raise ValueError(f'Incorrect row count: {cid}')
        written = [r for r in rows if r['result'] == 'written']
        rows_by_clip[cid] = written
        for file in [metadata_path, csv_path]:
            shutil.copy2(file, output / 'data/diagnostics' / file.name)
        video = output / 'data/videos' / f'{cid}.mp4'
        if not video.is_file():
            raise ValueError(f'Prepare playback copy first: {video}')
        main_size = max(metadata['nativeFrameCounts'], key=metadata['nativeFrameCounts'].get).replace('x', '×')
        dataset['clips'].append({
            'id': cid, 'label': clip.get('display_label', labels[i] if i < len(labels) else clip['label']),
            'started_at': metadata['startedAt'], 'duration_seconds': clip['duration_seconds'],
            'video_file': str(video.relative_to(output)), 'poster_file': '',
            'quality_note': clip.get('quality_note', f'主要收到 {main_size}；抽查存在花屏'),
            'requested_quality': f"{metadata['requestedResolution']}@{metadata['frameRate']}",
            'source_mov_filename': f'{cid}.mov',
            'source_mov_sha256': sha256(source / f'{cid}.mov'),
            'playback_sha256': sha256(video),
            'review_coverage': clip['review_coverage'],
        })
    for item in review['observations']:
        o = dict(item)
        o['id'] = o.pop('observation_id')
        source_image = Path(o.pop('evidence_image'))
        target_image = output / 'data/evidence' / (o['id'] + source_image.suffix.lower())
        shutil.copy2(source_image, target_image)
        o['evidence_file'] = str(target_image.relative_to(output))
        o['evidence_sha256'] = sha256(target_image)
        nearest = min(rows_by_clip[o['clip_id']], key=lambda r: abs(float(r['outputPTSSeconds']) - o['output_pts_seconds']))
        difference = abs(float(nearest['outputPTSSeconds']) - o['output_pts_seconds'])
        if difference >= 0.002:
            raise ValueError(f'No matching recorded frame for {o["id"]}: {difference}')
        o['source_frame_index'] = int(nearest['frameIndex'])
        o['source_width'] = int(nearest['sourceWidth'])
        o['source_height'] = int(nearest['sourceHeight'])
        o['csv_output_pts_seconds'] = float(nearest['outputPTSSeconds'])
        o['timestamp_match_error_seconds'] = difference
        # A partial, medium-confidence glimpse stays in history, but is not
        # promoted to the primary "last clearly seen" answer.
        o['usable_as_clear_evidence'] = o.get('confidence_qualitative') == 'high'
        dataset['observations'].append(o)
    for clip in dataset['clips']:
        candidates = [o for o in dataset['observations'] if o['clip_id'] == clip['id'] and o['usable_as_clear_evidence'] and o.get('is_object_visible')]
        if not candidates:
            raise ValueError(f'No clear poster for {clip["id"]}')
        clip['poster_file'] = candidates[0]['evidence_file']
    # Preserve the visual review as provenance without retaining machine-specific paths.
    audit = {k: v for k, v in review.items() if k not in ('clips', 'observations')}
    audit['note_zh'] = '原复核提出的最新可辨帧包含部分中等置信画面。主答案另采用高置信清楚画面；全部观察保留于索引与时间线。'
    (output / 'data/review/visual-review.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    (output / 'data/index.json').write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'clips': len(dataset['clips']), 'observations': len(dataset['observations']), 'clear': sum(o['usable_as_clear_evidence'] and o.get('use_for_last_seen', True) and o.get('is_object_visible', True) and o['state'] != 'uncertain' for o in dataset['observations'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
