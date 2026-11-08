"""Create an explicitly unreviewed label template. This never infers labels."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    path = a.dataset / 'manifest.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != 'robomemo-tenframe-dataset-v1' or len(data['items']) != 64:
        raise ValueError('Expected the complete frozen 64-case dataset')
    result = {'schema': 'robomemo-tenframe-reviewed-labels-v1',
              'protocol_sha256': data['protocol_sha256'],
              'dataset_manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'status': 'blank_unreviewed_not_training_ready',
              'items': [dict(item_id=x['item_id'], split=x['split'], label=None,
                  status='held', teacher_decision=None, blind_review_decision=None,
                  blind_review_id=None, blind_review_saw_teacher=False,
                  repeat_decisions=[], evidence_frame_ids=[], rationale='',
                  input_scope='same_ten_224_pngs') for x in data['items']]}
    with a.output.open('x', encoding='utf-8') as out:
        out.write(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print('UNREVIEWED_LABEL_TEMPLATE:', a.output)

if __name__ == '__main__':
    main()
