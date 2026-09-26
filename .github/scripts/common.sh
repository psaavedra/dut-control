#!/bin/bash
# Shared helpers for the sanatizers. Sourced, not executed, and named so
# that run-all-sanatizers does not pick it up as one of them.

fatal() {
    echo "$@" >&2
    exit 1
}

# Echo the first of the given commands that exists.
find_tool() {
    local cmd
    for cmd in "$@"; do
        if command -v "$cmd" >/dev/null 2>&1; then
            echo "$cmd"
            return 0
        fi
    done
    return 1
}

# Directories that are not ours to lint.
prune_args=(-name .git -o -name .venv -o -name __pycache__
            -o -name .pytest_cache -o -name '*.egg-info')

# Files named after $1, plus executables whose shebang names $2. Keying
# only on the shebang skips package modules, which have none.
discover() {
    {
        find . \( "${prune_args[@]}" \) -prune -o \
            -type f -name "$1" -print
        find . \( "${prune_args[@]}" \) -prune -o \
            -type f -perm -u+x -print0 |
            xargs -0 -r grep -lE "^#! *(|/usr/bin/env +|/bin/|/usr/bin/)($2)"
    } | sort -u
}

python_sources() { discover '*.py' 'python|python3'; }

shell_sources() { discover '*.sh' 'sh|bash|dash'; }

# cd to the repository root, so `find .` sees the tree and not the
# directory the sanatizer happened to be invoked from.
cd_repo_root() {
    cd "$(dirname "${BASH_SOURCE[0]}")/../.." || fatal "no repository root"
}
