# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Packages, uploads and registers a version of the Greengrass component described by
component.json, in one command:

1) Builds the component (libs/build_component.py): renders recipe.yaml and zips artifacts/.
2) Refuses to continue if that component version already exists in the account.
3) Creates the artifact bucket <publishBucketPrefix>-<region>-<account> if it is
   missing (public access blocked, SSE-S3 default encryption).
4) Creates the CloudWatch Logs group /aws/greengrass/UserComponent/<region>/<name>
   if missing and sets 7-day retention.
5) Uploads the artifacts archive to S3 and points the recipe at it.
6) Creates the component version in the Greengrass cloud service and waits until it
   is DEPLOYABLE.
7) Writes build/device-role-policy.json: the S3, ECR, CloudWatch Logs and SiteWise
   permissions the core device's token exchange role needs (see README step 4).

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
from libs import build_component
from libs.component_config import ComponentConfig

DEPLOYABLE_TIMEOUT_SECONDS = 120
DEPLOYABLE_POLL_SECONDS = 2
TERMINAL_FAILURE_STATES = ('FAILED', 'DEPRECATED')
DEVICE_ROLE_DOCS = 'https://docs.aws.amazon.com/greengrass/v2/developerguide/device-service-role.html'
DEVICE_ROLE_POLICY_FILE = 'build/device-role-policy.json'
DEFAULT_ECR_REPO = 'opcua-data-sim'
DEFAULT_SITEWISE_ALIAS_PREFIX = '/opcua-sim'
LOG_RETENTION_DAYS = 7


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
    return True


def ensure_log_group(logs, log_group, retention_days=LOG_RETENTION_DAYS):
    """
    Makes sure the CloudWatch Logs group the Greengrass Log manager uploads to exists and
    keeps events for retention_days. Returns True if this call created it. Retention is
    applied every time (idempotent), also to an existing group, because a group the Log
    manager created on its own never expires.
    """
    created = True
    try:
        logs.create_log_group(logGroupName=log_group)
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
            raise SystemExit(f'Failed to create log group {log_group}: {e}') from None
        created = False
    try:
        logs.put_retention_policy(logGroupName=log_group, retentionInDays=retention_days)
    except ClientError as e:
        raise SystemExit(f'Failed to set retention on log group {log_group}: {e}') from None
    print(f'{"Created" if created else "Using existing"} log group {log_group} '
          f'({retention_days}-day retention)')
    return created


def generate_device_role_policy(config, bucket, ecr_repo, account,
                                sitewise_alias_prefix=DEFAULT_SITEWISE_ALIAS_PREFIX):
    """
    Generates the IAM policy JSON that the core device's token exchange role needs,
    writes it to build/device-role-policy.json, and prints instructions.
    """
    region = config.region()
    log_group_arn = f"arn:aws:logs:{region}:{account}:log-group:{config.log_group_name()}"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "S3ArtifactAccess",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": f"arn:aws:s3:::{bucket}/*"
            },
            {
                # Accepted wildcard: ecr:GetAuthorizationToken has no resource-level
                # permissions (AWS IAM: "Resource types: none"), so "*" is the only
                # valid Resource. The token alone grants no image access; pulls are
                # limited to one repository by the ECRImagePull statement below.
                "Sid": "ECRAuthToken",
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": "*"
            },
            {
                # Scoped to the single simulator repository. ECR has no IAM condition
                # keys for image tags or digests on these actions, so a tag-level
                # restriction is not expressible in IAM; the recipe pins the image URI.
                "Sid": "ECRImagePull",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability"
                ],
                "Resource": f"arn:aws:ecr:{region}:{account}:repository/{ecr_repo}"
            },
            {
                # Actions the Log manager needs. The group ARN covers CreateLogGroup and
                # DescribeLogStreams; ":*" covers the log-stream ARNs (CreateLogStream,
                # PutLogEvents) of this one group only, whose names contain the date.
                "Sid": "CloudWatchLogsUpload",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams"
                ],
                "Resource": [log_group_arn, f"{log_group_arn}:*"]
            },
            {
                # The SiteWise Edge publisher reads its gateway's capability
                # configuration at startup; verified live as the only read it needs.
                "Sid": "SiteWiseGatewayConfigRead",
                "Effect": "Allow",
                "Action": ["iotsitewise:DescribeGatewayCapabilityConfiguration"],
                "Resource": f"arn:aws:iotsitewise:{region}:{account}:gateway/*"
            },
            {
                "Sid": "SiteWiseIngestByAlias",
                "Effect": "Allow",
                "Action": ["iotsitewise:BatchPutAssetPropertyValue"],
                "Resource": f"arn:aws:iotsitewise:{region}:{account}:time-series/*",
                "Condition": {
                    "StringLike": {"iotsitewise:propertyAlias": f"{sitewise_alias_prefix.rstrip('/')}/*"}
                }
            }
        ]
    }
    os.makedirs(os.path.dirname(DEVICE_ROLE_POLICY_FILE), exist_ok=True)
    with open(DEVICE_ROLE_POLICY_FILE, 'w', encoding='utf-8') as f:
        json.dump(policy, f, indent=2)

    print(f'\nWrote {DEVICE_ROLE_POLICY_FILE} — add this policy to your core device\'s token exchange role.')
    print('Example (replace YOUR_ROLE_NAME with your actual role, e.g. GreengrassV2TokenExchangeRole):')
    print(f'  aws iam put-role-policy --policy-name OpcUaSimulatorAccess '
          f'--policy-document file://{DEVICE_ROLE_POLICY_FILE} --role-name YOUR_ROLE_NAME')
    print(f'See {DEVICE_ROLE_DOCS}\n')


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


def publish(config, ecr_repo=DEFAULT_ECR_REPO, sitewise_alias_prefix=DEFAULT_SITEWISE_ALIAS_PREFIX):
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
    ensure_log_group(boto3.client('logs', region_name=region), config.log_group_name())
    s3_uri = upload_artifact(s3, bucket, artifact_key(config), zip_path)

    recipe = load_recipe(recipe_path, s3_uri)
    recipe_json_path = os.path.join(os.path.dirname(recipe_path), f'{config.name()}-{config.version()}.json')
    create_component_version(greengrass, recipe, recipe_json_path)
    wait_until_deployable(greengrass, arn)

    # Always regenerate, so the file matches the latest publish
    generate_device_role_policy(config, bucket, ecr_repo, account, sitewise_alias_prefix)

    print(f'Created component version {config.version()}: {arn}')
    print(f'Deploy it with: python3 deploy_component_version.py {config.version()} <thing-name>')
    return arn


def main():
    """ Parses arguments and publishes the component """
    parser = argparse.ArgumentParser(
        description='Package, upload and register a version of the Greengrass component')
    parser.add_argument('--config', default='component.json',
                        help='Path to the component configuration file (default: component.json)')
    parser.add_argument('--ecr-repo', default=DEFAULT_ECR_REPO,
                        help=f'ECR repository name for the device role policy (default: {DEFAULT_ECR_REPO})')
    parser.add_argument('--sitewise-alias-prefix', default=DEFAULT_SITEWISE_ALIAS_PREFIX,
                        help='SiteWise Edge measurementDataStreamPrefix the device role policy is '
                             f'scoped to (default: {DEFAULT_SITEWISE_ALIAS_PREFIX})')
    args = parser.parse_args()

    # Clients are created inside publish() so --help needs no credentials
    publish(ComponentConfig(args.config), ecr_repo=args.ecr_repo,
            sitewise_alias_prefix=args.sitewise_alias_prefix)


if __name__ == '__main__':
    main()
