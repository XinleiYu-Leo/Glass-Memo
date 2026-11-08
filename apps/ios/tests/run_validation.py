#!/usr/bin/env python3
"""Build and run actual recorder code using SDK data-only test value modules.

This verifies persistence on macOS, not glasses availability or iOS transport.
The separate unsigned iOS app build verifies the real SDK API linkage.
"""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess

import sys
sys.dont_write_bytecode = True
from test_tools import configure

root = Path(__file__).resolve().parent
p = argparse.ArgumentParser(description=__doc__)
a, output, build, base = configure(p, root, 'recorder')
modules = build / 'modules'
modules.mkdir()
for name, source in [('MWDATCore', 'SDKCoreValueStubs.swift'), ('MWDATMotion', 'SDKMotionValueStubs.swift')]:
    subprocess.run(base + ['-emit-library', '-emit-module', '-module-name', name, str(root / source),
                          '-o', str(modules / ('lib' + name + '.dylib')),
                          '-emit-module-path', str(modules / (name + '.swiftmodule'))], check=True)
sources = [a.app / 'DisplayAccess/ViewModels/RoboMemoVideoRecorder.swift',
           a.app / 'DisplayAccess/ViewModels/RoboMemoSensorRecorder.swift',
           root / 'RecorderTestSupport.swift', root / 'MultiSensorValidation.swift',
           root / 'TimelineAndSensorRegression.swift']
executable = build / 'recorder-validation'
subprocess.run(base + ['-I', str(modules), '-L', str(modules), '-lMWDATCore', '-lMWDATMotion',
                      '-Xlinker', '-rpath', '-Xlinker', str(modules)] + [str(s) for s in sources]
               + ['-o', str(executable)], check=True)
run = subprocess.run([str(executable), str(output)], capture_output=True, text=True)
(output / 'checks.log').write_text(run.stdout + run.stderr)
validation = {
    'passed': run.returncode == 0,
    'exit_code': run.returncode,
    'scope': 'Actual recorder sources on macOS AVFoundation; SDK value-only stubs. No physical sensors.',
    'sources_sha256': {s.name: hashlib.sha256(s.read_bytes()).hexdigest() for s in sources},
    'checks_passed': run.stdout.count('PASS '),
}
(output / 'validation.json').write_text(json.dumps(validation, indent=2) + '\n')
print(json.dumps(validation, indent=2))
print('RESULTS:', output)
if run.returncode:
    print((run.stdout + run.stderr)[-12000:])
raise SystemExit(run.returncode)
