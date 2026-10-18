# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
OPC UA Simulator for AWS IoT Greengrass V2

Greengrass component that generates configurable sensor data with pluggable
patterns (sine, steady, drift, duty_cycle, state_machine). Under Greengrass the
configuration is the component's simulatorConfig configuration value, read over
IPC and re-applied live whenever a deployment changes it; the JSON config file
is the fallback (and the only source when run locally). Control commands arrive
from AWS IoT Core through the Greengrass IPC MQTT proxy for runtime anomaly
injection, pause, and resume.

Usage: python simulator.py <config.json>
"""
# The image ships this single file, so the module is deliberately longer than pylint's default.
# pylint: disable=too-many-lines

import asyncio
import functools
import json
import logging
import math
import os
import random
import sys
import threading
import time
from asyncua import Server, ua
from awsiot.greengrasscoreipc.clientv2 import GreengrassCoreIPCClientV2
from awsiot.greengrasscoreipc.model import QOS

NAMESPACE = "urn:opcua-simulator"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("opcua-simulator")
# asyncua logs ~1000 INFO lines while importing its standard address space; keep only warnings
logging.getLogger("asyncua").setLevel(logging.WARNING)

# Thread lock for shared state between MQTT handler and main loop
_lock = threading.Lock()


def load_config() -> dict:
    """Load config from a JSON file."""
    if len(sys.argv) >= 2:
        config_path = sys.argv[1]
        log.info(f"Loading config from file: {config_path}")
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    log.error("Usage: python simulator.py <config.json>")
    sys.exit(1)


# --- Tag data types (config "type" -> OPC UA variant) ---

UA_TYPE_MAP = {
    "double": ua.VariantType.Double,
    "boolean": ua.VariantType.Boolean,
    "string": ua.VariantType.String,
    "int": ua.VariantType.Int64,
}

INITIAL_VALUES = {
    "double": 0.0,
    "boolean": False,
    "string": "",
    "int": 0,
}


# --- Value generators ---

class TagState:  # pylint: disable=too-many-instance-attributes,too-few-public-methods
    """Holds runtime state for one OPC UA tag (config values are cached as attributes by design)."""

    def __init__(self, tag_cfg: dict):
        self.cfg = tag_cfg
        self.name = tag_cfg["name"]
        self.data_type = tag_cfg.get("type", "double")
        if self.data_type not in UA_TYPE_MAP:
            # Fail at startup with a readable message instead of a KeyError on the first write
            raise ValueError(
                f"Tag {self.name}: unsupported type '{self.data_type}' "
                f"(expected one of {', '.join(UA_TYPE_MAP)})"
            )
        self.pattern = tag_cfg.get("pattern", "steady")
        self.node = None  # set after OPC UA node creation
        self.last_value = None  # cache for log sampling

        # Cache config values that never change (avoid repeated dict lookups)
        self.base = tag_cfg.get("base", 50.0)
        self.amplitude = tag_cfg.get("amplitude", 10.0)
        self.noise = tag_cfg.get("noise", 0.5)
        self.phase_offset = tag_cfg.get("phaseOffset", 0.0)
        self.on_probability = tag_cfg.get("onProbability", 0.5)
        self.drift_rate = tag_cfg.get("driftRate", 0.01)
        self.drift_min = tag_cfg.get("min", 0.0)
        self.drift_max = tag_cfg.get("max", 100.0)
        self.states = tag_cfg.get("states", ["ON", "OFF"])
        self.transition_prob = tag_cfg.get("transitionProbability", 0.003)
        self.requires_tag = tag_cfg.get("requiresTag")

        # State for different patterns
        self.bool_state = random.random() < self.on_probability
        self.string_state = self.states[0] if self.states else "ON"
        self.drift_value = self.base

        # Override: when set, generate() returns this value instead of the pattern
        self.override_value = None

    def generate(self, hour: float, device_tags: dict) -> object:
        """Generate a value based on the configured pattern."""
        with _lock:
            override = self.override_value
        if override is not None:
            self.last_value = override
            return override

        # If this tag requires another boolean tag to be True, return zero/off
        if self.requires_tag and self.requires_tag in device_tags:
            dep = device_tags[self.requires_tag]
            if dep.data_type == "boolean" and not dep.bool_state:
                val = 0.0 if self.data_type == "double" else False
                self.last_value = val
                return val

        if self.pattern == "sine":
            value = self.base + self.amplitude * math.sin(
                (hour - 6) * math.pi / 12 + self.phase_offset
            )
            val = round(value + random.gauss(0, self.noise), 2)

        elif self.pattern == "steady":
            val = round(self.base + random.gauss(0, self.noise), 2)

        elif self.pattern == "drift":
            self.drift_value += random.gauss(0, self.drift_rate)
            self.drift_value = max(self.drift_min, min(self.drift_max, self.drift_value))
            val = round(self.drift_value + random.gauss(0, self.noise), 2)

        elif self.pattern == "duty_cycle":
            if random.random() < 0.005:
                self.bool_state = not self.bool_state
            if random.random() < 0.001:
                self.bool_state = random.random() < self.on_probability
            val = self.bool_state

        elif self.pattern == "state_machine":
            if random.random() < self.transition_prob:
                self.string_state = random.choice(self.states)
            val = self.string_state

        else:
            val = 0.0

        self.last_value = val
        return val


class DeviceState:  # pylint: disable=too-few-public-methods
    """Holds all tag states for one device (a plain container, no behavior needed)."""

    def __init__(self, device_cfg: dict):
        self.name = device_cfg["name"]
        self.folder = None  # leaf OPC UA folder, set after node creation
        self.tags = {}
        for tag_cfg in device_cfg.get("tags", []):
            tag = TagState(tag_cfg)
            self.tags[tag.name] = tag


# --- Config validation and sources ---

# Component configuration key that holds the config (the recipe default is artifacts/config.json)
CONFIG_KEY = "simulatorConfig"
REQUIRED_CONFIG_KEYS = ("endpoint", "updateIntervalSeconds", "timeAcceleration", "devices")


def _is_positive_number(value) -> bool:
    """True for a finite number > 0 (bool excluded; Greengrass may turn ints into floats)."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and _is_finite_double(value) and value > 0)


def _device_error(index: int, device_cfg, seen: set):  # pylint: disable=too-many-return-statements
    """Why one entry of "devices" cannot run, or None (one early return per check)."""
    if not isinstance(device_cfg, dict):
        return f"devices[{index}] must be an object"
    name = device_cfg.get("name")
    if not isinstance(name, str) or not name:
        return f"devices[{index}] needs a non-empty string 'name'"
    if name in seen:
        return f"duplicate device name '{name}'"
    seen.add(name)
    tags = device_cfg.get("tags", [])
    if not isinstance(tags, list):
        return f"device {name}: 'tags' must be a list"
    tag_names = set()
    for j, tag_cfg in enumerate(tags):
        if not isinstance(tag_cfg, dict):
            return f"device {name}: tags[{j}] must be an object"
        tag_name = tag_cfg.get("name")
        if not isinstance(tag_name, str) or not tag_name:
            return f"device {name}: tags[{j}] needs a non-empty string 'name'"
        if tag_name in tag_names:
            return f"device {name}: duplicate tag name '{tag_name}'"
        tag_names.add(tag_name)
        try:
            TagState(tag_cfg)
        except (ValueError, TypeError) as e:
            return f"device {name}: {e}"
    return None


def validate_config(cfg):  # pylint: disable=too-many-return-statements  # one early return per check
    """
    Return why ``cfg`` cannot run, or None when it is valid.

    The same check gates every config source: the component configuration at
    startup, the config file fallback, and every live configuration update.
    """
    if not isinstance(cfg, dict):
        return f"config must be a JSON object, got {type(cfg).__name__}"
    missing = [key for key in REQUIRED_CONFIG_KEYS if key not in cfg]
    if missing:
        return f"missing required key(s): {', '.join(missing)}"
    if not isinstance(cfg["endpoint"], str) or not cfg["endpoint"]:
        return "'endpoint' must be a non-empty string"
    for key in ("updateIntervalSeconds", "timeAcceleration"):
        if not _is_positive_number(cfg[key]):
            return f"'{key}' must be a positive number"
    if not isinstance(cfg["devices"], list):
        return "'devices' must be a list"
    seen = set()
    for index, device_cfg in enumerate(cfg["devices"]):
        error = _device_error(index, device_cfg, seen)
        if error:
            return error
    return None


def config_counts(devices) -> tuple[int, int]:
    """(number of devices, number of tags) of a list of DeviceState."""
    return len(devices), sum(len(dev.tags) for dev in devices)


def fetch_component_config(client):
    """
    Read the simulatorConfig component configuration over IPC.

    Returns (config, None), or (None, reason) when it is missing or empty. A
    component reading its own configuration needs no accessControl policy.
    """
    try:
        response = client.get_configuration(key_path=[CONFIG_KEY])
    except Exception as e:
        return None, f"GetConfiguration failed ({type(e).__name__}: {e})"
    value = response.value
    # The Nucleus wraps a leaf value (for example a string merged by mistake) as
    # {key: value}; unwrap it so validation reports the real type.
    if isinstance(value, dict) and set(value) == {CONFIG_KEY}:
        value = value[CONFIG_KEY]
    if not value:
        return None, f"{CONFIG_KEY} is missing or empty"
    return value, None


def select_startup_config(client) -> dict:
    """
    Choose the startup config: the component configuration when IPC is
    available and it is valid, otherwise the config file. Exits when the file
    is invalid too.
    """
    if client is not None:
        cfg, reason = fetch_component_config(client)
        if reason is None:
            reason = validate_config(cfg)
        if reason is None:
            log.info(f"CONFIG: Using the component configuration ({CONFIG_KEY})")
            return cfg
        log.warning(f"CONFIG: Component configuration not usable ({reason}); using the config file instead")
    else:
        log.info("CONFIG: Greengrass IPC not available — using the config file")
    cfg = load_config()
    reason = validate_config(cfg)
    if reason is not None:
        log.error(f"CONFIG: Invalid config file: {reason}")
        sys.exit(1)
    return cfg


# --- MQTT Control ---

# Global state shared between MQTT handler and main loop
_devices_by_name: dict = {}  # populated in main()
_paused = False
_ipc_client = None  # keeps the Greengrass IPC client (and its subscription) alive

IPC_CONNECT_ATTEMPTS = 5
IPC_CONNECT_RETRY_SECONDS = 5
# SubscribeToIoTCore is retried forever with exponential backoff: the Nucleus
# can reject it (UnauthorizedError) until a deployment corrects accessControl,
# and such a configuration change does not restart the component.
IPC_SUBSCRIBE_RETRY_SECONDS = 5
IPC_SUBSCRIBE_RETRY_MAX_SECONDS = 60

# Value range of the OPC UA Int64 variant used for "int" tags
INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1


def _is_finite_double(value) -> bool:
    """True when ``value`` converts to a finite float (JSON allows huge ints, NaN and Infinity)."""
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _validate_override_value(tag: TagState, value) -> bool:
    """Validate that the override value matches the tag's data type and fits its OPC UA variant."""
    if isinstance(value, bool):
        # bool is a subclass of int in Python; only boolean tags may accept it
        return tag.data_type == "boolean"
    if tag.data_type == "boolean":
        return False
    if tag.data_type == "double":
        return isinstance(value, (int, float)) and _is_finite_double(value)
    if tag.data_type == "int":
        return isinstance(value, int) and INT64_MIN <= value <= INT64_MAX
    if tag.data_type == "string":
        return isinstance(value, str)
    return True


def coerce_value(data_type: str, value):
    """Coerce a generated/override value to the Python type the OPC UA variant expects."""
    if data_type == "double":
        return float(value)
    if data_type == "int":
        return int(value)
    return value


def handle_control_message(payload: dict):
    """
    Handle a control message from the MQTT topic.

    Commands:
      {"action": "inject_anomaly", "device": "...", "tag": "...", "value": ...}
      {"action": "clear_anomaly", "device": "...", "tag": "..."}
      {"action": "clear_all"}
      {"action": "pause"}
      {"action": "resume"}
    """
    global _paused  # pylint: disable=global-statement  # module-level flag shared with the update loop
    action = payload.get("action", "")

    if action == "inject_anomaly":
        device_name = payload.get("device", "")
        tag_name = payload.get("tag", "")
        value = payload.get("value")
        dev = _devices_by_name.get(device_name)
        if not dev or tag_name not in dev.tags:
            log.warning(f"CONTROL: Device/tag not found: {device_name}/{tag_name}")
            return
        tag = dev.tags[tag_name]
        if not _validate_override_value(tag, value):
            # The value itself is not logged: it may be arbitrarily large
            log.warning(
                f"CONTROL: Invalid value for {device_name}/{tag_name} "
                f"(expected {tag.data_type} within the OPC UA range, got {type(value).__name__})"
            )
            return
        with _lock:
            tag.override_value = value
        log.info(f"CONTROL: Injected anomaly {device_name}/{tag_name} = {value}")

    elif action == "clear_anomaly":
        device_name = payload.get("device", "")
        tag_name = payload.get("tag", "")
        dev = _devices_by_name.get(device_name)
        if dev and tag_name in dev.tags:
            with _lock:
                dev.tags[tag_name].override_value = None
            log.info(f"CONTROL: Cleared anomaly {device_name}/{tag_name}")
        else:
            log.warning(f"CONTROL: Device/tag not found: {device_name}/{tag_name}")

    elif action == "clear_all":
        with _lock:
            for dev in _devices_by_name.values():
                for tag in dev.tags.values():
                    tag.override_value = None
        log.info("CONTROL: Cleared all anomalies")

    elif action == "pause":
        with _lock:
            _paused = True
        log.info("CONTROL: Simulation paused")

    elif action == "resume":
        with _lock:
            _paused = False
        log.info("CONTROL: Simulation resumed")

    else:
        log.warning(f"CONTROL: Unknown action '{action}'")


def ipc_available() -> bool:
    """
    True when the Greengrass Nucleus has provided IPC credentials to this process.

    The Nucleus sets SVCUID and AWS_GG_NUCLEUS_DOMAIN_SOCKET_FILEPATH_FOR_COMPONENT
    for every component lifecycle script; the recipe forwards both into the
    container and bind-mounts the socket at the same path. Locally (no Nucleus)
    they are absent and MQTT control is simply disabled. The SVCUID value is an
    authentication token and is never logged.
    """
    if not os.environ.get("SVCUID"):
        return False
    socket_path = os.environ.get("AWS_GG_NUCLEUS_DOMAIN_SOCKET_FILEPATH_FOR_COMPONENT")
    return bool(socket_path) and os.path.exists(socket_path)


def make_control_handler(ipc, status_topic: str, thing_name: str):
    """
    Build the SubscribeToIoTCore stream callback for the control topic.

    Each message is decoded as JSON, dispatched to handle_control_message and
    acknowledged on the status topic. The V2 client runs this callback on an
    executor thread, so the blocking publish is safe here.
    """

    def on_stream_event(event):
        try:
            raw = event.message.payload
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
            if not isinstance(payload, dict):
                log.warning(f"CONTROL: Ignoring non-object payload ({type(payload).__name__})")
                return
            handle_control_message(payload)
            action = payload.get("action", "unknown")
            ipc.publish_to_iot_core(
                topic_name=status_topic,
                qos=QOS.AT_LEAST_ONCE,
                payload=json.dumps({"ack": action, "thingName": thing_name}),
            )
            log.info(f"CONTROL: Published ack '{action}' to {status_topic}")
        except json.JSONDecodeError as e:
            log.error(f"CONTROL: Invalid JSON payload: {e}")
        except Exception as e:
            log.error(f"CONTROL: Failed to process message: {e}")

    return on_stream_event


def _on_stream_error(error: Exception) -> bool:
    """Log a stream error; returning False keeps the subscription open."""
    log.error(f"CONTROL: IPC subscription stream error: {error}")
    return False


def _close_ipc_client(client):
    """Release a (possibly already dead) IPC client without blocking on its executor."""
    try:
        client.close(executor_wait=False)
    except Exception as e:
        log.debug(f"CONTROL: Ignoring error while closing stale IPC client: {e}")


def start_ipc_control_listener(control_topic: str, status_topic: str, thing_name: str,
                               client_factory=GreengrassCoreIPCClientV2):
    """
    Subscribe to the control topic through the Greengrass IPC MQTT proxy.

    Runs in a daemon thread so a slow Nucleus start never blocks the OPC UA
    server. If the Nucleus later closes an established subscription stream
    (for example an IPC connection drop without a component restart), a new
    thread reconnects and re-subscribes with the same backoff, so control
    recovers on its own. Returns the thread, or None when IPC is not
    available (local run). ``client_factory`` exists so tests can inject a
    fake client.
    """
    if not ipc_available():
        log.info("CONTROL: Greengrass IPC not available (SVCUID / IPC socket not provided) — MQTT control disabled")
        return None

    def _connect():
        for attempt in range(1, IPC_CONNECT_ATTEMPTS + 1):
            try:
                return client_factory()
            except Exception as e:
                log.warning(f"CONTROL: IPC connect attempt {attempt}/{IPC_CONNECT_ATTEMPTS} failed: {e}")
                if attempt < IPC_CONNECT_ATTEMPTS:
                    time.sleep(IPC_CONNECT_RETRY_SECONDS)
        log.error("CONTROL: Could not connect to Greengrass IPC — MQTT control disabled")
        return None

    def _subscribe(client):
        # The SDK also fires on_stream_closed when the Nucleus rejects the
        # subscribe request itself; that case is handled by the retry loop in
        # _ipc_thread, so only an established subscription triggers a reconnect.
        subscribed = threading.Event()

        def on_stream_closed():
            if not subscribed.wait(IPC_SUBSCRIBE_RETRY_SECONDS):
                return
            log.warning("CONTROL: IPC subscription stream closed — reconnecting")
            _start_thread()

        client.subscribe_to_iot_core(
            topic_name=control_topic,
            qos=QOS.AT_LEAST_ONCE,
            on_stream_event=make_control_handler(client, status_topic, thing_name),
            on_stream_error=_on_stream_error,
            on_stream_closed=on_stream_closed,
        )
        subscribed.set()

    def _announce_online(client):
        # Kept outside the retried subscribe: a failed announcement must not
        # add a second subscription to the same client. Control still works
        # without it (acks are published per message).
        try:
            client.publish_to_iot_core(
                topic_name=status_topic,
                qos=QOS.AT_LEAST_ONCE,
                payload=json.dumps({
                    "status": "online",
                    "thingName": thing_name,
                    "controlTopic": control_topic,
                }),
            )
        except Exception as e:
            log.error(f"CONTROL: Could not publish online status to '{status_topic}' ({type(e).__name__}: {e})")

    def _ipc_thread():
        global _ipc_client  # pylint: disable=global-statement  # keeps the client/subscription alive
        if _ipc_client is not None:
            _close_ipc_client(_ipc_client)
            _ipc_client = None
        client = _connect()
        if client is None:
            return
        _ipc_client = client
        delay = IPC_SUBSCRIBE_RETRY_SECONDS
        attempt = 0
        while True:
            attempt += 1
            try:
                _subscribe(client)
            except Exception as e:
                if _ipc_client is not client:
                    return  # a reconnect thread has replaced this client
                log.warning(
                    f"CONTROL: IPC subscribe attempt {attempt} to '{control_topic}' failed "
                    f"({type(e).__name__}: {e}); retrying in {delay}s"
                )
                time.sleep(delay)
                delay = min(delay * 2, IPC_SUBSCRIBE_RETRY_MAX_SECONDS)
                continue
            log.info(f"CONTROL: Connected to Greengrass IPC, subscribed to '{control_topic}'")
            _announce_online(client)
            return

    def _start_thread():
        t = threading.Thread(target=_ipc_thread, name="ipc-control", daemon=True)
        t.start()
        return t

    return _start_thread()


# --- OPC UA Server ---

# Errors from a single tag's coercion or write that are logged and skipped
# instead of stopping the update loop.
NON_FATAL_WRITE_ERRORS = (ua.UaStatusCodeError, OverflowError, ValueError)

# Cache for folder lookups during node creation
_folder_cache: dict = {}


async def create_device_nodes(server, idx, objects, device: DeviceState, writable: bool = False):
    """
    Create OPC UA folder + variable nodes for a device, supporting nested paths.

    Variables are read-only for clients unless ``writable`` is True
    (config key ``writableTags``).
    """
    # pylint: disable=unused-argument  # ``server`` is kept for call-site compatibility
    parts = device.name.split("/")
    parent = objects
    path_so_far = ""

    for part in parts:
        path_so_far = f"{path_so_far}/{part}" if path_so_far else part
        if path_so_far in _folder_cache:
            parent = _folder_cache[path_so_far]
        else:
            parent = await parent.add_folder(idx, part)
            _folder_cache[path_so_far] = parent
    device.folder = parent

    # Create tag variables under the leaf folder
    for tag in device.tags.values():
        await add_tag_node(idx, parent, tag, writable)


async def add_tag_node(idx, folder, tag: TagState, writable: bool):
    """Create the OPC UA variable for one tag under ``folder``."""
    # TagState has already rejected unsupported types, so both lookups are exact
    node = await folder.add_variable(
        idx, tag.name, INITIAL_VALUES[tag.data_type], UA_TYPE_MAP[tag.data_type]
    )
    if writable:
        await node.set_writable()
    tag.node = node


async def update_device(device: DeviceState, hour: float):
    """
    Update all tag values for a device.

    Every write carries an explicit variant type so an int override on a Double
    node (or the generator's float on an Int64 node) cannot cause a type
    mismatch. A value that cannot be coerced (e.g. an int too large for a
    float) or a write rejected by the server is logged and skipped; no single
    tag can stop the simulation loop.
    """
    tags = []
    coros = []
    for tag in (t for t in device.tags.values() if t.node):
        try:
            value = coerce_value(tag.data_type, tag.generate(hour, device.tags))
        except (OverflowError, ValueError) as e:
            log.error(f"Skipping {device.name}/{tag.name}: cannot coerce value to {tag.data_type}: {e}")
            continue
        tags.append(tag)
        coros.append(tag.node.write_value(value, UA_TYPE_MAP[tag.data_type]))

    results = await asyncio.gather(*coros, return_exceptions=True)
    for tag, result in zip(tags, results):
        if isinstance(result, NON_FATAL_WRITE_ERRORS):
            log.error(f"Write failed for {device.name}/{tag.name}: {type(result).__name__}: {result}")
        elif isinstance(result, BaseException):
            raise result


class Simulation:  # pylint: disable=too-many-instance-attributes
    """
    The running OPC UA server and the configuration it serves.

    The update loop and apply_config both run on the asyncio loop; ``lock``
    keeps a live configuration change from interleaving with an update pass.
    """

    def __init__(self, server, idx, config: dict, devices: list):
        self.server = server
        self.idx = idx
        self.config = config
        self.devices = devices
        # Bound at startup; a live change only logs a warning
        self.endpoint = config["endpoint"]
        self.control_topic = config.get("controlTopic")
        self.interval = config["updateIntervalSeconds"]
        self.time_accel = config["timeAcceleration"]
        self.writable = bool(config.get("writableTags", False))
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self._clock_start = time.monotonic()
        self._hour_offset = 0.0

    def hour(self) -> float:
        """Current simulated hour of the day (0-24)."""
        return (self._hour_offset + (time.monotonic() - self._clock_start) / self.time_accel) % 24

    def set_time_acceleration(self, time_accel):
        """Change the simulated time speed without a jump in the simulated hour."""
        self._hour_offset = self.hour()
        self._clock_start = time.monotonic()
        self.time_accel = time_accel

    def set_interval(self, interval):
        """Change the update interval and wake the loop, so it applies now rather than after the old interval."""
        self.interval = interval
        self.wake.set()

    async def sleep_until(self, deadline: float) -> float:
        """Sleep until ``deadline`` (monotonic) or an interval change; returns the next schedule base."""
        try:
            await asyncio.wait_for(self.wake.wait(), timeout=max(0.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            return deadline
        self.wake.clear()
        return time.monotonic()


async def build_server(config: dict) -> Simulation:
    """Create and configure the OPC UA server and all device nodes from config."""
    server = Server()
    await server.init()
    server.set_endpoint(config["endpoint"])
    # Sample simulator: anonymous, unencrypted access only (explicit, not asyncua's default list).
    # set_identity_tokens replaces the deprecated set_security_IDs(["Anonymous"]).
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    server.set_identity_tokens([ua.AnonymousIdentityToken])
    server.set_server_name("OPC UA Simulator for AWS IoT Greengrass V2")
    idx = await server.register_namespace(NAMESPACE)
    objects = server.nodes.objects
    writable = config.get("writableTags", False)

    devices = []
    for dev_cfg in config["devices"]:
        dev = DeviceState(dev_cfg)
        await create_device_nodes(server, idx, objects, dev, writable=writable)
        devices.append(dev)
    return Simulation(server, idx, config, devices)


async def _delete_nodes(server, nodes):
    """Delete OPC UA nodes, and everything below them, from the address space."""
    nodes = [node for node in nodes if node is not None]
    if nodes:
        await server.delete_nodes(nodes, recursive=True)


async def _prune_folders(server, device_names):
    """Delete the cached device folders that no device in ``device_names`` uses any more."""
    needed = set()
    for name in device_names:
        parts = name.split("/")
        needed.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    unused = [path for path in _folder_cache if path not in needed]
    unused_set = set(unused)
    # Deleting a folder deletes everything below it, so delete only the top-most unused ones
    top = [path for path in unused if "/" not in path or path.rsplit("/", 1)[0] not in unused_set]
    await _delete_nodes(server, [_folder_cache[path] for path in top])
    for path in unused:
        del _folder_cache[path]


async def _apply_device_tags(state: Simulation, dev: DeviceState, dev_cfg: dict, writable: bool,
                             changes: dict):
    """
    Bring the tags of an existing device in line with ``dev_cfg``.

    An unchanged tag keeps its state, including an injected anomaly. A changed
    tag gets a fresh TagState; when its type is unchanged it keeps its OPC UA
    node (client subscriptions continue) and its anomaly, otherwise it gets a
    new node and keeps the anomaly only if the value fits the new type.
    Removed tags lose their node and their anomaly.
    """
    new_tags = {}
    stale_nodes = []
    carried = []  # (old, new) pairs whose anomaly is carried over at the swap
    for tag_cfg in dev_cfg.get("tags", []):
        old = dev.tags.get(tag_cfg["name"])
        if old is not None and old.cfg == tag_cfg:
            new_tags[old.name] = old
            continue
        tag = TagState(tag_cfg)
        if old is None:
            changes["added"] += 1
        else:
            changes["changed"] += 1
            carried.append((old, tag))
            if old.data_type == tag.data_type:
                tag.node = old.node
            else:
                stale_nodes.append(old.node)
        new_tags[tag.name] = tag
    removed = [tag for name, tag in dev.tags.items() if name not in new_tags]
    changes["removed"] += len(removed)
    await _delete_nodes(state.server, stale_nodes + [tag.node for tag in removed])
    for tag in new_tags.values():
        if tag.node is None:
            await add_tag_node(state.idx, dev.folder, tag, writable)
    # Read the old anomalies and swap in one step under _lock, so an
    # inject_anomaly arriving on the IPC thread during the awaits above is not lost
    with _lock:
        for old, tag in carried:
            override = old.override_value
            if override is not None and _validate_override_value(tag, override):
                tag.override_value = override
        dev.tags = new_tags


async def apply_config(state: Simulation, cfg: dict):
    """
    Apply a validated config to the running server without a restart.

    Devices and tags are added to, removed from and changed in the OPC UA
    address space; updateIntervalSeconds, timeAcceleration and writableTags
    take effect at once. The pause state is kept, and so are injected
    anomalies on tags that still exist. The endpoint and controlTopic are bound
    at startup: a change is logged as a warning and needs a component restart.
    Returns the names of those settings that differ from what is running.
    """
    restart_required = []
    if cfg["endpoint"] != state.endpoint:
        restart_required.append("endpoint")
        log.warning(f"CONFIG: endpoint changes need a component restart; still serving {state.endpoint}")
    if cfg.get("controlTopic") != state.control_topic:
        restart_required.append("controlTopic")
        log.warning("CONFIG: controlTopic changes need a component restart; still using the startup control topic")
    writable = bool(cfg.get("writableTags", False))
    changes = {"added": 0, "removed": 0, "changed": 0}

    async with state.lock:
        old_devices = {dev.name: dev for dev in state.devices}
        new_names = [dev_cfg["name"] for dev_cfg in cfg["devices"]]
        # Removed devices first, so their folders are gone before new devices are added
        removed = [dev for name, dev in old_devices.items() if name not in new_names]
        await _delete_nodes(state.server, [tag.node for dev in removed for tag in dev.tags.values()])
        changes["removed"] += sum(len(dev.tags) for dev in removed)
        await _prune_folders(state.server, new_names)

        devices = []
        for dev_cfg in cfg["devices"]:
            dev = old_devices.get(dev_cfg["name"])
            if dev is None:
                dev = DeviceState(dev_cfg)
                await create_device_nodes(state.server, state.idx, state.server.nodes.objects, dev,
                                          writable=writable)
                changes["added"] += len(dev.tags)
            else:
                await _apply_device_tags(state, dev, dev_cfg, writable, changes)
            devices.append(dev)

        if writable != state.writable:
            for tag in (t for dev in devices for t in dev.tags.values()):
                await tag.node.set_writable(writable)
            state.writable = writable
        state.devices = devices
        state.config = cfg
        register_devices(devices)
        if cfg["timeAcceleration"] != state.time_accel:
            state.set_time_acceleration(cfg["timeAcceleration"])
        if cfg["updateIntervalSeconds"] != state.interval:
            state.set_interval(cfg["updateIntervalSeconds"])

    n_devices, n_tags = config_counts(devices)
    log.info(
        f"CONFIG: Applied configuration update: {n_devices} devices with {n_tags} tags "
        f"(tags added {changes['added']}, removed {changes['removed']}, changed {changes['changed']}); "
        f"interval={state.interval}s, time_accel={state.time_accel}x"
    )
    return restart_required


def register_devices(devices: list[DeviceState]):
    """Make ``devices`` the lookup table the MQTT control handler uses."""
    global _devices_by_name  # pylint: disable=global-statement  # lookup table shared with the control handler
    _devices_by_name = {dev.name: dev for dev in devices}


def thing_and_status_topic() -> tuple[str, str]:
    """The core's thing name and the status topic the simulator publishes to."""
    thing_name = os.environ.get("AWS_IOT_THING_NAME", "local")
    return thing_name, f"simulator/{thing_name}/status"


def start_control(config: dict, devices: list[DeviceState]):
    """Register the devices for the control handler and start the IPC control listener."""
    register_devices(devices)

    # MQTT control goes through the Greengrass IPC MQTT proxy
    thing_name, status_topic = thing_and_status_topic()
    control_topic = config.get("controlTopic", f"simulator/{thing_name}/control")
    return start_ipc_control_listener(control_topic, status_topic, thing_name)


# --- Live component configuration ---

# One deployment fires an update event per changed key; wait this long after the
# last event before reading the whole simulatorConfig again.
CONFIG_RELOAD_DELAY_SECONDS = 1.0


def connect_config_client(client_factory=GreengrassCoreIPCClientV2):
    """
    Open the IPC connection used to read and watch the component configuration
    and to publish config status events. Returns None locally (no Nucleus).
    """
    if not ipc_available():
        return None
    for attempt in range(1, IPC_CONNECT_ATTEMPTS + 1):
        try:
            return client_factory()
        except Exception as e:
            log.warning(f"CONFIG: IPC connect attempt {attempt}/{IPC_CONNECT_ATTEMPTS} failed: {e}")
            if attempt < IPC_CONNECT_ATTEMPTS:
                time.sleep(IPC_CONNECT_RETRY_SECONDS)
    log.error("CONFIG: Could not connect to Greengrass IPC — live configuration updates disabled")
    return None


def _on_config_stream_error(error: Exception) -> bool:
    """Log a configuration stream error; returning False keeps the subscription open."""
    log.error(f"CONFIG: IPC configuration update stream error: {error}")
    return False


class ConfigWatcher:  # pylint: disable=too-many-instance-attributes
    """
    Applies simulatorConfig changes from Greengrass deployments while the simulator runs.

    SubscribeToConfigurationUpdate callbacks arrive on an IPC executor thread
    and are marshalled onto the asyncio loop with call_soon_threadsafe. Events
    are coalesced for CONFIG_RELOAD_DELAY_SECONDS, then the whole simulatorConfig
    is read again with GetConfiguration, checked with validate_config (the same
    check used at startup) and applied with apply_config. Each outcome is
    published to the status topic as a config_applied or config_rejected event.
    """

    def __init__(self, client, state: Simulation, loop, status_topic: str, thing_name: str,
                 client_factory=GreengrassCoreIPCClientV2):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        self.client = client
        self.state = state
        self.loop = loop
        self.status_topic = status_topic
        self.thing_name = thing_name
        self.client_factory = client_factory
        self._reload_handle = None
        self._reload_lock = asyncio.Lock()
        self._tasks = set()

    # IPC thread side

    def start(self):
        """Subscribe in a daemon thread (the IPC calls block); returns the thread."""
        return self._start_thread(reconnect=False)

    def _start_thread(self, reconnect: bool):
        thread = threading.Thread(target=self._subscribe, args=(reconnect,), name="ipc-config", daemon=True)
        thread.start()
        return thread

    def _on_stream_event(self, event):
        """SubscribeToConfigurationUpdate callback (IPC executor thread)."""
        update = getattr(event, "configuration_update_event", None)
        log.info(f"CONFIG: Configuration update event for {getattr(update, 'key_path', None)}")
        self.loop.call_soon_threadsafe(self.request_reload)

    def _make_on_stream_closed(self, subscribed: threading.Event):
        def on_stream_closed():
            # The SDK also calls this when the subscribe request itself fails;
            # the retry loop handles that, so only an established stream reconnects.
            if not subscribed.wait(IPC_SUBSCRIBE_RETRY_SECONDS):
                return
            log.warning("CONFIG: Configuration update stream closed — reconnecting")
            self._start_thread(reconnect=True)
        return on_stream_closed

    def _subscribe(self, reconnect: bool):
        """Subscribe to simulatorConfig updates, retrying with backoff until it succeeds."""
        if reconnect:
            _close_ipc_client(self.client)
            client = connect_config_client(self.client_factory)
            if client is None:
                return
            self.client = client
        delay = IPC_SUBSCRIBE_RETRY_SECONDS
        attempt = 0
        while True:
            attempt += 1
            subscribed = threading.Event()
            try:
                self.client.subscribe_to_configuration_update(
                    key_path=[CONFIG_KEY],
                    on_stream_event=self._on_stream_event,
                    on_stream_error=_on_config_stream_error,
                    on_stream_closed=self._make_on_stream_closed(subscribed),
                )
            except Exception as e:
                log.warning(
                    f"CONFIG: Subscribe attempt {attempt} to configuration updates failed "
                    f"({type(e).__name__}: {e}); retrying in {delay}s"
                )
                time.sleep(delay)
                delay = min(delay * 2, IPC_SUBSCRIBE_RETRY_MAX_SECONDS)
                continue
            subscribed.set()
            log.info(f"CONFIG: Subscribed to component configuration updates ({CONFIG_KEY})")
            # Catch a change deployed between the startup read (or a lost stream) and now
            self.loop.call_soon_threadsafe(self.request_reload)
            return

    # asyncio loop side

    def request_reload(self):
        """Schedule a reload, restarting the coalescing delay (asyncio loop thread only)."""
        if self._reload_handle is not None:
            self._reload_handle.cancel()
        self._reload_handle = self.loop.call_later(CONFIG_RELOAD_DELAY_SECONDS, self._start_reload)

    def _start_reload(self):
        self._reload_handle = None
        task = self.loop.create_task(self.reload())
        self._tasks.add(task)  # keep a reference until it finishes
        task.add_done_callback(self._tasks.discard)

    async def reload(self):
        """Read, validate and apply the current simulatorConfig, then publish the outcome."""
        async with self._reload_lock:
            cfg, reason = await self.loop.run_in_executor(None, fetch_component_config, self.client)
            if reason is None:
                reason = validate_config(cfg)
            if reason is not None:
                log.error(f"CONFIG: Rejected configuration update ({reason}); keeping the current configuration")
                await self.publish_event("config_rejected", reason)
                return
            if cfg == self.state.config:
                log.info("CONFIG: simulatorConfig unchanged; nothing to apply")
                return
            try:
                restart_required = await apply_config(self.state, cfg)
            except Exception as e:
                reason = f"apply failed ({type(e).__name__}: {e})"
                log.error(f"CONFIG: Could not apply configuration update: {reason}")
                await self.publish_event("config_rejected", reason)
                return
            await self.publish_event("config_applied", restart_required=restart_required)

    def status_payload(self, event: str, reason=None, restart_required=None) -> dict:
        """Status topic message for a config event; counts are of the configuration now running."""
        devices, tags = config_counts(self.state.devices)
        payload = {"event": event, "thingName": self.thing_name, "devices": devices, "tags": tags}
        if restart_required:
            payload["restartRequired"] = restart_required
        if reason is not None:
            payload["reason"] = reason
        return payload

    async def publish_event(self, event: str, reason=None, restart_required=None):
        """Publish a config event to the status topic (the blocking IPC call runs in an executor)."""
        publish = functools.partial(
            self.client.publish_to_iot_core,
            topic_name=self.status_topic,
            qos=QOS.AT_LEAST_ONCE,
            payload=json.dumps(self.status_payload(event, reason, restart_required)),
        )
        try:
            await self.loop.run_in_executor(None, publish)
            log.info(f"CONFIG: Published '{event}' to {self.status_topic}")
        except Exception as e:
            log.error(f"CONFIG: Could not publish '{event}' to '{self.status_topic}' ({type(e).__name__}: {e})")


def log_sample(device: DeviceState, hour: float, paused: bool):
    """Log up to three current tag values of a device as a progress heartbeat."""
    samples = []
    for tag in list(device.tags.values())[:3]:
        if tag.last_value is not None:
            samples.append(f"{tag.name}={tag.last_value}")
    status = " [PAUSED]" if paused else ""
    log.info(f"[sim {hour:05.2f}h{status}] {device.name}: {', '.join(samples)}")


async def run_loop(state: Simulation):
    """Write new values for every tag each interval, reading the live configuration each tick."""
    next_tick = time.monotonic()
    tick = 0
    while True:
        with _lock:
            paused = _paused

        async with state.lock:
            hour = state.hour()
            if not paused:
                for dev in state.devices:
                    await update_device(dev, hour)

            tick += 1
            if tick % 10 == 0 and state.devices:
                log_sample(state.devices[0], hour, paused)

        # Fixed-rate schedule: compensate for the time spent writing tags
        next_tick = await state.sleep_until(next_tick + state.interval)


async def main():
    """Entry point: choose the config, build the server, start the listeners, run the update loop."""
    config_client = connect_config_client()
    config = select_startup_config(config_client)

    state = await build_server(config)
    n_devices, n_tags = config_counts(state.devices)

    log.info(f"Created {n_devices} devices with {n_tags} tags")
    log.info(f"Config: interval={state.interval}s, time_accel={state.time_accel}x")
    log.info(f"Endpoint: {state.endpoint}")

    start_control(config, state.devices)

    async with state.server:
        log.info("Server running. Press Ctrl+C to stop.")
        if config_client is not None:
            thing_name, status_topic = thing_and_status_topic()
            ConfigWatcher(config_client, state, asyncio.get_running_loop(), status_topic, thing_name).start()
        await run_loop(state)


if __name__ == "__main__":
    asyncio.run(main())
