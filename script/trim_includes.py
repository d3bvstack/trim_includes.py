#!/usr/bin/env python3
"""Detect and trim unneeded includes in src/*.c by compiling without them.

For each C file, the script:
- finds the first contiguous block of `#include` lines
- tries compiling a temporary copy with each include removed
- marks includes as needed when compilation fails without them
- optionally rewrites the file so the include block only keeps needed headers

By default, include directories and CFLAGS are pulled from the top-level
Makefile when possible. You can override both via CLI flags.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Iterable, List, Sequence

INCLUDE_RE = re.compile(r"^\s*#\s*include\s*([<\"])([^>\"]+)[>\"]")


@dataclass
class IncludeLine:
    idx: int
    text: str
    target: str


def parse_make_vars(makefile: pathlib.Path) -> dict[str, str]:
    """Return a naive variable map from the Makefile (single-line assignments)."""
    vars: dict[str, str] = {}
    if not makefile.exists():
        return vars
    for raw in makefile.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, val = line.split("=", 1)
        name = name.strip()
        val = val.split("#", 1)[0].strip()
        vars[name] = val
    # very small $(VAR) expansion using the map built so far
    def expand(value: str) -> str:
        for var in re.findall(r"\$\(([^)]+)\)", value):
            value = value.replace(f"$({var})", vars.get(var, ""))
        return value

    return {k: expand(v) for k, v in vars.items()}


def _normalize_includes(include_tokens: List[str], base_dir: pathlib.Path) -> List[str]:
    """Turn raw include tokens into -I-prefixed, absolute include flags."""
    normalized: List[str] = []
    for tok in include_tokens:
        if not tok:
            continue
        if tok.startswith("-I"):
            path_part = tok[2:]
        else:
            path_part = tok
        path = pathlib.Path(path_part)
        if not path.is_absolute():
            path = (base_dir / path_part).resolve()
        normalized.append(f"-I{path}")
    return normalized


def makefile_flags(makefile: pathlib.Path) -> tuple[List[str], List[str]]:
    vars = parse_make_vars(makefile)
    include_tokens = shlex.split(vars.get("INCLUDES", ""))
    base_dir = makefile.parent.resolve()
    includes = _normalize_includes(include_tokens, base_dir)
    cflags = shlex.split(vars.get("CFLAGS", ""))
    return includes, cflags


def find_include_block(lines: Sequence[str]) -> tuple[int, int, List[IncludeLine]] | None:
    start = None
    for i, line in enumerate(lines):
        if INCLUDE_RE.match(line):
            start = i
            break
    if start is None:
        return None
    end = start
    includes: List[IncludeLine] = []
    while end < len(lines) and (INCLUDE_RE.match(lines[end]) or not lines[end].strip()):
        m = INCLUDE_RE.match(lines[end])
        if m:
            includes.append(IncludeLine(idx=end, text=lines[end], target=m.group(2)))
        end += 1
    return start, end, includes


def compile_check(source: pathlib.Path, compiler: str, includes: Sequence[str], cflags: Sequence[str], verbose: bool) -> bool:
    tmp_obj = tempfile.NamedTemporaryFile(delete=False, suffix=".o")
    tmp_obj.close()
    cmd = [compiler, "-c", str(source), "-o", tmp_obj.name, *cflags, *includes]
    proc = subprocess.run(cmd, capture_output=not verbose, text=True)
    if verbose:
        print(" ".join(cmd))
        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
    os.unlink(tmp_obj.name)
    return proc.returncode == 0


def write_temp(lines: Sequence[str]) -> pathlib.Path:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".c", mode="w")
    tmp.writelines(lines)
    tmp.close()
    return pathlib.Path(tmp.name)


def determine_needed(file_path: pathlib.Path, include_block: List[IncludeLine], lines: Sequence[str], compiler: str, includes: Sequence[str], cflags: Sequence[str], verbose: bool) -> tuple[set[str], bool]:
    """Return (needed_include_texts, baseline_ok)."""
    baseline_path = write_temp(lines)
    baseline_ok = compile_check(baseline_path, compiler, includes, cflags, verbose)
    os.unlink(baseline_path)
    if not baseline_ok:
        return set(), False

    needed: set[str] = set()
    for inc in include_block:
        trimmed = [ln for i, ln in enumerate(lines) if i != inc.idx]
        tmp_path = write_temp(trimmed)
        ok = compile_check(tmp_path, compiler, includes, cflags, verbose)
        os.unlink(tmp_path)
        if not ok:
            needed.add(inc.text)
    return needed, True


def rebuild_file(lines: List[str], start: int, end: int, include_block: List[IncludeLine], keep_texts: set[str]) -> List[str]:
    new_block: List[str] = []
    seen = set()
    for inc in include_block:
        if inc.text in keep_texts and inc.text not in seen:
            seen.add(inc.text)
            new_block.append(inc.text if inc.text.endswith("\n") else inc.text + "\n")
    if new_block and (end >= len(lines) or lines[end].strip()):
        new_block.append("\n")
    return lines[:start] + new_block + lines[end:]


def compile_lines(lines: Sequence[str], compiler: str, includes: Sequence[str], cflags: Sequence[str], verbose: bool) -> bool:
    temp_path = write_temp(lines)
    ok = compile_check(temp_path, compiler, includes, cflags, verbose)
    os.unlink(temp_path)
    return ok


def process_file(path: pathlib.Path, args, includes: Sequence[str], cflags: Sequence[str]) -> bool:
    lines = path.read_text().splitlines(keepends=True)
    block_info = find_include_block(lines)
    if not block_info:
        if args.verbose:
            print(f"[skip] {path}: no include block found")
        return True

    start, end, include_block = block_info
    needed, baseline_ok = determine_needed(path, include_block, lines, args.compiler, includes, cflags, args.verbose)
    if not baseline_ok:
        print(f"[error] {path}: failed to compile baseline; skipping")
        return False

    kept = [inc for inc in include_block if inc.text in needed]
    removed = [inc for inc in include_block if inc.text not in needed]

    keep_set = set(inc.text for inc in kept)

    # Second pass: ensure the reduced include block actually compiles. If it
    # does not, progressively re-add previously removable includes (in order)
    # until compilation succeeds or nothing is left to add.
    if args.fix:
        candidate_lines = rebuild_file(lines, start, end, include_block, keep_set)
        if not compile_lines(candidate_lines, args.compiler, includes, cflags, args.verbose):
            for inc in removed:
                keep_set.add(inc.text)
                candidate_lines = rebuild_file(lines, start, end, include_block, keep_set)
                if compile_lines(candidate_lines, args.compiler, includes, cflags, args.verbose):
                    break
            else:
                print(f"[error] {path}: trimmed includes fail to compile; keeping original block")
                return False

        new_lines = rebuild_file(lines, start, end, include_block, keep_set)
        if new_lines != lines:
            path.write_text("".join(new_lines))
            print(f"[fix] {path}: kept {len(keep_set)}, removed {len(include_block) - len(keep_set)}")
        else:
            if args.verbose:
                print(f"[noop] {path}: no changes needed")
    else:
        print(f"[check] {path}: needed {len(kept)}, removable {len(removed)}")
        if removed:
            for inc in removed:
                print(f"    removable: {inc.text.strip()}")
    return True


def collect_files(src_dir: pathlib.Path, explicit: Iterable[str] | None) -> List[pathlib.Path]:
    if explicit:
        return [pathlib.Path(p) for p in explicit]
    return sorted(src_dir.rglob("*.c"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trim unneeded includes in C sources by compiling without them.")
    parser.add_argument("--src-dir", default="src", type=pathlib.Path, help="Root directory to scan for .c files")
    parser.add_argument("--compiler", default="cc", help="C compiler to use (defaults to cc)")
    parser.add_argument("--makefile", default="Makefile", type=pathlib.Path, help="Makefile to read INCLUDES/CFLAGS from")
    parser.add_argument("--include", action="append", default=None, help="Additional include flag (e.g. -Ifoo). Overrides Makefile if provided.")
    parser.add_argument("--cflag", action="append", default=None, help="Additional cflag (e.g. -DMACRO=1). Overrides Makefile if provided.")
    parser.add_argument("--file", action="append", help="Process only these files (relative or absolute paths)")
    parser.add_argument("--fix", action="store_true", help="Rewrite files to keep only needed includes")
    parser.add_argument("--verbose", action="store_true", help="Show compiler output")
    args = parser.parse_args(argv)

    mf_includes, mf_cflags = makefile_flags(args.makefile)
    includes = args.include if args.include is not None else mf_includes
    cflags = args.cflag if args.cflag is not None else mf_cflags

    files = collect_files(args.src_dir, args.file)
    if not files:
        print(f"No .c files found under {args.src_dir}")
        return 1

    ok = True
    for path in files:
        ok = process_file(path, args, includes, cflags) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
