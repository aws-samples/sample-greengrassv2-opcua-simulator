# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for deploy_component_version.py using botocore Stubber (no network, no credentials)."""

# pytest fixture conventions: fixtures and parametrize values are passed as arguments by name.
# pylint: disable=missing-function-docstring,unused-argument,redefined-outer-name
# pylint: disable=too-many-arguments,too-many-positional-arguments

import argparse
import copy
import json

import boto3
import pytest
from botocore.stub import Stubber
from conftest import TEST_COMPONENT_CONFIG

import deploy_component_version
from libs.component_config import ComponentConfig

NAME = TEST_COMPONENT_CONFIG["componentName"]
ACCOUNT = "123456789012"
THING = "MyCore"
TARGET_ARN = f"arn:aws:iot:us-east-1:{ACCOUNT}:thing/{THING}"
DAM = deploy_component_version.COMPONENT_DOCKER_APPLICATION_MANAGER
TES = deploy_component_version.COMPONENT_TOKEN_EXCHANGE_SERVICE
LOG_MANAGER = deploy_component_version.COMPONENT_LOG_MANAGER
# No custom simulatorConfig recorded: both blocks return to the recipe default of the deployed version
OUR_ENTRY = {"componentVersion": "1.0.0",
             "configurationUpdate": {"reset": ["/accessControl", "/simulatorConfig"]}}
SIM_CONFIG = {"endpoint": "opc.tcp://0.0.0.0:4840/simulator/", "updateIntervalSeconds": 1.0,
              "timeAcceleration": 60,
              "devices": [{"name": "Pump01", "tags": [{"name": "Flow"}, {"name": "Running", "type": "boolean"}]}]}


@pytest.fixture(autouse=True)
def dummy_credentials(monkeypatch):
    """Client construction must never look for real credentials or a real region."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def config(tmp_path):
    (tmp_path / "component.json").write_text(json.dumps(TEST_COMPONENT_CONFIG), encoding="utf-8")
    return ComponentConfig(str(tmp_path / "component.json"))


@pytest.fixture
def greengrass():
    """A stubbed greengrassv2 client; with no queued responses any API call raises."""
    client = boto3.client("greengrassv2", region_name="us-east-1")
    with Stubber(client) as stub:
        yield client, stub


def _ctx(config, client, **kwargs):
    return deploy_component_version.Context(config=config, client=client, account=ACCOUNT,
                                            partition="aws", version="1.0.0",
                                            core_device_thing_name=THING, **kwargs)


def _versions(stub, name, version):
    arn = f"arn:aws:greengrass:us-east-1:aws:components:{name}"
    stub.add_response("list_component_versions",
                      {"componentVersions": [{"componentName": name, "componentVersion": version,
                                              "arn": f"{arn}:versions:{version}"}]},
                      {"arn": arn})


def _log_manager_merge(deployment):
    return json.loads(deployment["components"][LOG_MANAGER]["configurationUpdate"]["merge"])


def _new_deployment_with_log_manager(config, greengrass):
    gg, stub = greengrass
    _versions(stub, DAM, "2.0.17")
    _versions(stub, TES, "2.0.3")
    _versions(stub, LOG_MANAGER, "2.3.14")
    deployment = {"targetArn": TARGET_ARN, "components": {}}
    deploy_component_version.update_deployment(_ctx(config, gg), deployment)
    stub.assert_no_pending_responses()
    return deployment


def test_log_upload_interval_constant_is_60_seconds():
    assert deploy_component_version.LOG_UPLOAD_INTERVAL_SECONDS == 60


def test_update_adds_log_manager_with_component_only_merge_and_60s(config, greengrass, capsys):
    deployment = _new_deployment_with_log_manager(config, greengrass)
    log_manager = deployment["components"][LOG_MANAGER]
    assert log_manager["componentVersion"] == "2.3.14"
    assert _log_manager_merge(deployment) == {
        "logsUploaderConfiguration": {"componentLogsConfigurationMap": {NAME: {"minimumLogLevel": "INFO"}}},
        "periodicUploadIntervalSec": 60}
    assert "systemLogsConfiguration" not in log_manager["configurationUpdate"]["merge"]
    assert deployment["components"][NAME] == OUR_ENTRY
    assert deployment["components"][DAM] == {"componentVersion": "2.0.17"}
    assert deployment["components"][TES] == {"componentVersion": "2.0.3"}
    assert "every 60 s" in capsys.readouterr().out


def test_existing_log_manager_kept_and_entry_merged(config, greengrass):
    existing_merge = {
        "logsUploaderConfiguration": {
            "systemLogsConfiguration": {"uploadToCloudWatch": "true"},
            "componentLogsConfigurationMap": {"com.example.Other": {"minimumLogLevel": "WARN"}}},
        "periodicUploadIntervalSec": 300}
    run_with = {"posixUser": "ggc_user"}
    deployment = {"targetArn": TARGET_ARN, "components": {
        DAM: {"componentVersion": "2.0.17"},
        TES: {"componentVersion": "2.0.3"},
        LOG_MANAGER: {"componentVersion": "2.3.7", "runWith": run_with,
                      "configurationUpdate": {"merge": json.dumps(existing_merge), "reset": ["/foo"]}}}}
    gg, stub = greengrass  # no responses: any list call would raise

    deploy_component_version.update_deployment(_ctx(config, gg), deployment)

    stub.assert_no_pending_responses()
    log_manager = deployment["components"][LOG_MANAGER]
    assert log_manager["componentVersion"] == "2.3.7"
    assert log_manager["runWith"] == run_with
    assert log_manager["configurationUpdate"]["reset"] == ["/foo"]
    expected = copy.deepcopy(existing_merge)
    expected["logsUploaderConfiguration"]["componentLogsConfigurationMap"][NAME] = {"minimumLogLevel": "INFO"}
    assert _log_manager_merge(deployment) == expected
    assert deployment["components"][NAME] == OUR_ENTRY


def test_existing_log_manager_without_configuration_gets_entry_only(config, greengrass):
    deployment = {"targetArn": TARGET_ARN, "components": {
        DAM: {"componentVersion": "2.0.17"},
        TES: {"componentVersion": "2.0.3"},
        LOG_MANAGER: {"componentVersion": "2.3.14"}}}
    gg, stub = greengrass

    deploy_component_version.update_deployment(_ctx(config, gg), deployment)

    stub.assert_no_pending_responses()
    assert deployment["components"][LOG_MANAGER]["componentVersion"] == "2.3.14"
    assert _log_manager_merge(deployment) == {
        "logsUploaderConfiguration": {"componentLogsConfigurationMap": {NAME: {"minimumLogLevel": "INFO"}}}}


def test_existing_log_manager_entry_for_component_untouched(config, greengrass, capsys):
    merge = json.dumps({"logsUploaderConfiguration": {
        "componentLogsConfigurationMap": {NAME: {"minimumLogLevel": "DEBUG"}}}})
    deployment = {"targetArn": TARGET_ARN, "components": {
        DAM: {"componentVersion": "2.0.17"},
        TES: {"componentVersion": "2.0.3"},
        LOG_MANAGER: {"componentVersion": "2.3.14", "configurationUpdate": {"merge": merge}}}}
    gg, _ = greengrass

    deploy_component_version.update_deployment(_ctx(config, gg), deployment)

    assert deployment["components"][LOG_MANAGER]["configurationUpdate"]["merge"] == merge
    assert "leaving its configuration unchanged" in capsys.readouterr().out


@pytest.mark.parametrize("version,warns", [("2.1.3", True), ("2.2.0", False), ("2.3.14", False)])
def test_existing_pre_2_2_log_manager_warns(config, greengrass, capsys, version, warns):
    deployment = {"targetArn": TARGET_ARN, "components": {
        DAM: {"componentVersion": "2.0.17"},
        TES: {"componentVersion": "2.0.3"},
        LOG_MANAGER: {"componentVersion": version}}}
    gg, _ = greengrass

    deploy_component_version.update_deployment(_ctx(config, gg), deployment)

    assert deployment["components"][LOG_MANAGER]["componentVersion"] == version
    assert ("predates 2.2.0" in capsys.readouterr().out) is warns


def test_existing_log_manager_malformed_merge_exits(config, greengrass, capsys):
    deployment = {"targetArn": TARGET_ARN, "components": {
        DAM: {"componentVersion": "2.0.17"},
        TES: {"componentVersion": "2.0.3"},
        LOG_MANAGER: {"componentVersion": "2.3.14", "configurationUpdate": {"merge": "{not json"}}}}
    gg, _ = greengrass

    with pytest.raises(SystemExit) as excinfo:
        deploy_component_version.update_deployment(_ctx(config, gg), deployment)

    assert excinfo.value.code == 1
    assert "is not valid JSON" in capsys.readouterr().out


def test_create_deployment_sends_log_manager(config, greengrass):
    deployment = _new_deployment_with_log_manager(config, greengrass)
    gg, stub = greengrass
    stub.add_response("create_deployment", {"deploymentId": "d-1"}, {
        "targetArn": TARGET_ARN,
        "deploymentName": f"Deployment for {THING}",
        "components": deployment["components"]})

    assert deploy_component_version.create_deployment(_ctx(config, gg), deployment) == "d-1"
    stub.assert_no_pending_responses()
    assert LOG_MANAGER in deployment["components"]


# --- simulatorConfig component configuration ---

def _existing_deployment(our_update=None):
    """A thing deployment that already has every dependency and, optionally, our component."""
    components = {DAM: {"componentVersion": "2.0.17"}, TES: {"componentVersion": "2.0.3"},
                  LOG_MANAGER: {"componentVersion": "2.3.14"}}
    if our_update is not None:
        components[NAME] = {"componentVersion": "0.9.0", "configurationUpdate": our_update}
    return {"targetArn": TARGET_ARN, "components": components}


def _our_update(config, greengrass, deployment, **ctx_kwargs):
    gg, stub = greengrass  # no responses: any list call would raise
    deploy_component_version.update_deployment(_ctx(config, gg, **ctx_kwargs), deployment)
    stub.assert_no_pending_responses()
    assert deployment["components"][NAME]["componentVersion"] == "1.0.0"
    return deployment["components"][NAME]["configurationUpdate"]


def test_config_flag_replaces_simulator_config(config, greengrass, capsys):
    recorded = {"reset": ["/accessControl"], "merge": json.dumps({"simulatorConfig": {"old": True}})}
    update = _our_update(config, greengrass, _existing_deployment(recorded), simulator_config=SIM_CONFIG)

    # Reset runs before merge, so this is a full replace of simulatorConfig
    assert update["reset"] == ["/accessControl", "/simulatorConfig"]
    assert json.loads(update["merge"]) == {"simulatorConfig": SIM_CONFIG}
    assert "replacing simulatorConfig with the --config file (1 devices, 2 tags)" in capsys.readouterr().out


def test_reset_config_flag_drops_recorded_merge(config, greengrass, capsys):
    recorded = {"reset": ["/accessControl", "/simulatorConfig"],
                "merge": json.dumps({"simulatorConfig": SIM_CONFIG})}
    update = _our_update(config, greengrass, _existing_deployment(recorded), reset_config=True)

    assert update == {"reset": ["/accessControl", "/simulatorConfig"]}
    assert "recipe default of version 1.0.0" in capsys.readouterr().out


@pytest.mark.parametrize("recorded_reset,expected_reset", [
    (["/accessControl", "/simulatorConfig"], ["/accessControl", "/simulatorConfig"]),  # full replace kept
    (["/accessControl"], ["/accessControl"]),  # partial merge over the default kept as a partial merge
])
def test_no_flag_preserves_recorded_simulator_config(config, greengrass, capsys, recorded_reset, expected_reset):
    recorded = {"reset": recorded_reset,
                "merge": json.dumps({"simulatorConfig": SIM_CONFIG, "accessControl": {"x": 1}})}
    update = _our_update(config, greengrass, _existing_deployment(recorded))

    assert update["reset"] == expected_reset
    # Only simulatorConfig is carried over; accessControl always returns to the recipe default
    assert json.loads(update["merge"]) == {"simulatorConfig": SIM_CONFIG}
    assert "keeping the simulatorConfig merge recorded in the current deployment" in capsys.readouterr().out


def test_no_flag_without_recorded_config_resets_to_default(config, greengrass):
    update = _our_update(config, greengrass, _existing_deployment({"reset": ["/accessControl"]}))
    assert update == {"reset": ["/accessControl", "/simulatorConfig"]}


def test_no_flag_malformed_recorded_merge_exits(config, greengrass, capsys):
    deployment = _existing_deployment({"merge": "{not json"})
    gg, _ = greengrass
    with pytest.raises(SystemExit) as excinfo:
        deploy_component_version.update_deployment(_ctx(config, gg), deployment)
    assert excinfo.value.code == 1
    assert "pass --config / --reset-config" in capsys.readouterr().out


@pytest.mark.parametrize("content,message", [
    ("{not json", "is not valid JSON"),
    ("[]", "must be a JSON object"),
    ('{"devices": {}}', '"devices" must be a list'),
    ('{"devices": [{"tags": []}]}', 'needs a non-empty "name"'),
    ('{"devices": [{"name": "M"}]}', 'needs a "tags" list'),
])
def test_load_simulator_config_rejects_bad_shape(tmp_path, content, message):
    path = tmp_path / "my-config.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        deploy_component_version.load_simulator_config(str(path))
    assert message in str(excinfo.value)


def test_load_simulator_config_warns_when_large(tmp_path, capsys):
    big = dict(SIM_CONFIG, devices=[{"name": f"Device{i:04d}", "tags": [{"name": "T" * 200}]}
                                    for i in range(600)])
    path = tmp_path / "big.json"
    path.write_text(json.dumps(big), encoding="utf-8")

    assert deploy_component_version.load_simulator_config(str(path)) == big
    out = capsys.readouterr().out
    assert "Warning:" in out and "over 100 KB" in out


def test_load_simulator_config_small_does_not_warn(tmp_path, capsys):
    path = tmp_path / "my-config.json"
    path.write_text(json.dumps(SIM_CONFIG), encoding="utf-8")
    assert deploy_component_version.load_simulator_config(str(path)) == SIM_CONFIG
    out = capsys.readouterr().out
    assert "Warning:" not in out
    assert "(1 devices, 2 tags)" in out


def test_main_config_and_reset_config_are_mutually_exclusive(tmp_path, monkeypatch, capsys):
    (tmp_path / "component.json").write_text(json.dumps(TEST_COMPONENT_CONFIG), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["deploy_component_version.py", "1.0.0", THING,
                                     "--config", "x.json", "--reset-config"])
    with pytest.raises(SystemExit) as excinfo:
        deploy_component_version.main()
    assert excinfo.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


@pytest.mark.parametrize("flags,expected_config,expected_reset", [
    (["--config", "my-config.json"], SIM_CONFIG, False),
    (["--reset-config"], None, True),
    ([], None, False),
])
def test_main_passes_config_options_to_context(tmp_path, monkeypatch, mocker, flags, expected_config,
                                               expected_reset):
    (tmp_path / "component.json").write_text(json.dumps(TEST_COMPONENT_CONFIG), encoding="utf-8")
    (tmp_path / "my-config.json").write_text(json.dumps(SIM_CONFIG), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["deploy_component_version.py", "1.0.0", THING, *flags])
    sts = mocker.Mock()
    sts.get_caller_identity.return_value = {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/x"}
    mocker.patch.object(deploy_component_version.boto3, "client", side_effect=[sts, mocker.Mock()])
    mocker.patch.object(deploy_component_version, "get_deployment", return_value={"components": {}})
    update = mocker.patch.object(deploy_component_version, "update_deployment")
    mocker.patch.object(deploy_component_version, "create_deployment", return_value="d-1")
    mocker.patch.object(deploy_component_version, "wait_for_deployment_to_finish")

    deploy_component_version.main()

    ctx = update.call_args.args[0]
    assert ctx.simulator_config == expected_config
    assert ctx.reset_config is expected_reset


COLLECTOR = deploy_component_version.COMPONENT_SITEWISE_COLLECTOR


@pytest.mark.parametrize("components,previous,expect", [
    ({COLLECTOR: {"componentVersion": "3.2.1"}}, "0.9.0", "0.9.0 -> 1.0.0"),
    ({COLLECTOR: {"componentVersion": "3.2.1"}}, None, "new install of 1.0.0"),
    ({COLLECTOR: {"componentVersion": "3.2.1"}}, "1.0.0", None),
    ({}, "0.9.0", None),
])
def test_collector_restart_hint(config, components, previous, expect):
    ctx = _ctx(config, None)
    hint = deploy_component_version.collector_restart_hint(ctx, {"components": components}, previous)
    if expect is None:
        assert hint is None
    else:
        assert expect in hint
        assert deploy_component_version.COLLECTOR_RESTART_COMMAND in hint


def test_update_deployment_returns_previous_version(config, mocker):
    ctx = _ctx(config, None)
    mocker.patch.object(deploy_component_version, "add_log_manager")
    deployment = {"components": {
        deploy_component_version.COMPONENT_DOCKER_APPLICATION_MANAGER: {"componentVersion": "2.0.17"},
        deploy_component_version.COMPONENT_TOKEN_EXCHANGE_SERVICE: {"componentVersion": "2.0.3"},
        NAME: {"componentVersion": "0.9.0"}}}
    assert deploy_component_version.update_deployment(ctx, deployment) == "0.9.0"
    assert deployment["components"][NAME]["componentVersion"] == "1.0.0"


def test_publish_address_flag_is_merged(config):
    ctx = _ctx(config, None, publish_address="10.1.3.92")
    update = deploy_component_version.with_publish_address(ctx, {}, {"reset": ["/accessControl"]})
    assert update["reset"] == ["/accessControl"]
    assert json.loads(update["merge"]) == {"publishAddress": "10.1.3.92"}


def test_publish_address_kept_from_existing_deployment_with_simulator_config(config):
    ctx = _ctx(config, None)
    existing = {"configurationUpdate": {"merge": json.dumps({"publishAddress": "0.0.0.0"})}}
    base = {"reset": ["/accessControl"], "merge": json.dumps({"simulatorConfig": SIM_CONFIG})}
    update = deploy_component_version.with_publish_address(ctx, existing, base)
    assert json.loads(update["merge"]) == {"simulatorConfig": SIM_CONFIG, "publishAddress": "0.0.0.0"}


def test_publish_address_absent_leaves_recipe_default(config):
    base = {"reset": ["/accessControl", "/simulatorConfig"]}
    assert deploy_component_version.with_publish_address(_ctx(config, None), {}, base) == base


@pytest.mark.parametrize("value,ok", [("127.0.0.1", True), ("0.0.0.0", True), ("::1", False),
                                      ("1.2.3.4;rm -rf /", False), ("host.example", False)])
def test_validate_publish_address(value, ok):
    if ok:
        assert deploy_component_version.validate_publish_address(value) == value
    else:
        with pytest.raises(argparse.ArgumentTypeError):
            deploy_component_version.validate_publish_address(value)


@pytest.mark.parametrize("address,previous,expect_hint", [
    ("10.1.3.92", "1.0.0", True),     # same version already running: Greengrass does not restart it
    ("10.1.3.92", "0.9.0", False),    # version change restarts the container anyway
    ("10.1.3.92", None, False),       # fresh install starts with the new address
    (None, "1.0.0", False),           # no --publish-address
])
def test_publish_address_hint(config, address, previous, expect_hint):
    hint = deploy_component_version.publish_address_hint(_ctx(config, None, publish_address=address), previous)
    assert (hint is not None) == expect_hint
    if expect_hint:
        assert deploy_component_version.SIMULATOR_RESTART_COMMAND in hint
