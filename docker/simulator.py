# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
OPC UA Simulator for AWS IoT Greengrass V2

Greengrass component that generates configurable sensor data with pluggable
patterns (sine, steady, drift, duty_cycle, state_machine). Configuration is
read from a JSON config file. Control commands arrive from AWS IoT Core through
the Greengrass IPC MQTT proxy for runtime anomaly injection, pause, and resume.

Usage: python simulator.py <config.json>
"""

import asyncio
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
        self.tags = {}
        for tag_cfg in device_cfg.get("tags", []):
            tag = TagState(tag_cfg)
            self.tags[tag.name] = tag


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

    # Create tag variables under the leaf folder
    for tag in device.tags.values():
        # TagState has already rejected unsupported types, so both lookups are exact
        node = await parent.add_variable(
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


async def build_server(config: dict) -> tuple[Server, list[DeviceState]]:
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
    return server, devices


def start_control(config: dict, devices: list[DeviceState]):
    """Register the devices for the control handler and start the IPC control listener."""
    global _devices_by_name  # pylint: disable=global-statement  # lookup table shared with the control handler
    _devices_by_name = {dev.name: dev for dev in devices}

    # MQTT control goes through the Greengrass IPC MQTT proxy
    thing_name = os.environ.get("AWS_IOT_THING_NAME", "local")
    control_topic = config.get("controlTopic", f"simulator/{thing_name}/control")
    status_topic = f"simulator/{thing_name}/status"
    return start_ipc_control_listener(control_topic, status_topic, thing_name)


def log_sample(device: DeviceState, hour: float, paused: bool):
    """Log up to three current tag values of a device as a progress heartbeat."""
    samples = []
    for tag in list(device.tags.values())[:3]:
        if tag.last_value is not None:
            samples.append(f"{tag.name}={tag.last_value}")
    status = " [PAUSED]" if paused else ""
    log.info(f"[sim {hour:05.2f}h{status}] {device.name}: {', '.join(samples)}")


async def main():
    """Entry point: build the server, start the control listener, run the update loop."""
    config = load_config()
    endpoint = config["endpoint"]
    interval = config["updateIntervalSeconds"]
    time_accel = config["timeAcceleration"]

    server, devices = await build_server(config)
    total_tags = sum(len(dev.tags) for dev in devices)

    log.info(f"Created {len(devices)} devices with {total_tags} tags")
    log.info(f"Config: interval={interval}s, time_accel={time_accel}x")
    log.info(f"Endpoint: {endpoint}")

    start_control(config, devices)

    async with server:
        log.info("Server running. Press Ctrl+C to stop.")
        start = time.monotonic()
        next_tick = start
        tick = 0
        while True:
            elapsed = time.monotonic() - start
            hour = (elapsed / time_accel) % 24

            with _lock:
                paused = _paused

            if not paused:
                for dev in devices:
                    await update_device(dev, hour)

            tick += 1
            if tick % 10 == 0:
                log_sample(devices[0], hour, paused)

            # Fixed-rate schedule: compensate for the time spent writing tags
            next_tick += interval
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))


if __name__ == "__main__":
    asyncio.run(main())
