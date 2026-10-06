import json
import logging


LOGGER = logging.getLogger(__name__)
FILE_CONFIG = "/usr/data/config/mod_data/file.json"


class AFCState:
    IDLE = "idle"


class AFCLaneState:
    EMPTY = "empty"
    LOADED = "loaded"


class AFC:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = "ZMOD"
        self.lanes = {
            f"E{index}": AFCLane(
                self.printer,
                f"E{index}",
                index,
                index + 1,
                "ZMOD",
                "extruder",
            )
            for index in range(4)
        }
        self.unit = AFCUnit(self.printer, self.name, self.lanes)
        self.units = {self.name: self.unit}

        objects = {
            "AFC": self,
            "AFC_unit ZMOD": self.unit,
            **{
                f"AFC_lane {lane.name}": lane
                for lane in self.lanes.values()
            },
        }
        for name in objects:
            if self.printer.lookup_object(name, None) is not None:
                raise config.error(
                    f"Cannot enable Zmod AFC Lite: Klipper object {name!r} "
                    "already exists. Disable the conflicting AFC integration."
                )
        for name, obj in objects.items():
            self.printer.add_object(name, obj)

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        slots = [lane.zmod_slot for lane in self.lanes.values()]
        if len(slots) != 4 or set(slots) != {1, 2, 3, 4}:
            raise RuntimeError(
                "Zmod AFC Lite requires four unique lanes mapped to IFS slots 1-4"
            )
        for lane in self.lanes.values():
            lane._handle_ready()
        self.unit._handle_ready()

    def get_status(self, eventtime=None):
        current_slot = None
        if self.lanes:
            zmod_color = self.printer.lookup_object("zmod_color")
            current_slot = zmod_color.get_current_channel()

        current_lane = None
        for lane in self.lanes.values():
            if lane.zmod_slot == current_slot:
                current_lane = lane.name
                break

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
            # the "AFC_<type> <name>" object, i.e. "AFC_unit ZMOD".
            "units": [f"unit {name}" for name in self.units],
            "lanes": list(self.lanes),
            # No AFC_extruder object exists, so list none (as AFC-Lite does)
            # instead of making the UI draw an empty toolhead card.
            "extruders": [],
            "hubs": [],
            "buffers": [],
            "message": "",
            "led_state": "",
        }


class AFCLane:
    def __init__(
        self, printer, name, lane_index, zmod_slot, unit_name, extruder_name
    ):
        self.printer = printer
        self.name = name
        self.unit_name = unit_name
        self.lane_index = lane_index
        self.zmod_slot = zmod_slot
        self.extruder_name = extruder_name

    def _handle_ready(self):
        self.zmod_color = self.printer.lookup_object("zmod_color")
        self.zmod_ifs = self.printer.lookup_object("zmod_ifs")

        required_methods = (
            (self.zmod_color, "get_printer_data_detail"),
            (self.zmod_color, "get_current_channel"),
            (self.zmod_color, "get_extruder_sensor"),
            (self.zmod_color, "cmd_IN_ZCOLOR"),
            (self.zmod_color, "cmd_CHANGE_ZCOLOR"),
            (self.zmod_ifs, "get_port"),
        )
        for backend, method in required_methods:
            if not callable(getattr(backend, method, None)):
                raise RuntimeError(
                    "Zmod AFC Lite requires Zmod AD5X method "
                    f"{backend.__class__.__name__}.{method}"
                )

    def _read_slot(self):
        result, payload = self.zmod_color.get_printer_data_detail()
        if result != 200:
            raise RuntimeError(
                f"Zmod could not read IFS spool metadata: {payload}"
            )
        if not isinstance(payload, dict):
            raise RuntimeError("Zmod returned invalid IFS spool metadata")

        detail = payload.get("detail", {})
        slot_infos = detail.get("matlStationInfo", {}).get("slotInfos", [])
        for slot in slot_infos:
            if str(slot.get("slotId")) == str(self.zmod_slot):
                return slot
        raise RuntimeError(
            f"Zmod did not return metadata for IFS slot {self.zmod_slot}"
        )

    def _mapped_tool(self):
        try:
            with open(FILE_CONFIG, "r", encoding="utf-8") as config_file:
                mapping = json.load(config_file)
        except FileNotFoundError:
            return "NONE"
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.error("Unable to read Zmod tool-to-slot mapping: %s", exc)
            return "NONE"

        if not isinstance(mapping, list):
            LOGGER.error("Zmod tool-to-slot mapping is not a list")
            return "NONE"

        for tool_index, slot in enumerate(mapping):
            try:
                if int(slot) == self.zmod_slot:
                    return f"T{tool_index}"
            except (TypeError, ValueError):
                LOGGER.error(
                    "Invalid slot value in Zmod tool-to-slot mapping: %r", slot
                )
                return "NONE"
        return "NONE"

    def get_status(self, eventtime=None):
        slot = self._read_slot()
        loaded = bool(self.zmod_ifs.get_port(self.zmod_slot))
        current_slot = self.zmod_color.get_current_channel()
        tool_loaded = (
            current_slot == self.zmod_slot
            and bool(self.zmod_color.get_extruder_sensor())
        )
        color = str(slot.get("materialColor", "#161616"))
        if not color.startswith("#"):
            color = f"#{color}"
        rgb = color[1:7].upper()
        if len(rgb) != 6:
            raise RuntimeError(
                f"Zmod returned invalid color metadata for IFS slot {self.zmod_slot}"
            )
        try:
            int(rgb, 16)
        except ValueError as exc:
            raise RuntimeError(
                f"Zmod returned invalid color metadata for IFS slot {self.zmod_slot}"
            ) from exc
        color = f"#{rgb}"
        material = str(slot.get("materialName", "NONE")).upper()

        status = {
            "name": self.name,
            "unit": self.unit_name,
            "lane": self.lane_index,
            "zmod_slot": self.zmod_slot,
            "extruder": self.extruder_name,
            "map": self._mapped_tool(),
            "load": loaded,
            "prep": loaded,
            "tool_loaded": tool_loaded,
            "loaded_to_hub": False,
            "material": material,
            "spool_id": None,
            "color": color,
            # weight intentionally omitted; Zmod doesn't track it and the UI
            # hides it when absent.
            "runout_lane": "NONE",
            "filament_status": "unknown",
            "filament_status_led": "gray",
            "status": AFCLaneState.LOADED if loaded else AFCLaneState.EMPTY,
        }
        if material not in ("NONE", "N/A", "?", ""):
            status["filament_name"] = material
        return status


class AFCUnit:
    def __init__(self, printer, name, lanes):
        self.printer = printer
        self.name = name
        self.lanes = lanes

    def _handle_ready(self):
        self.lanes = {
            lane.name: lane
            for lane in self.lanes.values()
            if lane.unit_name == self.name
        }

    def get_status(self, eventtime=None):
        return {
            "lanes": list(self.lanes),
            "extruders": [],
            "hubs": [],
            "buffers": [],
        }


def load_config(config):
    return AFC(config)
