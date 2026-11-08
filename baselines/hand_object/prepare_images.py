"""Letterbox collaborator-owned images to square inputs for the legacy detector."""
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image, ImageOps


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--images', nargs='+', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--size', type=int, default=600)
    args = parser.parse_args()
    if args.size < 32:
        parser.error('--size must be at least 32')
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError('Use a new or empty output directory')
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, path in enumerate(args.images):
        with Image.open(path) as raw:
            image = ImageOps.exif_transpose(raw).convert('RGB')
            width, height = image.size
            scale = min(args.size / width, args.size / height)
            new_width = max(1, round(width * scale))
            new_height = max(1, round(height * scale))
            resized = image.resize((new_width, new_height), Image.Resampling.BILINEAR)
            left = (args.size - new_width) // 2
            top = (args.size - new_height) // 2
            square = Image.new('RGB', (args.size, args.size), (114, 114, 114))
            square.paste(resized, (left, top))
            name = f'image_{index:05d}.png'
            output = args.output / name
            square.save(output)
            rows.append({'item_id': f'image_{index:05d}', 'output_file': name,
                         'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                         'input_dimensions': [width, height],
                         'output_dimensions': [args.size, args.size],
                         'resize_dimensions': [new_width, new_height],
                         'padding_left_top': [left, top],
                         'scale_xy': [new_width / width, new_height / height],
                         'source_pixels_after_exif_orientation': True,
                         'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest()})
    (args.output / 'transforms.json').write_text(json.dumps({'images': rows}, indent=2) + '\n', encoding='utf-8')
    print(f'Prepared {len(rows)} square input images')


if __name__ == '__main__':
    main()
