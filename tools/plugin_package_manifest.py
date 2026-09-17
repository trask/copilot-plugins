#!/usr/bin/env python3
"""Create and verify canonical installed-plugin package manifests."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unicodedata
from typing import Any


SCHEMA_ID = "github.copilot.plugin-package-manifest"
SCHEMA_VERSION = 1
GENERATOR_NAME = "trask/copilot-plugins plugin_package_manifest"
GENERATOR_VERSION = "1.0.0"
PLUGIN_NAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
ALGORITHM = {
    "aggregate": "sha256",
    "digest_encoding": "lowercase hexadecimal ASCII",
    "file_set": (
        "Every regular Git blob recursively tracked below plugins/<name> at "
        "source_commit, with no missing or extra installed regular files and "
        "no symlinks."
    ),
    "ordering": "Ascending lexicographic order of normalized UTF-8 path bytes.",
    "path_normalization": (
        "Plugin-relative Unicode NFC path with forward-slash separators; "
        "absolute paths, empty components, dot components, backslashes, NUL, "
        "CR, LF, and normalization collisions are rejected."
    ),
    "record_framing": (
        "path_utf8 + NUL + decimal_byte_size_ascii + NUL + "
        "file_sha256_lowercase_hex_ascii + LF"
    ),
}


class ManifestError(RuntimeError):
    pass


def process_options() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def run_git(
    repository_root: Path,
    *arguments: str,
    text: bool = False,
) -> bytes | str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        **process_options(),
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode(
            "utf-8", errors="replace"
        )
        raise ManifestError(
            f"git {' '.join(arguments)} failed: {stderr.strip()}"
        )
    return completed.stdout


def canonical_relative_path(parts: tuple[str, ...]) -> str:
    if not parts:
        raise ManifestError("package path is empty")
    normalized_parts = []
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or "\\" in part
            or any(character in part for character in "\0\r\n")
        ):
            raise ManifestError(f"package path component is invalid: {part!r}")
        normalized_parts.append(unicodedata.normalize("NFC", part))
    return "/".join(normalized_parts)


def source_files(
    repository_root: Path,
    commit: str,
    plugin: str,
) -> tuple[str, dict[str, str]]:
    resolved_commit = str(
        run_git(repository_root, "rev-parse", f"{commit}^{{commit}}", text=True)
    ).strip()
    prefix = f"plugins/{plugin}/"
    raw = bytes(
        run_git(
            repository_root,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            resolved_commit,
            "--",
            f"plugins/{plugin}",
        )
    )
    files: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            identity, raw_path = entry.split(b"\t", 1)
            mode, object_type, object_id = identity.decode("ascii").split(" ")
            source_path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise ManifestError("Git package entry is malformed") from error
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ManifestError(
                f"unsupported Git package entry: {mode} {object_type} {source_path}"
            )
        if not source_path.startswith(prefix):
            raise ManifestError(f"Git package path escaped its prefix: {source_path}")
        relative = canonical_relative_path(
            tuple(source_path[len(prefix) :].split("/"))
        )
        if relative in files:
            raise ManifestError(
                f"Git package paths collide after normalization: {relative}"
            )
        files[relative] = object_id
    if not files:
        raise ManifestError(f"Git package is empty or missing: {plugin}")
    return resolved_commit, files


def installed_files(plugin_root: Path) -> dict[str, Path]:
    if (
        not plugin_root.is_dir()
        or plugin_root.is_symlink()
    ):
        raise ManifestError(f"installed package directory is invalid: {plugin_root}")
    files: dict[str, Path] = {}
    for current, directories, names in os.walk(plugin_root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            path = current_path / directory
            if path.is_symlink():
                raise ManifestError(f"installed package contains a symlink: {path}")
        for name in names:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise ManifestError(
                    f"installed package contains a non-regular file: {path}"
                )
            relative = canonical_relative_path(
                path.relative_to(plugin_root).parts
            )
            if relative in files:
                raise ManifestError(
                    f"installed paths collide after normalization: {relative}"
                )
            files[relative] = path
    return files


def record_bytes(path: str, size: int, digest: str) -> bytes:
    if (
        canonical_relative_path(tuple(path.split("/"))) != path
        or size < 0
        or SHA256_PATTERN.fullmatch(digest) is None
    ):
        raise ManifestError("canonical package record is malformed")
    return (
        path.encode("utf-8")
        + b"\0"
        + str(size).encode("ascii")
        + b"\0"
        + digest.encode("ascii")
        + b"\n"
    )


def package_digest(files: list[dict[str, Any]]) -> str:
    if not isinstance(files, list) or not files:
        raise ManifestError("canonical package file list is empty")
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size", "sha256"}
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("size"), int)
            or isinstance(item["size"], bool)
            or not isinstance(item.get("sha256"), str)
        ):
            raise ManifestError("canonical package file record is malformed")
    ordered = sorted(files, key=lambda item: item["path"].encode("utf-8"))
    if len({item["path"] for item in ordered}) != len(ordered):
        raise ManifestError("canonical package file paths are not unique")
    framed = b"".join(
        record_bytes(item["path"], item["size"], item["sha256"])
        for item in ordered
    )
    return hashlib.sha256(framed).hexdigest()


def package_evidence(
    repository_root: Path,
    installed_root: Path,
    commit: str,
    plugin: str,
) -> tuple[str, dict[str, Any]]:
    if PLUGIN_NAME_PATTERN.fullmatch(plugin) is None:
        raise ManifestError(f"plugin name is invalid: {plugin}")
    resolved_commit, source = source_files(repository_root, commit, plugin)
    installed = installed_files(installed_root / plugin)
    source_paths = set(source)
    installed_paths = set(installed)
    if source_paths != installed_paths:
        raise ManifestError(
            f"installed file set mismatch for {plugin}: "
            f"missing={sorted(source_paths - installed_paths)!r}, "
            f"extra={sorted(installed_paths - source_paths)!r}"
        )
    files = []
    for path in sorted(source, key=lambda value: value.encode("utf-8")):
        published = bytes(
            run_git(repository_root, "cat-file", "blob", source[path])
        )
        installed_bytes = installed[path].read_bytes()
        if published != installed_bytes:
            raise ManifestError(f"installed bytes differ from Git for {plugin}/{path}")
        digest = hashlib.sha256(installed_bytes).hexdigest()
        files.append(
            {
                "path": path,
                "size": len(installed_bytes),
                "sha256": digest,
            }
        )
    manifest = json.loads(
        (installed_root / plugin / "plugin.json").read_text(encoding="utf-8")
    )
    if manifest.get("name") != plugin or not isinstance(
        manifest.get("version"), str
    ):
        raise ManifestError(f"installed plugin manifest is malformed: {plugin}")
    tree_oid = str(
        run_git(
            repository_root,
            "rev-parse",
            f"{resolved_commit}:plugins/{plugin}",
            text=True,
        )
    ).strip()
    return resolved_commit, {
        "name": plugin,
        "version": manifest["version"],
        "file_count": len(files),
        "byte_count": sum(item["size"] for item in files),
        "package_sha256": package_digest(files),
        "published_git_tree_oid": tree_oid,
        "files": files,
    }


def build_manifest(
    repository_root: Path,
    installed_root: Path,
    commit: str,
    plugins: list[str],
    command_argv: list[str],
) -> dict[str, Any]:
    if not plugins or len(set(plugins)) != len(plugins):
        raise ManifestError("plugins must be a non-empty unique list")
    packages = []
    resolved_commits = set()
    for plugin in sorted(plugins):
        resolved_commit, package = package_evidence(
            repository_root, installed_root, commit, plugin
        )
        resolved_commits.add(resolved_commit)
        packages.append(package)
    if len(resolved_commits) != 1:
        raise ManifestError("packages did not resolve to one source commit")
    return {
        "schema": {
            "id": SCHEMA_ID,
            "version": SCHEMA_VERSION,
        },
        "generator": {
            "name": GENERATOR_NAME,
            "version": GENERATOR_VERSION,
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "command_argv": command_argv,
        },
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "source_commit": resolved_commits.pop(),
        "installed_root": str(installed_root.resolve()),
        "algorithm": ALGORITHM,
        "packages": packages,
    }


def validate_manifest_shape(manifest: Any) -> list[str]:
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "schema",
            "generator",
            "generated_at",
            "source_commit",
            "installed_root",
            "algorithm",
            "packages",
        }
        or manifest.get("schema")
        != {"id": SCHEMA_ID, "version": SCHEMA_VERSION}
        or manifest.get("algorithm") != ALGORITHM
        or not isinstance(manifest.get("generator"), dict)
        or manifest["generator"].get("name") != GENERATOR_NAME
        or manifest["generator"].get("version") != GENERATOR_VERSION
        or not isinstance(manifest["generator"].get("command_argv"), list)
        or not isinstance(manifest.get("source_commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", manifest["source_commit"])
        is None
        or not isinstance(manifest.get("installed_root"), str)
        or not isinstance(manifest.get("generated_at"), str)
        or not isinstance(manifest.get("packages"), list)
        or not manifest["packages"]
    ):
        raise ManifestError("package manifest schema or fields are invalid")
    plugins = []
    for package in manifest["packages"]:
        if (
            not isinstance(package, dict)
            or set(package)
            != {
                "name",
                "version",
                "file_count",
                "byte_count",
                "package_sha256",
                "published_git_tree_oid",
                "files",
            }
            or not isinstance(package.get("name"), str)
            or PLUGIN_NAME_PATTERN.fullmatch(package["name"]) is None
            or not isinstance(package.get("version"), str)
            or not isinstance(package.get("file_count"), int)
            or isinstance(package["file_count"], bool)
            or not isinstance(package.get("byte_count"), int)
            or isinstance(package["byte_count"], bool)
            or not isinstance(package.get("files"), list)
            or package["file_count"] != len(package["files"])
            or package["byte_count"]
            != sum(
                item.get("size", -1)
                for item in package["files"]
                if isinstance(item, dict)
            )
            or SHA256_PATTERN.fullmatch(
                str(package.get("package_sha256", ""))
            )
            is None
            or re.fullmatch(
                r"[0-9a-f]{40}|[0-9a-f]{64}",
                str(package.get("published_git_tree_oid", "")),
            )
            is None
            or [
                item.get("path")
                for item in package["files"]
                if isinstance(item, dict)
            ]
            != sorted(
                (
                    item.get("path")
                    for item in package["files"]
                    if isinstance(item, dict)
                ),
                key=lambda value: str(value).encode("utf-8"),
            )
            or package_digest(package["files"]) != package["package_sha256"]
        ):
            raise ManifestError("package manifest entry is invalid")
        plugins.append(package["name"])
    if plugins != sorted(set(plugins)):
        raise ManifestError("package manifest entries are not unique and ordered")
    return plugins


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise ManifestError(f"refusing to overwrite package manifest: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def command_create(args: argparse.Namespace) -> None:
    repository_root = Path(args.repository_root).resolve()
    installed_root = Path(args.installed_root).resolve()
    output = Path(args.output).resolve()
    command_argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        *sys.argv[1:],
    ]
    manifest = build_manifest(
        repository_root,
        installed_root,
        args.commit,
        args.plugin,
        command_argv,
    )
    write_manifest(output, manifest)
    print(
        json.dumps(
            {
                "result": "created",
                "manifest": str(output),
                "manifest_sha256": hashlib.sha256(
                    output.read_bytes()
                ).hexdigest(),
                "source_commit": manifest["source_commit"],
                "packages": [
                    {
                        "name": package["name"],
                        "version": package["version"],
                        "file_count": package["file_count"],
                        "package_sha256": package["package_sha256"],
                    }
                    for package in manifest["packages"]
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


def command_verify(args: argparse.Namespace) -> None:
    repository_root = Path(args.repository_root).resolve()
    manifest_path = Path(args.manifest).resolve()
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    plugins = validate_manifest_shape(manifest)
    rebuilt = build_manifest(
        repository_root,
        Path(args.installed_root).resolve(),
        manifest["source_commit"],
        plugins,
        manifest["generator"]["command_argv"],
    )
    if rebuilt["packages"] != manifest["packages"]:
        raise ManifestError("installed packages do not match the sealed manifest")
    print(
        json.dumps(
            {
                "result": "verified",
                "manifest": str(manifest_path),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "source_commit": manifest["source_commit"],
                "packages": [
                    {
                        "name": package["name"],
                        "version": package["version"],
                        "file_count": package["file_count"],
                        "package_sha256": package["package_sha256"],
                    }
                    for package in manifest["packages"]
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--repository-root", required=True)
    create.add_argument("--installed-root", required=True)
    create.add_argument("--commit", required=True)
    create.add_argument("--plugin", action="append", required=True)
    create.add_argument("--output", required=True)
    create.set_defaults(function=command_create)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--repository-root", required=True)
    verify.add_argument("--installed-root", required=True)
    verify.add_argument("--manifest", required=True)
    verify.set_defaults(function=command_verify)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.function(args)
        return 0
    except (ManifestError, json.JSONDecodeError, OSError) as error:
        print(
            json.dumps(
                {"result": "error", "error": str(error)},
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
