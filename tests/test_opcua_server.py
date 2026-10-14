# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Server-backed tests for the OPC UA write path in docker/simulator.py.

These start a real asyncua server on an ephemeral loopback port and check that
``update_device`` writes every supported data type with the correct variant
type, tolerates numeric overrides of the "wrong" Python type, and survives a
write rejected by the server.
"""
# pytest fixture conventions: fixtures are passed as arguments by name and tests
# reach into the module's private state on purpose.
# pylint: disable=missing-function-docstring,protected-access,unused-argument,redefined-outer-name

import asyncio
import socket

from asyncua import Server, ua

DEVICE_CFG = {
    "name": "Motor01",
    "tags": [
        {"name": "Temperature", "type": "double", "pattern": "steady",
         "base": 50.0, "noise": 0.0},
        {"name": "Count", "type": "int", "pattern": "steady",
         "base": 5.0, "noise": 0.0},
        {"name": "Running", "type": "boolean", "pattern": "duty_cycle",
         "onProbability": 1.0},
        {"name": "Mode", "type": "string", "pattern": "state_machine",
         "states": ["AUTO"]},
    ],
}


def _free_port() -> int:
    """Pick an unused loopback TCP port (asyncua does not rewrite port 0)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _run(sim):
    sim._folder_cache.clear()
    try:
        dev = sim.DeviceState(DEVICE_CFG)
        port = _free_port()

        server = Server()
        await server.init()
        server.set_endpoint(f"opc.tcp://127.0.0.1:{port}/t/")
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        server.set_identity_tokens([ua.AnonymousIdentityToken])
        idx = await server.register_namespace("urn:test")
        await sim.create_device_nodes(server, idx, server.nodes.objects, dev)

        async with server:
            # First pass: generator values (float for double, float for int tag)
            await sim.update_device(dev, 0.0)

            # Second pass: int override on a Double node, int override on Int64 node
            dev.tags["Temperature"].override_value = 200
            dev.tags["Count"].override_value = 7
            await sim.update_device(dev, 0.0)

            read = {}
            for name, tag in dev.tags.items():
                read[name] = await tag.node.read_data_value()
        return read
    finally:
        sim._folder_cache.clear()


def test_update_device_writes_all_four_types(sim):
    read = asyncio.run(_run(sim))

    assert read["Temperature"].Value.VariantType == ua.VariantType.Double
    assert read["Temperature"].Value.Value == 200.0
    assert isinstance(read["Temperature"].Value.Value, float)

    assert read["Count"].Value.VariantType == ua.VariantType.Int64
    assert read["Count"].Value.Value == 7

    assert read["Running"].Value.VariantType == ua.VariantType.Boolean
    assert read["Running"].Value.Value is True

    assert read["Mode"].Value.VariantType == ua.VariantType.String
    assert read["Mode"].Value.Value == "AUTO"


def test_update_device_survives_bad_status(sim, mocker):
    dev = sim.DeviceState({
        "name": "Motor01",
        "tags": [{"name": "Temperature", "type": "double", "pattern": "steady",
                  "base": 50.0, "noise": 0.0}],
    })
    fake_node = mocker.Mock()
    fake_node.write_value = mocker.AsyncMock(
        side_effect=ua.UaStatusCodeError(ua.StatusCodes.BadTypeMismatch)
    )
    dev.tags["Temperature"].node = fake_node

    # Must log the failure and return normally rather than propagate
    asyncio.run(sim.update_device(dev, 0.0))

    fake_node.write_value.assert_awaited_once_with(50.0, ua.VariantType.Double)


def test_update_device_skips_value_that_overflows_float(sim, mocker):
    """An override too large for float() is skipped (and other tags still written), never raised."""
    dev = sim.DeviceState({
        "name": "Motor01",
        "tags": [
            {"name": "Temperature", "type": "double", "pattern": "steady", "base": 50.0, "noise": 0.0},
            {"name": "Count", "type": "int", "pattern": "steady", "base": 5.0, "noise": 0.0},
        ],
    })
    for tag in dev.tags.values():
        tag.node = mocker.Mock()
        tag.node.write_value = mocker.AsyncMock()
    # Bypass the control-boundary validation to exercise the write-path guard directly
    dev.tags["Temperature"].override_value = 10 ** 400

    asyncio.run(sim.update_device(dev, 0.0))

    dev.tags["Temperature"].node.write_value.assert_not_awaited()
    dev.tags["Count"].node.write_value.assert_awaited_once_with(5, ua.VariantType.Int64)


def test_update_device_survives_overflow_from_write(sim, mocker):
    dev = sim.DeviceState({
        "name": "Motor01",
        "tags": [{"name": "Count", "type": "int", "pattern": "steady", "base": 5.0, "noise": 0.0}],
    })
    fake_node = mocker.Mock()
    fake_node.write_value = mocker.AsyncMock(side_effect=OverflowError("int too large"))
    dev.tags["Count"].node = fake_node

    asyncio.run(sim.update_device(dev, 0.0))

    fake_node.write_value.assert_awaited_once_with(5, ua.VariantType.Int64)
