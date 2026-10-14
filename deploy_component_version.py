# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Deploys a component version to the Greengrass core device target. This should be
called after "python3 publish_component.py".

Example execution:
python3 deploy_component_version.py 1.0.0 MyCoreDeviceThingName
"""

import argparse
import sys
import time
from dataclasses import dataclass
import boto3
from libs.component_config import ComponentConfig

COMPONENT_DOCKER_APPLICATION_MANAGER = 'aws.greengrass.DockerApplicationManager'
COMPONENT_TOKEN_EXCHANGE_SERVICE = 'aws.greengrass.TokenExchangeService'
DEPLOYMENT_TIMEOUT_SECONDS = 900
DEPLOYMENT_POLL_SECONDS = 5


@dataclass
class Context:
    """ Everything the deployment steps need: configuration, clients and caller identity """
    config: ComponentConfig
    client: object
    account: str
    partition: str
    version: str
    core_device_thing_name: str


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
    """ Gets the details of the existing deployment """
    thing_arn = f'arn:{ctx.partition}:iot:{ctx.config.region()}:{ctx.account}:thing/{ctx.core_device_thing_name}'

    print(f'Searching for existing single Thing deployment for {ctx.core_device_thing_name}')

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

    # We expect to update an existing deployment, not create a new one
    if len(response['deployments']) == 0:
        print('No existing Thing deployment for this Core Device. Abort.')
        sys.exit(1)

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

def update_deployment(ctx, deployment):
    """ Updates the current deployment with the desired versions of the components """
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

    # Add or update our component to the specified version
    if component_name not in deployment['components']:
        print(f'Adding {component_name} {ctx.version} to the deployment')
    else:
        print(f'Updating deployment with {component_name} {ctx.version}')
    # Greengrass preserves component configuration across version changes, so
    # recipe DefaultConfiguration changes to accessControl would otherwise never
    # apply. Reset only that block so user-merged configuration (e.g.
    # controlTopic) is kept.
    deployment['components'].update({component_name: {
        'componentVersion': ctx.version,
        'configurationUpdate': {'reset': ['/accessControl']}
    }})

def create_deployment(ctx, deployment):
    """ Creates a deployment of the component to the given Greengrass core device """

    # Give the deployment a name if it doesn't already have one
    if 'deploymentName' in deployment:
        deployment_name = deployment['deploymentName']
    else:
        deployment_name = f'Deployment for {ctx.core_device_thing_name}'
        print(f'Renaming deployment to "{deployment_name}"')

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


def main():
    """ Parses arguments, resolves the caller identity and deploys the component version """
    config = ComponentConfig()

    parser = argparse.ArgumentParser(description=f'Deploy a version of the {config.name()} component')
    parser.add_argument('version', help='Version of the component to be deployed (Example: 1.0.0)')
    parser.add_argument('coreDeviceThingName', help='Greengrass core device to deploy to')
    args = parser.parse_args()

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
        core_device_thing_name=args.coreDeviceThingName
    )

    print(f'Attempting deployment of version {ctx.version} to core device {ctx.core_device_thing_name}')

    # Get the latest (single Thing) deployment for the specified core device
    current_deployment = get_deployment(ctx)

    # Update the components of the current deployment
    update_deployment(ctx, current_deployment)

    # Create a new deployment
    new_deployment_id = create_deployment(ctx, current_deployment)
    print(f'Deployment {new_deployment_id} successfully created. Waiting for completion ...')
    wait_for_deployment_to_finish(ctx, new_deployment_id)


if __name__ == '__main__':
    main()
