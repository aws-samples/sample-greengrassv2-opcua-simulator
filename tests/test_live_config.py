# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for the simulatorConfig component configuration in docker/simulator.py:
config validation, startup source selection (component configuration over
IPC, else the config file), live application of an update to a running OPC UA
server, and the IPC watcher that turns configuration update events into
config_applied / config_rejected status events. The IPC client is always a mock.
"""
# pytest fixture conventions: fixtures and parametrize values are passed as arguments
# by name, and tests reach into the module's private state on purpose.
# pylint: disable=missing-function-docstring,protected-access,unused-argument,redefined-outer-name
# pylint: disable=too-many-arguments,too-many-positional-arguments

import asyncio
import copy
import json
import logging
import os
import socket
import threading
from types import SimpleNamespace

import pytest
from asyncua import ua
from awsiot.greengrasscoreipc.model import QOS

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_SOCKET_VAR = "AWS_GG_NUCLEUS_DOMAIN_SOCKET_FILEPATH_FOR_COMPONENT"

BASE_CONFIG = {
    "endpoint": "opc.tcp://0.0.0.0:4840/simulator/",
    "updateIntervalSeconds": 1.0,
    "timeAcceleration": 60,
    "devices": [{"name": "Motor01", "tags": [{"name": "Temperature", "type": "double"}]}],
}


def _with(**changes):
    cfg = copy.deepcopy(BASE_CONFIG)
    cfg.update(changes)
    return cfg


def _ipc_client(mocker, value=None, error=None):
    """A mock GreengrassCoreIPCClientV2 whose GetConfiguration returns ``value`` (or raises ``error``)."""
    client = mocker.Mock()
    if error is not None:
        client.get_configuration.side_effect = error
    else:
        client.get_configuration.return_value = SimpleNamespace(value=value)
    return client


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Point argv at a valid config file distinct from the IPC config."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_with(timeAcceleration=1)), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["simulator.py", str(path)])
    return path


@pytest.fixture
def isolated_globals(sim, monkeypatch):
    """Keep the module-level control state and folder cache from leaking between tests."""
    monkeypatch.setattr(sim, "_devices_by_name", {})
    monkeypatch.setattr(sim, "_paused", False)
    sim._folder_cache.clear()
    yield
    sim._folder_cache.clear()


# --- validate_config ---

def test_shipped_config_is_valid(sim):
    with open(os.path.join(_REPO_ROOT, "artifacts", "config.json"), encoding="utf-8") as f:
        assert sim.validate_config(json.load(f)) is None


def test_floats_from_greengrass_are_valid(sim):
    """Greengrass may turn JSON integers into floats; that must not make a config invalid."""
    assert sim.validate_config(_with(timeAcceleration=60.0, updateIntervalSeconds=2.0)) is None


@pytest.mark.parametrize("cfg,reason", [
    ("text", "config must be a JSON object, got str"),
    ({"endpoint": "x"}, "missing required key(s): updateIntervalSeconds, timeAcceleration, devices"),
    (_with(endpoint=""), "'endpoint' must be a non-empty string"),
    (_with(updateIntervalSeconds=0), "'updateIntervalSeconds' must be a positive number"),
    (_with(timeAcceleration=True), "'timeAcceleration' must be a positive number"),
    (_with(timeAcceleration=float("inf")), "'timeAcceleration' must be a positive number"),
    (_with(devices={}), "'devices' must be a list"),
    (_with(devices=[1]), "devices[0] must be an object"),
    (_with(devices=[{"tags": []}]), "devices[0] needs a non-empty string 'name'"),
    (_with(devices=[{"name": "A"}, {"name": "A"}]), "duplicate device name 'A'"),
    (_with(devices=[{"name": "A", "tags": {}}]), "device A: 'tags' must be a list"),
    (_with(devices=[{"name": "A", "tags": ["T"]}]), "device A: tags[0] must be an object"),
    (_with(devices=[{"name": "A", "tags": [{"type": "double"}]}]), "device A: tags[0] needs a non-empty string"),
    (_with(devices=[{"name": "A", "tags": [{"name": "T"}, {"name": "T"}]}]), "device A: duplicate tag name 'T'"),
    (_with(devices=[{"name": "A", "tags": [{"name": "T", "type": "float"}]}]),
     "device A: Tag T: unsupported type 'float'"),
])
def test_validate_config_rejects(sim, cfg, reason):
    assert sim.validate_config(cfg).startswith(reason)


# --- Startup config source ---

def test_startup_without_ipc_uses_file(sim, config_file, caplog):
    caplog.set_level(logging.INFO, logger="opcua-simulator")
    assert sim.select_startup_config(None) == _with(timeAcceleration=1)
    assert "Greengrass IPC not available — using the config file" in caplog.text


def test_startup_with_ipc_uses_component_configuration(sim, monkeypatch, mocker, caplog):
    caplog.set_level(logging.INFO, logger="opcua-simulator")
    monkeypatch.setattr("sys.argv", ["simulator.py", "/does/not/exist.json"])  # must not be read
    client = _ipc_client(mocker, value=BASE_CONFIG)

    assert sim.select_startup_config(client) == BASE_CONFIG
    client.get_configuration.assert_called_once_with(key_path=["simulatorConfig"])
    assert "Using the component configuration (simulatorConfig)" in caplog.text


@pytest.mark.parametrize("value,error,reason", [
    (None, RuntimeError("ResourceNotFoundError"), "GetConfiguration failed (RuntimeError: ResourceNotFoundError)"),
    ({}, None, "simulatorConfig is missing or empty"),
    ({"simulatorConfig": "not an object"}, None, "config must be a JSON object, got str"),
    ({"devices": []}, None, "missing required key(s)"),
])
def test_startup_falls_back_to_file_when_ipc_config_unusable(sim, config_file, mocker, caplog,
                                                             value, error, reason):
    client = _ipc_client(mocker, value=value, error=error)
    assert sim.select_startup_config(client) == _with(timeAcceleration=1)
    assert f"Component configuration not usable ({reason}" in caplog.text
    assert "using the config file instead" in caplog.text


def test_startup_exits_when_file_fallback_invalid(sim, tmp_path, monkeypatch, caplog):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"devices": []}), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["simulator.py", str(path)])
    with pytest.raises(SystemExit):
        sim.select_startup_config(None)
    assert "Invalid config file: missing required key(s)" in caplog.text


def test_connect_config_client_without_ipc_is_none(sim, monkeypatch, mocker):
    monkeypatch.delenv("SVCUID", raising=False)
    monkeypatch.delenv(_SOCKET_VAR, raising=False)
    factory = mocker.Mock()
    assert sim.connect_config_client(factory) is None
    factory.assert_not_called()


# --- Live apply against a real in-process OPC UA server ---

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _browse(state, node=None, path=""):
    """{device path: {tag name: variant type}} of the simulator namespace, read from the server."""
    node = node or state.server.nodes.objects
    found = {}
    for child in await node.get_children():
        name = await child.read_browse_name()
        if name.NamespaceIndex != state.idx:
            continue
        child_path = f"{path}/{name.Name}" if path else name.Name
        if await child.read_node_class() == ua.NodeClass.Variable:
            found.setdefault(path, {})[name.Name] = await child.read_data_type_as_variant_type()
        else:
            found.setdefault(child_path, {})
            found.update(await _browse(state, child, child_path))
    return found


def _server_configs(port):
    old = {
        "endpoint": f"opc.tcp://127.0.0.1:{port}/t/", "updateIntervalSeconds": 1.0, "timeAcceleration": 60,
        "devices": [
            {"name": "Motor01", "tags": [
                {"name": "Temperature", "type": "double", "pattern": "steady", "base": 50.0, "noise": 0.0},
                {"name": "Current", "type": "double", "pattern": "steady", "base": 45.0, "noise": 0.0},
                {"name": "Vibration", "type": "double"},
                {"name": "Running", "type": "boolean", "pattern": "duty_cycle", "onProbability": 1.0},
            ]},
            {"name": "Line1/Pump01", "tags": [{"name": "Flow"}]},
        ],
    }
    new = {
        "endpoint": f"opc.tcp://127.0.0.1:{port + 1}/other/", "updateIntervalSeconds": 0.5, "timeAcceleration": 120,
        "devices": [
            {"name": "Motor01", "tags": [
                {"name": "Temperature", "type": "int", "pattern": "steady", "base": 50.0, "noise": 0.0},
                {"name": "Current", "type": "double", "pattern": "steady", "base": 99.0, "noise": 0.0},
                {"name": "Running", "type": "boolean", "pattern": "duty_cycle", "onProbability": 1.0},
                {"name": "Pressure", "type": "double", "pattern": "steady", "base": 7.0, "noise": 0.0},
            ]},
            {"name": "Line2/Fan01", "tags": [{"name": "Speed"}]},
        ],
    }
    return old, new


async def _apply_scenario(sim):
    old_cfg, new_cfg = _server_configs(_free_port())
    state = await sim.build_server(old_cfg)
    sim.register_devices(state.devices)
    async with state.server:
        before = await _browse(state)
        motor = state.devices[0]
        current_node = motor.tags["Current"].node
        temperature_node = motor.tags["Temperature"].node
        running = motor.tags["Running"]
        for name, value in (("Running", True), ("Current", 77.0), ("Vibration", 1.0), ("Temperature", 200.5)):
            motor.tags[name].override_value = value
        sim._paused = True

        await sim.apply_config(state, new_cfg)

        after = await _browse(state)
        for dev in state.devices:
            await sim.update_device(dev, 0.0)
        pressure = (await motor.tags["Pressure"].node.read_data_value()).Value.Value
        return SimpleNamespace(
            state=state, before=before, after=after, motor=motor, running=running, pressure=pressure,
            current_node_kept=motor.tags["Current"].node.nodeid == current_node.nodeid,
            temperature_node_replaced=motor.tags["Temperature"].node.nodeid != temperature_node.nodeid,
            new_cfg=new_cfg,
        )


def test_apply_config_updates_address_space_live(sim, isolated_globals, caplog):
    result = asyncio.run(_apply_scenario(sim))
    double, int64, boolean = ua.VariantType.Double, ua.VariantType.Int64, ua.VariantType.Boolean

    assert result.before == {
        "Motor01": {"Temperature": double, "Current": double, "Vibration": double, "Running": boolean},
        "Line1": {}, "Line1/Pump01": {"Flow": double},
    }
    # Vibration and Line1/Pump01 (with its now unused Line1 folder) are gone, Pressure and
    # Line2/Fan01 are new, Temperature changed type
    assert result.after == {
        "Motor01": {"Temperature": int64, "Current": double, "Running": boolean, "Pressure": double},
        "Line2": {}, "Line2/Fan01": {"Speed": double},
    }
    assert set(sim._folder_cache) == {"Motor01", "Line2", "Line2/Fan01"}
    assert result.current_node_kept  # same type: same NodeId, client subscriptions continue
    assert result.temperature_node_replaced
    assert result.pressure == 7.0

    motor = result.motor
    assert motor.tags["Running"] is result.running  # unchanged tag keeps its state
    assert motor.tags["Running"].override_value is True
    assert motor.tags["Current"].override_value == 77.0  # changed tag, same type: anomaly kept
    assert motor.tags["Current"].base == 99.0
    assert motor.tags["Temperature"].override_value is None  # 200.5 does not fit the new int type
    assert sim._paused is True  # pause state is kept

    state = result.state
    assert state.config == result.new_cfg
    assert state.interval == 0.5 and state.wake.is_set()
    assert state.time_accel == 120
    assert set(sim._devices_by_name) == {"Motor01", "Line2/Fan01"}
    # The endpoint cannot change live
    assert state.endpoint != result.new_cfg["endpoint"]
    assert "endpoint changes need a component restart" in caplog.text
    assert "tags added 2, removed 2, changed 2" in caplog.text


async def _writable_scenario(sim):
    cfg, _ = _server_configs(_free_port())
    state = await sim.build_server(cfg)
    async with state.server:
        node = state.devices[0].tags["Current"].node
        await sim.apply_config(state, dict(cfg, writableTags=True))
        bits = (await node.read_attribute(ua.AttributeIds.AccessLevel)).Value.Value
        return state, bits


def test_apply_config_toggles_writable_tags(sim, isolated_globals, caplog):
    state, bits = asyncio.run(_writable_scenario(sim))
    assert state.writable is True
    assert bits & (1 << ua.AccessLevel.CurrentWrite)
    assert "endpoint changes" not in caplog.text


# --- Simulation clock and interval ---

def test_time_acceleration_change_keeps_simulated_hour_continuous(sim, mocker):
    clock = [0.0]
    mocker.patch.object(sim.time, "monotonic", side_effect=lambda: clock[0])
    state = sim.Simulation(None, None, BASE_CONFIG, [])
    clock[0] = 60.0
    assert state.hour() == pytest.approx(1.0)
    state.set_time_acceleration(120)
    assert state.hour() == pytest.approx(1.0)
    clock[0] = 180.0
    assert state.hour() == pytest.approx(2.0)


def test_interval_change_wakes_the_loop(sim):
    async def scenario():
        state = sim.Simulation(None, None, BASE_CONFIG, [])
        past = await state.sleep_until(0.0)  # deadline already passed: no wait
        state.set_interval(0.25)
        woke = await asyncio.wait_for(state.sleep_until(float("inf")), timeout=5)
        return state, past, woke

    state, past, woke = asyncio.run(scenario())
    assert past == 0.0
    assert woke != float("inf")
    assert not state.wake.is_set()
    assert state.interval == 0.25


# --- ConfigWatcher: reload, status events, IPC marshalling ---

def _state(sim, cfg=None):
    cfg = cfg or BASE_CONFIG
    return sim.Simulation(None, None, cfg, [sim.DeviceState(d) for d in cfg["devices"]])


def _published(client):
    kwargs = client.publish_to_iot_core.call_args.kwargs
    assert kwargs["topic_name"] == "simulator/t/status"
    assert kwargs["qos"] == QOS.AT_LEAST_ONCE
    return json.loads(kwargs["payload"])


def _reload(sim, client, state):
    async def scenario():
        watcher = sim.ConfigWatcher(client, state, asyncio.get_running_loop(), "simulator/t/status", "t")
        await watcher.reload()
    asyncio.run(scenario())


def test_reload_applies_valid_update_and_publishes_config_applied(sim, mocker):
    new_cfg = _with(devices=[{"name": "A", "tags": [{"name": "X"}, {"name": "Y"}]}, {"name": "B", "tags": []}])
    state = _state(sim)

    async def fake_apply(st, cfg):
        st.devices = [sim.DeviceState(d) for d in cfg["devices"]]
        st.config = cfg
    apply = mocker.patch.object(sim, "apply_config", side_effect=fake_apply)
    client = _ipc_client(mocker, value=new_cfg)

    _reload(sim, client, state)

    apply.assert_called_once_with(state, new_cfg)
    assert _published(client) == {"event": "config_applied", "thingName": "t", "devices": 2, "tags": 2}


@pytest.mark.parametrize("value,error,reason", [
    ({"devices": []}, None, "missing required key(s): endpoint, updateIntervalSeconds, timeAcceleration"),
    (_with(devices=[{"name": "A", "tags": [{"name": "T", "type": "float"}]}]), None,
     "device A: Tag T: unsupported type 'float' (expected one of double, boolean, string, int)"),
    (None, RuntimeError("socket closed"), "GetConfiguration failed (RuntimeError: socket closed)"),
])
def test_reload_rejects_invalid_update_and_keeps_config(sim, mocker, caplog, value, error, reason):
    state = _state(sim)
    devices = state.devices
    apply = mocker.patch.object(sim, "apply_config")
    client = _ipc_client(mocker, value=value, error=error)

    _reload(sim, client, state)

    apply.assert_not_called()
    assert state.config == BASE_CONFIG and state.devices is devices
    # Counts describe the configuration that keeps running
    assert _published(client) == {"event": "config_rejected", "thingName": "t", "devices": 1, "tags": 1,
                                  "reason": reason}
    assert f"Rejected configuration update ({reason}); keeping the current configuration" in caplog.text


def test_reload_unchanged_config_publishes_nothing(sim, mocker):
    apply = mocker.patch.object(sim, "apply_config")
    client = _ipc_client(mocker, value=copy.deepcopy(BASE_CONFIG))
    _reload(sim, client, _state(sim))
    apply.assert_not_called()
    client.publish_to_iot_core.assert_not_called()


def test_reload_apply_failure_publishes_config_rejected(sim, mocker, caplog):
    mocker.patch.object(sim, "apply_config", side_effect=ua.UaStatusCodeError(ua.StatusCodes.BadNodeIdUnknown))
    client = _ipc_client(mocker, value=_with(timeAcceleration=5))
    _reload(sim, client, _state(sim))
    payload = _published(client)
    assert payload["event"] == "config_rejected"
    assert payload["reason"].startswith("apply failed (BadNodeIdUnknown")
    assert "Could not apply configuration update" in caplog.text


def test_publish_failure_is_logged_not_raised(sim, mocker, caplog):
    mocker.patch.object(sim, "apply_config")
    client = _ipc_client(mocker, value={"devices": []})
    client.publish_to_iot_core.side_effect = RuntimeError("UnauthorizedError")
    _reload(sim, client, _state(sim))
    assert "Could not publish 'config_rejected'" in caplog.text


def test_update_events_from_ipc_thread_are_coalesced_onto_the_loop(sim, monkeypatch, mocker):
    monkeypatch.setattr(sim, "CONFIG_RELOAD_DELAY_SECONDS", 0)
    event = SimpleNamespace(configuration_update_event=SimpleNamespace(key_path=["simulatorConfig", "devices"]))

    async def scenario():
        loop = asyncio.get_running_loop()
        watcher = sim.ConfigWatcher(mocker.Mock(), _state(sim), loop, "simulator/t/status", "t")
        reloaded = asyncio.Event()
        calls = []

        async def fake_reload():
            calls.append(threading.current_thread() is threading.main_thread())
            reloaded.set()
        watcher.reload = fake_reload

        # The SDK delivers events on its own executor thread; a deployment sends a burst of them
        for _ in range(3):
            thread = threading.Thread(target=watcher._on_stream_event, args=(event,))
            thread.start()
            thread.join()
        await asyncio.wait_for(reloaded.wait(), timeout=5)
        await asyncio.sleep(0)  # let any (unexpected) extra reload run
        return calls

    assert asyncio.run(scenario()) == [True]  # one reload, on the loop's thread


def _ipc_env(monkeypatch, tmp_path):
    socket_file = tmp_path / "ipc.socket"
    socket_file.touch()
    monkeypatch.setenv("SVCUID", "x")
    monkeypatch.setenv(_SOCKET_VAR, str(socket_file))


def _run_subscribe(sim, mocker, client, factory=None):
    """Start the watcher's subscribe thread, wait for every ipc-config thread, return the watcher."""
    async def scenario():
        watcher = sim.ConfigWatcher(client, _state(sim), asyncio.get_running_loop(), "simulator/t/status", "t",
                                    client_factory=factory or mocker.Mock())
        watcher.request_reload = mocker.Mock()
        watcher.start().join(timeout=5)
        await asyncio.sleep(0)  # run the call_soon_threadsafe callback
        return watcher
    return asyncio.run(scenario())


def test_watcher_subscribes_to_simulator_config_and_rechecks(sim, mocker):
    client = mocker.Mock()
    client.subscribe_to_configuration_update.return_value = (None, None)

    watcher = _run_subscribe(sim, mocker, client)

    kwargs = client.subscribe_to_configuration_update.call_args.kwargs
    assert kwargs["key_path"] == ["simulatorConfig"]
    assert callable(kwargs["on_stream_event"]) and callable(kwargs["on_stream_closed"])
    # A change deployed between the startup read and the subscription is not missed
    watcher.request_reload.assert_called_once_with()


def test_watcher_retries_subscribe_with_backoff(sim, mocker):
    sleep = mocker.patch.object(sim.time, "sleep")
    client = mocker.Mock()
    client.subscribe_to_configuration_update.side_effect = [RuntimeError("busy"), RuntimeError("busy"), (None, None)]

    watcher = _run_subscribe(sim, mocker, client)

    assert client.subscribe_to_configuration_update.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [5, 10]
    watcher.request_reload.assert_called_once_with()


def test_watcher_reconnects_when_established_stream_closes(sim, monkeypatch, tmp_path, mocker):
    _ipc_env(monkeypatch, tmp_path)
    first, second = mocker.Mock(name="first"), mocker.Mock(name="second")
    for client in (first, second):
        client.subscribe_to_configuration_update.return_value = (None, None)
    factory = mocker.Mock(return_value=second)

    async def scenario():
        watcher = sim.ConfigWatcher(first, _state(sim), asyncio.get_running_loop(), "simulator/t/status", "t",
                                    client_factory=factory)
        watcher.request_reload = mocker.Mock()
        watcher.start().join(timeout=5)
        # The Nucleus closes the stream: the SDK invokes on_stream_closed on an executor thread
        first.subscribe_to_configuration_update.call_args.kwargs["on_stream_closed"]()
        for thread in [t for t in threading.enumerate() if t.name == "ipc-config"]:
            thread.join(timeout=5)
        await asyncio.sleep(0)  # run the call_soon_threadsafe callbacks
        return watcher

    watcher = asyncio.run(scenario())

    first.close.assert_called_once_with(executor_wait=False)
    factory.assert_called_once_with()
    second.subscribe_to_configuration_update.assert_called_once()
    assert watcher.client is second
    assert watcher.request_reload.call_count == 2  # after the first subscribe and after the reconnect


def test_reload_reports_settings_that_need_a_restart(sim, mocker):
    """An endpoint/controlTopic change is applied as far as possible and listed in restartRequired."""
    state = _state(sim)
    new = _with(endpoint="opc.tcp://0.0.0.0:4841/simulator/", controlTopic="custom/control")
    client = _ipc_client(mocker, value={"simulatorConfig": new})
    apply = mocker.patch.object(sim, "apply_config", mocker.AsyncMock(return_value=["endpoint", "controlTopic"]))
    _reload(sim, client, state)
    apply.assert_awaited_once()
    assert _published(client) == {"event": "config_applied", "thingName": "t", "devices": 1, "tags": 1,
                                  "restartRequired": ["endpoint", "controlTopic"]}


def test_apply_config_returns_restart_required(sim, mocker, caplog):
    state = _state(sim)
    state.server = mocker.Mock(delete_nodes=mocker.AsyncMock())
    state.endpoint = BASE_CONFIG["endpoint"]
    state.control_topic = None
    dev = state.devices[0]
    mocker.patch.object(sim, "_apply_device_tags", mocker.AsyncMock())
    mocker.patch.object(sim, "_prune_folders", mocker.AsyncMock())
    mocker.patch.object(sim, "register_devices")
    dev.name = "Motor01"
    assert asyncio.run(sim.apply_config(state, copy.deepcopy(BASE_CONFIG))) == []
    changed = _with(endpoint="opc.tcp://0.0.0.0:4841/simulator/")
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(sim.apply_config(state, changed)) == ["endpoint"]
    assert "endpoint changes need a component restart" in caplog.text


def test_changed_tag_keeps_anomaly_injected_during_apply(sim, mocker):
    """An inject_anomaly that lands while the new tags are being built is carried to the new TagState."""
    state = _state(sim)
    dev = state.devices[0]
    old = dev.tags["Temperature"]
    old.node = object()
    dev.folder = mocker.Mock()
    state.server = mocker.Mock(delete_nodes=mocker.AsyncMock())

    async def add_node(idx, folder, tag, writable):
        # Simulates the IPC thread injecting an anomaly on the old tag mid-apply
        with sim._lock:
            old.override_value = 99.5
        tag.node = object()

    mocker.patch.object(sim, "add_tag_node", side_effect=add_node)
    new_cfg = {"name": "Motor01", "tags": [{"name": "Temperature", "type": "double", "base": 1.0},
                                           {"name": "Extra", "type": "double"}]}
    changes = {"added": 0, "removed": 0, "changed": 0}
    asyncio.run(sim._apply_device_tags(state, dev, new_cfg, False, changes))
    assert dev.tags["Temperature"] is not old
    assert dev.tags["Temperature"].override_value == 99.5
    assert changes == {"added": 1, "removed": 0, "changed": 1}
