#!/bin/sh
[ -f /usr/data/zmod/zmod/.shell/0.sh ] && . /usr/data/zmod/zmod/.shell/0.sh

set -eu

PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

for EXTRAS_DIR in \
    /usr/prog/klipper/klippy/extras \
    /usr/data/zmod/klipper/klippy/extras \
    ${KLIPPER_DIR:+"$KLIPPER_DIR/klippy/extras"}
do
    if [ -d "$EXTRAS_DIR" ]; then
        for module in zmod_afclite.py AFC.py AFC_lane.py AFC_unit.py afc_bridge.py; do
            target="$EXTRAS_DIR/$module"
            if [ -L "$target" ]; then
                current=$(readlink "$target")
                resolved=$(readlink -f "$target" || true)
                if [ "$current" = "$PLUGIN_DIR/$module" ] || [ "$resolved" = "$PLUGIN_DIR/$module" ]; then
                    rm "$target"
                fi
            fi
        done
    fi
done

echo "Zmod AFC Lite Klipper module links removed"
