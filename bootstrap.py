#!/usr/bin/env python3
"""
bootstrap.py - Scaffold a C or C++ project (sources, headers, Makefile, git
repo, dependencies, tests and docs) from a JSON configuration file.

Usage:
    python3 bootstrap.py PROJECT_NAME
    python3 bootstrap.py PROJECT_NAME --config /path/to/config.json
    python3 bootstrap.py PROJECT_NAME -c /path/to/config.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------
# Colored output + progress spinner
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
    """Shows `message` with a spinning indicator while a slow operation (a
    git clone, a file/directory copy, a dependency build, ...) runs on the
    main thread. Falls back to printing the message once, with no animation,
    when stdout isn't a terminal -- so redirected/logged output stays clean
    and isn't spammed with carriage returns."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message: str):
        self.message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    def _spin(self) -> None:
        for frame in itertools.cycle(self.FRAMES):
            if self._stop.is_set():
                break
            dot = _paint(frame, Color.CYAN, sys.stdout)
            sys.stdout.write(f"\r{dot} {self.message}")
            sys.stdout.flush()
            time.sleep(0.08)

    def __enter__(self) -> "Spinner":
        if self._is_tty:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            print(self.message)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            sys.stdout.write("\r" + " " * (len(self.message) + 4) + "\r")
            sys.stdout.flush()
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

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="Bootstrap a C project from a JSON configuration file.",
    )
    parser.add_argument("project_name", metavar="PROJECT_NAME",
                         help="Name of the project to create.")
    parser.add_argument("-c", "--config", metavar="PATH", default=None,
                         help="Path to the configuration file "
                              "(default: ~/.bootstrap.json).")
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
    """Turn PROJECT into a valid, upper-case C identifier for include guards."""
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
# 4-7. Source / header generation
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


def default_link_flags(dep_path: Path, wd: Path) -> str:
    """Inspect a built dependency for lib*.a archives and derive -L/-l flags."""
    archives = sorted(dep_path.rglob("lib*.a")) if dep_path.is_dir() else (
        [dep_path] if dep_path.is_file() and dep_path.name.startswith("lib") and dep_path.suffix == ".a" else []
    )
    flags = []
    seen_dirs = set()
    for archive in archives:
        libname = archive.stem[3:]  # strip "lib" prefix, stem already strips ".a"
        if not libname:
            continue
        rel_dir = archive.parent.relative_to(wd).as_posix()
        entry = f"-L{rel_dir} -l{libname}"
        key = (rel_dir, libname)
        if key not in seen_dirs:
            seen_dirs.add(key)
            flags.append(entry)
    return " ".join(flags)


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
            link = default_link_flags(dep_path, wd)
        deps_info.append({
            "name": name,
            "path": dep_path,
            "buildable": has_makefile_,
            "link": link,
            "failed": has_makefile_ and not built,
        })
    return deps_info


# --------------------------------------------------------------------------
# 8, 10, 11. Makefile generation
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
    config_path = Path(args.config).expanduser() if args.config else Path("~/.bootstrap.json").expanduser()

    try:
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
