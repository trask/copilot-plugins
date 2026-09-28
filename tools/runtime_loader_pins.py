#!/usr/bin/env python3
"""Generate and check byte-pinned Runtime source loaders."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
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
    "consumers",
}
CONSUMER_FIELDS = {"path", "constant"}


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
        or data["schema"] != 2
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
            or not all(
                isinstance(dependency[field], str) and dependency[field]
                for field in DEPENDENCY_FIELDS - {"consumers"}
            )
            or IDENTIFIER_PATTERN.fullmatch(dependency["constant"]) is None
            or IDENTIFIER_PATTERN.fullmatch(dependency["loader"]) is None
            or IDENTIFIER_PATTERN.fullmatch(dependency["module"]) is None
            or SHA256_PATTERN.fullmatch(dependency["sha256"]) is None
            or not isinstance(dependency["consumers"], list)
            or not dependency["consumers"]
        ):
            raise PinError(f"Runtime loader dependency {name!r} is malformed")
        safe_path(dependency["source"])
        safe_path(dependency["installed_path"], require_file=False)
        for consumer in dependency["consumers"]:
            if (
                not isinstance(consumer, dict)
                or set(consumer) != CONSUMER_FIELDS
                or not isinstance(consumer["constant"], str)
                or IDENTIFIER_PATTERN.fullmatch(consumer["constant"]) is None
                or not isinstance(consumer["path"], str)
            ):
                raise PinError(f"Runtime loader consumer of {name!r} is malformed")
            safe_path(consumer["path"])
    source_paths = [dependency["source"] for dependency in data["dependencies"].values()]
    if len(set(source_paths)) != len(source_paths):
        raise PinError("Runtime loader sources must be unique")
    pins = [
        (consumer["path"], consumer["constant"])
        for dependency in data["dependencies"].values()
        for consumer in dependency["consumers"]
    ]
    if len(set(pins)) != len(pins):
        raise PinError("Runtime loader consumer assignments must be unique")
    dependency_order(data)
    return data


def safe_path(value: str, *, require_file: bool = True) -> Path:
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or ":" in value
    ):
        raise PinError(f"unsafe Runtime loader path: {value!r}")
    path = ROOT.joinpath(*value.split("/"))
    if (
        (require_file and not path.is_file())
        or path.is_symlink()
        or not path.resolve().is_relative_to(ROOT.resolve())
    ):
        raise PinError(f"invalid Runtime loader file: {value!r}")
    return path


def dependency_order(data: dict[str, Any]) -> list[str]:
    dependencies = data["dependencies"]
    sources = {entry["source"]: name for name, entry in dependencies.items()}
    incoming: dict[str, set[str]] = {name: set() for name in dependencies}
    for name, entry in dependencies.items():
        for consumer in entry["consumers"]:
            downstream = sources.get(consumer["path"])
            if downstream is not None:
                incoming[downstream].add(name)
    order: list[str] = []
    while incoming:
        ready = sorted(name for name, parents in incoming.items() if not parents)
        if not ready:
            raise PinError("Runtime loader pin dependencies contain a cycle")
        order.extend(ready)
        for name in ready:
            del incoming[name]
        for parents in incoming.values():
            parents.difference_update(ready)
    return order


def source_digest(dependency: dict[str, Any]) -> str:
    path = safe_path(dependency["source"])
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise PinError(f"could not read Runtime source {path}: {error}") from None


def pin_literal(source: bytes, constant: str) -> tuple[int, int, str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, UnicodeError, ValueError) as error:
        raise PinError(f"invalid Runtime loader consumer source: {error}") from None
    matches = [
        node.value
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and (
            any(isinstance(target, ast.Name) and target.id == constant for target in node.targets)
            if isinstance(node, ast.Assign)
            else isinstance(node.target, ast.Name) and node.target.id == constant
        )
    ]
    if len(matches) != 1 or not isinstance(matches[0], ast.Constant) or not isinstance(matches[0].value, str):
        raise PinError(f"expected one literal assignment to {constant}")
    value = matches[0]
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    start = offsets[value.lineno - 1] + value.col_offset
    end = offsets[value.end_lineno - 1] + value.end_col_offset
    literal = source[start:end]
    if re.fullmatch(rb"""["'][0-9a-f]{64}["']""", literal) is None:
        raise PinError(f"expected one SHA256 string literal for {constant}")
    return start, end, value.value


def prepared_pins(data: dict[str, Any], *, update: bool) -> tuple[dict[Path, bytes], list[str]]:
    files: dict[Path, bytes] = {}
    stale: list[str] = []
    for name in dependency_order(data):
        dependency = data["dependencies"][name]
        source = safe_path(dependency["source"])
        contents = files.get(source)
        if contents is None:
            contents = source.read_bytes()
        digest = hashlib.sha256(contents).hexdigest()
        if dependency["sha256"] != digest:
            stale.append(f"{name}: expected {dependency['sha256']}, found {digest}")
        if update:
            dependency["sha256"] = digest
        for consumer in dependency["consumers"]:
            path = safe_path(consumer["path"])
            content = files.get(path)
            if content is None:
                content = path.read_bytes()
            start, end, value = pin_literal(content, consumer["constant"])
            if value != digest:
                stale.append(f"{consumer['path']}:{consumer['constant']}: expected {digest}, found {value}")
                if update:
                    quote = content[start:start + 1]
                    files[path] = content[:start] + quote + digest.encode("ascii") + quote + content[end:]
    return files, stale


def check_spec(path: Path) -> None:
    data = read_spec(path)
    _, stale = prepared_pins(data, update=False)
    if stale:
        raise PinError(
            "Runtime loader pins are stale; run "
            f"`python tools/runtime_loader_pins.py update`: {'; '.join(stale)}"
        )


def update_spec(path: Path) -> None:
    data = read_spec(path)
    files, _ = prepared_pins(data, update=True)
    spec_bytes = (json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")
    updates = {**files, path: spec_bytes}
    staged: list[tuple[Path, Path, Path]] = []
    temporary_files: list[Path] = []
    applied: list[tuple[Path, Path]] = []
    retained: set[Path] = set()
    try:
        for target, content in updates.items():
            original = target.read_bytes()
            if original == content:
                continue
            replacement = stage_file(target, content)
            temporary_files.append(replacement)
            backup = stage_file(target, original)
            temporary_files.append(backup)
            staged.append((target, replacement, backup))
        for target, replacement, backup in staged:
            os.replace(replacement, target)
            applied.append((target, backup))
    except OSError as error:
        restore_errors = []
        for target, backup in reversed(applied):
            try:
                os.replace(backup, target)
            except OSError as restore_error:
                retained.add(backup)
                restore_errors.append(f"{target}: {restore_error} (backup: {backup})")
        detail = f"could not update Runtime loader pins: {error}"
        if restore_errors:
            detail += "; could not restore " + "; ".join(restore_errors)
        raise PinError(detail) from error
    finally:
        for temporary in temporary_files:
            if temporary not in retained:
                temporary.unlink(missing_ok=True)


def stage_file(target: Path, content: bytes) -> Path:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    assert temporary is not None
    return temporary


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
