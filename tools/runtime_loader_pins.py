#!/usr/bin/env python3
"""Generate and check byte-pinned Runtime source loaders."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = Path(__file__).with_name("runtime-loader-pins.json")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DEPENDENCY_FIELDS = {
    "constant",
    "installed_path",
    "loader",
    "module",
    "sha256",
    "source",
}


class PinError(RuntimeError):
    pass


def read_spec(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PinError(f"could not read {path}: {error}") from None
    if (
        not isinstance(data, dict)
        or set(data) != {"schema", "dependencies"}
        or data["schema"] != 1
        or not isinstance(data["dependencies"], dict)
        or not data["dependencies"]
    ):
        raise PinError("Runtime loader pin spec has an unsupported shape")
    for name, dependency in data["dependencies"].items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(dependency, dict)
            or set(dependency) != DEPENDENCY_FIELDS
            or not all(isinstance(value, str) and value for value in dependency.values())
            or IDENTIFIER_PATTERN.fullmatch(dependency["constant"]) is None
            or IDENTIFIER_PATTERN.fullmatch(dependency["loader"]) is None
            or IDENTIFIER_PATTERN.fullmatch(dependency["module"]) is None
            or SHA256_PATTERN.fullmatch(dependency["sha256"]) is None
        ):
            raise PinError(f"Runtime loader dependency {name!r} is malformed")
        source = Path(dependency["source"])
        installed = Path(dependency["installed_path"])
        if (
            source.is_absolute()
            or installed.is_absolute()
            or ".." in source.parts
            or ".." in installed.parts
        ):
            raise PinError(f"Runtime loader dependency {name!r} has an unsafe path")
    return data


def source_digest(dependency: dict[str, str]) -> str:
    path = ROOT / dependency["source"]
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise PinError(f"could not read Runtime source {path}: {error}") from None


def check_spec(path: Path) -> None:
    data = read_spec(path)
    stale = []
    for name, dependency in sorted(data["dependencies"].items()):
        actual = source_digest(dependency)
        if actual != dependency["sha256"]:
            stale.append(f"{name}: expected {dependency['sha256']}, found {actual}")
    if stale:
        raise PinError(
            "Runtime loader pins are stale; run "
            f"`python tools/runtime_loader_pins.py update`: {'; '.join(stale)}"
        )


def update_spec(path: Path) -> None:
    data = read_spec(path)
    for dependency in data["dependencies"].values():
        dependency["sha256"] = source_digest(dependency)
    path.write_text(
        json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def render_loader(name: str, dependency: dict[str, str]) -> str:
    constant = dependency["constant"]
    installed_parts = ", ".join(
        repr(part) for part in Path(dependency["installed_path"]).parts
    )
    return f'''import hashlib
import sys
from pathlib import Path
from types import ModuleType


{constant} = "{dependency["sha256"]}"
{constant.removesuffix("_SHA256")}_RELATIVE_PATH = Path({installed_parts})


def {dependency["loader"]}(source_path: Path) -> ModuleType:
    if (
        not source_path.is_absolute()
        or not source_path.is_file()
        or source_path.is_symlink()
        or source_path.parent.is_symlink()
    ):
        raise RuntimeError("{name} Runtime source path is invalid")
    source_path = source_path.resolve()
    source = source_path.read_bytes()
    if hashlib.sha256(source).hexdigest() != {constant}:
        raise RuntimeError("{name} Runtime source digest changed")
    module = ModuleType("{dependency["module"]}")
    module.__file__ = str(source_path)
    sys.modules[module.__name__] = module
    try:
        exec(compile(source, str(source_path), "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        sys.modules.pop(module.__name__, None)
        raise
    return module
'''


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("update")
    generate = commands.add_parser("generate")
    generate.add_argument("dependency")
    generate.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "check":
            check_spec(args.spec)
            return 0
        if args.command == "update":
            update_spec(args.spec)
            check_spec(args.spec)
            return 0
        data = read_spec(args.spec)
        dependency = data["dependencies"].get(args.dependency)
        if dependency is None:
            choices = ", ".join(sorted(data["dependencies"]))
            raise PinError(
                f"unknown Runtime dependency {args.dependency!r}; choose {choices}"
            )
        check_spec(args.spec)
        rendered = render_loader(args.dependency, dependency)
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            try:
                args.output.write_text(rendered, encoding="utf-8", newline="\n")
            except OSError as error:
                raise PinError(f"could not write {args.output}: {error}") from None
        return 0
    except PinError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
