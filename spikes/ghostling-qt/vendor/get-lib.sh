#!/bin/sh
# Fetch the runtime artifacts into vendor/:
#
# 1. libghostty-vt.so, extracted from the pyghostty wheel (pyghostty
#    bundles an official build; we do not use its Python API, only the
#    library). Requires: curl, unzip.
# 2. JetBrains Mono (OFL 1.1), the face Ghostty itself ships. Vendored
#    so the spike has deterministic font metrics on any host and no
#    system font dependency; terminal.py registers it with
#    QFontDatabase at startup.
set -eu
cd "$(dirname "$0")"
mkdir -p vendor/fonts

for style in Regular Bold Italic BoldItalic; do
  if [ ! -f "vendor/fonts/JetBrainsMono-$style.ttf" ]; then
    curl -sfL -o "vendor/fonts/JetBrainsMono-$style.ttf" \
      "https://raw.githubusercontent.com/JetBrains/JetBrainsMono/v2.304/fonts/ttf/JetBrainsMono-$style.ttf"
  fi
done
echo "fonts: $(find vendor/fonts -name '*.ttf' | wc -l) JetBrains Mono faces"

if [ -f vendor/libghostty-vt.so ]; then
  exit 0
fi

url=$(curl -s https://pypi.org/pypi/pyghostty/0.1.4/json | python3 -c "
import json, sys, platform
machine = platform.machine()
arch = 'x86_64' if machine in ('x86_64', 'AMD64') else 'aarch64'
for wheel in json.load(sys.stdin)['urls']:
    name = wheel['filename']
    if 'manylinux' in name and arch in name:
        print(wheel['url'])
        break
")
curl -sL -o vendor/wheel.zip "$url"
unzip -o -q vendor/wheel.zip 'pyghostty/_lib/*' -d vendor/
mv vendor/pyghostty/_lib/libghostty-vt.so vendor/
rm -rf vendor/pyghostty vendor/wheel.zip
echo "vendored: $(stat -c%s vendor/libghostty-vt.so) bytes"
