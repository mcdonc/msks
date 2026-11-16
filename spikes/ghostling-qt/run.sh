#!/bin/sh
# Run ghostling-qt on NixOS, where the pip-installed PySide6 wheel
# needs host shared libraries (libstdc++, glib, X11, ...) that are not
# in a global path. Resolves them from nixpkgs, caches the result in
# .libdirs, then execs the venv python. Usage:
#
#   ./run.sh                # interactive terminal (main.py)
#   ./run.sh smoke          # offscreen smoke test
set -eu
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet PySide6
fi

if [ ! -f vendor/libghostty-vt.so ]; then
  sh vendor/get-lib.sh
fi

# Run native Wayland, not XWayland: a KDE session exports
# QT_PLUGIN_PATH with system Qt paths that shadow the wheel's own
# platform plugins, silently dropping the app onto xcb. kwin then
# rescales the XWayland window and glyph detail is lost. Drop the
# shadowing paths so PySide6 loads its bundled wayland plugin, and
# make the platform explicit.
unset QT_PLUGIN_PATH
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland}"

if [ ! -s .libdirs ]; then
  rm -f .libdirs
  paths=""
  for spec in gcc.cc.lib glib^out libX11^out libxcb^out \
    libXrender^out libXext^out libXi^out libxkbcommon^out \
    libGL^out freetype^out fontconfig^lib dbus^lib \
    wayland^out; do
    out=$(nix build "nixpkgs#$spec" --print-out-paths --no-link \
      2>/dev/null | head -1)
    if [ -n "$out" ]; then
      paths="$paths:$out/lib"
    else
      echo "run.sh: nix build failed for $spec" >&2
    fi
  done
  if [ -z "$paths" ]; then
    echo "run.sh: could not resolve any library paths via nix;" \
      "is nix on PATH?" >&2
    exit 1
  fi
  printf '%s' "$paths" >.libdirs
fi

if [ "${1:-}" = smoke ]; then
  shift
  LD_LIBRARY_PATH="$(cat .libdirs)" exec .venv/bin/python smoke.py "$@"
fi
LD_LIBRARY_PATH="$(cat .libdirs)" exec .venv/bin/python main.py "$@"
