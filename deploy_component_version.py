# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Deploys a component version to a Greengrass core device. This should be called
after "python3 publish_component.py".

The deployment always targets the core device's thing, never a thing group, so
other devices are unaffected. If the thing already has a deployment, it is
revised to add (or update) this component; otherwise a new thing deployment is
created. Either way the Docker Application Manager, Token Exchange Service and
Log manager are added if missing; the Log manager uploads only this component's
log to CloudWatch Logs every 60 seconds. An existing Log manager keeps its
version and configuration; only this component's entry is added. Deployments that reach the device through thing groups
(for example the installer's quick-start group) are left untouched; the device
runs the components of all its deployments together.

The simulator's devices and tags live in the simulatorConfig component
configuration (default: artifacts/config.json, built into the recipe):
- --config PATH replaces simulatorConfig with the JSON file at PATH (reset, then
  merge). The running simulator applies it live, without a new component version.
- --reset-config returns simulatorConfig to the recipe default.
- With neither flag, a simulatorConfig merge recorded in the current thing
  deployment is kept; otherwise simulatorConfig is reset to the default of the
  version being deployed.

Example execution:
python3 deploy_component_version.py 1.0.0 MyCoreDeviceThingName
python3 deploy_component_version.py 1.0.0 MyCoreDeviceThingName --config my-config.json
"""

import argparse
import ipaddress
import json
import sys
import time
from dataclasses import dataclass
from typing import Optional
import boto3
from libs import simulator_config as simulator_config_lib
from libs.component_config import ComponentConfig

COMPONENT_DOCKER_APPLICATION_MANAGER = 'aws.greengrass.DockerApplicationManager'
COMPONENT_TOKEN_EXCHANGE_SERVICE = 'aws.greengrass.TokenExchangeService'
COMPONENT_LOG_MANAGER = 'aws.greengrass.LogManager'
COMPONENT_SITEWISE_COLLECTOR = 'aws.iot.SiteWiseEdgeCollectorOpcua'
SIMULATOR_RESTART_COMMAND = ('sudo /greengrass/v2/bin/greengrass-cli component restart '
                             '--names com.amazon.samples.OpcUaDataSim')
COLLECTOR_RESTART_COMMAND = ('sudo /greengrass/v2/bin/greengrass-cli component restart '
                             f'--names {COMPONENT_SITEWISE_COLLECTOR}')
LOG_UPLOAD_INTERVAL_SECONDS = 60
LOG_MINIMUM_LEVEL = 'INFO'
# componentLogsConfigurationMap was added in Log manager 2.2.0; 2.1.x reads a list instead
LOG_MANAGER_MAP_MIN_VERSION = (2, 2)
DEPLOYMENT_TIMEOUT_SECONDS = 900
DEPLOYMENT_POLL_SECONDS = 5
SIMULATOR_CONFIG_KEY = 'simulatorConfig'
SIMULATOR_CONFIG_RESET = f'/{SIMULATOR_CONFIG_KEY}'
ACCESS_CONTROL_RESET = '/accessControl'
PUBLISH_ADDRESS_KEY = 'publishAddress'


@dataclass
class Context:  # pylint: disable=too-many-instance-attributes  # a plain parameter bundle
    """ Everything the deployment steps need: configuration, clients and caller identity """
    config: ComponentConfig
    client: object
    account: str
    partition: str
    version: str
    core_device_thing_name: str
    # --config: the simulator config to deploy; --reset-config: back to the recipe default
    simulator_config: Optional[dict] = None
    reset_config: bool = False
    # --publish-address: host address for TCP 4840 (the recipe default is 127.0.0.1)
    publish_address: Optional[str] = None


def get_newest_component_version(ctx, component_name):
    """ Gets the newest version of a component """
    component_arn = f'arn:{ctx.partition}:greengrass:{ctx.config.region()}:aws:components:{component_name}'

    try:
        response = ctx.client.list_component_versions(arn=component_arn)
    except Exception as e:
        print(f'Failed to get component versions for {component_name}\nException: {e}')
        sys.exit(1)

    return response['componentVersions'][0]['componentVersion']

def get_deployment(ctx):
    """
    Gets the latest deployment that targets the core device's thing, or a new,
    empty one for that thing if none exists yet.
    """
    thing_arn = f'arn:{ctx.partition}:iot:{ctx.config.region()}:{ctx.account}:thing/{ctx.core_device_thing_name}'

    print(f'Searching for an existing deployment that targets thing {ctx.core_device_thing_name}')

    try:
        # Get the latest deployment for the specified core device name
        response = ctx.client.list_deployments(
            targetArn=thing_arn,
            historyFilter='LATEST_ONLY',
            maxResults=1
        )
    except Exception as e:
        print(f'Failed to list deployments\nException: {e}')
        sys.exit(1)

    # No thing deployment yet (common when the core was provisioned into a thing
    # group): start a new one. Group deployments keep applying alongside it.
    if len(response['deployments']) == 0:
        print(f'No deployment targets thing {ctx.core_device_thing_name}; creating a new one '
              '(thing group deployments are left unchanged)')
        return {'targetArn': thing_arn, 'components': {}}

    # We expect at most one result in the list
    deployment_id = response['deployments'][0]['deploymentId']

    try:
        response = ctx.client.get_deployment(deploymentId=deployment_id)

        if 'deploymentName' in response:
            print(f'Found existing named deployment "{response["deploymentName"]}"')
        else:
            print(f'Found existing unnamed deployment {deployment_id}')
    except Exception as e:
        print(f'Failed to get deployment\nException: {e}')
        sys.exit(1)

    return response

def _version_tuple(version):
    """ (major, minor) of a semantic version; (0, 0) if it cannot be parsed """
    try:
        major, minor = version.split('.')[:2]
        return int(major), int(minor)
    except ValueError:
        return 0, 0

def add_log_manager(ctx, deployment):
    """
    Makes the Log manager upload this component's log to CloudWatch Logs. If it is
    missing, adds the latest version configured for this component only (system logs
    stay off by default) with a 60-second upload interval.
    """
    components = deployment['components']
    name = ctx.config.name()
    entry = {'minimumLogLevel': LOG_MINIMUM_LEVEL}

    if COMPONENT_LOG_MANAGER not in components:
        version = get_newest_component_version(ctx, COMPONENT_LOG_MANAGER)
        print(f'Adding {COMPONENT_LOG_MANAGER} {version} to the deployment (uploads {name} logs '
              f'to CloudWatch Logs every {LOG_UPLOAD_INTERVAL_SECONDS} s)')
        merge = {'logsUploaderConfiguration': {'componentLogsConfigurationMap': {name: entry}},
                 'periodicUploadIntervalSec': LOG_UPLOAD_INTERVAL_SECONDS}
        components[COMPONENT_LOG_MANAGER] = {'componentVersion': version,
                                             'configurationUpdate': {'merge': json.dumps(merge)}}
        return

    # Already deployed: keep its version and configuration (other components' log
    # settings, system logs, the upload interval or its absence) and only add this
    # component's entry, so the user's Log manager setup is never overwritten.
    existing = components[COMPONENT_LOG_MANAGER]
    if _version_tuple(existing.get('componentVersion', '')) < LOG_MANAGER_MAP_MIN_VERSION:
        print(f'Warning: {COMPONENT_LOG_MANAGER} {existing.get("componentVersion")} predates 2.2.0 and '
              f'ignores componentLogsConfigurationMap; {name} logs will not upload until it is upgraded')
    update = existing.setdefault('configurationUpdate', {})
    try:
        merge = json.loads(update.get('merge') or '{}')
    except json.JSONDecodeError as e:
        print(f'The existing deployment\'s {COMPONENT_LOG_MANAGER} configurationUpdate.merge is not '
              f'valid JSON; fix it and retry\nException: {e}')
        sys.exit(1)
    component_map = merge.setdefault('logsUploaderConfiguration', {}).setdefault(
        'componentLogsConfigurationMap', {})
    if name in component_map:
        print(f'{COMPONENT_LOG_MANAGER} already uploads {name} logs; leaving its configuration unchanged')
        return
    component_map[name] = entry
    update['merge'] = json.dumps(merge)
    print(f'Adding {name} to the existing {COMPONENT_LOG_MANAGER} configuration '
          '(version and other settings kept)')

def update_deployment(ctx, deployment):
    """
    Updates the current deployment with the desired versions of the components.
    Returns the version of this component the deployment had before (None if it
    was not in it), so the caller can tell whether the container restarts.
    """
    component_name = ctx.config.name()

    # If Docker Application Manager is not in the deployment, add the latest version
    if COMPONENT_DOCKER_APPLICATION_MANAGER not in deployment['components']:
        version = get_newest_component_version(ctx, COMPONENT_DOCKER_APPLICATION_MANAGER)
        print(f'Adding {COMPONENT_DOCKER_APPLICATION_MANAGER} {version} to the deployment')
        deployment['components'].update({COMPONENT_DOCKER_APPLICATION_MANAGER: {'componentVersion': version}})

    # If Token Exchange Service is not in the deployment, add the latest version.
    # Required so the device can fetch ECR credentials to pull the simulator image.
    if COMPONENT_TOKEN_EXCHANGE_SERVICE not in deployment['components']:
        version = get_newest_component_version(ctx, COMPONENT_TOKEN_EXCHANGE_SERVICE)
        print(f'Adding {COMPONENT_TOKEN_EXCHANGE_SERVICE} {version} to the deployment')
        deployment['components'].update({COMPONENT_TOKEN_EXCHANGE_SERVICE: {'componentVersion': version}})

    # Upload this component's log to CloudWatch Logs
    add_log_manager(ctx, deployment)

    # Add or update our component to the specified version
    if component_name not in deployment['components']:
        print(f'Adding {component_name} {ctx.version} to the deployment')
    else:
        print(f'Updating deployment with {component_name} {ctx.version}')
    existing = deployment['components'].get(component_name, {})
    previous_version = existing.get('componentVersion')
    deployment['components'].update({component_name: {
        'componentVersion': ctx.version,
        'configurationUpdate': with_publish_address(ctx, existing, simulator_configuration_update(ctx, existing))
    }})
    return previous_version


def publish_address_hint(ctx, previous_version):
    """
    The restart command to print when --publish-address changes the address of a
    simulator version that is already running, or None. Greengrass stores the new
    value but does not restart the component for a configuration-only change; a
    new version or a fresh install starts the container with the new address.
    """
    if ctx.publish_address is None or previous_version != ctx.version:
        return None
    return (f'Note: the new publish address takes effect when the simulator container restarts. '
            f'Restart it on the core:\n  {SIMULATOR_RESTART_COMMAND}')


def collector_restart_hint(ctx, deployment, previous_version):
    """
    A hint to print after the deployment when it restarts the simulator container
    on a core that also runs the SiteWise Edge OPC UA collector, or None.
    A new simulator version replaces the container (a configuration-only change
    does not). The collector can keep a subscription that the new OPC UA server
    rejects and then receive no values until it is restarted.
    """
    if COMPONENT_SITEWISE_COLLECTOR not in deployment['components'] or previous_version == ctx.version:
        return None
    change = f'{previous_version} -> {ctx.version}' if previous_version else f'new install of {ctx.version}'
    return (f'Note: this deployment restarted the simulator container ({change}) on a core that runs '
            f'SiteWise Edge. If SiteWise stops receiving values, restart the OPC UA collector on the '
            f'core:\n  {COLLECTOR_RESTART_COMMAND}\n(see docs/OperationsGuide.md, Troubleshooting)')

def _existing_merge(existing):
    """ The configurationUpdate.merge recorded in the current deployment's entry, as a dict """
    update = existing.get('configurationUpdate') or {}
    try:
        merge = json.loads(update.get('merge') or '{}')
    except json.JSONDecodeError as e:
        print(f'The existing deployment\'s configurationUpdate.merge for this component is not '
              f'valid JSON; fix it or pass --config / --reset-config\nException: {e}')
        sys.exit(1)
    return merge if isinstance(merge, dict) else {}


def _existing_simulator_config(existing):
    """
    The simulatorConfig merge recorded in the current deployment's entry for this
    component, and whether that entry also reset /simulatorConfig; (None, False) if absent.
    """
    merge = _existing_merge(existing)
    if SIMULATOR_CONFIG_KEY not in merge:
        return None, False
    update = existing.get('configurationUpdate') or {}
    return merge[SIMULATOR_CONFIG_KEY], SIMULATOR_CONFIG_RESET in (update.get('reset') or [])


def validate_publish_address(value):
    """ argparse type for --publish-address: an IPv4 address (the recipe checks this too) """
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise argparse.ArgumentTypeError(f'expected an IPv4 address such as 127.0.0.1, got {value!r}') from None


def with_publish_address(ctx, existing, update):
    """
    Add publishAddress to the configurationUpdate when --publish-address is given
    or the current deployment already sets it; otherwise the recipe default
    (127.0.0.1) applies. The value is read when the container starts, so a change
    on an already running version needs a restart (see publish_address_hint).
    """
    address = ctx.publish_address or _existing_merge(existing).get(PUBLISH_ADDRESS_KEY)
    if address is None:
        return update
    merge = json.loads(update.get('merge') or '{}')
    merge[PUBLISH_ADDRESS_KEY] = address
    source = '--publish-address' if ctx.publish_address else 'kept from the current deployment'
    note = '' if address == '127.0.0.1' else '; restrict TCP 4840 to trusted OPC UA clients'
    print(f'OPC UA port: publishing TCP 4840 on {address} ({source}{note})')
    return {**update, 'merge': json.dumps(merge)}

def simulator_configuration_update(ctx, existing):
    """
    The configurationUpdate for this component. Greengrass keeps a component's
    configuration across version changes, so recipe DefaultConfiguration changes
    would otherwise never apply: /accessControl is always reset. Reset runs before
    merge, so resetting /simulatorConfig and merging a new one replaces it entirely
    (devices or keys missing from the new config disappear).
    """
    if ctx.simulator_config is not None:
        devices, tags = simulator_config_lib.counts(ctx.simulator_config)
        print(f'Simulator config: replacing simulatorConfig with the --config file '
              f'({devices} devices, {tags} tags); the running simulator applies it live')
        return {'reset': [ACCESS_CONTROL_RESET, SIMULATOR_CONFIG_RESET],
                'merge': json.dumps({SIMULATOR_CONFIG_KEY: ctx.simulator_config})}

    if not ctx.reset_config:
        kept, kept_with_reset = _existing_simulator_config(existing)
        if kept is not None:
            # Keep the recorded custom config exactly as it was deployed (a full
            # replace if it was one, a partial merge over the default otherwise).
            reset = [ACCESS_CONTROL_RESET] + ([SIMULATOR_CONFIG_RESET] if kept_with_reset else [])
            print('Simulator config: keeping the simulatorConfig merge recorded in the current '
                  'deployment (pass --reset-config to return to the recipe default)')
            return {'reset': reset, 'merge': json.dumps({SIMULATOR_CONFIG_KEY: kept})}

    why = '--reset-config' if ctx.reset_config else 'no custom simulatorConfig in the current deployment'
    print(f'Simulator config: resetting simulatorConfig to the recipe default of version {ctx.version} '
          f'(artifacts/config.json at build time; {why})')
    return {'reset': [ACCESS_CONTROL_RESET, SIMULATOR_CONFIG_RESET]}

def create_deployment(ctx, deployment):
    """ Creates a deployment of the component to the given Greengrass core device """

    # Give the deployment a name if it doesn't already have one
    if 'deploymentName' in deployment:
        deployment_name = deployment['deploymentName']
    else:
        deployment_name = f'Deployment for {ctx.core_device_thing_name}'
        print(f'Naming deployment "{deployment_name}"')

    kwargs = {
        'targetArn': deployment['targetArn'],
        'deploymentName': deployment_name,
        'components': deployment['components']
    }
    # Carry over the existing deployment's policies, job configuration and tags, if any
    for key in ('deploymentPolicies', 'iotJobConfiguration', 'tags'):
        if deployment.get(key):
            kwargs[key] = deployment[key]

    try:
        response = ctx.client.create_deployment(**kwargs)
    except Exception as e:
        print(f'Failed to create deployment\nException: {e}')
        sys.exit(1)

    return response['deploymentId']

def wait_for_deployment_to_finish(ctx, deploy_id):
    """ Waits for the deployment to complete """
    deployment_status = 'ACTIVE'
    snapshot = time.time()

    while deployment_status == 'ACTIVE' and (time.time() - snapshot) < DEPLOYMENT_TIMEOUT_SECONDS:
        try:
            response = ctx.client.get_deployment(deploymentId=deploy_id)
            deployment_status = response['deploymentStatus']
        except Exception as e:
            print(f'Failed to get deployment\nException: {e}')
            sys.exit(1)
        if deployment_status == 'ACTIVE':
            time.sleep(DEPLOYMENT_POLL_SECONDS)

    if deployment_status == 'COMPLETED':
        print(f'Deployment completed successfully in {time.time() - snapshot:.1f} seconds')
    elif deployment_status == 'ACTIVE':
        print('Deployment timed out')
        sys.exit(1)
    else:
        print(f'Deployment error: {deployment_status}')
        sys.exit(1)


def load_simulator_config(path):
    """ Loads and shape-checks a --config file; warns (but continues) when it is large """
    cfg = simulator_config_lib.load(path)
    size = len(json.dumps({SIMULATOR_CONFIG_KEY: cfg}).encode('utf-8'))
    if size > simulator_config_lib.SIZE_WARNING_BYTES:
        print(f'Warning: {path} is {size // 1024} KB as a configuration merge (over '
              f'{simulator_config_lib.SIZE_WARNING_BYTES // 1024} KB); large configurations make every '
              'deployment of this thing larger')
    devices, tags = simulator_config_lib.counts(cfg)
    print(f'Loaded simulator config {path} ({devices} devices, {tags} tags)')
    return cfg


def main():
    """ Parses arguments, resolves the caller identity and deploys the component version """
    config = ComponentConfig()

    parser = argparse.ArgumentParser(description=f'Deploy a version of the {config.name()} component')
    parser.add_argument('version', help='Version of the component to be deployed (Example: 1.0.0)')
    parser.add_argument('coreDeviceThingName', help='Greengrass core device to deploy to')
    config_group = parser.add_mutually_exclusive_group()
    config_group.add_argument('--config', metavar='PATH',
                              help='Simulator config JSON (same format as artifacts/config.json) to replace '
                                   'the simulatorConfig component configuration with; applied live')
    config_group.add_argument('--reset-config', action='store_true',
                              help='Return the simulatorConfig component configuration to the recipe default')
    parser.add_argument('--publish-address', metavar='IPV4', type=validate_publish_address,
                        help='Host address to publish OPC UA TCP 4840 on (default 127.0.0.1: this core only). '
                             'Use an address of this host only for OPC UA clients on other hosts')
    args = parser.parse_args()

    # Validate the simulator config before any AWS call
    simulator_config = load_simulator_config(args.config) if args.config else None

    # Resolve account and partition only after argument parsing so --help needs no credentials
    sts = boto3.client('sts', region_name=config.region())
    identity = sts.get_caller_identity()
    account = identity['Account']
    partition = identity['Arn'].split(':')[1]

    ctx = Context(
        config=config,
        client=boto3.client('greengrassv2', region_name=config.region()),
        account=account,
        partition=partition,
        version=args.version,
        core_device_thing_name=args.coreDeviceThingName,
        simulator_config=simulator_config,
        reset_config=args.reset_config,
        publish_address=args.publish_address
    )

    print(f'Attempting deployment of version {ctx.version} to core device {ctx.core_device_thing_name}')

    # Get the latest (single Thing) deployment for the specified core device
    current_deployment = get_deployment(ctx)

    # Update the components of the current deployment
    previous_version = update_deployment(ctx, current_deployment)

    # Create a new deployment
    new_deployment_id = create_deployment(ctx, current_deployment)
    print(f'Deployment {new_deployment_id} successfully created. Waiting for completion ...')
    wait_for_deployment_to_finish(ctx, new_deployment_id)
    for hint in (publish_address_hint(ctx, previous_version),
                 collector_restart_hint(ctx, current_deployment, previous_version)):
        if hint:
            print(hint)


if __name__ == '__main__':
    main()
