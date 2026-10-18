# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Sets up AWS IoT SiteWise Edge on the same Greengrass core device as the OPC UA
simulator, in one command. Run it after "python3 deploy_component_version.py":
1) Reuses the SiteWise Edge gateway already registered for the core, or creates
   an MQTT-enabled, V3 gateway named opcua-sim-gateway (the OS/architecture is
   read from the Greengrass core device).
2) Configures the gateway's OPC UA data source (the simulator at
   opc.tcp://localhost:4840/simulator/) and its SiteWise real-time destination.
3) Adds the EMQX broker, the OPC UA collector and the publisher to the core's
   thing deployment, next to the simulator, and waits for the deployment.
4) Waits until both gateway capabilities are IN_SYNC and lists the data streams
   that appear under the alias prefix (for example /opcua-sim/Motor01/Temperature).

The deployment always targets the core device's thing, never a thing group.
Components already in that deployment keep their versions; everything else in
it is left untouched. The core device role needs the SiteWise statements from
build/device-role-policy.json (README step 4).

With --delete, the three SiteWise Edge components are removed from the thing
deployment (the simulator stays) and the core's gateway is deleted. Time series,
assets and asset models are not deleted; the script prints how to remove them.

Example execution:
python3 setup_sitewise_edge.py MyCoreDeviceThingName
python3 setup_sitewise_edge.py MyCoreDeviceThingName --delete
"""

import argparse
import json
import time
import boto3
from botocore.exceptions import ClientError
from deploy_component_version import (Context, create_deployment, get_deployment,
                                      get_newest_component_version, wait_for_deployment_to_finish)
from libs.component_config import ComponentConfig
from publish_component import DEFAULT_SITEWISE_ALIAS_PREFIX, caller_identity

GATEWAY_NAME = 'opcua-sim-gateway'
GATEWAY_VERSION = '3'
COLLECTOR_NAMESPACE = 'iotsitewise:opcuacollector:3'
PUBLISHER_NAMESPACE = 'iotsitewise:publisher:3'
SIMULATOR_ENDPOINT_URI = 'opc.tcp://localhost:4840/simulator/'
SOURCE_NAME = 'opcua-sim'

COMPONENT_EMQX = 'aws.greengrass.clientdevices.mqtt.EMQX'
COMPONENT_COLLECTOR = 'aws.iot.SiteWiseEdgeCollectorOpcua'
COMPONENT_PUBLISHER = 'aws.iot.SiteWiseEdgePublisher'
SITEWISE_COMPONENTS = (COMPONENT_EMQX, COMPONENT_COLLECTOR, COMPONENT_PUBLISHER)

# EMQX for a V3 gateway: TCP listener on loopback port 1883 only, Greengrass auth
# bypassed (only local components can reach it), data kept in the emqx-data volume.
EMQX_CONFIGURATION = {
    'emqxConfig': {
        'authorization': {'no_match': 'allow'},
        'listeners': {'tcp': {'default': {'enabled': True, 'enable_authn': False}}}
    },
    'authMode': 'bypass',
    'dockerOptions': '-p 127.0.0.1:1883:1883 -v emqx-data:/opt/emqx/data -e EMQX_NODE__NAME=emqx@local',
    'requiresPrivilege': 'true'
}

# (platform, architecture) reported by Greengrass -> SiteWise coreDeviceOperatingSystem
OPERATING_SYSTEMS = {
    ('linux', 'amd64'): 'LINUX_AMD64',
    ('linux', 'x86_64'): 'LINUX_AMD64',
    ('linux', 'aarch64'): 'LINUX_AARCH64',
    ('linux', 'arm64'): 'LINUX_AARCH64',
}

SYNC_TIMEOUT_SECONDS = 300
SYNC_POLL_SECONDS = 10
TIME_SERIES_TIMEOUT_SECONDS = 180
TIME_SERIES_POLL_SECONDS = 15


def _compact(value):
    """ JSON without whitespace, as in the README's CLI examples """
    return json.dumps(value, separators=(',', ':'))


def _polls(timeout_seconds, poll_seconds):
    """ Number of polls that fit in the timeout (at least one) """
    return max(1, timeout_seconds // poll_seconds)


def core_device_operating_system(greengrass, thing_name, region):
    """ Maps the core device's platform and architecture to a SiteWise gateway OS """
    try:
        core = greengrass.get_core_device(coreDeviceThingName=thing_name)
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            raise SystemExit(f'Greengrass core device {thing_name} not found in {region}. Check the thing '
                             'name and the "region" in component.json.') from None
        raise SystemExit(f'Failed to get core device {thing_name}: {e}') from None
    platform = str(core.get('platform', '')).lower()
    architecture = str(core.get('architecture', '')).lower()
    operating_system = OPERATING_SYSTEMS.get((platform, architecture))
    if operating_system is None:
        raise SystemExit(f'Core device {thing_name} reports platform "{platform}" and architecture '
                         f'"{architecture}"; this sample supports Linux on amd64/x86_64 or aarch64/arm64.')
    return operating_system


def find_gateway(sitewise, thing_name):
    """ Returns the summary of the SiteWise gateway registered for the core device, or None """
    try:
        for page in sitewise.get_paginator('list_gateways').paginate():
            for summary in page['gatewaySummaries']:
                greengrass_v2 = summary.get('gatewayPlatform', {}).get('greengrassV2', {})
                if greengrass_v2.get('coreDeviceThingName') == thing_name:
                    return summary
    except ClientError as e:
        raise SystemExit(f'Failed to list SiteWise gateways: {e}') from None
    return None


def ensure_gateway(sitewise, greengrass, thing_name, region):
    """ Returns the id of the core's V3 gateway, creating the gateway if there is none """
    existing = find_gateway(sitewise, thing_name)
    if existing:
        version = existing.get('gatewayVersion')
        if version and version != GATEWAY_VERSION:
            raise SystemExit(f'Gateway {existing["gatewayName"]} ({existing["gatewayId"]}) on core {thing_name} '
                             f'is version {version}; this script configures V{GATEWAY_VERSION} gateways. '
                             'Delete it first (python3 setup_sitewise_edge.py <thing-name> --delete).')
        print(f'Using existing SiteWise gateway {existing["gatewayName"]} ({existing["gatewayId"]})')
        return existing['gatewayId']

    operating_system = core_device_operating_system(greengrass, thing_name, region)
    try:
        response = sitewise.create_gateway(
            gatewayName=GATEWAY_NAME,
            gatewayPlatform={'greengrassV2': {'coreDeviceThingName': thing_name,
                                              'coreDeviceOperatingSystem': operating_system}},
            gatewayVersion=GATEWAY_VERSION)
    except ClientError as e:
        raise SystemExit(f'Failed to create SiteWise gateway {GATEWAY_NAME}: {e}') from None
    print(f'Created SiteWise gateway {GATEWAY_NAME} ({response["gatewayId"]}) for {operating_system}')
    return response['gatewayId']


def collector_configuration(alias_prefix):
    """ OPC UA collector: reads the simulator over loopback, no security, anonymous """
    return {'sources': [{
        'name': SOURCE_NAME,
        'endpoint': {
            'certificateTrust': {'type': 'TrustAny'},
            'endpointUri': SIMULATOR_ENDPOINT_URI,
            'securityPolicy': 'NONE',
            'messageSecurityMode': 'NONE',
            'identityProvider': {'type': 'Anonymous'},
            'nodeFilterRules': []
        },
        'measurementDataStreamPrefix': alias_prefix,
        'destination': {'type': 'MQTT'}
    }]}


def publisher_configuration():
    """ Publisher: sends everything from the local broker to SiteWise in real time """
    return {
        'sources': [{'type': 'MQTT'}],
        'destinations': [{
            'type': 'SITEWISE_REALTIME',
            'name': 'sitewise-realtime',
            'config': {'publishingOrder': 'TIME_ORDER', 'enableCompression': True, 'maxBatchWaitTime': '10s'},
            'filters': [{'type': 'PATH', 'config': {'paths': ['#']}}]
        }]
    }


def configure_capabilities(sitewise, gateway_id, alias_prefix):
    """ Sets the OPC UA data source and the SiteWise destination on the gateway """
    for namespace, configuration in ((COLLECTOR_NAMESPACE, collector_configuration(alias_prefix)),
                                     (PUBLISHER_NAMESPACE, publisher_configuration())):
        try:
            response = sitewise.update_gateway_capability_configuration(
                gatewayId=gateway_id, capabilityNamespace=namespace,
                capabilityConfiguration=_compact(configuration))
        except ClientError as e:
            raise SystemExit(f'Failed to configure {namespace} on gateway {gateway_id}: {e}') from None
        print(f'Configured {namespace} ({response["capabilitySyncStatus"]})')


def require_simulator(ctx, deployment):
    """ Refuses to continue unless the thing deployment already contains the simulator """
    if ctx.config.name() not in deployment['components']:
        raise SystemExit(f'The deployment for thing {ctx.core_device_thing_name} does not contain '
                         f'{ctx.config.name()}. Deploy the simulator first: '
                         f'python3 deploy_component_version.py <version> {ctx.core_device_thing_name}')


def add_sitewise_components(ctx, deployment):
    """ Adds EMQX, the collector and the publisher; existing entries keep their versions """
    components = deployment['components']
    for name in SITEWISE_COMPONENTS:
        if name in components:
            entry = dict(components[name])
            print(f'Keeping {name} {entry["componentVersion"]} in the deployment')
        else:
            entry = {'componentVersion': get_newest_component_version(ctx, name)}
            print(f'Adding {name} {entry["componentVersion"]} to the deployment')
        if name == COMPONENT_EMQX:
            entry['configurationUpdate'] = {'merge': _compact(EMQX_CONFIGURATION)}
        components[name] = entry


def deploy(ctx, deployment):
    """ Creates the revised thing deployment and waits for it to finish """
    deployment_id = create_deployment(ctx, deployment)
    print(f'Deployment {deployment_id} created. Waiting for completion ...')
    wait_for_deployment_to_finish(ctx, deployment_id)


def wait_for_capabilities_in_sync(sitewise, gateway_id):
    """ Polls the gateway until the collector and publisher capabilities are IN_SYNC """
    statuses = {}
    for _ in range(_polls(SYNC_TIMEOUT_SECONDS, SYNC_POLL_SECONDS)):
        try:
            response = sitewise.describe_gateway(gatewayId=gateway_id)
        except ClientError as e:
            raise SystemExit(f'Failed to describe gateway {gateway_id}: {e}') from None
        statuses = {summary['capabilityNamespace']: summary['capabilitySyncStatus']
                    for summary in response['gatewayCapabilitySummaries']}
        pending = {ns: statuses.get(ns, 'UNKNOWN') for ns in (COLLECTOR_NAMESPACE, PUBLISHER_NAMESPACE)
                   if statuses.get(ns) != 'IN_SYNC'}
        if not pending:
            print('Gateway capabilities are IN_SYNC')
            return
        print(f'Waiting for gateway capabilities: {pending} ...')
        time.sleep(SYNC_POLL_SECONDS)
    raise SystemExit(f'Timed out after {SYNC_TIMEOUT_SECONDS} s waiting for gateway {gateway_id} capabilities '
                     f'to be IN_SYNC (last status: {statuses}). Check the components on the core with '
                     f'"aws greengrassv2 list-installed-components" and the gateway with '
                     f'"aws iotsitewise describe-gateway --gateway-id {gateway_id}".')


def list_data_streams(sitewise, alias_prefix):
    """ Returns the aliases of the disassociated time series under the prefix """
    try:
        pages = sitewise.get_paginator('list_time_series').paginate(
            timeSeriesType='DISASSOCIATED', aliasPrefix=alias_prefix)
        return sorted(summary['alias'] for page in pages for summary in page['TimeSeriesSummaries']
                      if summary.get('alias'))
    except ClientError as e:
        raise SystemExit(f'Failed to list time series: {e}') from None


def wait_for_data_streams(sitewise, alias_prefix):
    """ Polls for data streams for a few minutes; returns the aliases found (may be empty) """
    polls = _polls(TIME_SERIES_TIMEOUT_SECONDS, TIME_SERIES_POLL_SECONDS)
    for attempt in range(polls):
        aliases = list_data_streams(sitewise, alias_prefix)
        if aliases:
            return aliases
        if attempt < polls - 1:
            print('Waiting for data streams ...')
            time.sleep(TIME_SERIES_POLL_SECONDS)
    return []


def print_next_steps(aliases, alias_prefix):
    """ Prints the data streams found and how to view and model the data """
    check = f'aws iotsitewise list-time-series --time-series-type DISASSOCIATED --alias-prefix {alias_prefix}'
    example = aliases[0] if aliases else f'{alias_prefix}/Motor01/Temperature'
    if aliases:
        print(f'Data streams under {alias_prefix}:')
        for alias in aliases:
            print(f'  {alias}')
    else:
        print('No data streams yet; data can take a few minutes to arrive. Check with:')
        print(f'  {check}')
    print('Read the latest value with:')
    print(f'  aws iotsitewise get-asset-property-value --property-alias {example}')
    print('Optional: to see modeled data, create an asset model and asset, then bind a stream to a property:')
    print(f'  aws iotsitewise associate-time-series-to-asset-property --alias {example} '
          '--asset-id <asset-id> --property-id <property-id>')
    print(f'In the AWS IoT SiteWise console, the streams are listed under Data streams and the gateway '
          f'({GATEWAY_NAME}) under Edge gateways.')


def setup(ctx, sitewise, alias_prefix=DEFAULT_SITEWISE_ALIAS_PREFIX):
    """ Gateway, capabilities, components, then waits for IN_SYNC and data streams """
    thing_name = ctx.core_device_thing_name
    deployment = get_deployment(ctx)
    require_simulator(ctx, deployment)

    gateway_id = ensure_gateway(sitewise, ctx.client, thing_name, ctx.config.region())
    configure_capabilities(sitewise, gateway_id, alias_prefix)

    add_sitewise_components(ctx, deployment)
    deploy(ctx, deployment)

    wait_for_capabilities_in_sync(sitewise, gateway_id)
    print_next_steps(wait_for_data_streams(sitewise, alias_prefix), alias_prefix)


def delete(ctx, sitewise, alias_prefix=DEFAULT_SITEWISE_ALIAS_PREFIX):
    """ Removes the SiteWise Edge components from the thing deployment and deletes the gateway """
    thing_name = ctx.core_device_thing_name
    deployment = get_deployment(ctx)
    removed = [name for name in SITEWISE_COMPONENTS if name in deployment['components']]
    if removed:
        for name in removed:
            print(f'Removing {name} from the deployment')
            del deployment['components'][name]
        deploy(ctx, deployment)
    else:
        print(f'No SiteWise Edge components in the deployment for thing {thing_name}')

    gateway = find_gateway(sitewise, thing_name)
    if gateway:
        try:
            sitewise.delete_gateway(gatewayId=gateway['gatewayId'])
        except ClientError as e:
            raise SystemExit(f'Failed to delete gateway {gateway["gatewayId"]}: {e}') from None
        print(f'Deleted SiteWise gateway {gateway["gatewayName"]} ({gateway["gatewayId"]})')
    else:
        print(f'No SiteWise gateway registered for core {thing_name}')

    print('Time series, assets and asset models are not deleted. To remove them: '
          'disassociate-time-series-from-asset-property -> delete-asset -> delete-asset-model -> '
          'aws iotsitewise delete-time-series --alias <alias> for each stream, listed with:')
    print(f'  aws iotsitewise list-time-series --alias-prefix {alias_prefix}')
    print('On the core, remove the EMQX volume once the EMQX container is gone: docker volume rm emqx-data')


def main():
    """ Parses arguments, resolves the caller identity and sets up (or deletes) SiteWise Edge """
    parser = argparse.ArgumentParser(
        description='Set up AWS IoT SiteWise Edge on the Greengrass core that runs the OPC UA simulator')
    parser.add_argument('coreDeviceThingName', help='Greengrass core device running the simulator')
    parser.add_argument('--delete', action='store_true',
                        help='Remove the SiteWise Edge components from the deployment and delete the gateway')
    args = parser.parse_args()

    # Load configuration and resolve the identity only after parsing so --help needs neither
    config = ComponentConfig()
    region = config.region()
    account, partition = caller_identity(boto3.client('sts', region_name=region))
    ctx = Context(
        config=config,
        client=boto3.client('greengrassv2', region_name=region),
        account=account,
        partition=partition,
        version=config.version(),
        core_device_thing_name=args.coreDeviceThingName
    )
    sitewise = boto3.client('iotsitewise', region_name=region)

    if args.delete:
        delete(ctx, sitewise)
    else:
        setup(ctx, sitewise)


if __name__ == '__main__':
    main()
