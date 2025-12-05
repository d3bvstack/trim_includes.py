<img alt="cover-trim_include" src="https://github.com/user-attachments/assets/cad3d3cd-0223-4d9a-8f55-cc26e0c6f777" />

# trim_includes.py

This README explains how to run `script/trim_includes.py`, what it does, and how to prepare any C project so the script can accurately detect and trim unused `#include` directives.

## What the script does
- Scans C files (default: everything under `src/`).
- Finds the first contiguous block of `#include` lines at the top of each file.
- Builds a temporary copy, removing one include at a time and compiling; if compilation fails without that header, the include is marked as needed.
- Optionally rewrites the file so only the needed includes remain.
- Safety pass: after trimming, it recompiles; if the reduced set fails, it re-adds removed headers (in original order) until compilation succeeds.

## Quick start (in this repo)
- Check only: `python3 script/trim_includes.py`
- Check verbose: `python3 script/trim_includes.py --verbose`
- Apply fixes: `python3 script/trim_includes.py --fix`
- Single file: `python3 script/trim_includes.py --file src/assemble/config_color.c --fix`

Defaults are derived from the top-level `Makefile` (`INCLUDES`, `CFLAGS`).

## CLI options
- `--src-dir DIR` : Root to search for `*.c` (default: `src`).
- `--compiler CC` : Compiler to use (default: `cc`).
- `--makefile PATH` : Makefile to read `INCLUDES`/`CFLAGS` from (default: `Makefile`).
- `--include FLAG` : Extra `-I…` flag; overrides Makefile includes when provided (repeatable).
- `--cflag FLAG` : Extra compiler flag; overrides Makefile CFLAGS when provided (repeatable).
- `--file PATH` : Limit to one or more files (repeatable). Paths can be relative or absolute.
- `--fix` : Rewrite include blocks to keep only needed headers.
- `--verbose` : Show compiler commands/stdout/stderr for each probe compile.

Exit code is non-zero if any file fails the baseline compile or if a fix attempt cannot produce a compilable result.

## Preparing any project to use the script
1) Ensure compilable units
- Each file should compile standalone with the project’s normal include paths and CFLAGS. If a file requires special flags that differ from the project defaults, pass them via `--include`/`--cflag` or a custom `--makefile`.

2) Expose include paths and flags
- Add `INCLUDES` (e.g., `-Iinclude -Isrc -Ithird_party`) and `CFLAGS` in your Makefile as single-line assignments so the script can parse them. Avoid multi-line continuations.
- If your build uses macros that are mandatory for headers to parse, add them to `CFLAGS` (or pass via `--cflag -DMACRO`).

3) Keep include blocks contiguous
- Place the file’s `#include` directives in one block at the top, optionally separated by blank lines. The script only inspects the first contiguous include/blank region.

4) Keep headers idempotent
- Headers should be guarded (`#ifndef/#define/#endif`). Non-idempotent headers can cause spurious compilation failures when probed.

5) Handle generated headers
- If some headers are generated, ensure they exist before running the script (run your codegen first or point `--include` to the generated output directory).

6) Optional: restrict scope
- For large trees, start with a subset: `--file path/to/foo.c --file path/to/bar.c`.

## Interpreting results
- `[check] file.c: needed N, removable M` : Dry-run summary. Removable items are listed.
- `[fix] file.c: kept N, removed M` : File rewritten with the trimmed include block.
- `[error] file.c: failed to compile baseline; skipping` : The original file did not compile with the provided flags; fix your flags or the code, then rerun.
- `[error] file.c: trimmed includes fail to compile; keeping original block` : The safety pass could not find a compilable reduced set; the file is left untouched.

## Tips for reliable results
- Run after a clean build setup so generated headers and submodules are present.
- Use `--verbose` when investigating why a header is considered needed.
- If your project mixes C and C++, do not point `--src-dir` at C++ files; the script assumes C compilation.
- For non-standard file layouts, specify both `--src-dir` and `--makefile` explicitly.

## Example for another project
Assume a project with sources under `source/`, headers in `include/`, and a Makefile with `INCLUDES`/`CFLAGS`:
```
python3 script/trim_includes.py \
  --src-dir source \
  --makefile Makefile \
  --fix
```
If your project needs extra flags for a specific platform:
```
python3 script/trim_includes.py \
  --src-dir source \
  --makefile Makefile \
  --cflag -DPLATFORM_LINUX \
  --include -Ithird_party/special \
  --fix
```

## Troubleshooting
- Baseline compile fails: ensure the file builds normally with the same compiler/flags; pass required `-I`/`-D` flags or fix the code.
- Wrong includes removed: rerun with `--verbose` to see failing probes; if needed, temporarily pin a header by adding a dependency that requires it.
- Multi-line Makefile vars: the parser is simple and only handles single-line assignments; flatten your `INCLUDES`/`CFLAGS` or override via CLI.
