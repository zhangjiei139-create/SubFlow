"""Remove an irrelevant developer launcher record from copied wheel metadata."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re

RECORD = Path('_internal/tools/ffsubsync/_internal/wheel-0.47.0.dist-info/RECORD')
PRIVATE_LAUNCHER = re.compile(r'(?i)^[a-z]:[\\/]Users[\\/][^\\/]+[\\/].*[\\/]Scripts[\\/]wheel\.exe$')


def clean_record(application):
    root = Path(application).resolve()
    record = root / RECORD
    if not record.is_file():
        return {'passed': True, 'record_present': False, 'removed_launcher_records': 0}
    assert record.resolve().is_relative_to(root) and not record.is_symlink()
    before = record.read_bytes()
    kept, removed = [], 0
    for line in before.splitlines(keepends=True):
        rows = list(csv.reader([line.decode('utf8')]))
        if rows and rows[0] and PRIVATE_LAUNCHER.fullmatch(rows[0][0]):
            removed += 1
        else:
            kept.append(line)
    assert removed <= 1, 'Unexpected wheel launcher metadata; inspect before publication'
    after = b''.join(kept)
    if after != before:
        temporary = record.with_name('RECORD.privacy.tmp')
        temporary.write_bytes(after)
        temporary.replace(record)
    return {'passed': True, 'record_present': True, 'relative_record': RECORD.as_posix(),
            'removed_launcher_records': removed,
            'before_sha256': hashlib.sha256(before).hexdigest(),
            'after_sha256': hashlib.sha256(after).hexdigest(),
            'other_record_bytes_preserved': True,
            'executables_or_native_libraries_changed': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--application-root', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = clean_record(args.application_root)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
