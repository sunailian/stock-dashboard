#!/usr/bin/env python3
"""Build an Alibaba FC zip containing app.py and the Linux Longport SDK wheel."""
import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def build_bundle(python_version, architecture, output):
    abi = 'cp' + python_version.replace('.', '')
    platform = 'manylinux2014_' + architecture
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='stock-dashboard-fc-') as temporary:
        staging = Path(temporary) / 'package'
        staging.mkdir()
        subprocess.run([
            sys.executable, '-m', 'pip', 'install',
            '--requirement', str(ROOT / 'requirements-fc.txt'),
            '--target', str(staging), '--platform', platform,
            '--python-version', python_version.replace('.', ''),
            '--implementation', 'cp', '--abi', abi,
            '--only-binary=:all:', '--no-compile',
        ], check=True)
        shutil.copy2(ROOT / 'app.py', staging / 'app.py')
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(staging))
    print(f'Built {output}')
    print(f'Runtime target: CPython {python_version} / {architecture} / {platform}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-version', default='3.12', choices=('3.8', '3.9', '3.10', '3.11', '3.12', '3.13'))
    parser.add_argument('--architecture', default='x86_64', choices=('x86_64', 'aarch64'))
    parser.add_argument('--output', default=str(ROOT / 'dist' / 'stock-dashboard-fc.zip'))
    args = parser.parse_args()
    build_bundle(args.python_version, args.architecture, args.output)


if __name__ == '__main__':
    main()
