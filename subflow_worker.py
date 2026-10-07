# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import license_manager
import strict_inspection
import strict_verification
import subtitle_tool_core as core


EXIT_OK = 0
EXIT_NEEDS_PROCESSING = 10
EXIT_NEEDS_REVIEW = 20
EXIT_RETRYABLE = 30
EXIT_TERMINAL = 40
EXIT_LICENSE_UNAVAILABLE = 50


def _installed_root() -> Path:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "SubFlow"


def configure_tool_paths() -> None:
    app_root = core.app_base_dir()
    installed_root = _installed_root()
    roots = [app_root, app_root / "_internal", installed_root, installed_root / "_internal"]

    def first(relative: str, current: str) -> str:
        for root in roots:
            candidate = root / relative
            if candidate.is_file():
                return str(candidate)
        return current

    core.MKVMERGE = first(r"tools\mkvtoolnix\mkvmerge.exe", core.MKVMERGE)
    core.MKVEXTRACT = first(r"tools\mkvtoolnix\mkvextract.exe", core.MKVEXTRACT)
    core.FFMPEG = first(r"tools\ffmpeg\bin\ffmpeg.exe", core.FFMPEG)
    core.TESSERACT = first(r"tools\tesseract\tesseract.exe", core.TESSERACT)


def _license_result() -> tuple[bool, str]:
    config = license_manager.load_config()
    if not config.get("license", {}).get("enabled", False):
        return True, "license_disabled"
    return license_manager.local_license_valid(config)


def _write_json(payload: dict, output_json: str | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if output_json:
        destination = Path(output_json)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(rendered + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    else:
        sys.stdout.write(rendered + "\n")


def _inspect(args: argparse.Namespace) -> int:
    if args.runtime_dir:
        os.environ["SUBFLOW_RUNTIME_DIR"] = str(Path(args.runtime_dir).resolve())
    ok, message = _license_result()
    if not ok:
        _write_json(
            {
                "schema_version": 1,
                "command": "inspect",
                "status": "license_unavailable",
                "reason_codes": ["license_unavailable"],
                "detail": message,
            },
            args.output_json,
        )
        return EXIT_LICENSE_UNAVAILABLE

    try:
        report = strict_inspection.inspect_video(args.input, log=lambda message: print(message, file=sys.stderr))
    except FileNotFoundError as exc:
        _write_json(
            {
                "schema_version": 1,
                "command": "inspect",
                "status": "failed_terminal",
                "reason_codes": ["input_not_found"],
                "detail": str(exc),
            },
            args.output_json,
        )
        return EXIT_TERMINAL
    except Exception as exc:
        _write_json(
            {
                "schema_version": 1,
                "command": "inspect",
                "status": "failed_retryable",
                "reason_codes": ["inspection_failed"],
                "detail": str(exc),
            },
            args.output_json,
        )
        return EXIT_RETRYABLE

    _write_json(report, args.output_json)
    return {
        "accepted": EXIT_OK,
        "needs_processing": EXIT_NEEDS_PROCESSING,
        "needs_review": EXIT_NEEDS_REVIEW,
    }[report["status"]]


def _verify(args: argparse.Namespace) -> int:
    if args.runtime_dir:
        os.environ["SUBFLOW_RUNTIME_DIR"] = str(Path(args.runtime_dir).resolve())
    ok, message = _license_result()
    if not ok:
        _write_json(
            {
                "schema_version": 1,
                "command": "verify",
                "status": "license_unavailable",
                "reason_codes": ["license_unavailable"],
                "detail": message,
            },
            args.output_json,
        )
        return EXIT_LICENSE_UNAVAILABLE
    try:
        report = strict_verification.verify_output(
            args.input,
            source_path=args.source,
            log=lambda message: print(message, file=sys.stderr),
        )
    except FileNotFoundError as exc:
        _write_json(
            {
                "schema_version": 1,
                "command": "verify",
                "status": "failed_terminal",
                "reason_codes": ["input_not_found"],
                "detail": str(exc),
            },
            args.output_json,
        )
        return EXIT_TERMINAL
    except Exception as exc:
        _write_json(
            {
                "schema_version": 1,
                "command": "verify",
                "status": "failed_retryable",
                "reason_codes": ["verification_error"],
                "detail": str(exc),
            },
            args.output_json,
        )
        return EXIT_RETRYABLE
    _write_json(report, args.output_json)
    return EXIT_OK if report["validation"]["passed"] else EXIT_NEEDS_REVIEW


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="SubFlowWorker", description="SubFlow machine worker")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_parser = commands.add_parser("inspect", help="strict read-only media inspection")
    inspect_parser.add_argument("--input", required=True)
    inspect_parser.add_argument("--output-json")
    inspect_parser.add_argument(
        "--runtime-dir",
        help="persistent worker cache/state directory; defaults to the GUI runtime directory",
    )
    inspect_parser.set_defaults(handler=_inspect)
    verify_parser = commands.add_parser("verify", help="strict independent output verification")
    verify_parser.add_argument("--input", required=True, help="output MKV to verify")
    verify_parser.add_argument("--source", help="original source for duration/audio/video comparison")
    verify_parser.add_argument("--output-json")
    verify_parser.add_argument("--runtime-dir")
    verify_parser.set_defaults(handler=_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_tool_paths()
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
