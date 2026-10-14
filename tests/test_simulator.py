# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the OPC UA simulator application (docker/simulator.py)."""

# pytest fixture conventions: fixtures are passed as arguments by name and tests
# reach into the module's private state on purpose.
# pylint: disable=missing-function-docstring,protected-access,unused-argument,redefined-outer-name

import json
import threading
from types import SimpleNamespace

import pytest
from awsiot.greengrasscoreipc.model import QOS


# --- TagState value generation ---

def test_tagstate_defaults(sim):
    tag = sim.TagState({"name": "T1"})
    assert tag.name == "T1"
    assert tag.data_type == "double"
    assert tag.pattern == "steady"
    assert tag.node is None
    assert tag.override_value is None


def test_tagstate_rejects_unsupported_type(sim):
    """A config tag with a type outside UA_TYPE_MAP fails at construction with a readable message."""
    expected = r"Tag T1: unsupported type 'float' \(expected one of double, boolean, string, int\)"
    with pytest.raises(ValueError, match=expected):
        sim.TagState({"name": "T1", "type": "float"})


def test_generate_steady_no_noise_returns_base(sim):
    tag = sim.TagState({"name": "T", "pattern": "steady", "base": 42.0, "noise": 0.0})
    assert tag.generate(0.0, {}) == 42.0
    assert tag.last_value == 42.0


def test_generate_sine_is_deterministic_without_noise(sim):
    tag = sim.TagState({
        "name": "T", "pattern": "sine",
        "base": 50.0, "amplitude": 10.0, "noise": 0.0, "phaseOffset": 0.0,
    })
    # At hour 6 the sine argument is 0 -> value == base
    assert tag.generate(6.0, {}) == 50.0
    # At hour 12 the sine argument is pi/2 -> value == base + amplitude
    assert tag.generate(12.0, {}) == 60.0


def test_generate_drift_stays_within_bounds(sim):
    tag = sim.TagState({
        "name": "T", "pattern": "drift",
        "base": 50.0, "driftRate": 5.0, "noise": 0.0, "min": 0.0, "max": 100.0,
    })
    for _ in range(500):
        val = tag.generate(0.0, {})
        assert 0.0 <= val <= 100.0


def test_generate_duty_cycle_returns_bool(sim):
    tag = sim.TagState({"name": "R", "type": "boolean", "pattern": "duty_cycle"})
    assert isinstance(tag.generate(0.0, {}), bool)


def test_generate_state_machine_returns_known_state(sim):
    states = ["AUTO", "MANUAL", "FAULT"]
    tag = sim.TagState({
        "name": "M", "type": "string", "pattern": "state_machine",
        "states": states, "transitionProbability": 1.0,
    })
    for _ in range(50):
        assert tag.generate(0.0, {}) in states


def test_generate_unknown_pattern_returns_zero(sim):
    tag = sim.TagState({"name": "T", "pattern": "does_not_exist"})
    assert tag.generate(0.0, {}) == 0.0


def test_override_value_takes_precedence(sim):
    tag = sim.TagState({"name": "T", "pattern": "steady", "base": 10.0, "noise": 0.0})
    tag.override_value = 999.0
    assert tag.generate(0.0, {}) == 999.0
    assert tag.last_value == 999.0


def test_requires_tag_double_returns_zero_when_dependency_off(sim):
    running = sim.TagState({"name": "Running", "type": "boolean", "pattern": "duty_cycle"})
    running.bool_state = False
    rpm = sim.TagState({
        "name": "RPM", "type": "double", "pattern": "steady",
        "base": 1500.0, "noise": 0.0, "requiresTag": "Running",
    })
    device_tags = {"Running": running, "RPM": rpm}
    assert rpm.generate(0.0, device_tags) == 0.0


def test_requires_tag_boolean_returns_false_when_dependency_off(sim):
    running = sim.TagState({"name": "Running", "type": "boolean", "pattern": "duty_cycle"})
    running.bool_state = False
    fault = sim.TagState({
        "name": "Fault", "type": "boolean", "pattern": "duty_cycle",
        "requiresTag": "Running",
    })
    device_tags = {"Running": running, "Fault": fault}
    assert fault.generate(0.0, device_tags) is False


def test_requires_tag_generates_normally_when_dependency_on(sim):
    running = sim.TagState({"name": "Running", "type": "boolean", "pattern": "duty_cycle"})
    running.bool_state = True
    rpm = sim.TagState({
        "name": "RPM", "type": "double", "pattern": "steady",
        "base": 1500.0, "noise": 0.0, "requiresTag": "Running",
    })
    device_tags = {"Running": running, "RPM": rpm}
    assert rpm.generate(0.0, device_tags) == 1500.0


# --- Override value validation ---

@pytest.mark.parametrize("data_type,value,expected", [
    ("double", 12.5, True),
    ("double", 3, True),
    ("double", True, False),
    ("double", "nope", False),
    ("string", "OK", True),
    ("string", 5, False),
    ("boolean", True, True),
    ("boolean", "true", False),
    ("int", 7, True),
    ("int", True, False),
    ("int", 7.0, False),
    ("int", "7", False),
    # Values that pass the type check but do not fit the OPC UA variant
    ("double", 10 ** 400, False),  # float() raises OverflowError
    ("double", float("inf"), False),  # json.loads accepts Infinity/NaN
    ("double", float("nan"), False),
    ("int", 2 ** 63, False),  # outside Int64
    ("int", -(2 ** 63) - 1, False),
    ("int", 2 ** 63 - 1, True),
    ("int", -(2 ** 63), True),
])
def test_validate_override_value(sim, data_type, value, expected):
    tag = sim.TagState({"name": "T", "type": data_type})
    assert sim._validate_override_value(tag, value) is expected


# --- Value coercion for OPC UA writes ---

def test_coerce_value_int_to_double(sim):
    result = sim.coerce_value("double", 200)
    assert result == 200.0
    assert isinstance(result, float)


def test_coerce_value_float_to_int(sim):
    result = sim.coerce_value("int", 5.0)
    assert result == 5
    assert isinstance(result, int)


def test_coerce_value_passthrough_for_string_and_bool(sim):
    assert sim.coerce_value("string", "AUTO") == "AUTO"
    assert sim.coerce_value("boolean", True) is True


# --- DeviceState ---

def test_devicestate_builds_tag_map(sim):
    device = sim.DeviceState({
        "name": "Pump01",
        "tags": [
            {"name": "Flow", "type": "double"},
            {"name": "Running", "type": "boolean"},
        ],
    })
    assert device.name == "Pump01"
    assert set(device.tags) == {"Flow", "Running"}
    assert device.tags["Flow"].data_type == "double"


# --- MQTT control message handling ---

def test_inject_anomaly_sets_override(sim, registered_device):
    sim.handle_control_message({
        "action": "inject_anomaly", "device": "Motor01",
        "tag": "Temperature", "value": 200.0,
    })
    assert registered_device.tags["Temperature"].override_value == 200.0


def test_inject_anomaly_rejects_wrong_type(sim, registered_device):
    sim.handle_control_message({
        "action": "inject_anomaly", "device": "Motor01",
        "tag": "Temperature", "value": "hot",
    })
    assert registered_device.tags["Temperature"].override_value is None


def test_inject_anomaly_unknown_device_is_ignored(sim, registered_device):
    # Should not raise even though the device does not exist
    sim.handle_control_message({
        "action": "inject_anomaly", "device": "Ghost",
        "tag": "Temperature", "value": 1.0,
    })
    assert registered_device.tags["Temperature"].override_value is None


def test_clear_anomaly_removes_override(sim, registered_device):
    registered_device.tags["Temperature"].override_value = 200.0
    sim.handle_control_message({
        "action": "clear_anomaly", "device": "Motor01", "tag": "Temperature",
    })
    assert registered_device.tags["Temperature"].override_value is None


def test_clear_all_removes_every_override(sim, registered_device):
    for tag in registered_device.tags.values():
        tag.override_value = 1
    sim.handle_control_message({"action": "clear_all"})
    assert all(t.override_value is None for t in registered_device.tags.values())


def test_pause_and_resume_toggle_global_flag(sim, registered_device):
    sim.handle_control_message({"action": "pause"})
    assert sim._paused is True
    sim.handle_control_message({"action": "resume"})
    assert sim._paused is False


def test_unknown_action_is_ignored(sim, registered_device):
    # Should not raise or change paused state
    sim.handle_control_message({"action": "explode"})
    assert sim._paused is False


# --- Greengrass IPC control transport ---

_SOCKET_VAR = "AWS_GG_NUCLEUS_DOMAIN_SOCKET_FILEPATH_FOR_COMPONENT"


def _control_event(payload: bytes):
    """Mimic the IoTCoreMessage shape delivered by SubscribeToIoTCore."""
    return SimpleNamespace(message=SimpleNamespace(payload=payload))


def test_ipc_unavailable_disables_control(sim, monkeypatch, mocker):
    monkeypatch.delenv("SVCUID", raising=False)
    monkeypatch.delenv(_SOCKET_VAR, raising=False)
    factory = mocker.Mock()
    assert sim.start_ipc_control_listener("t/c", "t/s", "t", client_factory=factory) is None
    factory.assert_not_called()


def test_ipc_available_requires_existing_socket(sim, monkeypatch, tmp_path):
    socket_file = tmp_path / "ipc.socket"
    monkeypatch.setenv("SVCUID", "secret")
    monkeypatch.setenv(_SOCKET_VAR, str(socket_file))
    assert sim.ipc_available() is False
    socket_file.touch()
    assert sim.ipc_available() is True


def test_control_handler_dispatches_and_acks(sim, registered_device, mocker):
    ipc = mocker.Mock()
    handler = sim.make_control_handler(ipc, "simulator/t/status", "t")
    handler(_control_event(b'{"action":"pause"}'))
    assert sim._paused is True
    ipc.publish_to_iot_core.assert_called_once()
    kwargs = ipc.publish_to_iot_core.call_args.kwargs
    assert kwargs["topic_name"] == "simulator/t/status"
    assert kwargs["qos"] == QOS.AT_LEAST_ONCE
    ack = json.loads(kwargs["payload"])
    assert ack["ack"] == "pause"
    assert ack["thingName"] == "t"


def test_control_handler_invalid_json_does_not_raise(sim, registered_device, mocker):
    ipc = mocker.Mock()
    handler = sim.make_control_handler(ipc, "simulator/t/status", "t")
    handler(_control_event(b"not json"))
    ipc.publish_to_iot_core.assert_not_called()
    assert sim._paused is False


def test_control_handler_non_dict_payload_ignored(sim, registered_device, mocker):
    ipc = mocker.Mock()
    handler = sim.make_control_handler(ipc, "simulator/t/status", "t")
    handler(_control_event(b"[1,2]"))
    ipc.publish_to_iot_core.assert_not_called()
    assert sim._paused is False


def test_start_ipc_listener_subscribes_and_publishes_online(sim, monkeypatch, tmp_path, mocker):
    socket_file = tmp_path / "ipc.socket"
    socket_file.touch()
    monkeypatch.setenv("SVCUID", "x")
    monkeypatch.setenv(_SOCKET_VAR, str(socket_file))
    monkeypatch.setattr(sim, "_ipc_client", None)

    client = mocker.Mock()
    client.subscribe_to_iot_core.return_value = (None, None)

    thread = sim.start_ipc_control_listener(
        "simulator/t/control", "simulator/t/status", "t", client_factory=lambda: client,
    )
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()

    client.subscribe_to_iot_core.assert_called_once()
    sub_kwargs = client.subscribe_to_iot_core.call_args.kwargs
    assert sub_kwargs["topic_name"] == "simulator/t/control"
    assert sub_kwargs["qos"] == QOS.AT_LEAST_ONCE
    assert callable(sub_kwargs["on_stream_event"])

    client.publish_to_iot_core.assert_called_once()
    pub_kwargs = client.publish_to_iot_core.call_args.kwargs
    assert pub_kwargs["topic_name"] == "simulator/t/status"
    assert pub_kwargs["qos"] == QOS.AT_LEAST_ONCE
    online = json.loads(pub_kwargs["payload"])
    assert online["status"] == "online"
    assert online["controlTopic"] == "simulator/t/control"
    assert sim._ipc_client is client


def test_start_ipc_listener_gives_up_after_connect_failures(sim, monkeypatch, tmp_path, mocker):
    socket_file = tmp_path / "ipc.socket"
    socket_file.touch()
    monkeypatch.setenv("SVCUID", "x")
    monkeypatch.setenv(_SOCKET_VAR, str(socket_file))
    monkeypatch.setattr(sim, "_ipc_client", None)
    monkeypatch.setattr(sim, "IPC_CONNECT_RETRY_SECONDS", 0)
    factory = mocker.Mock(side_effect=RuntimeError("socket refused"))

    thread = sim.start_ipc_control_listener("t/c", "t/s", "t", client_factory=factory)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert factory.call_count == sim.IPC_CONNECT_ATTEMPTS
    assert sim._ipc_client is None


def test_start_ipc_listener_retries_subscribe_with_backoff(sim, monkeypatch, tmp_path, mocker):
    """A refused SubscribeToIoTCore (e.g. UnauthorizedError) is retried until it succeeds."""
    socket_file = tmp_path / "ipc.socket"
    socket_file.touch()
    monkeypatch.setenv("SVCUID", "x")
    monkeypatch.setenv(_SOCKET_VAR, str(socket_file))
    monkeypatch.setattr(sim, "_ipc_client", None)
    monkeypatch.setattr(sim, "IPC_SUBSCRIBE_RETRY_SECONDS", 5)
    sleep = mocker.patch.object(sim.time, "sleep")

    client = mocker.Mock()
    client.subscribe_to_iot_core.side_effect = [
        RuntimeError("UnauthorizedError"),
        RuntimeError("UnauthorizedError"),
        (None, None),
    ]

    thread = sim.start_ipc_control_listener("t/c", "t/s", "t", client_factory=lambda: client)
    thread.join(timeout=5)
    assert not thread.is_alive()

    assert client.subscribe_to_iot_core.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [5, 10]  # doubling backoff
    client.publish_to_iot_core.assert_called_once()  # 'online' only after success
    assert sim._ipc_client is client


def test_online_publish_failure_does_not_resubscribe(sim, monkeypatch, tmp_path, mocker):
    """A failed 'online' announcement is logged; it must not add a second subscription to the client."""
    socket_file = tmp_path / "ipc.socket"
    socket_file.touch()
    monkeypatch.setenv("SVCUID", "x")
    monkeypatch.setenv(_SOCKET_VAR, str(socket_file))
    monkeypatch.setattr(sim, "_ipc_client", None)
    monkeypatch.setattr(sim, "IPC_SUBSCRIBE_RETRY_SECONDS", 0)
    sleep = mocker.patch.object(sim.time, "sleep")

    client = mocker.Mock()
    client.subscribe_to_iot_core.return_value = (None, None)
    client.publish_to_iot_core.side_effect = RuntimeError("UnauthorizedError")

    thread = sim.start_ipc_control_listener("t/c", "t/s", "t", client_factory=lambda: client)
    thread.join(timeout=5)
    assert not thread.is_alive()

    assert client.subscribe_to_iot_core.call_count == 1
    client.publish_to_iot_core.assert_called_once()
    sleep.assert_not_called()
    assert sim._ipc_client is client


def test_stream_closed_after_subscribe_reconnects(sim, monkeypatch, tmp_path, mocker):
    """Losing an established subscription stream opens a new client and re-subscribes."""
    socket_file = tmp_path / "ipc.socket"
    socket_file.touch()
    monkeypatch.setenv("SVCUID", "x")
    monkeypatch.setenv(_SOCKET_VAR, str(socket_file))
    monkeypatch.setattr(sim, "_ipc_client", None)

    first, second = mocker.Mock(name="first"), mocker.Mock(name="second")
    for client in (first, second):
        client.subscribe_to_iot_core.return_value = (None, None)
    factory = mocker.Mock(side_effect=[first, second])

    thread = sim.start_ipc_control_listener("t/c", "t/s", "t", client_factory=factory)
    thread.join(timeout=5)
    assert sim._ipc_client is first

    # The Nucleus closes the stream: the SDK invokes on_stream_closed on an executor thread
    first.subscribe_to_iot_core.call_args.kwargs["on_stream_closed"]()
    for reconnect in [t for t in threading.enumerate() if t.name == "ipc-control"]:
        reconnect.join(timeout=5)  # the reconnect thread may already have finished
    assert factory.call_count == 2

    first.close.assert_called_once_with(executor_wait=False)
    second.subscribe_to_iot_core.assert_called_once()
    second.publish_to_iot_core.assert_called_once()  # new 'online' announcement
    assert sim._ipc_client is second


def test_stream_closed_from_rejected_subscribe_does_not_reconnect(sim, monkeypatch, tmp_path, mocker):
    """A close that accompanies a rejected subscribe is left to the retry loop (no second thread)."""
    socket_file = tmp_path / "ipc.socket"
    socket_file.touch()
    monkeypatch.setenv("SVCUID", "x")
    monkeypatch.setenv(_SOCKET_VAR, str(socket_file))
    monkeypatch.setattr(sim, "_ipc_client", None)
    monkeypatch.setattr(sim, "IPC_SUBSCRIBE_RETRY_SECONDS", 0)
    mocker.patch.object(sim.time, "sleep")

    client = mocker.Mock()
    client.subscribe_to_iot_core.side_effect = [RuntimeError("UnauthorizedError"), (None, None)]
    factory = mocker.Mock(side_effect=[client])

    thread = sim.start_ipc_control_listener("t/c", "t/s", "t", client_factory=factory)
    thread.join(timeout=5)
    assert client.subscribe_to_iot_core.call_count == 2

    # on_stream_closed handed to the *rejected* attempt must be a no-op
    client.subscribe_to_iot_core.call_args_list[0].kwargs["on_stream_closed"]()
    assert factory.call_count == 1
    client.close.assert_not_called()
    assert sim._ipc_client is client


# --- Config loading ---

def test_load_config_reads_file(sim, tmp_path, monkeypatch):
    cfg = {"endpoint": "opc.tcp://0.0.0.0:4840/simulator/", "devices": []}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(cfg))
    monkeypatch.setattr(sim.sys, "argv", ["simulator.py", str(config_file)])
    assert sim.load_config() == cfg


def test_load_config_without_args_exits(sim, monkeypatch):
    monkeypatch.setattr(sim.sys, "argv", ["simulator.py"])
    with pytest.raises(SystemExit):
        sim.load_config()
