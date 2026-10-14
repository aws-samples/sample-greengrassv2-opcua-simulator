# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Packages, uploads and registers a version of the Greengrass component described by
component.json, in one command:

1) Builds the component (build_component.py): renders recipe.yaml and zips artifacts/.
2) Refuses to continue if that component version already exists in the account.
3) Creates the artifact bucket <publishBucketPrefix>-<region>-<account> if it is
   missing (public access blocked, SSE-S3 default encryption).
4) Uploads the artifacts archive to S3 and points the recipe at it.
5) Creates the component version in the Greengrass cloud service and waits until it
   is DEPLOYABLE.

Existing versions are never overwritten: bump "version" in component.json instead.

Example execution:
python3 publish_component.py
"""

import argparse
import json
import os
import time
import boto3
import yaml
from botocore.exceptions import ClientError
import build_component
from libs.component_config import ComponentConfig

DEPLOYABLE_TIMEOUT_SECONDS = 120
DEPLOYABLE_POLL_SECONDS = 2
TERMINAL_FAILURE_STATES = ('FAILED', 'DEPRECATED')
DEVICE_ROLE_DOCS = 'https://docs.aws.amazon.com/greengrass/v2/developerguide/device-service-role.html'


def component_version_arn(partition, region, account, config):
    """ Builds the ARN of the component version described by the configuration """
    return (f'arn:{partition}:greengrass:{region}:{account}:components:'
            f'{config.name()}:versions:{config.version()}')


def refuse_if_version_exists(greengrass, arn, config):
    """ Exits if the component version already exists; a published version is immutable """
    try:
        greengrass.describe_component(arn=arn)
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return
        raise SystemExit(f'Failed to check whether {arn} exists: {e}') from None

    raise SystemExit(
        f'Component {config.name()} version {config.version()} already exists in {config.region()}. '
        f'Bump "version" in {config.path} and publish again. Existing versions are never '
        'overwritten: core devices cache a version\'s artifacts immutably, so a re-published '
        'version would keep running the old files.')


def ensure_bucket(s3, bucket, region):
    """
    Makes sure the artifact bucket exists. Returns True if this call created it.
    A bucket this script creates gets all four public access blocks and SSE-S3
    default encryption; an existing bucket is left untouched.
    """
    try:
        s3.head_bucket(Bucket=bucket)
        print(f'Using existing artifact bucket {bucket}')
        return False
    except ClientError as e:
        code = e.response['Error']['Code']
        if code not in ('404', 'NoSuchBucket', 'NotFound'):
            raise SystemExit(
                f'Cannot access bucket {bucket} ({code}). If the name is taken by another '
                'account, change "publishBucketPrefix" in component.json.') from None

    print(f'Creating artifact bucket {bucket}')
    kwargs = {'Bucket': bucket}
    # us-east-1 is the default location and rejects an explicit LocationConstraint
    if region != 'us-east-1':
        kwargs['CreateBucketConfiguration'] = {'LocationConstraint': region}
    s3.create_bucket(**kwargs)
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            'BlockPublicAcls': True,
            'IgnorePublicAcls': True,
            'BlockPublicPolicy': True,
            'RestrictPublicBuckets': True
        })
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]
        })
    print(f'Created artifact bucket {bucket}')
    print(f'Reminder: grant each core device\'s token exchange role s3:GetObject on '
          f'arn:aws:s3:::{bucket}/* so it can download the artifacts ({DEVICE_ROLE_DOCS})')
    return True


def upload_artifact(s3, bucket, key, zip_path):
    """ Uploads the artifacts archive and returns its s3:// URI """
    with open(zip_path, 'rb') as zip_file:
        s3.put_object(Bucket=bucket, Key=key, Body=zip_file)
    uri = f's3://{bucket}/{key}'
    print(f'Uploaded artifacts archive to {uri}')
    return uri


def rewrite_artifact_uri(recipe, s3_uri):
    """ Points the recipe's single s3:// artifact at the uploaded archive """
    for manifest in recipe.get('Manifests', []):
        for artifact in manifest.get('Artifacts', []):
            if artifact.get('Uri', '').startswith('s3://'):
                artifact['Uri'] = s3_uri
                return recipe
    raise SystemExit('The rendered recipe has no s3:// artifact to rewrite')


def create_component_version(greengrass, recipe, recipe_json_path):
    """ Writes the final recipe as JSON for inspection and registers it with Greengrass """
    recipe_bytes = json.dumps(recipe, indent=2).encode('utf-8')
    os.makedirs(os.path.dirname(recipe_json_path), exist_ok=True)
    with open(recipe_json_path, 'wb') as recipe_file:
        recipe_file.write(recipe_bytes)
    print(f'Creating component version from {recipe_json_path}')
    try:
        return greengrass.create_component_version(inlineRecipe=recipe_bytes)
    except ClientError as e:
        raise SystemExit(f'Failed to create component version: {e}') from None


def wait_until_deployable(greengrass, arn, timeout_seconds=DEPLOYABLE_TIMEOUT_SECONDS,
                          poll_seconds=DEPLOYABLE_POLL_SECONDS):
    """ Polls the component until the Greengrass cloud service reports it DEPLOYABLE """
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = greengrass.describe_component(arn=arn).get('status', {})
        state = status.get('componentState')
        if state == 'DEPLOYABLE':
            return
        if state in TERMINAL_FAILURE_STATES:
            raise SystemExit(f'Component version is {state}: {status.get("message", "")} '
                             f'{status.get("errors", {})}')
        if time.monotonic() >= deadline:
            raise SystemExit(f'Timed out after {timeout_seconds} s waiting for {arn} to become '
                             f'DEPLOYABLE (last state: {state})')
        print(f'Component state is {state}, waiting ...')
        time.sleep(poll_seconds)


def caller_identity(sts):
    """ Returns (account, partition) of the current credentials """
    identity = sts.get_caller_identity()
    return identity['Account'], identity['Arn'].split(':')[1]


def artifact_key(config):
    """ S3 object key of the artifacts archive: <name>/<version>/opcua-data-sim.zip """
    return f'{config.name()}/{config.version()}/{build_component.FILE_ZIP_BASE}.{build_component.FILE_ZIP_EXT}'


def load_recipe(recipe_path, s3_uri):
    """ Loads the rendered recipe and points its s3:// artifact at the uploaded archive """
    with open(recipe_path, encoding='utf-8') as recipe_file:
        recipe = yaml.safe_load(recipe_file)
    return rewrite_artifact_uri(recipe, s3_uri)


def publish(config):
    """ Runs the whole build + publish flow for the given configuration """
    recipe_path, zip_path = build_component.build(config)

    region = config.region()
    s3 = boto3.client('s3', region_name=region)
    greengrass = boto3.client('greengrassv2', region_name=region)
    account, partition = caller_identity(boto3.client('sts', region_name=region))

    arn = component_version_arn(partition, region, account, config)
    refuse_if_version_exists(greengrass, arn, config)

    bucket = config.bucket_name(account)
    ensure_bucket(s3, bucket, region)
    s3_uri = upload_artifact(s3, bucket, artifact_key(config), zip_path)

    recipe = load_recipe(recipe_path, s3_uri)
    recipe_json_path = os.path.join(os.path.dirname(recipe_path), f'{config.name()}-{config.version()}.json')
    create_component_version(greengrass, recipe, recipe_json_path)
    wait_until_deployable(greengrass, arn)

    print(f'Created component version {config.version()}: {arn}')
    print(f'Deploy it with: python3 deploy_component_version.py {config.version()} <thing-name>')
    return arn


def main():
    """ Parses arguments and publishes the component """
    parser = argparse.ArgumentParser(
        description='Package, upload and register a version of the Greengrass component')
    parser.add_argument('--config', default='component.json',
                        help='Path to the component configuration file (default: component.json)')
    args = parser.parse_args()

    # Clients are created inside publish() so --help needs no credentials
    publish(ComponentConfig(args.config))


if __name__ == '__main__':
    main()
