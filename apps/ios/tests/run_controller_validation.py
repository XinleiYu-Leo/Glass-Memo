#!/usr/bin/env python3
"""Exercise actual controller against asynchronous SDK test doubles, no hardware."""
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
a, output, build, base = configure(p, root, 'controller')
modules = build / 'modules'
modules.mkdir()
for name, sources, dependencies in [
    ('MWDATCore', ['SDKCoreValueStubs.swift', 'ControllerSDKCore.swift'], []),
    ('MWDATMotion', ['SDKMotionValueStubs.swift', 'ControllerSDKMotion.swift'], ['-lMWDATCore']),
    ('MWDATCamera', ['ControllerSDKCamera.swift'], ['-lMWDATCore']),
]:
    subprocess.run(base + ['-I', str(modules), '-L', str(modules)] + dependencies +
                   ['-emit-library', '-emit-module', '-module-name', name] + [str(root / s) for s in sources] +
                   ['-o', str(modules / ('lib' + name + '.dylib')), '-emit-module-path', str(modules / (name + '.swiftmodule'))], check=True)
sources = [a.app / 'DisplayAccess/ViewModels' / file for file in
           ['RoboMemoVideoRecorder.swift', 'RoboMemoSensorRecorder.swift', 'RoboMemoMultiSensorController.swift']]
sources += [root / 'RecorderTestSupport.swift', root / 'ControllerValidation.swift']
executable = build / 'controller-validation'
subprocess.run(base + ['-I', str(modules), '-L', str(modules), '-lMWDATCore', '-lMWDATMotion', '-lMWDATCamera',
                      '-Xlinker', '-rpath', '-Xlinker', str(modules)] + [str(s) for s in sources] + ['-o', str(executable)], check=True)
run = subprocess.run([str(executable), str(output)], capture_output=True, text=True, timeout=50)
(output / 'checks.log').write_text(run.stdout + run.stderr)
validation = {'passed': run.returncode == 0, 'exit_code': run.returncode,
              'scope': 'Actual controller/recorder sources with asynchronous SDK behavior test doubles; no hardware.',
              'sources_sha256': {s.name: hashlib.sha256(s.read_bytes()).hexdigest() for s in sources},
              'checks_passed': run.stdout.count('PASS ')}
(output / 'validation.json').write_text(json.dumps(validation, indent=2) + '\n')
print(json.dumps(validation, indent=2)); print('RESULTS:', output)
if run.returncode: print((run.stdout + run.stderr)[-10000:])
raise SystemExit(run.returncode)
