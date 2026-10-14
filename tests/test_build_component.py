# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for build_component.py (recipe rendering and artifact archive)."""

# pytest fixture conventions: fixtures are passed as arguments by name.
# pylint: disable=missing-function-docstring,unused-argument,redefined-outer-name

import os
import zipfile

import pytest
import yaml
from conftest import TEST_IMAGE_URI

import build_component
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


def test_zip_contains_artifacts(component_workdir, config):
    _, zip_path = build_component.build(config)
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert "config.json" in names
    assert "docker-compose.yml" in names


def test_render_recipe_is_pure(component_workdir, config):
    text = build_component.render_recipe(config, "example.com/image:tag")
    assert "docker:example.com/image:tag" in text
    assert not os.path.exists("build")


def test_placeholder_image_uri_exits(component_workdir, config):
    compose = component_workdir / "artifacts" / "docker-compose.yml"
    compose.write_text(
        compose.read_text(encoding="utf-8").replace(
            TEST_IMAGE_URI, "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/opcua-data-sim:latest"),
        encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        build_component.build(config)
    assert "build_image.sh" in str(exc.value)
    assert not os.path.exists("build/recipes/recipe.yaml")


def test_main_builds_from_component_json(component_workdir, monkeypatch):
    monkeypatch.setattr("sys.argv", ["build_component.py"])
    build_component.main()
    assert os.path.isfile("build/recipes/recipe.yaml")
    assert os.path.isfile("build/artifacts/com.amazon.samples.OpcUaDataSim/1.0.0/opcua-data-sim.zip")
