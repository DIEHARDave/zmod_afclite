import copy
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request


LOGGER = logging.getLogger(__name__)
FILE_CONFIG = "/usr/data/config/mod_data/file.json"

# OrcaSlicer reads filament per slot from Moonraker's lane_data namespace. On the
# AD5X, HelixScreen and SpoolSync already keep "lane1".."lane4" there, so the
# same keys are used and only color/material are merged into existing entries.
LANE_DATA_NAMESPACE = "lane_data"
LANE_DATA_POLL = 2.0
LANE_DATA_RETRY = 10.0
LANE_DATA_OWNER_KEY = "zmod_afclite"
UNSET_MATERIALS = ("NONE", "N/A", "?", "")
# save_variables key for a lane's weight; keep in sync with SET_WEIGHT.
WEIGHT_VARIABLE_PREFIX = "zmod_afclite_weight_"

# Zmod's slot count (color_limit) is 4 for a single IFS, and grows to the IFS
# Jacker's detected channel count when it chains several IFS units together.
# Lanes beyond the first IFS are only shown while the IFS Jacker plugin is
# loaded. The UI only subscribes to objects that exist when it connects, so
# lanes for up to four IFS units are registered up front and listed only while
# Zmod reports the slot; any further lanes are registered as they are detected
# and appear after the UI reconnects.
PREREGISTERED_SLOTS = 16
SLOTS_PER_UNIT = 4
UNIT_NAME = "IFS"


class AFCState:
    IDLE = "idle"


class AFCLaneState:
    EMPTY = "empty"
    LOADED = "loaded"


class ZmodState:
    """One parsed snapshot of Zmod's IFS status per Klipper status poll, shared
    by the AFC, unit and lane objects so zmod_color is queried once per poll."""

    def __init__(self, printer, on_slot_count):
        self.printer = printer
        self.on_slot_count = on_slot_count
        self.zmod_color = None
        self.ifs_jacker = False
        self.save_variables = None
        self.default_weight = 0.
        self._eventtime = None
        self._snapshot = None
        self._mapping_mtime = None
        self._mapping = {}
        self._bad_colors = set()

    def handle_ready(self):
        self.zmod_color = self.printer.lookup_object("zmod_color")
        for method in ("get_status", "cmd_IN_ZCOLOR", "cmd_CHANGE_ZCOLOR"):
            if not callable(getattr(self.zmod_color, method, None)):
                raise RuntimeError(
                    "Zmod AFC Lite requires Zmod AD5X method "
                    f"{self.zmod_color.__class__.__name__}.{method}"
                )
        # Only the IFS Jacker plugin chains extra IFS units; without it, a
        # color_limit above 4 does not mean more physical slots.
        self.ifs_jacker = self.printer.lookup_object("ifs_jacker", None) is not None
        # Lane weights set through SET_WEIGHT are kept in Zmod's save_variables.
        self.save_variables = self.printer.lookup_object("save_variables", None)

    def get(self, eventtime):
        if self._snapshot is None or eventtime is None or eventtime != self._eventtime:
            self._snapshot = self._read(eventtime)
            self._eventtime = eventtime
        return self._snapshot

    def _read(self, eventtime):
        # get_status() is cached by file mtime and has no side effects, unlike
        # get_current_channel(), which re-reads the config file on every call.
        status = self.zmod_color.get_status(eventtime)
        slot_count = max(1, int(status.get("color_limit", SLOTS_PER_UNIT)))
        if not self.ifs_jacker:
            slot_count = min(slot_count, SLOTS_PER_UNIT)
        self.on_slot_count(slot_count)
        slots = {}
        for slot in status.get("slots", []):
            try:
                slots[int(slot.get("ID"))] = slot
            except (TypeError, ValueError):
                continue
        return {
            "slot_count": slot_count,
            "unit_count": -(-slot_count // SLOTS_PER_UNIT),
            "current_slot": int(status.get("channel", 0)),
            "extruder_sensor": bool(status.get("extruder_sensor")),
            "slots": slots,
            "mapping": self._read_mapping(),
            "weights": dict(getattr(self.save_variables, "allVariables", None) or {}),
        }

    def _read_mapping(self):
        """Zmod's per-print tool-to-slot mapping as {slot: "T<n>"}, re-read
        only when file.json changes."""
        try:
            mtime = os.stat(FILE_CONFIG).st_mtime
        except OSError:
            self._mapping_mtime = None
            self._mapping = {}
            return self._mapping
        if mtime == self._mapping_mtime:
            return self._mapping

        self._mapping_mtime = mtime
        self._mapping = {}
        try:
            with open(FILE_CONFIG, "r", encoding="utf-8") as config_file:
                mapping = json.load(config_file)
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.error("Unable to read Zmod tool-to-slot mapping: %s", exc)
            return self._mapping

        if not isinstance(mapping, list):
            LOGGER.error("Zmod tool-to-slot mapping is not a list")
            return self._mapping

        for tool_index, slot in enumerate(mapping):
            try:
                self._mapping.setdefault(int(slot), f"T{tool_index}")
            except (TypeError, ValueError):
                LOGGER.error(
                    "Invalid slot value in Zmod tool-to-slot mapping: %r", slot
                )
                self._mapping = {}
                break
        return self._mapping

    def color(self, zmod_slot, slot):
        rgb = str(slot.get("HEX", "")).replace("#", "").upper()[:6]
        try:
            if len(rgb) == 6:
                int(rgb, 16)
                return f"#{rgb}"
        except ValueError:
            pass
        if zmod_slot not in self._bad_colors:
            self._bad_colors.add(zmod_slot)
            LOGGER.warning(
                "Zmod returned invalid color %r for IFS slot %d", rgb, zmod_slot
            )
        return "#FFFFFF"


class LaneDataSync:
    """Mirror Zmod's slot color/material into Moonraker's lane_data namespace.

    The reactor timer only takes a snapshot of Zmod's slots; HTTP runs on a
    worker thread so Klipper never blocks on Moonraker. After one reconcile at
    startup, a lane is written only when Zmod's own data for it changes, so
    other writers (HelixScreen, SpoolSync) are never fought over."""

    def __init__(self, printer, state, url):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.state = state
        self.url = url.rstrip("/") + "/server/database/item"
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._desired = None
        self._slot_count = 0
        self._synced = {}
        self._timer = None
        self._thread = None
        printer.register_event_handler("klippy:ready", self._handle_ready)
        printer.register_event_handler("klippy:disconnect", self._handle_disconnect)

    def _handle_ready(self):
        self._thread = threading.Thread(target=self._run, name="zmod_afclite lane_data")
        self._thread.daemon = True
        self._thread.start()
        self._timer = self.reactor.register_timer(self._poll, self.reactor.NOW)

    def _handle_disconnect(self):
        # Klipper RESTART builds new objects in the same process; stop this
        # instance's thread so only the new one writes.
        self._stop = True
        self._wake.set()
        if self._timer is not None:
            self.reactor.unregister_timer(self._timer)
            self._timer = None

    def _poll(self, eventtime):
        if self.state.zmod_color is None:
            return eventtime + LANE_DATA_POLL
        try:
            snapshot = self.state.get(eventtime)
            desired = {}
            for zmod_slot in range(1, snapshot["slot_count"] + 1):
                slot = snapshot["slots"].get(zmod_slot)
                if slot is None:
                    continue
                material = str(slot.get("Material", "")).upper()
                desired[f"lane{zmod_slot}"] = {
                    "lane": str(zmod_slot - 1),
                    "color": self.state.color(zmod_slot, slot),
                    "material": None if material in UNSET_MATERIALS else material,
                }
            with self._lock:
                changed = desired != self._desired
                self._desired = desired
                self._slot_count = snapshot["slot_count"]
            if changed:
                self._wake.set()
        except Exception:
            LOGGER.exception("Zmod AFC Lite: unable to read slots for lane_data")
        return eventtime + LANE_DATA_POLL

    def _run(self):
        while not self._stop:
            self._wake.wait()
            self._wake.clear()
            if self._stop:
                break
            with self._lock:
                desired = copy.deepcopy(self._desired)
                slot_count = self._slot_count
            if desired is None:
                continue
            try:
                self._sync(desired, slot_count)
            except Exception as exc:
                LOGGER.warning("Zmod AFC Lite: lane_data sync failed, retrying: %s", exc)
                self._wake.wait(LANE_DATA_RETRY)
                self._wake.set()

    def _request(self, method, query=None, body=None):
        url = self.url
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def _sync(self, desired, slot_count):
        try:
            current = self._request("GET", {"namespace": LANE_DATA_NAMESPACE})
            current = current.get("result", {}).get("value") or {}
        except urllib.error.HTTPError as exc:
            if exc.code != 404:  # 404: the namespace does not exist yet
                raise
            current = {}

        for key, zmod in desired.items():
            if self._synced.get(key) == zmod:
                continue
            existing = current.get(key)
            value = copy.deepcopy(existing) if isinstance(existing, dict) else {
                LANE_DATA_OWNER_KEY: True,
            }
            value["lane"] = zmod["lane"]
            if not value.get("helix_locked_color"):
                value["color"] = zmod["color"]
            if zmod["material"] is not None and not value.get("helix_locked_material"):
                value["material"] = zmod["material"]
            if value != existing:
                self._request("POST", body={
                    "namespace": LANE_DATA_NAMESPACE, "key": key, "value": value,
                })
            self._synced[key] = zmod

        # Remove only entries this plugin created for slots that no longer
        # exist (an IFS Jacker unit was removed); never other writers' lanes.
        for key, existing in current.items():
            if not (isinstance(existing, dict) and existing.get(LANE_DATA_OWNER_KEY)):
                continue
            try:
                slot = int(key[len("lane"):]) if key.startswith("lane") else 0
            except ValueError:
                continue
            if slot > slot_count:
                self._request("DELETE", {"namespace": LANE_DATA_NAMESPACE, "key": key})
                self._synced.pop(key, None)


def unit_names(unit_count):
    """A single IFS keeps the plain "IFS" unit; chained IFS units (IFS Jacker)
    become "IFS_1", "IFS_2", ... with four lanes each."""
    if unit_count <= 1:
        return [UNIT_NAME]
    return [f"{UNIT_NAME}_{index + 1}" for index in range(unit_count)]


class AFC:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.state = ZmodState(self.printer, self._ensure_slots)
        # Mainsail's filament dialog keeps "Set" disabled until the lane has a
        # weight, so report one by default; 0 hides weights again.
        self.state.default_weight = config.getfloat("default_weight", 1000., minval=0.)
        self.lanes = {}
        self.units = {}

        objects = {"AFC": self, **self._new_objects(PREREGISTERED_SLOTS)}
        for name in objects:
            if self.printer.lookup_object(name, None) is not None:
                raise config.error(
                    f"Cannot enable Zmod AFC Lite: Klipper object {name!r} "
                    "already exists. Disable the conflicting AFC integration."
                )
        for name, obj in objects.items():
            self.printer.add_object(name, obj)

        self.lane_data = None
        if config.getboolean("lane_data", True):
            self.lane_data = LaneDataSync(
                self.printer, self.state,
                config.get("moonraker_url", "http://127.0.0.1:7125"),
            )

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        self.state.handle_ready()

    def _new_objects(self, slot_count):
        """Create the lane and unit objects needed for slot_count slots that do
        not exist yet, and return them keyed by Klipper object name."""
        objects = {}
        for index in range(len(self.lanes), slot_count):
            lane = AFCLane(self.state, f"E{index}", index, "extruder")
            self.lanes[lane.name] = lane
            objects[f"AFC_lane {lane.name}"] = lane
        unit_count = -(-slot_count // SLOTS_PER_UNIT)
        for name in unit_names(1) + unit_names(max(unit_count, 2)):
            if name not in self.units:
                unit = AFCUnit(self.state, name, self.lanes)
                self.units[name] = unit
                objects[f"AFC_unit {name}"] = unit
        return objects

    def _ensure_slots(self, slot_count):
        """Register lanes for IFS Jacker channels beyond the preregistered ones.
        The UI picks them up the next time it loads the printer's objects."""
        if slot_count <= len(self.lanes):
            return
        for name, obj in self._new_objects(slot_count).items():
            if self.printer.lookup_object(name, None) is not None:
                LOGGER.error(
                    "Zmod AFC Lite: Klipper object %r already exists; "
                    "not registering it for IFS slot growth", name
                )
                continue
            self.printer.add_object(name, obj)
        LOGGER.info("Zmod AFC Lite: registered lanes for %d IFS slots", slot_count)

    def get_status(self, eventtime=None):
        state = self.state.get(eventtime)
        lanes = [
            lane.name for lane in self.lanes.values()
            if lane.zmod_slot <= state["slot_count"]
        ]

        current_lane = None
        if 1 <= state["current_slot"] <= state["slot_count"]:
            current_lane = f"E{state['current_slot'] - 1}"

        return {
            "current_load": None,
            "current_lane": current_lane,
            "next_lane": None,
            "current_state": AFCState.IDLE,
            "current_toolchange": 0,
            "number_of_toolchanges": 0,
            "spoolman": False,
            "td1_present": False,
            "lane_data_enabled": False,
            "error_state": False,
            "bypass_state": False,
            "quiet_mode": False,
            "position_saved": False,
            # Mainsail/Fluidd expect "<type> <name>" and look the unit up as
            # the "AFC_<type> <name>" object, i.e. "AFC_unit IFS".
            "units": [f"unit {name}" for name in unit_names(state["unit_count"])],
            "lanes": lanes,
            # No AFC_extruder object exists, so list none (as AFC-Lite does)
            # instead of making the UI draw an empty toolhead card.
            "extruders": [],
            "hubs": [],
            "buffers": [],
            "message": "",
            "led_state": "",
        }


class AFCLane:
    def __init__(self, state, name, lane_index, extruder_name):
        self.state = state
        self.name = name
        self.lane_index = lane_index
        self.zmod_slot = lane_index + 1
        self.extruder_name = extruder_name

    def unit_name(self, unit_count):
        return unit_names(unit_count)[
            min(self.lane_index // SLOTS_PER_UNIT, unit_count - 1)
        ]

    def get_status(self, eventtime=None):
        state = self.state.get(eventtime)
        active = self.zmod_slot <= state["slot_count"]
        slot = state["slots"].get(self.zmod_slot, {}) if active else {}

        loaded = bool(slot.get("hasFilament"))
        tool_loaded = (
            active
            and state["current_slot"] == self.zmod_slot
            and state["extruder_sensor"]
        )
        material = str(slot.get("Material", "NONE")).upper()

        status = {
            "name": self.name,
            "unit": self.unit_name(state["unit_count"]),
            "lane": self.lane_index,
            "zmod_slot": self.zmod_slot,
            "extruder": self.extruder_name,
            "map": state["mapping"].get(self.zmod_slot, "NONE"),
            "load": loaded,
            "prep": loaded,
            "tool_loaded": tool_loaded,
            "loaded_to_hub": False,
            "material": material,
            "spool_id": None,
            "color": self.state.color(self.zmod_slot, slot) if active else "#FFFFFF",
            "runout_lane": "NONE",
            "filament_status": "unknown",
            "filament_status_led": "gray",
            "status": AFCLaneState.LOADED if loaded else AFCLaneState.EMPTY,
        }
        if material not in UNSET_MATERIALS:
            status["filament_name"] = material
        weight = self.weight(state)
        if weight > 0:
            status["weight"] = weight
        return status

    def weight(self, state):
        """Weight saved by SET_WEIGHT, else the configured default. Zmod does
        not measure filament, so this is only what the user entered."""
        saved = state["weights"].get(f"{WEIGHT_VARIABLE_PREFIX}{self.name.lower()}")
        try:
            return max(0, round(float(saved)))
        except (TypeError, ValueError):
            return round(self.state.default_weight)


class AFCUnit:
    def __init__(self, state, name, lanes):
        self.state = state
        self.name = name
        self.lanes = lanes

    def get_status(self, eventtime=None):
        state = self.state.get(eventtime)
        lanes = [
            lane.name for lane in self.lanes.values()
            if lane.zmod_slot <= state["slot_count"]
            and lane.unit_name(state["unit_count"]) == self.name
        ]
        return {
            "lanes": lanes,
            "extruders": [],
            "hubs": [],
            "buffers": [],
        }


def load_config(config):
    return AFC(config)
