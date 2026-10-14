# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for libs/component_config.py (component.json API)."""

# pylint: disable=missing-function-docstring

import json
import os

import pytest
from conftest import _REPO_ROOT, TEST_COMPONENT_CONFIG

from libs.component_config import ComponentConfig


def _write(tmp_path, data):
    path = tmp_path / "component.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_valid_config_exposes_all_values(tmp_path):
    cfg = ComponentConfig(_write(tmp_path, TEST_COMPONENT_CONFIG))
    assert cfg.name() == "com.amazon.samples.OpcUaDataSim"
    assert cfg.version() == "1.0.0"
    assert cfg.author() == "Amazon"
    assert cfg.region() == "us-east-1"
    assert cfg.bucket_prefix() == "opcua-data-sim-samples"
    assert cfg.bucket_name("123456789012") == "opcua-data-sim-samples-us-east-1-123456789012"


@pytest.mark.parametrize("missing", sorted(TEST_COMPONENT_CONFIG))
def test_missing_key_exits(tmp_path, missing):
    data = {k: v for k, v in TEST_COMPONENT_CONFIG.items() if k != missing}
    with pytest.raises(SystemExit) as exc:
        ComponentConfig(_write(tmp_path, data))
    assert missing in str(exc.value)


@pytest.mark.parametrize("version", ["NEXT_PATCH", "1.0", "1.0.0-beta", ""])
def test_non_semver_version_exits(tmp_path, version):
    data = dict(TEST_COMPONENT_CONFIG, version=version)
    with pytest.raises(SystemExit) as exc:
        ComponentConfig(_write(tmp_path, data))
    assert "MAJOR.MINOR.PATCH" in str(exc.value)


def test_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ComponentConfig(str(tmp_path / "nope.json"))
    assert "not found" in str(exc.value)


def test_invalid_json_exits(tmp_path):
    path = tmp_path / "component.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        ComponentConfig(str(path))
    assert "not valid JSON" in str(exc.value)


def test_repo_component_json_is_the_release_config():
    cfg = ComponentConfig(os.path.join(_REPO_ROOT, "component.json"))
    assert cfg.name() == "com.amazon.samples.OpcUaDataSim"
    assert cfg.version() == "1.0.0"
    assert cfg.author() == "Amazon"
