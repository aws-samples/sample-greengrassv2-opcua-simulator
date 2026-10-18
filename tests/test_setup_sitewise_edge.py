# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for setup_sitewise_edge.py using botocore Stubber (no network, no credentials)."""

# pytest fixture conventions: fixtures are passed as arguments by name.
# pylint: disable=missing-function-docstring,unused-argument,redefined-outer-name

import copy
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import boto3
import pytest
from botocore.stub import Stubber
from conftest import TEST_COMPONENT_CONFIG, _REPO_ROOT

import setup_sitewise_edge as sse
from deploy_component_version import Context
from libs.component_config import ComponentConfig

ACCOUNT = "123456789012"
REGION = "us-east-1"
THING = "MyCore"
THING_ARN = f"arn:aws:iot:{REGION}:{ACCOUNT}:thing/{THING}"
SIM = TEST_COMPONENT_CONFIG["componentName"]
GATEWAY_ID = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)
LATEST = {sse.COMPONENT_EMQX: "2.0.4", sse.COMPONENT_COLLECTOR: "3.2.1", sse.COMPONENT_PUBLISHER: "4.1.5"}

# Copied verbatim from the README "Same core" CLI steps
README_COLLECTOR = ('{"sources":[{"name":"opcua-sim","endpoint":{"certificateTrust":{"type":"TrustAny"},'
                    '"endpointUri":"opc.tcp://localhost:4840/simulator/","securityPolicy":"NONE",'
                    '"messageSecurityMode":"NONE","identityProvider":{"type":"Anonymous"},"nodeFilterRules":[]},'
                    '"measurementDataStreamPrefix":"/opcua-sim","destination":{"type":"MQTT"}}]}')
README_PUBLISHER = ('{"sources":[{"type":"MQTT"}],"destinations":[{"type":"SITEWISE_REALTIME",'
                    '"name":"sitewise-realtime","config":{"publishingOrder":"TIME_ORDER","enableCompression":true,'
                    '"maxBatchWaitTime":"10s"},"filters":[{"type":"PATH","config":{"paths":["#"]}}]}]}')
README_EMQX_MERGE = ('{"emqxConfig":{"authorization":{"no_match":"allow"},"listeners":{"tcp":{"default":'
                     '{"enabled":true,"enable_authn":false}}}},"authMode":"bypass","dockerOptions":'
                     '"-p 127.0.0.1:1883:1883 -v emqx-data:/opt/emqx/data -e EMQX_NODE__NAME=emqx@local",'
                     '"requiresPrivilege":"true"}')

BASE_COMPONENTS = {
    "aws.greengrass.DockerApplicationManager": {"componentVersion": "2.0.17"},
    "aws.greengrass.TokenExchangeService": {"componentVersion": "2.0.3"},
    SIM: {"componentVersion": "1.0.0", "configurationUpdate": {"reset": ["/accessControl"]}},
}


@pytest.fixture(autouse=True)
def dummy_credentials(monkeypatch):
    """Client construction must never look for real credentials or a real region."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "component.json"
    path.write_text(json.dumps(TEST_COMPONENT_CONFIG), encoding="utf-8")
    return ComponentConfig(str(path))


def _stubbed(service):
    client = boto3.client(service, region_name=REGION)
    stubber = Stubber(client)
    stubber.activate()
    return client, stubber


@pytest.fixture
def gg():
    return _stubbed("greengrassv2")


@pytest.fixture
def sw():
    return _stubbed("iotsitewise")


@pytest.fixture
def ctx(config, gg):
    return Context(config=config, client=gg[0], account=ACCOUNT, partition="aws",
                   version="1.0.0", core_device_thing_name=THING)


def _gateway_summary(thing=THING, gateway_id=GATEWAY_ID, version="3"):
    return {"gatewayId": gateway_id, "gatewayName": sse.GATEWAY_NAME, "gatewayVersion": version,
            "gatewayPlatform": {"greengrassV2": {"coreDeviceThingName": thing}},
            "creationDate": NOW, "lastUpdateDate": NOW}


def _add_existing_deployment(stub, components):
    stub.add_response("list_deployments", {"deployments": [{"deploymentId": "dep-old"}]},
                      {"targetArn": THING_ARN, "historyFilter": "LATEST_ONLY", "maxResults": 1})
    stub.add_response("get_deployment", {"targetArn": THING_ARN, "deploymentId": "dep-old",
                                         "deploymentName": "Deployment for MyCore",
                                         "components": copy.deepcopy(components)},
                      {"deploymentId": "dep-old"})


def _add_latest_versions(stub, names):
    for name in names:
        stub.add_response("list_component_versions",
                          {"componentVersions": [{"componentName": name, "componentVersion": LATEST[name]}]},
                          {"arn": f"arn:aws:greengrass:{REGION}:aws:components:{name}"})


def _add_deploy(stub, components):
    stub.add_response("create_deployment", {"deploymentId": "dep-new"},
                      {"targetArn": THING_ARN, "deploymentName": "Deployment for MyCore", "components": components})
    stub.add_response("get_deployment", {"deploymentStatus": "COMPLETED"}, {"deploymentId": "dep-new"})


def _sync_response(collector, publisher):
    return {"gatewayId": GATEWAY_ID, "gatewayName": sse.GATEWAY_NAME, "gatewayArn": "arn:gw",
            "creationDate": NOW, "lastUpdateDate": NOW,
            "gatewayCapabilitySummaries": [
                {"capabilityNamespace": sse.COLLECTOR_NAMESPACE, "capabilitySyncStatus": collector},
                {"capabilityNamespace": sse.PUBLISHER_NAMESPACE, "capabilitySyncStatus": publisher}]}


def _time_series(*aliases):
    return {"TimeSeriesSummaries": [
        {"alias": alias, "timeSeriesId": f"{i:08d}-2222-3333-4444-555555555555", "dataType": "DOUBLE",
         "timeSeriesCreationDate": NOW, "timeSeriesLastUpdateDate": NOW, "timeSeriesArn": f"arn:ts:{i}"}
        for i, alias in enumerate(aliases)]}


# --- core_device_operating_system ---

@pytest.mark.parametrize("architecture,expected", [
    ("amd64", "LINUX_AMD64"), ("x86_64", "LINUX_AMD64"), ("aarch64", "LINUX_AARCH64"), ("arm64", "LINUX_AARCH64")])
def test_operating_system_mapping(gg, architecture, expected):
    client, stub = gg
    stub.add_response("get_core_device", {"platform": "linux", "architecture": architecture},
                      {"coreDeviceThingName": THING})
    assert sse.core_device_operating_system(client, THING, REGION) == expected
    stub.assert_no_pending_responses()


def test_operating_system_unsupported_exits(gg):
    client, stub = gg
    stub.add_response("get_core_device", {"platform": "windows", "architecture": "amd64"})
    with pytest.raises(SystemExit) as exc:
        sse.core_device_operating_system(client, THING, REGION)
    assert "windows" in str(exc.value)
    assert "supports Linux" in str(exc.value)


def test_core_device_not_found_names_region(gg):
    client, stub = gg
    stub.add_client_error("get_core_device", "ResourceNotFoundException", "missing", http_status_code=404)
    with pytest.raises(SystemExit) as exc:
        sse.core_device_operating_system(client, THING, REGION)
    assert f"{THING} not found in {REGION}" in str(exc.value)


# --- ensure_gateway ---

def test_existing_gateway_is_reused(sw, gg, capsys):
    client, stub = sw
    other = _gateway_summary(thing="Other", gateway_id="99999999-2222-3333-4444-555555555555")
    stub.add_response("list_gateways", {"gatewaySummaries": [other], "nextToken": "t"}, {})
    stub.add_response("list_gateways", {"gatewaySummaries": [_gateway_summary()]}, {"nextToken": "t"})
    assert sse.ensure_gateway(client, gg[0], THING, REGION) == GATEWAY_ID
    stub.assert_no_pending_responses()
    gg[1].assert_no_pending_responses()  # no get_core_device when reusing
    assert "Using existing SiteWise gateway" in capsys.readouterr().out


def test_existing_non_v3_gateway_refused(sw, gg):
    client, stub = sw
    stub.add_response("list_gateways", {"gatewaySummaries": [_gateway_summary(version="2")]}, {})
    with pytest.raises(SystemExit) as exc:
        sse.ensure_gateway(client, gg[0], THING, REGION)
    assert "--delete" in str(exc.value)


def test_gateway_created_when_missing(sw, gg, capsys):
    client, stub = sw
    gg[1].add_response("get_core_device", {"platform": "linux", "architecture": "aarch64"},
                       {"coreDeviceThingName": THING})
    stub.add_response("list_gateways", {"gatewaySummaries": []}, {})
    stub.add_response("create_gateway", {"gatewayId": GATEWAY_ID, "gatewayArn": "arn:gw"}, {
        "gatewayName": "opcua-sim-gateway", "gatewayVersion": "3",
        "gatewayPlatform": {"greengrassV2": {"coreDeviceThingName": THING,
                                             "coreDeviceOperatingSystem": "LINUX_AARCH64"}}})
    assert sse.ensure_gateway(client, gg[0], THING, REGION) == GATEWAY_ID
    stub.assert_no_pending_responses()
    assert "Created SiteWise gateway opcua-sim-gateway" in capsys.readouterr().out


# --- capabilities ---

def test_capability_payloads_match_readme(sw):
    client, stub = sw
    stub.add_response("update_gateway_capability_configuration",
                      {"capabilityNamespace": sse.COLLECTOR_NAMESPACE, "capabilitySyncStatus": "OUT_OF_SYNC"},
                      {"gatewayId": GATEWAY_ID, "capabilityNamespace": "iotsitewise:opcuacollector:3",
                       "capabilityConfiguration": README_COLLECTOR})
    stub.add_response("update_gateway_capability_configuration",
                      {"capabilityNamespace": sse.PUBLISHER_NAMESPACE, "capabilitySyncStatus": "OUT_OF_SYNC"},
                      {"gatewayId": GATEWAY_ID, "capabilityNamespace": "iotsitewise:publisher:3",
                       "capabilityConfiguration": README_PUBLISHER})
    sse.configure_capabilities(client, GATEWAY_ID, "/opcua-sim")
    stub.assert_no_pending_responses()


def test_collector_uses_given_prefix_and_localhost():
    source = sse.collector_configuration("/plant1")["sources"][0]
    assert source["measurementDataStreamPrefix"] == "/plant1"
    assert source["endpoint"]["endpointUri"] == "opc.tcp://localhost:4840/simulator/"


# --- deployment components ---

def test_components_added_with_latest_versions(ctx, gg):
    _, stub = gg
    _add_latest_versions(stub, sse.SITEWISE_COMPONENTS)
    deployment = {"targetArn": THING_ARN, "components": copy.deepcopy(BASE_COMPONENTS)}
    sse.add_sitewise_components(ctx, deployment)
    stub.assert_no_pending_responses()
    components = deployment["components"]
    for name, version in BASE_COMPONENTS.items():
        assert components[name] == version  # untouched
    assert components[sse.COMPONENT_EMQX] == {"componentVersion": "2.0.4",
                                              "configurationUpdate": {"merge": README_EMQX_MERGE}}
    assert components[sse.COMPONENT_COLLECTOR] == {"componentVersion": "3.2.1"}
    assert components[sse.COMPONENT_PUBLISHER] == {"componentVersion": "4.1.5"}


def test_existing_component_versions_preserved(ctx, gg):
    _, stub = gg
    _add_latest_versions(stub, [sse.COMPONENT_PUBLISHER])
    components = dict(copy.deepcopy(BASE_COMPONENTS), **{
        sse.COMPONENT_EMQX: {"componentVersion": "2.0.1"},
        sse.COMPONENT_COLLECTOR: {"componentVersion": "3.0.0"}})
    deployment = {"targetArn": THING_ARN, "components": components}
    sse.add_sitewise_components(ctx, deployment)
    stub.assert_no_pending_responses()  # only the publisher's latest version was looked up
    assert components[sse.COMPONENT_EMQX] == {"componentVersion": "2.0.1",
                                              "configurationUpdate": {"merge": README_EMQX_MERGE}}
    assert components[sse.COMPONENT_COLLECTOR] == {"componentVersion": "3.0.0"}
    assert components[sse.COMPONENT_PUBLISHER] == {"componentVersion": "4.1.5"}


def test_setup_refuses_without_simulator(ctx, gg, sw):
    _, stub = gg
    stub.add_response("list_deployments", {"deployments": []})
    with pytest.raises(SystemExit) as exc:
        sse.setup(ctx, sw[0])
    assert "deploy_component_version.py" in str(exc.value)
    assert SIM in str(exc.value)
    sw[1].assert_no_pending_responses()  # no gateway created


# --- waits ---

def test_wait_in_sync_succeeds_after_polling(sw):
    client, stub = sw
    stub.add_response("describe_gateway", _sync_response("OUT_OF_SYNC", "OUT_OF_SYNC"), {"gatewayId": GATEWAY_ID})
    stub.add_response("describe_gateway", _sync_response("IN_SYNC", "OUT_OF_SYNC"), {"gatewayId": GATEWAY_ID})
    stub.add_response("describe_gateway", _sync_response("IN_SYNC", "IN_SYNC"), {"gatewayId": GATEWAY_ID})
    sse.wait_for_capabilities_in_sync(client, GATEWAY_ID)
    stub.assert_no_pending_responses()


def test_wait_in_sync_times_out(sw, monkeypatch):
    monkeypatch.setattr(sse, "SYNC_TIMEOUT_SECONDS", 20)
    monkeypatch.setattr(sse, "SYNC_POLL_SECONDS", 10)
    client, stub = sw
    for _ in range(2):
        stub.add_response("describe_gateway", _sync_response("IN_SYNC", "SYNC_FAILED"))
    with pytest.raises(SystemExit) as exc:
        sse.wait_for_capabilities_in_sync(client, GATEWAY_ID)
    assert str(exc.value).startswith("Timed out")
    assert GATEWAY_ID in str(exc.value)
    assert "SYNC_FAILED" in str(exc.value)
    stub.assert_no_pending_responses()


def test_data_streams_found(sw, capsys):
    client, stub = sw
    expected = {"timeSeriesType": "DISASSOCIATED", "aliasPrefix": "/opcua-sim"}
    stub.add_response("list_time_series", _time_series(), expected)
    stub.add_response("list_time_series", _time_series("/opcua-sim/Motor01/Temperature",
                                                       "/opcua-sim/Motor01/Count"), expected)
    aliases = sse.wait_for_data_streams(client, "/opcua-sim")
    assert aliases == ["/opcua-sim/Motor01/Count", "/opcua-sim/Motor01/Temperature"]
    sse.print_next_steps(aliases, "/opcua-sim")
    out = capsys.readouterr().out
    assert "  /opcua-sim/Motor01/Temperature" in out
    assert "associate-time-series-to-asset-property" in out


def test_data_streams_none_prints_cli(sw, monkeypatch, capsys):
    monkeypatch.setattr(sse, "TIME_SERIES_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(sse, "TIME_SERIES_POLL_SECONDS", 15)
    client, stub = sw
    for _ in range(2):
        stub.add_response("list_time_series", _time_series())
    aliases = sse.wait_for_data_streams(client, "/opcua-sim")
    stub.assert_no_pending_responses()
    assert aliases == []
    sse.print_next_steps(aliases, "/opcua-sim")
    out = capsys.readouterr().out
    assert "few minutes" in out
    assert "aws iotsitewise list-time-series --time-series-type DISASSOCIATED --alias-prefix /opcua-sim" in out


# --- setup end to end ---

def test_setup_happy_path(ctx, gg, sw, capsys):
    _, gg_stub = gg
    sw_client, sw_stub = sw
    _add_existing_deployment(gg_stub, BASE_COMPONENTS)
    sw_stub.add_response("list_gateways", {"gatewaySummaries": []}, {})
    gg_stub.add_response("get_core_device", {"platform": "linux", "architecture": "amd64"},
                         {"coreDeviceThingName": THING})
    sw_stub.add_response("create_gateway", {"gatewayId": GATEWAY_ID, "gatewayArn": "arn:gw"}, {
        "gatewayName": "opcua-sim-gateway", "gatewayVersion": "3",
        "gatewayPlatform": {"greengrassV2": {"coreDeviceThingName": THING,
                                             "coreDeviceOperatingSystem": "LINUX_AMD64"}}})
    for namespace, payload in ((sse.COLLECTOR_NAMESPACE, README_COLLECTOR),
                               (sse.PUBLISHER_NAMESPACE, README_PUBLISHER)):
        sw_stub.add_response("update_gateway_capability_configuration",
                             {"capabilityNamespace": namespace, "capabilitySyncStatus": "OUT_OF_SYNC"},
                             {"gatewayId": GATEWAY_ID, "capabilityNamespace": namespace,
                              "capabilityConfiguration": payload})
    _add_latest_versions(gg_stub, sse.SITEWISE_COMPONENTS)
    expected_components = dict(copy.deepcopy(BASE_COMPONENTS), **{
        sse.COMPONENT_EMQX: {"componentVersion": "2.0.4", "configurationUpdate": {"merge": README_EMQX_MERGE}},
        sse.COMPONENT_COLLECTOR: {"componentVersion": "3.2.1"},
        sse.COMPONENT_PUBLISHER: {"componentVersion": "4.1.5"}})
    _add_deploy(gg_stub, expected_components)
    sw_stub.add_response("describe_gateway", _sync_response("IN_SYNC", "IN_SYNC"), {"gatewayId": GATEWAY_ID})
    sw_stub.add_response("list_time_series", _time_series("/opcua-sim/Motor01/Temperature"),
                         {"timeSeriesType": "DISASSOCIATED", "aliasPrefix": "/opcua-sim"})

    sse.setup(ctx, sw_client)

    gg_stub.assert_no_pending_responses()
    sw_stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert "Deployment completed successfully" in out
    assert "  /opcua-sim/Motor01/Temperature" in out


# --- delete ---

def test_delete_removes_only_sitewise_components_and_gateway(ctx, gg, sw, capsys):
    _, gg_stub = gg
    sw_client, sw_stub = sw
    other = {"com.example.Other": {"componentVersion": "1.2.3"}}
    _add_existing_deployment(gg_stub, dict(BASE_COMPONENTS, **other, **{
        sse.COMPONENT_EMQX: {"componentVersion": "2.0.4", "configurationUpdate": {"merge": README_EMQX_MERGE}},
        sse.COMPONENT_COLLECTOR: {"componentVersion": "3.2.1"},
        sse.COMPONENT_PUBLISHER: {"componentVersion": "4.1.5"}}))
    _add_deploy(gg_stub, dict(BASE_COMPONENTS, **other))
    sw_stub.add_response("list_gateways", {"gatewaySummaries": [_gateway_summary()]}, {})
    sw_stub.add_response("delete_gateway", {}, {"gatewayId": GATEWAY_ID})

    sse.delete(ctx, sw_client)

    gg_stub.assert_no_pending_responses()
    sw_stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert f"Deleted SiteWise gateway opcua-sim-gateway ({GATEWAY_ID})" in out
    assert "delete-time-series" in out


def test_delete_is_safe_when_nothing_exists(ctx, gg, sw, capsys):
    _, gg_stub = gg
    sw_client, sw_stub = sw
    gg_stub.add_response("list_deployments", {"deployments": []})
    sw_stub.add_response("list_gateways", {"gatewaySummaries": [_gateway_summary(thing="Other")]}, {})
    sse.delete(ctx, sw_client)  # no create_deployment, no delete_gateway stubbed
    gg_stub.assert_no_pending_responses()
    sw_stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert "No SiteWise Edge components" in out
    assert "No SiteWise gateway registered" in out


# --- CLI ---

def test_help_works_without_credentials_or_config(tmp_path):
    env = {"PATH": "", "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "none"),
           "AWS_CONFIG_FILE": str(tmp_path / "none")}
    result = subprocess.run([sys.executable, f"{_REPO_ROOT}/setup_sitewise_edge.py", "--help"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "coreDeviceThingName" in result.stdout
    assert "--delete" in result.stdout


def test_main_dispatches_delete(component_workdir, monkeypatch):
    calls = {}
    sts, sts_stub = _stubbed("sts")
    sts_stub.add_response("get_caller_identity", {
        "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/dev", "UserId": "AID"})
    clients = {"sts": sts, "greengrassv2": object(), "iotsitewise": object()}
    monkeypatch.setattr(boto3, "client", lambda service, region_name=None: clients[service])
    monkeypatch.setattr(sse, "delete", lambda c, s: calls.update(ctx=c, sitewise=s))
    monkeypatch.setattr(sse, "setup", lambda c, s: pytest.fail("setup must not run with --delete"))
    monkeypatch.setattr("sys.argv", ["setup_sitewise_edge.py", THING, "--delete"])
    sse.main()
    assert calls["ctx"].core_device_thing_name == THING
    assert calls["ctx"].account == ACCOUNT
    assert calls["sitewise"] is clients["iotsitewise"]
