# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for publish_component.py using botocore Stubber (no network, no credentials)."""

# pytest fixture conventions: fixtures are passed as arguments by name.
# pylint: disable=missing-function-docstring,unused-argument,redefined-outer-name

import json
import os
import time

import boto3
import pytest
import yaml
from botocore.stub import ANY, Stubber
from conftest import TEST_COMPONENT_CONFIG, TEST_IMAGE_URI

import build_component
import publish_component
from libs.component_config import ComponentConfig

NAME = TEST_COMPONENT_CONFIG["componentName"]
VERSION = TEST_COMPONENT_CONFIG["version"]
ACCOUNT = "123456789012"
BUCKET = f"opcua-data-sim-samples-us-east-1-{ACCOUNT}"
ARN = f"arn:aws:greengrass:us-east-1:{ACCOUNT}:components:{NAME}:versions:{VERSION}"
KEY = f"{NAME}/{VERSION}/opcua-data-sim.zip"
S3_URI = f"s3://{BUCKET}/{KEY}"
ALL_BLOCKED = {"BlockPublicAcls": True, "IgnorePublicAcls": True,
               "BlockPublicPolicy": True, "RestrictPublicBuckets": True}
SSE_S3 = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}


@pytest.fixture(autouse=True)
def dummy_credentials(monkeypatch):
    """Client construction must never look for real credentials or a real region."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "component.json"
    path.write_text(json.dumps(TEST_COMPONENT_CONFIG), encoding="utf-8")
    return ComponentConfig(str(path))


def _stubbed(service, region="us-east-1"):
    client = boto3.client(service, region_name=region)
    stubber = Stubber(client)
    stubber.activate()
    return client, stubber


def _describe_response(state, message=None):
    status = {"componentState": state}
    if message:
        status["message"] = message
        status["errors"] = {"ValidationError": message}
    return {"arn": ARN, "componentName": NAME, "componentVersion": VERSION, "status": status}


# --- component_version_arn / caller_identity ---

def test_component_version_arn(config):
    assert publish_component.component_version_arn("aws", "us-east-1", ACCOUNT, config) == ARN


def test_component_version_arn_other_partition(config):
    arn = publish_component.component_version_arn("aws-cn", "cn-north-1", ACCOUNT, config)
    assert arn.startswith(f"arn:aws-cn:greengrass:cn-north-1:{ACCOUNT}:components:")


def test_caller_identity_returns_account_and_partition():
    sts, stub = _stubbed("sts")
    stub.add_response("get_caller_identity", {
        "Account": ACCOUNT, "Arn": f"arn:aws-us-gov:iam::{ACCOUNT}:user/dev", "UserId": "AID"})
    assert publish_component.caller_identity(sts) == (ACCOUNT, "aws-us-gov")
    stub.assert_no_pending_responses()


# --- refuse_if_version_exists ---

def test_version_already_exists_refuses(config):
    gg, stub = _stubbed("greengrassv2")
    stub.add_response("describe_component", _describe_response("DEPLOYABLE"), {"arn": ARN})
    with pytest.raises(SystemExit) as exc:
        publish_component.refuse_if_version_exists(gg, ARN, config)
    message = str(exc.value)
    assert "already exists" in message
    assert "component.json" in message
    assert VERSION in message
    stub.assert_no_pending_responses()


def test_version_absent_continues(config):
    gg, stub = _stubbed("greengrassv2")
    stub.add_client_error("describe_component", "ResourceNotFoundException", "not found",
                          http_status_code=404, expected_params={"arn": ARN})
    publish_component.refuse_if_version_exists(gg, ARN, config)
    stub.assert_no_pending_responses()


def test_version_check_other_error_exits(config):
    gg, stub = _stubbed("greengrassv2")
    stub.add_client_error("describe_component", "AccessDeniedException", "denied", http_status_code=403)
    with pytest.raises(SystemExit) as exc:
        publish_component.refuse_if_version_exists(gg, ARN, config)
    assert "AccessDeniedException" in str(exc.value)


# --- ensure_bucket ---

def test_bucket_exists_is_reused(capsys):
    s3, stub = _stubbed("s3")
    stub.add_response("head_bucket", {}, {"Bucket": BUCKET})
    assert publish_component.ensure_bucket(s3, BUCKET, "us-east-1") is False
    stub.assert_no_pending_responses()
    assert f"Using existing artifact bucket {BUCKET}" in capsys.readouterr().out


def test_bucket_missing_us_east_1_created_without_location_constraint(capsys):
    s3, stub = _stubbed("s3")
    stub.add_client_error("head_bucket", "404", "Not Found", http_status_code=404)
    stub.add_response("create_bucket", {"Location": f"/{BUCKET}"}, {"Bucket": BUCKET})
    stub.add_response("put_public_access_block", {},
                      {"Bucket": BUCKET, "PublicAccessBlockConfiguration": ALL_BLOCKED})
    stub.add_response("put_bucket_encryption", {},
                      {"Bucket": BUCKET, "ServerSideEncryptionConfiguration": SSE_S3})
    assert publish_component.ensure_bucket(s3, BUCKET, "us-east-1") is True
    stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert f"Created artifact bucket {BUCKET}" in out
    assert "s3:GetObject" in out


def test_bucket_missing_other_region_uses_location_constraint():
    bucket = f"opcua-data-sim-samples-eu-west-1-{ACCOUNT}"
    s3, stub = _stubbed("s3", region="eu-west-1")
    stub.add_client_error("head_bucket", "404", "Not Found", http_status_code=404)
    stub.add_response("create_bucket", {"Location": f"http://{bucket}.s3.amazonaws.com/"},
                      {"Bucket": bucket, "CreateBucketConfiguration": {"LocationConstraint": "eu-west-1"}})
    stub.add_response("put_public_access_block", {},
                      {"Bucket": bucket, "PublicAccessBlockConfiguration": ALL_BLOCKED})
    stub.add_response("put_bucket_encryption", {},
                      {"Bucket": bucket, "ServerSideEncryptionConfiguration": SSE_S3})
    assert publish_component.ensure_bucket(s3, bucket, "eu-west-1") is True
    stub.assert_no_pending_responses()


def test_created_bucket_hardening_params_are_exact():
    """A wrong PublicAccessBlock or encryption parameter must make the Stubber raise."""
    s3, stub = _stubbed("s3")
    stub.add_client_error("head_bucket", "NoSuchBucket", "missing", http_status_code=404)
    stub.add_response("create_bucket", {}, {"Bucket": BUCKET})
    partially_blocked = dict(ALL_BLOCKED, RestrictPublicBuckets=False)
    stub.add_response("put_public_access_block", {},
                      {"Bucket": BUCKET, "PublicAccessBlockConfiguration": partially_blocked})
    with pytest.raises(Exception) as exc:
        publish_component.ensure_bucket(s3, BUCKET, "us-east-1")
    assert "Expected parameters" in str(exc.value)


def test_head_bucket_forbidden_exits():
    s3, stub = _stubbed("s3")
    stub.add_client_error("head_bucket", "403", "Forbidden", http_status_code=403)
    with pytest.raises(SystemExit) as exc:
        publish_component.ensure_bucket(s3, BUCKET, "us-east-1")
    assert BUCKET in str(exc.value)
    assert "publishBucketPrefix" in str(exc.value)
    stub.assert_no_pending_responses()


# --- upload_artifact / artifact_key ---

def test_artifact_key(config):
    assert publish_component.artifact_key(config) == KEY


def test_upload_artifact_puts_object(tmp_path):
    zip_path = tmp_path / "opcua-data-sim.zip"
    zip_path.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    s3, stub = _stubbed("s3")
    stub.add_response("put_object", {}, {"Bucket": BUCKET, "Key": KEY, "Body": ANY})
    assert publish_component.upload_artifact(s3, BUCKET, KEY, str(zip_path)) == S3_URI
    stub.assert_no_pending_responses()


# --- rewrite_artifact_uri / load_recipe ---

def _rendered_recipe(component_workdir, config):
    return yaml.safe_load(build_component.render_recipe(config, TEST_IMAGE_URI))


def test_rewrite_artifact_uri_replaces_only_s3_entry(component_workdir, config):
    recipe = _rendered_recipe(component_workdir, config)
    publish_component.rewrite_artifact_uri(recipe, S3_URI)
    artifacts = recipe["Manifests"][0]["Artifacts"]
    assert artifacts[0] == {"Uri": f"docker:{TEST_IMAGE_URI}"}
    assert artifacts[1]["Uri"] == S3_URI
    assert artifacts[1]["Unarchive"] == "ZIP"
    assert artifacts[1]["Permission"] == {"Read": "ALL"}
    assert "BUCKET_NAME" not in json.dumps(recipe)


def test_rewrite_artifact_uri_without_s3_entry_exits():
    recipe = {"Manifests": [{"Artifacts": [{"Uri": "docker:example/image"}]}]}
    with pytest.raises(SystemExit) as exc:
        publish_component.rewrite_artifact_uri(recipe, S3_URI)
    assert "no s3://" in str(exc.value)


def test_load_recipe_from_built_file(component_workdir, config):
    recipe_path, _ = build_component.build(config)
    recipe = publish_component.load_recipe(recipe_path, S3_URI)
    assert recipe["ComponentName"] == NAME
    assert recipe["Manifests"][0]["Artifacts"][1]["Uri"] == S3_URI


# --- create_component_version ---

def test_create_component_version_writes_json_and_sends_inline_recipe(tmp_path):
    recipe = {"RecipeFormatVersion": "2020-01-25", "ComponentName": NAME, "ComponentVersion": VERSION}
    expected_bytes = json.dumps(recipe, indent=2).encode("utf-8")
    gg, stub = _stubbed("greengrassv2")
    stub.add_response("create_component_version",
                      {"arn": ARN, "componentName": NAME, "componentVersion": VERSION,
                       "creationTimestamp": 0, "status": {"componentState": "REQUESTED"}},
                      {"inlineRecipe": expected_bytes})
    path = tmp_path / "build" / "recipes" / f"{NAME}-{VERSION}.json"
    response = publish_component.create_component_version(gg, recipe, str(path))
    assert response["arn"] == ARN
    assert path.read_bytes() == expected_bytes
    stub.assert_no_pending_responses()


def test_create_component_version_error_exits(tmp_path):
    gg, stub = _stubbed("greengrassv2")
    stub.add_client_error("create_component_version", "ValidationException", "bad recipe", http_status_code=400)
    with pytest.raises(SystemExit) as exc:
        publish_component.create_component_version(gg, {"a": 1}, str(tmp_path / "r.json"))
    assert "bad recipe" in str(exc.value)


# --- wait_until_deployable ---

def test_wait_until_deployable_polls_until_deployable(no_sleep):
    gg, stub = _stubbed("greengrassv2")
    for state in ("REQUESTED", "INITIATED", "DEPLOYABLE"):
        stub.add_response("describe_component", _describe_response(state), {"arn": ARN})
    publish_component.wait_until_deployable(gg, ARN)
    stub.assert_no_pending_responses()


def test_wait_until_deployable_failed_exits(no_sleep):
    gg, stub = _stubbed("greengrassv2")
    stub.add_response("describe_component", _describe_response("FAILED", "Artifact not found"), {"arn": ARN})
    with pytest.raises(SystemExit) as exc:
        publish_component.wait_until_deployable(gg, ARN)
    assert "FAILED" in str(exc.value)
    assert "Artifact not found" in str(exc.value)


def test_wait_until_deployable_times_out(monkeypatch, no_sleep):
    clock = {"now": 1000.0}

    def fake_monotonic():
        clock["now"] += 50.0
        return clock["now"]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    gg, stub = _stubbed("greengrassv2")
    for _ in range(5):
        stub.add_response("describe_component", _describe_response("REQUESTED"), {"arn": ARN})
    with pytest.raises(SystemExit) as exc:
        publish_component.wait_until_deployable(gg, ARN, timeout_seconds=120)
    assert str(exc.value).startswith("Timed out")
    assert "REQUESTED" in str(exc.value)


# --- main happy path ---

def test_main_happy_path(component_workdir, monkeypatch, no_sleep, capsys):
    sts, sts_stub = _stubbed("sts")
    sts_stub.add_response("get_caller_identity", {
        "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/dev", "UserId": "AID"})

    gg, gg_stub = _stubbed("greengrassv2")
    gg_stub.add_client_error("describe_component", "ResourceNotFoundException", "not found",
                             http_status_code=404, expected_params={"arn": ARN})
    gg_stub.add_response("create_component_version",
                         {"arn": ARN, "componentName": NAME, "componentVersion": VERSION,
                          "creationTimestamp": 0, "status": {"componentState": "REQUESTED"}},
                         {"inlineRecipe": ANY})
    gg_stub.add_response("describe_component", _describe_response("DEPLOYABLE"), {"arn": ARN})

    s3, s3_stub = _stubbed("s3")
    s3_stub.add_response("head_bucket", {}, {"Bucket": BUCKET})
    s3_stub.add_response("put_object", {}, {"Bucket": BUCKET, "Key": KEY, "Body": ANY})

    clients = {"sts": sts, "greengrassv2": gg, "s3": s3}
    monkeypatch.setattr(boto3, "client", lambda service, region_name=None: clients[service])
    monkeypatch.setattr("sys.argv", ["publish_component.py"])

    publish_component.main()

    for stub in (sts_stub, gg_stub, s3_stub):
        stub.assert_no_pending_responses()
    recipe_json = component_workdir / "build" / "recipes" / f"{NAME}-{VERSION}.json"
    assert recipe_json.is_file()
    recipe = json.loads(recipe_json.read_text(encoding="utf-8"))
    assert recipe["ComponentPublisher"] == "Amazon"
    assert recipe["Manifests"][0]["Artifacts"][1]["Uri"] == S3_URI
    assert os.path.isfile(component_workdir / "build" / "artifacts" / NAME / VERSION / "opcua-data-sim.zip")
    out = capsys.readouterr().out
    assert f"Created component version {VERSION}: {ARN}" in out
    assert f"python3 deploy_component_version.py {VERSION}" in out


def test_main_config_option(component_workdir, monkeypatch):
    """--config is honoured and the version check runs before any S3 call."""
    other = component_workdir / "other.json"
    other.write_text(json.dumps(dict(TEST_COMPONENT_CONFIG, version="2.0.0")), encoding="utf-8")

    sts, sts_stub = _stubbed("sts")
    sts_stub.add_response("get_caller_identity", {
        "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/dev", "UserId": "AID"})
    gg, gg_stub = _stubbed("greengrassv2")
    gg_stub.add_response("describe_component", _describe_response("DEPLOYABLE"),
                         {"arn": ARN.replace(VERSION, "2.0.0")})
    s3, s3_stub = _stubbed("s3")  # no responses: any S3 call would raise

    clients = {"sts": sts, "greengrassv2": gg, "s3": s3}
    monkeypatch.setattr(boto3, "client", lambda service, region_name=None: clients[service])
    monkeypatch.setattr("sys.argv", ["publish_component.py", "--config", str(other)])

    with pytest.raises(SystemExit) as exc:
        publish_component.main()
    assert "version 2.0.0 already exists" in str(exc.value)
    for stub in (sts_stub, gg_stub, s3_stub):
        stub.assert_no_pending_responses()
