"""Compact ZIP payloads supported by the Python-based web downloader."""
from __future__ import annotations
import argparse
import shutil
import zipfile
from pathlib import Path


def pack_directory(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_LZMA, allowZip64=True) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError("Payload must not contain symbolic links")
            if path.is_file():
                archive.write(path, (Path(source.name) / path.relative_to(source)).as_posix())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError("Payload CRC verification failed")


def repack_runtime(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        raise ValueError("Runtime source and output must differ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w", allowZip64=True) as output:
        names = original.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate runtime ZIP entries")
        expected = {}
        for member in original.infolist():
            if member.is_dir():
                continue
            metadata = zipfile.ZipInfo(member.filename, member.date_time)
            metadata.compress_type = zipfile.ZIP_LZMA
            metadata.external_attr = member.external_attr
            with original.open(member) as src, output.open(metadata, "w", force_zip64=True) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            expected[member.filename] = (member.file_size, member.CRC)
    with zipfile.ZipFile(destination) as archive:
        actual = {item.filename: (item.file_size, item.CRC) for item in archive.infolist()}
        if actual != expected or archive.testzip() is not None:
            raise ValueError("Repacked runtime differs from original")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("app", "runtime"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.kind == "app":
        pack_directory(args.source, args.destination)
    else:
        repack_runtime(args.source, args.destination)
    print(f"Verified {args.kind} payload: {args.destination.stat().st_size} bytes", flush=True)
