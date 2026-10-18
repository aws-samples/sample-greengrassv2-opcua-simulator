# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for libs/build_component.py (recipe rendering and artifact archive)."""

# pytest fixture conventions: fixtures are passed as arguments by name.
# pylint: disable=missing-function-docstring,unused-argument,redefined-outer-name

import json
import os
import zipfile

import pytest
import yaml
from conftest import PLACEHOLDER_IMAGE_URI, TEST_COMPONENT_CONFIG, TEST_IMAGE_URI

from libs import build_component
from libs.component_config import ComponentConfig


@pytest.fixture
def config(component_workdir):
    return ComponentConfig("component.json")


def _artifacts(recipe):
    return recipe["Manifests"][0]["Artifacts"]


def test_build_returns_recipe_and_zip_paths(component_workdir, config):
    recipe_path, zip_path = build_component.build(config)
    assert recipe_path == "build/recipes/recipe.yaml"
    assert zip_path == "build/artifacts/com.amazon.samples.OpcUaDataSim/1.0.0/opcua-data-sim.zip"
    assert os.path.isfile(recipe_path)
    assert os.path.isfile(zip_path)


def test_rendered_recipe_has_component_values(component_workdir, config):
    recipe_path, _ = build_component.build(config)
    with open(recipe_path, encoding="utf-8") as f:
        text = f.read()
    recipe = yaml.safe_load(text)

    assert recipe["ComponentName"] == "com.amazon.samples.OpcUaDataSim"
    assert recipe["ComponentVersion"] == "1.0.0"
    assert recipe["ComponentPublisher"] == "Amazon"
    assert "COMPONENT_" not in text
    assert "$DOCKER_IMAGE" not in text


def test_rendered_recipe_artifacts(component_workdir, config):
    recipe_path, _ = build_component.build(config)
    with open(recipe_path, encoding="utf-8") as f:
        artifacts = _artifacts(yaml.safe_load(f))

    docker = [a for a in artifacts if a["Uri"].startswith("docker:")]
    s3 = [a for a in artifacts if a["Uri"].startswith("s3://")]
    assert len(docker) == 1 and len(s3) == 1
    assert docker[0]["Uri"] == f"docker:{TEST_IMAGE_URI}"
    # The bucket is only known at publish time; the version path must already be rendered.
    assert s3[0]["Uri"] == "s3://BUCKET_NAME/com.amazon.samples.OpcUaDataSim/1.0.0/opcua-data-sim.zip"
    assert s3[0]["Unarchive"] == "ZIP"
    assert s3[0]["Permission"] == {"Read": "ALL"}


def _lifecycle(recipe_path):
    with open(recipe_path, encoding="utf-8") as f:
        return yaml.safe_load(f)["Manifests"][0]["Lifecycle"]


def test_rendered_recipe_runs_plain_docker(component_workdir, config):
    recipe_path, _ = build_component.build(config)
    lifecycle = _lifecycle(recipe_path)

    assert set(lifecycle) == {"Run", "Shutdown", "Recover"}
    assert all(step["RequiresPrivilege"] is True for step in lifecycle.values())
    run = lifecycle["Run"]["Script"]
    assert 'PUBLISH_ADDRESS="{configuration:/publishAddress}"' in run
    assert 'exec docker run --rm --name opcua-data-sim -p "$PUBLISH_ADDRESS:4840:4840"' in run
    assert "-p 4840:4840" not in run
    assert f"  {TEST_IMAGE_URI} /app/config.json" in run
    assert "{artifacts:decompressedPath}/opcua-data-sim/config.json:/app/config.json:ro" in run
    assert '-e AWS_IOT_THING_NAME="{iot:thingName}"' in run
    for flag in ("--cap-drop ALL", "--security-opt no-new-privileges:true", "--read-only", "--tmpfs /tmp"):
        assert flag in run
    # Greengrass interpolates {namespace:key}; shell ${...} expansions must not appear.
    assert "${" not in run
    assert "compose" not in run.lower()
    assert lifecycle["Shutdown"]["Script"] == "docker stop opcua-data-sim >/dev/null 2>&1 || true"
    assert lifecycle["Recover"]["Script"] == "docker rm -f opcua-data-sim >/dev/null 2>&1 || true"


def test_rendered_recipe_default_simulator_config_is_artifact_config(component_workdir, config):
    recipe_path, _ = build_component.build(config)
    with open(recipe_path, encoding="utf-8") as f:
        text = f.read()
    default = yaml.safe_load(text)["ComponentConfiguration"]["DefaultConfiguration"]
    with open("artifacts/config.json", encoding="utf-8") as f:
        artifact_config = json.load(f)

    # An object (not a JSON string), equal to the shipped config; accessControl is untouched
    assert isinstance(default["simulatorConfig"], dict)
    assert default["simulatorConfig"] == artifact_config
    assert "aws.greengrass.ipc.mqttproxy" in default["accessControl"]
    assert "$SIMULATOR_CONFIG" not in text


def test_rendered_simulator_config_survives_placeholder_like_text(component_workdir, config):
    """Text in the config that looks like a recipe placeholder is not rewritten."""
    cfg = {"endpoint": "opc.tcp://0.0.0.0:4840/COMPONENT_NAME/", "updateIntervalSeconds": 1,
           "timeAcceleration": 60, "devices": [{"name": "Line1/$DOCKER_IMAGE", "tags": [
               {"name": "Mode", "type": "string", "states": ["a: b", "{iot:thingName}", "\u00e9"]}]}]}
    (component_workdir / "artifacts" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    recipe = yaml.safe_load(build_component.render_recipe(config, "example.com/image:tag"))
    assert recipe["ComponentConfiguration"]["DefaultConfiguration"]["simulatorConfig"] == cfg


def test_invalid_artifact_config_fails_the_build(component_workdir, config):
    (component_workdir / "artifacts" / "config.json").write_text('{"devices": 1}', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        build_component.build(config)
    assert '"devices" must be a list' in str(exc.value)


def test_zip_contains_config_only(component_workdir, config):
    _, zip_path = build_component.build(config)
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert names == ["config.json"]


def test_render_recipe_is_pure(component_workdir, config):
    text = build_component.render_recipe(config, "example.com/image:tag")
    assert "docker:example.com/image:tag" in text
    assert not os.path.exists("build")


@pytest.mark.parametrize("image", [PLACEHOLDER_IMAGE_URI, None])
def test_missing_or_placeholder_image_uri_exits(component_workdir, image):
    data = dict(TEST_COMPONENT_CONFIG)
    if image:
        data["image"] = image
    (component_workdir / "component.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        build_component.build(ComponentConfig("component.json"))
    assert "build_image.sh" in str(exc.value)
    assert not os.path.exists("build/recipes/recipe.yaml")


def test_main_builds_from_component_json(component_workdir, monkeypatch):
    monkeypatch.setattr("sys.argv", ["build_component.py"])
    build_component.main()
    assert os.path.isfile("build/recipes/recipe.yaml")
    assert os.path.isfile("build/artifacts/com.amazon.samples.OpcUaDataSim/1.0.0/opcua-data-sim.zip")


def test_rendered_recipe_publishes_on_loopback_by_default(component_workdir):
    """The unauthenticated OPC UA port is reachable only from the core unless a deployment opts in."""
    recipe = yaml.safe_load(build_component.render_recipe(ComponentConfig(), TEST_IMAGE_URI))
    assert recipe["ComponentConfiguration"]["DefaultConfiguration"]["publishAddress"] == "127.0.0.1"
