# Bootstrap Project Generator

A small Python-based project bootstrapper for quickly creating a standardized **C or C++ project** from a configuration file.

The goal of this project is to eliminate the repetitive setup work involved in starting a new C/C++ project: creating directories, generating source and header files, configuring a Makefile, installing dependencies, setting up tests, initializing Git, and documenting the resulting project structure.

The generated project is intended to provide a clean starting point that can immediately be built and tested with `make`.

## Features

The bootstrap script can:

* Create a complete C/C++ project directory structure.
* Generate initial source and header files.
* Generate a configurable `Makefile`.
* Automatically collect project source files.
* Compile project sources into a static archive (`.a`).
* Build and run the final executable.
* Create a test script.
* Install dependencies from Git URLs or local paths.
* Add dependency include directories to the generated Makefile.
* Configure dependency linking explicitly.
* Build dependencies before building the main project.
* Forward `clean` and `fclean` operations to dependencies.
* Initialize a Git repository when requested.
* Generate a `.gitignore`.
* Generate a `layout.md` describing the resulting project structure.

The project is designed to be **configuration-driven**, so the same bootstrap script can be reused for different C and C++ projects without modifying the script itself.

---

# Usage

Run the bootstrapper with a project name:

```bash
python3 bootstrap.py my_project
```

By default, the script looks for:

```text
~/.bootstrap.json
```

A different configuration file can be supplied with `--config` or `-c`:

```bash
python3 bootstrap.py my_project --config /path/to/config.json
```

or:

```bash
python3 bootstrap.py my_project -c /path/to/config.json
```

The resulting project is created in the current working directory:

```text
./my_project/
```

---

# Configuration

The bootstrapper is controlled by a JSON configuration file.

The configuration determines whether the generated project is a **C project or a C++ project**, primarily through the source-file extension.

## C project example

```json
{
    "src_dir": "src",
    "source_ex": ".c",
    "header_ex": ".h",
    "inc_dir": "inc",
    "doc_dir": "doc",
    "test_dir": "test",
    "test_main": "main.sh",
    "git_init": true,

    "dep": {
        "libft": {
            "source": "https://example.com/libft.git",
            "link": "-Llib/libft -lft"
        }
    }
}
```

## C++ project example

```json
{
    "src_dir": "src",
    "source_ex": ".cpp",
    "header_ex": ".hpp",
    "inc_dir": "inc",
    "doc_dir": "doc",
    "test_dir": "test",
    "test_main": "main.sh",
    "git_init": true,

    "dep": {
        "mylib": {
            "source": "https://example.com/mylib.git",
            "link": "-Llib/mylib -lmylib"
        }
    }
}
```

The bootstrapper should use the configured extensions when generating files.

For example, a C++ project named `my_project` would generate:

```text
src/my_project.cpp
src/main.cpp
inc/my_project.hpp
```

while a C project would generate:

```text
src/my_project.c
src/main.c
inc/my_project.h
```

---

# Configuration fields

## `src_dir`

Directory containing source files.

```json
"src_dir": "src"
```

## `source_ex`

Extension used for source files.

For C:

```json
"source_ex": ".c"
```

For C++:

```json
"source_ex": ".cpp"
```

The source extension also determines the language used by the generated project.

The implementation should support common C/C++ source extensions such as:

```text
.c
.cc
.cpp
.cxx
```

when appropriate.

## `header_ex`

Extension used for header files.

For C:

```json
"header_ex": ".h"
```

For C++:

```json
"header_ex": ".hpp"
```

Other header extensions may also be used when explicitly configured.

## `inc_dir`

Directory containing project headers.

```json
"inc_dir": "inc"
```

## `doc_dir`

Directory containing project documentation.

```json
"doc_dir": "doc"
```

## `test_dir`

Directory containing test-related files.

```json
"test_dir": "test"
```

## `test_main`

Name of the generated test script.

```json
"test_main": "main.sh"
```

## `git_init`

Controls whether a Git repository is initialized.

```json
"git_init": true
```

When enabled, the bootstrapper creates a Git repository and a `.gitignore`.

## `dep`

Optional object containing project dependencies.

Each dependency specifies:

```json
"dependency_name": {
    "source": "...",
    "link": "..."
}
```

---

# C and C++ support

The generated Makefile must adapt to the selected language.

For a C project, it should use a C compiler such as:

```make
CC = cc
```

or:

```make
CC = gcc
```

For a C++ project, it should use a C++ compiler such as:

```make
CXX = c++
```

or:

```make
CXX = g++
```

The Makefile should choose the compiler according to the configured source extension.

For example:

```text
.c     -> C compiler
.cpp   -> C++ compiler
.cc    -> C++ compiler
.cxx   -> C++ compiler
```

The generated compilation commands must use:

```text
-Wall -Wextra -Werror
```

for both C and C++ projects.

The project may use additional language-specific flags when necessary, but the standard warning flags must always be present.

---

# Dependencies

Dependencies are declared under the `dep` object.

Each dependency has two main properties:

```json
{
    "source": "...",
    "link": "..."
}
```

## `source`

`source` specifies where the dependency comes from.

It can be:

* a Git URL;
* a local file;
* a local directory.

### Git dependency

```json
"libft": {
    "source": "https://example.com/libft.git",
    "link": "-Llib/libft -lft"
}
```

The bootstrapper clones the repository into:

```text
lib/libft/
```

### Local dependency

```json
"mylib": {
    "source": "/home/user/projects/mylib",
    "link": "-Llib/mylib -lmylib"
}
```

The dependency is copied into:

```text
lib/mylib/
```

The original dependency is not modified.

After cloning or copying a dependency, `.git` directories inside the copied dependency must be removed.

The bootstrapper must not modify `.git` directories outside the generated project.

---

# Dependency linking

Dependency linking is deliberately specified explicitly in the configuration.

For example:

```json
"libft": {
    "source": "https://example.com/libft.git",
    "link": "-Llib/libft -lft"
}
```

The bootstrapper does **not** attempt to guess how an arbitrary library should be linked.

The `link` value is inserted into the generated Makefile.

For example:

```make
LIBS = -Llib/libft -lft
```

For another dependency:

```json
"mlx": {
    "source": "https://example.com/mlx.git",
    "link": "-Llib/mlx -lmlx -lXext -lX11"
}
```

the generated Makefile can use:

```make
LIBS = -Llib/mlx -lmlx -lXext -lX11
```

This allows C and C++ projects to use dependencies with different build systems and linker requirements without requiring the bootstrapper to understand the internal implementation of each dependency.

---

# Dependency include paths

After installing dependencies, the bootstrapper recursively searches:

```text
lib/
```

for header files.

For every directory containing headers, an include path is added to the Makefile.

For example:

```text
lib/libft/include/libft.h
```

results in:

```make
-Ilib/libft/include
```

The header itself is not passed to `-I`; only the containing directory is used.

Duplicate include directories must be removed.

---

# Generated project

For a project named:

```text
my_project
```

a typical C project might look like:

```text
my_project/
├── Makefile
├── my_project.a
├── .gitignore
│
├── src/
│   ├── my_project.c
│   └── main.c
│
├── inc/
│   └── my_project.h
│
├── obj/
│
├── lib/
│   └── libft/
│
├── doc/
│   └── layout.md
│
└── test/
    └── main.sh
```

A C++ project would use the configured extensions:

```text
my_project/
├── Makefile
├── my_project.a
├── .gitignore
│
├── src/
│   ├── my_project.cpp
│   └── main.cpp
│
├── inc/
│   └── my_project.hpp
│
├── obj/
│
├── lib/
│
├── doc/
│   └── layout.md
│
└── test/
    └── main.sh
```

The exact structure depends on the configuration.

---

# Generated source files

The bootstrapper creates a minimal working program.

The generated project header declares:

```c
void debug(void);
```

For C++ projects, the generated declaration must be valid C++.

The generated source implements `debug()` and prints:

```text
Bye bye world
```

The generated `main` program calls `debug()` and returns `0`.

The purpose of this minimal code is to verify that the generated project can be compiled, linked, and executed immediately after creation.

The generated starter code should remain intentionally simple so that the user can replace it with the actual project implementation.

---

# Makefile

The generated Makefile is responsible for building the project.

The main targets are:

```text
all
archive
run
test
clean
fclean
re
```

## `make`

Build the complete project.

```bash
make
```

This includes building configured dependencies, compiling the project, creating the static archive, and linking the final executable.

## `make archive`

Compile the project library sources and create:

```text
my_project.a
```

Object files are stored under:

```text
obj/
```

## `make run`

Run the generated executable.

```bash
make run
```

## `make test`

Run the generated test script.

```bash
make test
```

## `make clean`

Remove generated object files and invoke `make clean` for configured dependencies.

## `make fclean`

Remove generated object files, the static archive, and the executable, then invoke `make fclean` for dependencies.

## `make re`

Perform a complete rebuild.

---

# Testing

The generated test script executes:

```bash
make run
```

and checks its exit status.

A successful execution results in a success message.

A non-zero exit status produces an error message on `stderr` and causes the test script to return a failure status.

A newly generated project should therefore be testable with:

```bash
make test
```

---

# Git support

When:

```json
"git_init": true
```

is specified, the bootstrapper runs:

```bash
git init
```

inside the generated project.

It also creates:

```text
.gitignore
```

and ignores generated object files, for example:

```gitignore
obj/
```

Dependency `.git` directories are removed after dependencies are installed so that dependencies do not become nested Git repositories inside the generated project.

---

# Documentation

The bootstrapper generates:

```text
doc/layout.md
```

containing a tree representation of the generated project.

For example:

```text
my_project/
├── Makefile
├── my_project.a
├── src/
│   ├── my_project.cpp
│   └── main.cpp
├── inc/
│   └── my_project.hpp
├── obj/
├── lib/
├── doc/
│   └── layout.md
└── test/
    └── main.sh
```

The documented layout should reflect the actual configuration and installed dependencies.

---

# Typical workflow

A typical C++ workflow might be:

```bash
python3 bootstrap.py my_project
cd my_project
make
make run
make test
```

Expected output from the generated application:

```text
Bye bye world
```

After bootstrapping, the generated project can be modified normally.

Additional source files can be added to `src/`, headers to `inc/`, and dependencies can be added or changed through the configuration.

---

# Design goals

The project is intentionally focused on **repeatable project initialization**, rather than acting as a complete replacement for a build system.

The bootstrapper should provide a predictable starting point while keeping important project-specific decisions in the configuration file.

In particular, dependency linking is explicitly configured rather than inferred.

The same tool should be able to bootstrap both C and C++ projects by changing configuration values such as:

```json
"source_ex": ".c"
```

or:

```json
"source_ex": ".cpp"
```

The intended result is a small, reusable tool that can turn:

```text
project name
+
configuration
```

into:

```text
ready-to-build C or C++ project
```

with minimal manual setup.
