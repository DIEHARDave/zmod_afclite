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
# save_variables key for a lane's Spoolman spool ID, set by SET_SPOOL_ID.
SPOOL_VARIABLE_PREFIX = "zmod_afclite_spool_"

# Spoolman is reached through Moonraker's [spoolman] proxy, so its URL is only
# configured once, in Moonraker. NFC tag UIDs live in the spool's "card_uids"
# custom field, the convention shared with the Snapmaker U1 SpoolLink apps.
SPOOLMAN_REMOTE_METHOD = "spoolman_set_active_spool"
# Moonraker can stall for 10-15 s right after boot while it scans files.
SPOOLMAN_TIMEOUT = 30.0
SPOOLMAN_POLL = 2.0
# Zmod briefly reports IFS slots as empty while it starts up, so a lane must
# read empty this long before its spool assignment is cleared.
SPOOLMAN_CLEAR_DELAY = 30.0
CARD_UIDS_FIELD = "card_uids"
CARD_UIDS_FIELD_DEFINITION = {
    "name": "Card UIDs",
    "field_type": "text",
    "order": 1,
    "default_value": json.dumps(""),
}

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


def moonraker_request(url, method, query=None, body=None, timeout=5):
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


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
        # Lane weights and spool IDs are kept in Zmod's save_variables.
        self.save_variables = self.printer.lookup_object("save_variables", None)

    def get(self, eventtime):
        if self.zmod_color is None:
            # Moonraker queries status while Klipper is still starting (e.g.
            # right after FIRMWARE_RESTART), before klippy:ready has run.
            self.zmod_color = self.printer.lookup_object("zmod_color", None)
            if self.zmod_color is None:
                return self._empty_snapshot()
        if self._snapshot is None or eventtime is None or eventtime != self._eventtime:
            self._snapshot = self._read(eventtime)
            self._eventtime = eventtime
        return self._snapshot

    @staticmethod
    def _empty_snapshot():
        """Placeholder until zmod_color is loaded; not cached."""
        return {
            "slot_count": SLOTS_PER_UNIT,
            "unit_count": 1,
            "current_slot": 0,
            "extruder_sensor": False,
            "slots": {},
            "mapping": {},
            "variables": {},
        }

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
            "variables": dict(getattr(self.save_variables, "allVariables", None) or {}),
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
        return moonraker_request(self.url, method, query, body)

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


def resolve_material(material, valid_types):
    """Zmod material type for a Spoolman material: an exact match, else the
    same fallbacks SpoolSync uses (ASA -> ABS, COPE and PLA blends -> PLA)."""
    material = str(material or "").strip().upper()
    valid = [t for t in valid_types if t not in UNSET_MATERIALS]
    if material in valid:
        return material
    if material == "ASA":
        fallback = "ABS"
    elif material == "COPE" or "PLA" in material:
        fallback = "PLA"
    else:
        return None
    return fallback if fallback in valid else None


def spoolman_color(filament):
    """First 6-digit RGB color of a Spoolman filament, or None."""
    colors = [filament.get("color_hex") or ""]
    colors += str(filament.get("multi_color_hexes") or "").split(",")
    for color in colors:
        color = str(color).replace("#", "").strip().upper()[:6]
        try:
            if len(color) == 6:
                int(color, 16)
                return color
        except ValueError:
            pass
    return None


def decode_card_uids(spool):
    """Card UIDs of a Spoolman spool. Spoolman stores custom text fields JSON
    encoded, so the value is usually '"AABBCCDD,11223344"'."""
    raw = (spool.get("extra") or {}).get(CARD_UIDS_FIELD) or ""
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, str):
            raw = decoded
    except ValueError:
        pass
    return [uid.strip().upper() for uid in str(raw).split(",") if uid.strip()]


def normalize_card_uid(uid):
    uid = "".join(ch for ch in str(uid) if ch not in ": -").upper()
    try:
        int(uid, 16)
    except ValueError:
        return None
    return uid


class SpoolmanLink:
    """Lane spool assignments backed by Spoolman.

    SET_SPOOL_ID looks a spool up by ID or NFC card UID through Moonraker's
    [spoolman] proxy, saves its ID for the lane, and writes the spool's color
    and material into the Zmod slot. Mainsail and Fluidd then show the spool's
    details from Spoolman. The spool in the active slot is reported to
    Moonraker as the active spool, so Spoolman tracks the filament used."""

    def __init__(self, printer, state, lanes, moonraker_url, clear_on_empty):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.state = state
        self.lanes = lanes
        self.url = moonraker_url.rstrip("/") + "/server/spoolman/proxy"
        self.clear_on_empty = clear_on_empty
        self.gcode = printer.lookup_object("gcode")
        self.webhooks = printer.lookup_object("webhooks")
        # Spool last reported to Moonraker. Starting at None means a spool set
        # outside this plugin is left alone until a lane with a spool is used.
        self._active = None
        self._loaded = set()
        self._empty_since = {}
        self._timer = None
        self.gcode.register_command(
            "SET_SPOOL_ID", self.cmd_SET_SPOOL_ID,
            desc="Assign a Spoolman spool to an AFC lane by ID or NFC card UID",
        )
        printer.register_event_handler("klippy:ready", self._handle_ready)
        printer.register_event_handler("klippy:disconnect", self._handle_disconnect)

    def available(self):
        """True while Moonraker has a [spoolman] section and is connected."""
        methods = getattr(self.webhooks, "_remote_methods", None) or {}
        return SPOOLMAN_REMOTE_METHOD in methods

    def _handle_ready(self):
        self._timer = self.reactor.register_timer(self._poll, self.reactor.NOW)

    def _handle_disconnect(self):
        if self._timer is not None:
            self.reactor.unregister_timer(self._timer)
            self._timer = None

    # Spoolman requests -----------------------------------------------------

    def _proxy(self, method, path, query=None, body=None):
        """Send a request through Moonraker's Spoolman proxy without blocking
        the reactor, and return Spoolman's response."""
        payload = {"request_method": method, "path": path, "use_v2_response": True}
        if query:
            payload["query"] = urllib.parse.urlencode(query)
        if body is not None:
            payload["body"] = body
        completion = self.reactor.completion()

        def run():
            try:
                result = (moonraker_request(
                    self.url, "POST", body=payload, timeout=SPOOLMAN_TIMEOUT
                ), None)
            except Exception as exc:
                result = (None, exc)
            self.reactor.async_complete(completion, result)

        thread = threading.Thread(target=run, name="zmod_afclite spoolman")
        thread.daemon = True
        thread.start()
        reply, exc = completion.wait(
            self.reactor.monotonic() + SPOOLMAN_TIMEOUT + 1.,
            (None, TimeoutError("no reply from Moonraker")),
        )
        if exc is not None:
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                exc = "Moonraker has no [spoolman] section"
            elif isinstance(exc, (TimeoutError, OSError)) and "timed out" in str(exc):
                exc = "Moonraker did not answer in time (busy?), try again"
            raise self.printer.command_error(f"Spoolman request failed: {exc}")
        result = reply.get("result") or {}
        error = result.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else error
            raise self.printer.command_error(f"Spoolman: {message}")
        return result.get("response")

    def _get_spool(self, spool_id):
        spool = self._proxy("GET", f"/v1/spool/{spool_id}")
        if not isinstance(spool, dict) or "id" not in spool:
            raise self.printer.command_error(f"Spoolman has no spool {spool_id}")
        return spool

    def _all_spools(self):
        spools = self._proxy("GET", "/v1/spool", {"allow_archived": "true"})
        return spools if isinstance(spools, list) else []

    def _find_by_card(self, card_uid):
        for spool in self._all_spools():
            if card_uid in decode_card_uids(spool) and not spool.get("archived"):
                return spool
        raise self.printer.command_error(
            f"No Spoolman spool has card UID {card_uid}. Pair it once with "
            f"SET_SPOOL_ID LANE=<lane> SPOOL_ID=<id> CARD_UID={card_uid}"
        )

    def _set_card_uids(self, spool, uids):
        self._proxy("PATCH", f"/v1/spool/{spool['id']}", body={
            "extra": {CARD_UIDS_FIELD: json.dumps(",".join(uids))},
        })

    def _bind_card(self, spool, card_uid):
        """Add card_uid to the spool and remove it from every other spool, so a
        later scan of the tag resolves to this spool."""
        fields = self._proxy("GET", "/v1/field/spool") or []
        if not any(field.get("key") == CARD_UIDS_FIELD for field in fields):
            self._proxy(
                "POST", f"/v1/field/spool/{CARD_UIDS_FIELD}",
                body=CARD_UIDS_FIELD_DEFINITION,
            )
        uids = decode_card_uids(spool)
        if card_uid not in uids:
            self._set_card_uids(spool, uids + [card_uid])
        for other in self._all_spools():
            other_uids = decode_card_uids(other)
            if other["id"] != spool["id"] and card_uid in other_uids:
                self._set_card_uids(other, [u for u in other_uids if u != card_uid])

    # Lane assignment -------------------------------------------------------

    def _save(self, lane, spool_id):
        if self.state.save_variables is None:
            raise self.printer.command_error(
                "No [save_variables] configured, so the lane spool cannot be saved."
            )
        self.gcode.run_script_from_command(
            f"SAVE_VARIABLE VARIABLE={SPOOL_VARIABLE_PREFIX}{lane.name.lower()} "
            f"VALUE={spool_id}"
        )

    def _apply(self, gcmd, lane, spool):
        """Write the spool's color and material into the lane's Zmod slot."""
        filament = spool.get("filament") or {}
        eventtime = self.reactor.monotonic()
        status = lane.get_status(eventtime)
        valid_types = self.state.zmod_color.get_status(eventtime).get("valid_types", [])
        color = spoolman_color(filament)
        material = resolve_material(filament.get("material"), valid_types)
        if material is None:
            gcmd.respond_info(
                f"Spoolman material {filament.get('material')!r} is not a Zmod "
                f"material; {lane.name} keeps {status['material']}."
            )
            material = status["material"]
            if material in UNSET_MATERIALS:
                material = "?"
        if color is None:
            color = status["color"].replace("#", "")
        self.gcode.run_script_from_command(
            f"CHANGE_ZCOLOR SLOT={lane.zmod_slot} HEX={color} TYPE={material} SILENT=1"
        )

    def cmd_SET_SPOOL_ID(self, gcmd):
        lane = self.lanes.get(gcmd.get("LANE", "").strip().upper())
        if lane is None:
            raise gcmd.error("LANE must be an AFC lane such as E0.")
        spool_id = gcmd.get("SPOOL_ID", "").strip()
        try:
            spool_id = int(spool_id or 0)
        except ValueError:
            raise gcmd.error("SPOOL_ID must be a Spoolman spool ID.")
        card_uid = gcmd.get("CARD_UID", "").strip()
        if card_uid:
            card_uid = normalize_card_uid(card_uid)
            if card_uid is None:
                raise gcmd.error("CARD_UID must be the tag UID in hex.")

        if spool_id <= 0 and not card_uid:
            self._save(lane, 0)
            gcmd.respond_info(f"{lane.name}: Spoolman spool cleared.")
            return
        if not self.available():
            raise gcmd.error(
                "Spoolman is not available. Add a [spoolman] section to "
                "mod_data/user.moonraker.conf and restart Moonraker."
            )

        if spool_id > 0:
            spool = self._get_spool(spool_id)
            if card_uid:
                self._bind_card(spool, card_uid)
        else:
            spool = self._find_by_card(card_uid)
        self._save(lane, spool["id"])
        self._apply(gcmd, lane, spool)
        filament = spool.get("filament") or {}
        vendor = (filament.get("vendor") or {}).get("name")
        name = " ".join(str(part) for part in (vendor, filament.get("name")) if part)
        gcmd.respond_info(f"{lane.name}: Spoolman spool {spool['id']} {name}".rstrip())

    # Active spool ----------------------------------------------------------

    def _poll(self, eventtime):
        if self.state.zmod_color is None or not self.available():
            return eventtime + SPOOLMAN_POLL
        try:
            self._update(self.state.get(eventtime), eventtime)
        except Exception:
            LOGGER.exception("Zmod AFC Lite: Spoolman active spool update failed")
        return eventtime + SPOOLMAN_POLL

    def _update(self, snapshot, eventtime):
        if self.clear_on_empty:
            self._clear_removed(snapshot, eventtime)
        active = None
        current = snapshot["current_slot"]
        if snapshot["extruder_sensor"] and 1 <= current <= snapshot["slot_count"]:
            lane = self.lanes.get(f"E{current - 1}")
            active = lane.spool_id(snapshot) if lane is not None else None
        if active == self._active:
            return
        try:
            self.webhooks.call_remote_method(SPOOLMAN_REMOTE_METHOD, spool_id=active)
        except self.printer.command_error as exc:
            LOGGER.info("Zmod AFC Lite: unable to set active spool: %s", exc)
            return
        self._active = active

    def _clear_removed(self, snapshot, eventtime):
        """Forget a lane's spool once its filament has been taken out of the
        IFS, as AFC does on eject; the next spool there needs its own
        SET_SPOOL_ID."""
        for zmod_slot in range(1, snapshot["slot_count"] + 1):
            slot = snapshot["slots"].get(zmod_slot)
            if slot is None:
                continue
            if slot.get("hasFilament"):
                self._loaded.add(zmod_slot)
                self._empty_since.pop(zmod_slot, None)
                continue
            if zmod_slot not in self._loaded:
                continue
            since = self._empty_since.setdefault(zmod_slot, eventtime)
            if eventtime - since < SPOOLMAN_CLEAR_DELAY:
                continue
            self._loaded.discard(zmod_slot)
            self._empty_since.pop(zmod_slot, None)
            lane = self.lanes.get(f"E{zmod_slot - 1}")
            if lane is not None and lane.spool_id(snapshot):
                LOGGER.info("Zmod AFC Lite: %s emptied, clearing its spool", lane.name)
                self.reactor.register_callback(
                    lambda e, name=lane.name.lower(): self.gcode.run_script(
                        f"SAVE_VARIABLE VARIABLE={SPOOL_VARIABLE_PREFIX}{name} VALUE=0"
                    )
                )


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

        moonraker_url = config.get("moonraker_url", "http://127.0.0.1:7125")
        self.lane_data = None
        if config.getboolean("lane_data", True):
            self.lane_data = LaneDataSync(self.printer, self.state, moonraker_url)
        self.spoolman = SpoolmanLink(
            self.printer, self.state, self.lanes, moonraker_url,
            clear_on_empty=config.getboolean("spoolman_clear_on_empty", True),
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
            "spoolman": self.spoolman.available(),
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
            "spool_id": self.spool_id(state) if active else None,
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
        saved = state["variables"].get(f"{WEIGHT_VARIABLE_PREFIX}{self.name.lower()}")
        try:
            return max(0, round(float(saved)))
        except (TypeError, ValueError):
            return round(self.state.default_weight)

    def spool_id(self, state):
        """Spoolman spool ID saved by SET_SPOOL_ID, or None."""
        saved = state["variables"].get(f"{SPOOL_VARIABLE_PREFIX}{self.name.lower()}")
        try:
            spool_id = int(saved)
        except (TypeError, ValueError):
            return None
        return spool_id if spool_id > 0 else None


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
