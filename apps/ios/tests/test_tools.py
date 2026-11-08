"""Portable full-Xcode discovery shared by the macOS validation runners."""
from pathlib import Path
import os
import platform
import subprocess
import time
import uuid


def configure(parser, root, prefix):
    parser.add_argument('--app', type=Path, default=root.parent,
                        help='DisplayAccess project directory (default: tests/..)')
    parser.add_argument('--xcode', type=Path,
                        help='Xcode.app or its Contents/Developer; otherwise DEVELOPER_DIR/xcode-select')
    parser.add_argument('--output', type=Path,
                        help='New output directory for reports and temporary build products')
    args = parser.parse_args()
    if platform.system() != 'Darwin':
        parser.error('These AVFoundation/Swift tests require macOS and full Xcode.')
    env = os.environ.copy()
    if args.xcode:
        developer = args.xcode.expanduser().resolve()
        if developer.suffix == '.app':
            developer /= 'Contents/Developer'
        env['DEVELOPER_DIR'] = str(developer)
    elif not env.get('DEVELOPER_DIR'):
        env['DEVELOPER_DIR'] = subprocess.check_output(['xcode-select', '-p'], text=True).strip()
    if not (Path(env['DEVELOPER_DIR']) / 'Platforms/MacOSX.platform').is_dir():
        parser.error('Select full Xcode with --xcode or DEVELOPER_DIR (Command Line Tools alone are insufficient).')
    compiler = subprocess.check_output(['xcrun', '--find', 'swiftc'], env=env, text=True).strip()
    sdk = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], env=env, text=True).strip()
    arch = platform.machine()
    if arch not in ('arm64', 'x86_64'):
        parser.error('Unsupported host architecture: ' + arch)
    args.app = args.app.expanduser().resolve()
    if not (args.app / 'DisplayAccess/ViewModels/RoboMemoVideoRecorder.swift').is_file():
        parser.error('--app must point to the directory containing DisplayAccess.xcodeproj')
    output = args.output or root / '.artifacts' / (prefix + '-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    output = output.expanduser().resolve()
    output.mkdir(exist_ok=False, parents=True)
    build = output / '.build'
    build.mkdir()
    base = [compiler, '-sdk', sdk, '-target', arch + '-apple-macos14.0', '-swift-version', '6',
            '-parse-as-library', '-module-cache-path', str(build / 'module-cache')]
    return args, output, build, base
