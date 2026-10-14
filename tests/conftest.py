# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Shared pytest fixtures and module loading for the simulator unit tests.

The simulator application lives in ``docker/simulator.py`` (a build context,
not an importable package), so it is loaded directly by file path.
"""

# pytest fixture conventions: fixtures are passed as arguments by name and tests
# reach into the module's private state on purpose.
# pylint: disable=missing-function-docstring,protected-access,unused-argument,redefined-outer-name

import importlib.util
import json
import os
import shutil
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
# Make build_component / publish_component / libs importable under bare ``pytest``
# (CI), which does not put the rootdir on sys.path.
sys.path.insert(0, _REPO_ROOT)
_SIMULATOR_PATH = os.path.join(_REPO_ROOT, "docker", "simulator.py")

TEST_IMAGE_URI = "123456789012.dkr.ecr.us-east-1.amazonaws.com/opcua-data-sim:test"
TEST_COMPONENT_CONFIG = {
    "componentName": "com.amazon.samples.OpcUaDataSim",
    "version": "1.0.0",
    "author": "Amazon",
    "publishBucketPrefix": "opcua-data-sim-samples",
    "region": "us-east-1",
}


def _load_simulator():
    """Load docker/simulator.py as an importable module named 'simulator'."""
    spec = importlib.util.spec_from_file_location("simulator", _SIMULATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["simulator"] = module
    spec.loader.exec_module(module)
    return module


simulator = _load_simulator()


@pytest.fixture
def sim():
    """Return the loaded simulator module."""
    return simulator


@pytest.fixture
def registered_device():
    """
    Build a DeviceState, register it in the module-level control globals,
    and reset the global paused flag. Cleans up afterwards.
    """
    device_cfg = {
        "name": "Motor01",
        "tags": [
            {"name": "Temperature", "type": "double", "pattern": "steady",
             "base": 50.0, "noise": 0.0},
            {"name": "Running", "type": "boolean", "pattern": "duty_cycle",
             "onProbability": 1.0},
            {"name": "Mode", "type": "string", "pattern": "state_machine",
             "states": ["AUTO", "MANUAL"]},
            {"name": "Count", "type": "int", "pattern": "steady", "base": 5.0},
        ],
    }
    device = simulator.DeviceState(device_cfg)

    saved_devices = simulator._devices_by_name
    saved_paused = simulator._paused
    simulator._devices_by_name = {device.name: device}
    simulator._paused = False

    yield device

    simulator._devices_by_name = saved_devices
    simulator._paused = saved_paused


@pytest.fixture
def component_workdir(tmp_path, monkeypatch):
    """
    A copy of the component sources (recipe.yaml, artifacts/) in tmp_path with a
    real image URI in docker-compose.yml and a component.json, chdir'd into so
    build_component / publish_component resolve their relative paths there.
    Returns the directory.
    """
    shutil.copy(os.path.join(_REPO_ROOT, "recipe.yaml"), tmp_path / "recipe.yaml")
    shutil.copytree(os.path.join(_REPO_ROOT, "artifacts"), tmp_path / "artifacts")
    compose = tmp_path / "artifacts" / "docker-compose.yml"
    compose.write_text(
        compose.read_text(encoding="utf-8").replace(
            "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/opcua-data-sim:latest", TEST_IMAGE_URI),
        encoding="utf-8")
    (tmp_path / "component.json").write_text(json.dumps(TEST_COMPONENT_CONFIG), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path
