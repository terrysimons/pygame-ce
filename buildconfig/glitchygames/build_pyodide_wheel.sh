#!/bin/bash
# Build a Pyodide wheel of this pygame-ce checkout (working tree included, so uncommitted
# changes are built too; the build-info file records whether there were any).
#
# GlitchyGames' fork (branch glitchygames-wasm): the WebAssembly fixes it carries are listed in
# that branch's history; .github/workflows/glitchygames-wasm-wheel.yml runs this script in CI.
#
# Reproduces how pyodide-recipes builds pygame-ce for Pyodide 314 (its 2.5.7 recipe)
# (https://github.com/pyodide/pyodide-recipes/blob/main/packages/pygame-ce/meta.yaml, commit
# 13a9a268fa): the same Emscripten ports and flags, then `pyodide build` against the Pyodide
# 314.0.3 cross-build environment (Python 3.14.2, Emscripten 5.0.3), which is the runtime
# PyScript 2026.7.3 pins.
#
# Usage: buildconfig/glitchygames/build_pyodide_wheel.sh [source_dir] [output_dir]
#   (defaults: this checkout, ./dist-pyodide)
# Environment: PYODIDE_VERSION selects another cross-build environment (default 314.0.3, the one
#   PyScript pins); version_matrix.py uses it to try newer Pyodide releases. The toolchain venv
#   is Python 3.14.2, so only cross-build environments for Python 3.14 work.
# Toolchain cache (about 2 GB): ~/.cache/glitchygames-pyodide-build
# Writes the wheel and build-info.json (commit, versions, sha256) to the output directory.
set -euo pipefail

PYODIDE_VERSION="${PYODIDE_VERSION:-314.0.3}"
# Pinned: an unpinned install takes whatever pyodide-build is newest when a cache is first
# filled, so the same source could build a different wheel. Every build so far used 0.39.2
# (build-info.json records it); move it on deliberately, with the version matrix checking it
PYODIDE_BUILD_VERSION="0.39.2"
script_directory="$(cd "$(dirname "$0")" && pwd)"
source_directory="${1:-$script_directory/../..}"
output_directory="${2:-$source_directory/dist-pyodide}"
cache_directory="$HOME/.cache/glitchygames-pyodide-build"
export PYODIDE_XBUILDENV_PATH="$cache_directory/xbuildenv"

source_directory="$(cd "$source_directory" && pwd)"
mkdir -p "$output_directory"
output_directory="$(cd "$output_directory" && pwd)"

# Toolchain: pyodide-build in its own venv, the cross-build environment and its Emscripten
if [ ! -x "$cache_directory/venv/bin/python" ]; then
    mkdir -p "$cache_directory"
    uv venv -q --python 3.14.2 "$cache_directory/venv"
fi
# Also corrects a cached venv made before the pin, or with another version
installed_pyodide_build="$("$cache_directory/venv/bin/python" -c \
    "import importlib.metadata as metadata; print(metadata.version('pyodide-build'))" 2>/dev/null || true)"
if [ "$installed_pyodide_build" != "$PYODIDE_BUILD_VERSION" ]; then
    uv pip install -q --python "$cache_directory/venv/bin/python" "pyodide-build==$PYODIDE_BUILD_VERSION"
fi
pyodide="$cache_directory/venv/bin/pyodide"
if [ ! -d "$PYODIDE_XBUILDENV_PATH/$PYODIDE_VERSION" ]; then
    "$pyodide" xbuildenv install "$PYODIDE_VERSION"
fi
"$pyodide" xbuildenv use "$PYODIDE_VERSION" > /dev/null
emsdk_environment="$PYODIDE_XBUILDENV_PATH/$PYODIDE_VERSION/emsdk/emsdk_env.sh"
# Checked on its own, not with the cross-build environment: an Emscripten install that failed
# after the environment's succeeded would otherwise never be retried
if [ ! -f "$emsdk_environment" ]; then
    "$pyodide" xbuildenv install-emscripten
fi
# shellcheck disable=SC1090  # Emscripten's own script, at a path chosen at run time
source "$emsdk_environment" > /dev/null 2>&1
echo "emcc: $(emcc --version | head -1)"
# pkg-config must not see the host's .pc files: on macOS with Homebrew, meson otherwise finds
# the host python-3.14.pc and compiles against host Python headers ("LONG_BIT definition appears
# wrong"). Restricted to the Emscripten sysroot, meson falls back to the cross build env's
# sysconfig for Python, as it does on Linux CI where no host .pc exists.
export PKG_CONFIG_LIBDIR="$PYODIDE_XBUILDENV_PATH/$PYODIDE_VERSION/emsdk/upstream/emscripten/cache/sysroot/lib/pkgconfig"
export PKG_CONFIG_PATH=""

# Build from a copy: the recipe edits meson.build, and the source tree must stay untouched
build_directory="$(mktemp -d "${TMPDIR:-/tmp}/pygame-ce-wasm-build.XXXXXX")"
trap 'rm -rf "$build_directory"' EXIT
rsync -a --exclude .git --exclude build --exclude dist --exclude dist-pyodide \
    "$source_directory/" "$build_directory/src/"
source_commit="$(git -C "$source_directory" rev-parse HEAD)"
source_dirty="$(git -C "$source_directory" status --porcelain --untracked-files=no | wc -l | tr -d ' ')"
# Reproducible archives: every timestamp in the wheel becomes the commit's time
SOURCE_DATE_EPOCH="$(git -C "$source_directory" log -1 --format=%ct)"
export SOURCE_DATE_EPOCH
cd "$build_directory/src"

# The recipe's script, verbatim apart from comments (see meta.yaml)
touch dummy_src_for_port.c
emcc dummy_src_for_port.c -sSUPPORT_LONGJMP="wasm" -sSIDE_MODULE=1 -fPIC \
    "--use-port=sdl2_image:formats=bmp,gif,jpg,lbm,pcx,png,pnm,qoi,svg,tga,xcf,xpm,xv"
emcc dummy_src_for_port.c -sSUPPORT_LONGJMP="wasm" -sSIDE_MODULE=1 -fPIC -sUSE_FREETYPE=1
embuilder --pic build sdl2 libhtml5 sdl2_ttf 'sdl2_mixer:formats=ogg,mp3,mod,mid'
# macOS sed needs an explicit (empty) backup suffix
sed -i.bak 's/-sRELOCATABLE=1//' meson.build && rm meson.build.bak

# Recipes link with the 'pyinit' export setting (only PyInit_<module> exported)
"$pyodide" build --exports pyinit --outdir "$output_directory" .

wheel_path="$(ls -t "$output_directory"/pygame_ce-*.whl | head -1)"
if command -v sha256sum > /dev/null; then
    wheel_sha256="$(sha256sum "$wheel_path" | cut -d' ' -f1)"
else
    wheel_sha256="$(shasum -a 256 "$wheel_path" | cut -d' ' -f1)"
fi
cat > "$output_directory/build-info.json" <<BUILD_INFO
{
  "wheel": "$(basename "$wheel_path")",
  "sha256": "$wheel_sha256",
  "bytes": $(wc -c < "$wheel_path" | tr -d ' '),
  "source_commit": "$source_commit",
  "uncommitted_tracked_changes": $source_dirty,
  "source_date_epoch": $SOURCE_DATE_EPOCH,
  "pyodide": "$PYODIDE_VERSION",
  "emscripten": "$(emcc --version | head -1 | sed -E 's/.* ([0-9]+\.[0-9]+\.[0-9]+).*/\1/')",
  "pyodide_build": "$("$pyodide" --version 2>/dev/null | grep -i 'pyodide-build' | sed -E 's/.*version:? *//')"
}
BUILD_INFO
cat "$output_directory/build-info.json"
