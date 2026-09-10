#!/usr/bin/env python3
"""
bootstrap.py - Scaffold a C or C++ project (sources, headers, Makefile, git
repo, dependencies, tests and docs) from a JSON configuration file, and add
classes/structs to an already-bootstrapped project.

Usage:
    python3 bootstrap.py PROJECT_NAME
    python3 bootstrap.py PROJECT_NAME --config /path/to/config.json
    python3 bootstrap.py PROJECT_NAME -c /path/to/config.json

    python3 bootstrap.py PROJECT_NAME -c config.json -k CLASS_NAME
    python3 bootstrap.py PROJECT_NAME -c config.json --class CLASS_NAME
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------
# Colored output + progress indicator
# --------------------------------------------------------------------------

class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"


def _use_color(stream) -> bool:
    """Color is opt-out: disabled via NO_COLOR, or automatically when the
    target stream isn't a terminal (e.g. output is redirected to a file)."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def _paint(text: str, color: str, stream) -> str:
    return f"{color}{text}{Color.RESET}" if _use_color(stream) else text


def info(msg: str) -> None:
    print(_paint(msg, Color.CYAN, sys.stdout))


def success(msg: str) -> None:
    print(_paint(msg, Color.GREEN, sys.stdout))


def warn(msg: str) -> None:
    print(_paint(msg, Color.YELLOW, sys.stderr), file=sys.stderr)


def error(msg: str) -> None:
    print(_paint(f"bootstrap.py: error: {msg}", Color.RED, sys.stderr), file=sys.stderr)


class Spinner:
    """Shows a single status line for a slow operation (a git clone, a
    file/directory copy, a dependency build, ...). Writes the message once
    (no animation loop) so it can't get garbled by terminals, pipes, or log
    captures that don't honor in-place carriage-return redraw -- a
    continuously-rewriting spinner showed up as dozens of concatenated
    frames on some setups instead of overwriting in place. On exit, a bare
    "\\r" ends the pending line before the caller prints its result line."""

    def __init__(self, message: str):
        self.message = message

    def __enter__(self) -> "Spinner":
        sys.stdout.write(self.message)
        sys.stdout.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        print("\r")
        return False


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class BootstrapError(Exception):
    """Any fatal, user-facing error. Caught in main() -> stderr + exit(1)."""


# Phony targets defined in the generated Makefile. The project name becomes
# $(NAME), the built executable's filename -- if it matches one of these, the
# executable-file rule and the phony-target rule collide (make keeps only the
# last recipe for a given target name), producing a self-recursive "make run"
# loop instead of a normal build. Reject such names up front.
RESERVED_MAKE_TARGETS = {"all", "archive", "run", "test", "clean", "fclean", "re", "deps"}


def validate_project_name(project: str) -> None:
    if project in RESERVED_MAKE_TARGETS:
        raise BootstrapError(
            f"project name '{project}' collides with a Makefile target "
            f"({', '.join(sorted(RESERVED_MAKE_TARGETS))}); "
            f"choose a different name"
        )


# --------------------------------------------------------------------------
# 1. Argument parsing
# --------------------------------------------------------------------------

class BootstrapArgumentParser(argparse.ArgumentParser):
    """argparse's default usage-error exit status is 2; the spec requires
    every fatal error -- including CLI usage errors like `-k` with no
    value -- to exit 1."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        error(message)
        sys.exit(1)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = BootstrapArgumentParser(
        prog="bootstrap.py",
        description="Bootstrap a C/C++ project from a JSON configuration "
                     "file, or add a class/struct to an existing one.",
    )
    parser.add_argument("project_name", metavar="PROJECT_NAME",
                         help="Name of the project to create or modify.")
    parser.add_argument("-c", "--config", metavar="PATH", default=None,
                         help="Path to the configuration file "
                              "(default: ~/.bootstrap/config.json).")
    parser.add_argument("-k", "--class", dest="class_name", metavar="CLASS_NAME",
                         default=None,
                         help="Add a class/struct named CLASS_NAME to an "
                              "existing bootstrapped project instead of "
                              "creating a new one.")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# 2. Configuration loading / validation
# --------------------------------------------------------------------------

REQUIRED_STRING_FIELDS = [
    "src_dir", "source_ex", "header_ex", "inc_dir",
    "doc_dir", "test_dir", "test_main",
]


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise BootstrapError(f"configuration file not found: {config_path}")
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BootstrapError(f"cannot read configuration file {config_path}: {exc}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"invalid JSON in configuration file {config_path}: {exc}")
    if not isinstance(data, dict):
        raise BootstrapError("configuration file must contain a JSON object")
    return data


def _normalize_dep_entry(name: str, value: Any) -> dict[str, str | None]:
    """Accept either a bare string (legacy) or {"source": ..., "link": ...}."""
    if isinstance(value, str):
        return {"source": value, "link": None}
    if isinstance(value, dict):
        source = value.get("source")
        link = value.get("link")
        if not isinstance(source, str) or not source:
            raise BootstrapError(f"dep.{name}.source must be a non-empty string")
        if link is not None and not isinstance(link, str):
            raise BootstrapError(f"dep.{name}.link must be a string if present")
        return {"source": source, "link": link}
    raise BootstrapError(
        f"dep.{name} must be a string or an object with a 'source' field"
    )


def validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    for field in REQUIRED_STRING_FIELDS:
        if field not in cfg:
            raise BootstrapError(f"configuration is missing required field: {field}")
        if not isinstance(cfg[field], str) or not cfg[field].strip():
            raise BootstrapError(f"configuration field '{field}' must be a non-empty string")

    for ext_field in ("source_ex", "header_ex"):
        if not cfg[ext_field].startswith("."):
            raise BootstrapError(
                f"configuration field '{ext_field}' must start with a dot "
                f"(got {cfg[ext_field]!r})"
            )

    if "git_init" in cfg and not isinstance(cfg["git_init"], bool):
        raise BootstrapError("configuration field 'git_init' must be a boolean")
    cfg.setdefault("git_init", False)

    valid_dep_failure_modes = {"abort", "warn"}
    on_dep_failure = cfg.get("on_dep_failure", "abort")
    if on_dep_failure not in valid_dep_failure_modes:
        raise BootstrapError(
            f"configuration field 'on_dep_failure' must be one of "
            f"{sorted(valid_dep_failure_modes)} (got {on_dep_failure!r})"
        )
    cfg["on_dep_failure"] = on_dep_failure

    # source_ex determines the project language, which in turn picks the
    # compiler (CC for C, CXX for C++) and its default command name.
    language = detect_language(cfg["source_ex"])
    cfg["_language"] = language

    if "cc" in cfg:
        if not isinstance(cfg["cc"], str) or not cfg["cc"].strip():
            raise BootstrapError("configuration field 'cc' must be a non-empty string")
    else:
        cfg["cc"] = default_compiler(language)

    dep = cfg.get("dep", {})
    if dep is None:
        dep = {}
    if not isinstance(dep, dict):
        raise BootstrapError("configuration field 'dep' must be an object")
    normalized = {name: _normalize_dep_entry(name, val) for name, val in dep.items()}
    cfg["dep"] = normalized

    # basic sanity: directory-name fields must not look like paths escaping WD
    for field in ("src_dir", "inc_dir", "doc_dir", "test_dir"):
        if cfg[field] in (".", "..") or cfg[field].startswith("/") or ".." in Path(cfg[field]).parts:
            raise BootstrapError(f"configuration field '{field}' is not a valid relative directory name")

    return cfg


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def c_identifier(name: str) -> str:
    """Turn a name into a valid, upper-case C identifier for include guards."""
    ident = re.sub(r"[^0-9A-Za-z_]", "_", name).upper()
    if not ident or ident[0].isdigit():
        ident = "_" + ident
    return ident


# Source extensions the bootstrapper understands, mapped to the language they
# imply. This drives which compiler (CC/CXX) the generated Makefile uses.
C_SOURCE_EXTENSIONS = {".c"}
CXX_SOURCE_EXTENSIONS = {".cc", ".cpp", ".cxx"}


def detect_language(source_ex: str) -> str:
    """Return 'c' or 'cpp' for a configured source_ex, per the extension
    table in the spec (.c -> C; .cc/.cpp/.cxx -> C++)."""
    if source_ex in C_SOURCE_EXTENSIONS:
        return "c"
    if source_ex in CXX_SOURCE_EXTENSIONS:
        return "cpp"
    raise BootstrapError(
        f"unsupported 'source_ex' value {source_ex!r}; expected one of "
        f"{', '.join(sorted(C_SOURCE_EXTENSIONS | CXX_SOURCE_EXTENSIONS))}"
    )


def default_compiler(language: str) -> str:
    return "cc" if language == "c" else "c++"


def is_url(source: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", source)) or source.startswith("git@")


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, shell=False,
        )
    except FileNotFoundError as exc:
        raise BootstrapError(f"command not found: {cmd[0]} ({exc})")


# --------------------------------------------------------------------------
# 3. Project directory creation
# --------------------------------------------------------------------------

def create_project_skeleton(wd: Path, cfg: dict[str, Any]) -> None:
    if wd.exists():
        raise BootstrapError(f"target directory already exists: {wd}")
    try:
        wd.mkdir(parents=True)
        for sub in (cfg["src_dir"], cfg["inc_dir"], cfg["doc_dir"],
                    cfg["test_dir"], "lib", "obj"):
            (wd / sub).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BootstrapError(f"failed to create project directories: {exc}")


# --------------------------------------------------------------------------
# 4-7. Source / header generation (initial project scaffold)
# --------------------------------------------------------------------------

def generate_header(wd: Path, cfg: dict[str, Any], project: str,
                     dep_headers: list[Path] | None = None) -> None:
    guard = f"{c_identifier(project)}_H"
    header_path = wd / cfg["inc_dir"] / f"{project}{cfg['header_ex']}"

    # Include every dependency header (deduplicated by filename) so the
    # project header pulls in whatever the dependencies expose. Order is
    # deterministic (sorted by path) for reproducible output.
    seen_names: set[str] = set()
    include_lines = []
    for header in sorted(dep_headers or [], key=lambda p: p.as_posix()):
        if header.name in seen_names:
            continue
        seen_names.add(header.name)
        include_lines.append(f'#include "{header.name}"')
    includes_block = "\n".join(include_lines) + "\n\n" if include_lines else ""

    content = (
        f"#ifndef {guard}\n"
        f"#define {guard}\n\n"
        f"{includes_block}"
        f"void\tdebug(void);\n\n"
        f"#endif\n"
    )
    header_path.write_text(content, encoding="utf-8")


def generate_source(wd: Path, cfg: dict[str, Any], project: str) -> None:
    src_path = wd / cfg["src_dir"] / f"{project}{cfg['source_ex']}"
    content = (
        f'#include "{project}{cfg["header_ex"]}"\n'
        f"#include <stdio.h>\n\n"
        f"void\tdebug(void)\n"
        f"{{\n"
        f'\tprintf("Bye bye world\\n");\n'
        f"}}\n"
    )
    src_path.write_text(content, encoding="utf-8")


def generate_main(wd: Path, cfg: dict[str, Any], project: str) -> None:
    main_path = wd / cfg["src_dir"] / f"main{cfg['source_ex']}"
    content = (
        f'#include "{project}{cfg["header_ex"]}"\n\n'
        f"int\tmain(void)\n"
        f"{{\n"
        f"\tdebug();\n"
        f"\treturn (0);\n"
        f"}}\n"
    )
    main_path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------
# 9-11. Dependencies
# --------------------------------------------------------------------------

def strip_git_dirs(root: Path) -> None:
    for git_dir in list(root.rglob(".git")):
        if git_dir.is_dir():
            shutil.rmtree(git_dir, ignore_errors=True)
        elif git_dir.is_file():
            git_dir.unlink()


def _combined_output(result: subprocess.CompletedProcess) -> str:
    """Full stdout+stderr from a subprocess result, for error messages.
    Build/config scripts often print the actually useful diagnostic (e.g.
    a missing-header or missing-package message) to stdout, so showing only
    stderr can hide the real cause of a failure."""
    parts = [p.strip() for p in (result.stdout, result.stderr) if p and p.strip()]
    return "\n".join(parts) if parts else "(no output captured)"


def install_one_dependency(name: str, source: str, lib_dir: Path) -> Path:
    """Clone/copy a single dependency into lib_dir. Return its on-disk path."""
    if is_url(source):
        dest = lib_dir / name
        with Spinner(f"Cloning dependency '{name}' from {source} ..."):
            result = run(["git", "clone", source, str(dest)])
        if result.returncode != 0:
            raise BootstrapError(
                f"failed to clone dependency '{name}' from {source}:\n{_combined_output(result)}"
            )
        strip_git_dirs(dest)
        success(f"Cloned dependency '{name}'")
        return dest

    src_path = Path(source).expanduser()
    if not src_path.exists():
        raise BootstrapError(f"dependency '{name}': local path does not exist: {source}")

    if src_path.is_file():
        dest = lib_dir / src_path.name
        with Spinner(f"Copying dependency '{name}' from {source} ..."):
            try:
                shutil.copy2(src_path, dest)
            except OSError as exc:
                raise BootstrapError(f"failed to copy dependency '{name}': {exc}")
        success(f"Copied dependency '{name}'")
        return dest

    dest = lib_dir / name
    with Spinner(f"Copying dependency '{name}' from {source} ..."):
        try:
            shutil.copytree(src_path, dest)
        except (OSError, shutil.Error) as exc:
            raise BootstrapError(f"failed to copy dependency '{name}': {exc}")
    strip_git_dirs(dest)
    success(f"Copied dependency '{name}'")
    return dest


def find_dependency_headers(lib_dir: Path, header_ex: str) -> list[Path]:
    """Recursively locate every dependency header matching the configured
    header extension. Returns an empty list if lib_dir doesn't exist yet."""
    if not lib_dir.is_dir():
        return []
    return sorted(lib_dir.rglob(f"*{header_ex}"))


def include_dirs_from_headers(headers: list[Path], wd: Path) -> list[str]:
    dirs = {header.parent.relative_to(wd).as_posix() for header in headers}
    return sorted(dirs)


def has_makefile(path: Path) -> Path | None:
    if not path.is_dir():
        return None
    for candidate in ("Makefile", "makefile", "GNUmakefile"):
        mf = path / candidate
        if mf.is_file():
            return mf
    return None


def build_dependency(name: str, dep_path: Path, strict: bool) -> tuple[bool, bool]:
    """Build a dependency if it looks like a Makefile project.

    Returns (has_makefile, succeeded):
      - has_makefile: whether dep_path contains a Makefile at all. This is
        what generate_makefile() uses to decide whether the generated
        project's `deps`/`clean`/`fclean` targets should recurse into it --
        that's still correct even if the build failed just now, since a
        later `make` in the finished project can retry once the underlying
        problem (e.g. a missing system package) is fixed.
      - succeeded: whether `make` actually completed successfully just now.

    If strict is True, a failed build raises BootstrapError as before. If
    strict is False, the failure is printed as a warning (with full
    stdout+stderr so the real cause is visible) and bootstrapping continues.
    """
    if has_makefile(dep_path) is None:
        return False, False
    with Spinner(f"Building dependency '{name}' ..."):
        result = run(["make", "-C", str(dep_path)])
    if result.returncode != 0:
        message = f"failed to build dependency '{name}':\n{_combined_output(result)}"
        if strict:
            raise BootstrapError(message)
        warn(message)
        warn(f"continuing without a successful build of '{name}' (on_dep_failure=warn)")
        return True, False
    success(f"Built dependency '{name}'")
    return True, True


def default_link_flags(dep_path: Path, wd: Path, name: str) -> str:
    """Inspect a built dependency and derive linker flags without requiring
    an explicit `link` in the config. Preference order:

    1. Conventional `lib<name>.a` archives -> `-L<dir> -l<name>`. Portable,
       works regardless of where the executable is invoked from.
    2. Any other `*.a` archive -> linked by its direct relative path, since
       a non-conventional archive name (anything other than `lib*.a`) can
       never be resolved via `-l` no matter what `-L` points at.
    3. A file at the dependency's top level sharing the dependency's own
       name (school-style Makefiles commonly name their output `NAME`
       rather than `libNAME.a`, e.g. libft's Makefile emitting a bare
       `lib/libft/libft`) -> also linked by direct relative path.
    4. Otherwise: no link flags (treated as header-only).
    """
    if dep_path.is_file():
        candidates = [dep_path]
    elif dep_path.is_dir():
        candidates = [p for p in dep_path.rglob("*") if p.is_file()]
    else:
        candidates = []

    conventional = sorted(
        p for p in candidates if p.name.startswith("lib") and p.suffix == ".a"
    )
    if conventional:
        flags, seen = [], set()
        for archive in conventional:
            libname = archive.stem[3:]  # strip "lib" prefix; stem already drops ".a"
            if not libname:
                continue
            rel_dir = archive.parent.relative_to(wd).as_posix()
            key = (rel_dir, libname)
            if key not in seen:
                seen.add(key)
                flags.append(f"-L{rel_dir} -l{libname}")
        return " ".join(flags)

    other_archives = sorted(p for p in candidates if p.suffix == ".a")
    if other_archives:
        return " ".join(p.relative_to(wd).as_posix() for p in other_archives)

    if dep_path.is_file():
        return dep_path.relative_to(wd).as_posix()
    if dep_path.is_dir():
        named = dep_path / name
        if named.is_file():
            return named.relative_to(wd).as_posix()

    return ""


def install_dependencies(wd: Path, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    lib_dir = wd / "lib"
    deps_info: list[dict[str, Any]] = []
    strict = cfg.get("on_dep_failure", "abort") == "abort"
    for name, spec in cfg["dep"].items():
        try:
            dep_path = install_one_dependency(name, spec["source"], lib_dir)
        except BootstrapError as exc:
            if strict:
                raise
            warn(str(exc))
            warn(f"continuing without dependency '{name}' (on_dep_failure=warn)")
            deps_info.append({
                "name": name,
                "path": None,
                "buildable": False,
                "link": spec["link"] or "",
                "failed": True,
            })
            continue

        has_makefile_, built = build_dependency(name, dep_path, strict)
        link = spec["link"]
        if link is None:
            link = default_link_flags(dep_path, wd, name)
        deps_info.append({
            "name": name,
            "path": dep_path,
            "buildable": has_makefile_,
            "link": link,
            "failed": has_makefile_ and not built,
        })
    return deps_info


# --------------------------------------------------------------------------
# 8, 10, 11. Makefile generation (initial project scaffold)
# --------------------------------------------------------------------------

def generate_makefile(wd: Path, cfg: dict[str, Any], project: str,
                       deps_info: list[dict[str, Any]],
                       dep_headers: list[Path]) -> None:
    src_dir = cfg["src_dir"]
    inc_dir = cfg["inc_dir"]
    source_ex = cfg["source_ex"]

    # C projects build with CC/CFLAGS; C++ projects build with CXX/CXXFLAGS.
    language = cfg.get("_language") or detect_language(source_ex)
    compiler_var = "CC" if language == "c" else "CXX"
    flags_var = "CFLAGS" if language == "c" else "CXXFLAGS"

    include_dirs = [inc_dir] + include_dirs_from_headers(dep_headers, wd)
    includes = " ".join(f"-I{d}" for d in include_dirs)

    dep_build_lines = []
    dep_clean_lines = []
    dep_fclean_lines = []
    dep_link_flags = []
    for dep in deps_info:
        if dep["path"] is None:
            # Install itself failed (only reachable with on_dep_failure=warn);
            # nothing on disk to recurse into, but still honor an explicit
            # link so the Makefile is ready once the user fixes it manually.
            if dep["link"]:
                dep_link_flags.append(dep["link"])
            continue
        rel_path = dep["path"].relative_to(wd).as_posix()
        if dep["buildable"]:
            dep_build_lines.append(f"\t@$(MAKE) -C {rel_path}")
            dep_clean_lines.append(f"\t@$(MAKE) -C {rel_path} clean")
            dep_fclean_lines.append(f"\t@$(MAKE) -C {rel_path} fclean")
        if dep["link"]:
            dep_link_flags.append(dep["link"])

    deps_target = "deps"
    build_deps_block = "\n".join(dep_build_lines) if dep_build_lines else "\t@:"
    clean_deps_block = "\n".join(dep_clean_lines) if dep_clean_lines else "\t@:"
    fclean_deps_block = "\n".join(dep_fclean_lines) if dep_fclean_lines else "\t@:"
    ldflags = " ".join(dep_link_flags)

    makefile = f"""\
NAME\t\t= {project}
{compiler_var}\t\t\t= {cfg["cc"]}
{flags_var}\t\t= -Wall -Wextra -Werror {includes}
LDFLAGS\t\t= {ldflags}

SRC_DIR\t\t= {src_dir}
INC_DIR\t\t= {inc_dir}
OBJ_DIR\t\t= obj
LIB_DIR\t\t= lib

BIN_SRC\t\t= $(SRC_DIR)/main{source_ex}
BIN_OBJ\t\t= $(OBJ_DIR)/main.o

LIB_SRC\t\t= $(filter-out $(BIN_SRC), $(wildcard $(SRC_DIR)/*{source_ex}))
LIB_OBJ\t\t= $(patsubst $(SRC_DIR)/%{source_ex},$(OBJ_DIR)/%.o,$(LIB_SRC))

NAME_A\t\t= {project}.a

.PHONY: all archive run test clean fclean re {deps_target}

all: $(NAME)

{deps_target}:
{build_deps_block}

$(OBJ_DIR)/%.o: $(SRC_DIR)/%{source_ex}
\t@mkdir -p $(OBJ_DIR)
\t$({compiler_var}) $({flags_var}) -c $< -o $@

archive: $(LIB_OBJ)
\t@mkdir -p $(OBJ_DIR)
\tar rcs $(NAME_A) $(LIB_OBJ)

$(NAME): {deps_target} archive $(BIN_OBJ)
\t$({compiler_var}) $({flags_var}) $(BIN_OBJ) $(NAME_A) $(LDFLAGS) -o $(NAME)

run: all
\t./$(NAME)

test: all
\t@./{cfg["test_dir"]}/{cfg["test_main"]}

clean:
\trm -rf $(OBJ_DIR)
{clean_deps_block}

fclean: clean
\trm -f $(NAME_A) $(NAME)
{fclean_deps_block}

re: fclean all
"""
    (wd / "Makefile").write_text(makefile, encoding="utf-8")


# --------------------------------------------------------------------------
# 12. Git initialization
# --------------------------------------------------------------------------

def init_git(wd: Path, cfg: dict[str, Any]) -> None:
    if not cfg.get("git_init"):
        return
    info("Initializing git repository ...")
    result = run(["git", "init"], cwd=wd)
    if result.returncode != 0:
        raise BootstrapError(f"failed to run 'git init':\n{result.stderr.strip()}")
    (wd / ".gitignore").write_text("obj/\n", encoding="utf-8")

    result = run(["git", "add", "."], cwd=wd)
    if result.returncode != 0:
        raise BootstrapError(f"failed to run 'git add .':\n{result.stderr.strip()}")

    commit_msg = "starting file added."
    result = run(["git", "commit", "-m", commit_msg], cwd=wd)
    if result.returncode != 0 and re.search(r"user\.(name|email)", result.stderr or ""):
        # No git identity configured (common on fresh machines/CI containers)
        # -- fall back to a local, commit-only identity instead of failing.
        result = run(
            [
                "git", "-c", "user.name=bootstrap.py", "-c", "user.email=bootstrap@localhost",
                "commit", "-m", commit_msg,
            ],
            cwd=wd,
        )
    if result.returncode != 0:
        raise BootstrapError(f"failed to run 'git commit':\n{result.stderr.strip()}")
    success("Initialized git repository and created initial commit")


# --------------------------------------------------------------------------
# 13. Test script
# --------------------------------------------------------------------------

def generate_test_script(wd: Path, cfg: dict[str, Any]) -> None:
    test_path = wd / cfg["test_dir"] / cfg["test_main"]
    script = """\
#!/bin/sh
set -e

if make run; then
\tstatus=0
else
\tstatus=$?
fi

if [ "$status" -eq 0 ]; then
\techo "test: OK"
else
\techo "test: FAILED (exit status $status)" >&2
fi

exit "$status"
"""
    test_path.write_text(script, encoding="utf-8")
    test_path.chmod(0o755)


# --------------------------------------------------------------------------
# 14. Layout documentation
# --------------------------------------------------------------------------

def generate_layout_doc(wd: Path, cfg: dict[str, Any], project: str,
                         deps_info: list[dict[str, Any]]) -> None:
    lines = [f"{project}/"]
    lines.append("├── Makefile")
    lines.append(f"├── {project}.a")
    lines.append(f"├── {cfg['src_dir']}/")
    lines.append(f"│   ├── {project}{cfg['source_ex']}")
    lines.append(f"│   └── main{cfg['source_ex']}")
    lines.append(f"├── {cfg['inc_dir']}/")
    lines.append(f"│   └── {project}{cfg['header_ex']}")
    lines.append("├── obj/")
    if deps_info:
        lines.append("├── lib/")
        for i, dep in enumerate(deps_info):
            connector = "└──" if i == len(deps_info) - 1 else "├──"
            label = f"{dep['path'].name}/" if dep["path"] is not None else f"{dep['name']} (not installed)"
            lines.append(f"│   {connector} {label}")
    else:
        lines.append("├── lib/")
    lines.append(f"├── {cfg['doc_dir']}/")
    lines.append("│   └── layout.md")
    lines.append(f"└── {cfg['test_dir']}/")
    lines.append(f"    └── {cfg['test_main']}")

    content = "# Project layout\n\n```text\n" + "\n".join(lines) + "\n```\n"
    (wd / cfg["doc_dir"] / "layout.md").write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------
# Class/struct generation mode (-k / --class)
# --------------------------------------------------------------------------

def validate_class_name(name: str) -> None:
    """CLASS_NAME must be safe as both a filename and a C/C++ identifier."""
    if not name:
        raise BootstrapError("class name must not be empty")
    if "/" in name or "\\" in name:
        raise BootstrapError(f"class name must not contain path separators: {name!r}")
    if ".." in name:
        raise BootstrapError(f"class name must not contain '..': {name!r}")
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise BootstrapError(
            f"class name {name!r} is not a valid identifier "
            f"(use letters, digits and underscores; it cannot start with a digit)"
        )


def build_class_header(class_name: str, cfg: dict[str, Any], body: str) -> str:
    guard_suffix = cfg["header_ex"].lstrip(".").upper()
    guard = f"{c_identifier(class_name)}_{guard_suffix}"
    return f"#ifndef {guard}\n#define {guard}\n\n{body}\n#endif\n"


def build_class_source(class_name: str, cfg: dict[str, Any],
                        extra_includes: list[str], body: str) -> str:
    header_name = f"{class_name}{cfg['header_ex']}"
    includes = "".join(f"#include {inc}\n" for inc in extra_includes)
    return f'#include "{header_name}"\n{includes}\n{body}'


def generate_class_c(class_name: str) -> tuple[str, str]:
    """C mode: a struct + create/erase/copy/get functions, per spec section 4.
    `copy` takes only `other` -- `self` isn't needed to allocate and copy
    `other`'s contents into a fresh structure."""
    lname = class_name.lower()
    struct_tag = f"s_{lname}"
    type_name = f"t_{lname}"
    prefix = f"{lname}_"

    header = (
        f"typedef struct {struct_tag}\n"
        f"{{\n"
        f"\t/* fields may be added later */\n"
        f"}}\t{type_name};\n\n"
        f"{type_name}\t*{prefix}create(void);\n"
        f"void\t{prefix}erase({type_name} *self);\n"
        f"{type_name}\t*{prefix}copy({type_name} *other);\n"
        f"int\t{prefix}get({type_name} *self, int param);\n"
    )
    source = (
        f"{type_name}\t*{prefix}create(void)\n"
        f"{{\n"
        f"\t{type_name}\t*self;\n\n"
        f"\tself = malloc(sizeof({type_name}));\n"
        f"\tif (!self)\n"
        f"\t\treturn (NULL);\n"
        f"\tbzero(self, sizeof({type_name}));\n"
        f"\treturn (self);\n"
        f"}}\n\n"
        f"void\t{prefix}erase({type_name} *self)\n"
        f"{{\n"
        f"\tif (!self)\n"
        f"\t\treturn ;\n"
        f"\tfree(self);\n"
        f"}}\n\n"
        f"{type_name}\t*{prefix}copy({type_name} *other)\n"
        f"{{\n"
        f"\t{type_name}\t*self;\n\n"
        f"\tself = malloc(sizeof({type_name}));\n"
        f"\tif (!self)\n"
        f"\t\treturn (NULL);\n"
        f"\t*self = *other;\n"
        f"\treturn (self);\n"
        f"}}\n\n"
        f"int\t{prefix}get({type_name} *self, int param)\n"
        f"{{\n"
        f"\t(void)self;\n"
        f"\t(void)param;\n"
        f"\treturn (-1);\n"
        f"}}\n"
    )
    return header, source


def generate_class_cpp(class_name: str) -> tuple[str, str]:
    """C++ mode: a class with the canonical form (ctor, copy ctor, dtor,
    operator=, operator[], operator()), per spec section 5. A single `_value`
    member is introduced so operator[] can return a real reference instead
    of one to a temporary."""
    header = (
        f"class {class_name}\n"
        f"{{\n"
        f"\tpublic:\n"
        f"\t\t{class_name}();\n"
        f"\t\t{class_name}(const {class_name} &other);\n"
        f"\t\t~{class_name}();\n\n"
        f"\t\t{class_name}\t&operator=(const {class_name} &other);\n"
        f"\t\tint\t\t&operator[](int index);\n"
        f"\t\tint\t\toperator()(int param) const;\n\n"
        f"\tprivate:\n"
        f"\t\tint\t_value;\n"
        f"}};\n"
    )
    source = (
        f"{class_name}::{class_name}() : _value(0)\n"
        f"{{\n}}\n\n"
        f"{class_name}::{class_name}(const {class_name} &other) : _value(other._value)\n"
        f"{{\n}}\n\n"
        f"{class_name}::~{class_name}()\n"
        f"{{\n}}\n\n"
        f"{class_name}\t&{class_name}::operator=(const {class_name} &other)\n"
        f"{{\n"
        f"\tif (this != &other)\n"
        f"\t\t_value = other._value;\n"
        f"\treturn (*this);\n"
        f"}}\n\n"
        f"int\t&{class_name}::operator[](int index)\n"
        f"{{\n"
        f"\t(void)index;\n"
        f"\treturn (_value);\n"
        f"}}\n\n"
        f"int\t{class_name}::operator()(int param) const\n"
        f"{{\n"
        f"\t(void)param;\n"
        f"\treturn (-1);\n"
        f"}}\n"
    )
    return header, source


def compute_makefile_update(makefile_text: str, source_rel: str) -> str | None:
    """Return updated Makefile text with source_rel added to the LIB_SRC
    source list, or None if no textual change is needed:
      - LIB_SRC is generated with $(wildcard ...) (the default this script
        produces) -- any file already placed in SRC_DIR is picked up on the
        next `make` automatically, nothing to edit.
      - source_rel is already listed (e.g. the class was added before) --
        avoid a duplicate entry.
    Raises BootstrapError if no LIB_SRC assignment can be found to update at
    all, since guessing at an unfamiliar Makefile's structure isn't safe.
    """
    lines = makefile_text.splitlines()

    block_start = None
    block_end = None
    for i, line in enumerate(lines):
        if block_start is None:
            if re.match(r"^\s*LIB_SRC\s*[:+]?=", line):
                block_start = i
                if not line.rstrip().endswith("\\"):
                    block_end = i
                    break
                continue
        else:
            if not line.rstrip().endswith("\\"):
                block_end = i
                break
    if block_start is None:
        raise BootstrapError("cannot update Makefile: no LIB_SRC assignment found")
    if block_end is None:
        block_end = len(lines) - 1  # unterminated trailing continuation

    block_text = "\n".join(lines[block_start:block_end + 1])

    if "$(wildcard" in block_text:
        return None

    if re.search(rf"(?<![\w./]){re.escape(source_rel)}(?![\w./])", block_text):
        return None

    if block_end > block_start:
        indent_match = re.match(r"^(\s*)", lines[block_start + 1])
        indent = indent_match.group(1) if indent_match else "\t"
        last_line = lines[block_end]
        if not last_line.rstrip().endswith("\\"):
            lines[block_end] = last_line + " \\"
        lines.insert(block_end + 1, f"{indent}{source_rel}")
    else:
        lines[block_start] = lines[block_start] + f" {source_rel}"

    new_text = "\n".join(lines)
    if makefile_text.endswith("\n"):
        new_text += "\n"
    return new_text


def add_class(project: str, config_path: Path, class_name: str) -> None:
    """Add a class/struct to an already-bootstrapped project. Validates
    everything (name, target files, Makefile update feasibility) before
    writing anything, per the spec's atomicity requirement, and rolls back
    files it already wrote if a later step in the same operation fails."""
    wd = Path.cwd() / project
    if not wd.is_dir():
        raise BootstrapError(f"project directory does not exist: {wd}")

    validate_class_name(class_name)
    cfg = validate_config(load_config(config_path))
    language = cfg["_language"]

    src_path = wd / cfg["src_dir"] / f"{class_name}{cfg['source_ex']}"
    header_path = wd / cfg["inc_dir"] / f"{class_name}{cfg['header_ex']}"

    existing = [str(p) for p in (header_path, src_path) if p.exists()]
    if existing:
        raise BootstrapError(f"refusing to overwrite existing file(s): {', '.join(existing)}")

    if language == "c":
        header_body, source_body = generate_class_c(class_name)
        extra_includes = ["<stdlib.h>", "<strings.h>"]
    else:
        header_body, source_body = generate_class_cpp(class_name)
        extra_includes = []

    header_text = build_class_header(class_name, cfg, header_body)
    source_text = build_class_source(class_name, cfg, extra_includes, source_body)

    makefile_path = wd / "Makefile"
    if not makefile_path.is_file():
        raise BootstrapError(f"Makefile not found in project: {makefile_path}")
    try:
        makefile_text = makefile_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BootstrapError(f"cannot read Makefile: {exc}")

    source_rel = f"{cfg['src_dir']}/{class_name}{cfg['source_ex']}"
    # Raises BootstrapError here if the Makefile can't be safely updated --
    # before anything has been written to disk.
    new_makefile_text = compute_makefile_update(makefile_text, source_rel)

    # Validation complete -- perform the writes, rolling back on failure.
    try:
        (wd / cfg["src_dir"]).mkdir(parents=True, exist_ok=True)
        (wd / cfg["inc_dir"]).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BootstrapError(f"failed to create directories: {exc}")

    try:
        header_path.write_text(header_text, encoding="utf-8")
    except OSError as exc:
        raise BootstrapError(f"failed to write header file: {exc}")

    try:
        src_path.write_text(source_text, encoding="utf-8")
    except OSError as exc:
        header_path.unlink(missing_ok=True)
        raise BootstrapError(f"failed to write source file: {exc}")

    if new_makefile_text is not None:
        try:
            makefile_path.write_text(new_makefile_text, encoding="utf-8")
        except OSError as exc:
            src_path.unlink(missing_ok=True)
            header_path.unlink(missing_ok=True)
            raise BootstrapError(f"failed to update Makefile: {exc}")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def bootstrap(project: str, config_path: Path) -> list[str]:
    """Scaffold the project. Returns the names of dependencies that failed
    to install or build (only non-empty when on_dep_failure is 'warn';
    otherwise such a failure raises BootstrapError instead)."""
    validate_project_name(project)
    cfg = validate_config(load_config(config_path))

    cwd = Path.cwd()
    wd = cwd / project

    info(f"Creating project directory layout in {wd} ...")
    create_project_skeleton(wd, cfg)

    try:
        if cfg["dep"]:
            info("Installing dependencies ...")
        deps_info = install_dependencies(wd, cfg)
        dep_headers = find_dependency_headers(wd / "lib", cfg["header_ex"])

        info("Generating sources and headers ...")
        generate_header(wd, cfg, project, dep_headers)
        generate_source(wd, cfg, project)
        generate_main(wd, cfg, project)

        info("Generating Makefile ...")
        generate_makefile(wd, cfg, project, deps_info, dep_headers)
        generate_test_script(wd, cfg)
        generate_layout_doc(wd, cfg, project, deps_info)
        init_git(wd, cfg)
    except BootstrapError:
        # Leave partial state for inspection rather than silently deleting it;
        # WD already didn't exist before this run, so nothing pre-existing is harmed.
        raise

    return [dep["name"] for dep in deps_info if dep.get("failed")]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    config_path = Path(args.config).expanduser() if args.config else Path("~/.bootstrap/config.json").expanduser()

    try:
        if args.class_name is not None:
            add_class(args.project_name, config_path, args.class_name)
            success(f"Added '{args.class_name}' to project '{args.project_name}'.")
            return 0
        failed_deps = bootstrap(args.project_name, config_path)
    except BootstrapError as exc:
        error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - final safety net, never fail silently
        error(f"unexpected error: {exc}")
        return 1

    if failed_deps:
        warn(
            f"Project '{args.project_name}' was created, but these dependencies "
            f"did not build cleanly: {', '.join(failed_deps)}. Fix the underlying "
            f"issue and re-run 'make' inside the project (or 'make -C lib/<name>' "
            f"for just that dependency)."
        )
    else:
        success(f"Project '{args.project_name}' created successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
