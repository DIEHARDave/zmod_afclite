#!/bin/sh
set -eu

PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
EXTRAS_DIR=
for candidate in \
    /usr/prog/klipper/klippy/extras \
    /usr/data/zmod/klipper/klippy/extras
do
    if [ -d "$candidate" ]; then
        EXTRAS_DIR=$candidate
        break
    fi
done

if [ -z "$EXTRAS_DIR" ]; then
    echo "Could not find a supported Zmod Klipper extras directory" >&2
    exit 1
fi

MODULE=zmod_afclite.py
SOURCE="$PLUGIN_DIR/$MODULE"
TARGET="$EXTRAS_DIR/$MODULE"

if [ -L "$TARGET" ]; then
    current=$(readlink "$TARGET")
    if [ "$current" != "$SOURCE" ]; then
        echo "Refusing to replace another symlink: $TARGET -> $current" >&2
        exit 1
    fi
elif [ -e "$TARGET" ]; then
    echo "Refusing to replace an existing Klipper module: $TARGET" >&2
    exit 1
fi

if [ ! -L "$TARGET" ]; then
    ln -s "$SOURCE" "$TARGET"
fi

# Clean up links created by the previous installer, but only when they belong
# to this plugin.
for module in AFC.py AFC_lane.py AFC_unit.py afc_bridge.py; do
    legacy="$EXTRAS_DIR/$module"
    if [ -L "$legacy" ] && [ "$(readlink "$legacy")" = "$PLUGIN_DIR/$module" ]; then
        rm "$legacy"
    fi
done

echo "Zmod AFC Lite installed in $EXTRAS_DIR"
