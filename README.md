# OPC UA Simulator for AWS IoT Greengrass V2

A generic OPC UA data source simulator packaged as an [AWS IoT Greengrass V2](https://docs.aws.amazon.com/greengrass/v2/developerguide/what-is-iot-greengrass.html) component named **com.amazon.samples.OpcUaDataSim**. It runs as a Docker container and generates configurable sensor data with realistic patterns. It exposes a standard OPC UA server, so any OPC UA client can read its tags. Its purpose is to supply realistic sample data, without physical equipment, for developing and demonstrating solutions that ingest OPC UA data. A common example is [AWS IoT SiteWise](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/what-is-sitewise.html), which can collect the data at the edge through an [AWS IoT SiteWise Edge](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/gateways.html) OPC UA data source.

> **Note: this is sample code, not for production use.** This project is provided for demonstration, prototyping, and educational purposes only — to show how AWS IoT SiteWise and other OPC UA clients can ingest industrial data. It generates synthetic (fake) data, is provided "as is" without warranty (see [LICENSE](LICENSE)), and is not intended for production workloads or for handling real or sensitive data. Review, test, and harden it to meet your security, reliability, and compliance requirements before adapting any part of it for production.

Define your equipment, sensors, and data patterns in a JSON config file — no code changes needed to switch between use cases. Control the simulator at runtime via MQTT commands from AWS IoT Core: pause/resume data generation, inject anomalies into specific tags, and monitor status.

Built on two Python libraries:
- [asyncua](https://github.com/FreeOpcUa/opcua-asyncio) — Python async OPC UA server implementation
- [awsiotsdk](https://github.com/aws/aws-iot-device-sdk-python-v2) — AWS IoT Device SDK v2 for Python, used as the Greengrass IPC client for MQTT control

## Table of Contents
* [Architecture](#architecture)
  * [Deployment topologies](#deployment-topologies)
    * [Co-located (default)](#co-located-default)
    * [Separate cores](#separate-cores)
* [Prerequisites](#prerequisites)
  * [Greengrass core device](#greengrass-core-device)
    * [Platform](#platform)
    * [Edge runtime](#edge-runtime)
    * [Docker requirements](#docker-requirements)
  * [Core device role](#core-device-role)
* [Deploy the simulator](#deploy-the-simulator)
  * [Set up your developer machine](#set-up-your-developer-machine)
  * [1. Build and push the Docker image](#1-build-and-push-the-docker-image)
  * [2. Set the image URI and region](#2-set-the-image-uri-and-region)
  * [3. Publish the component](#3-publish-the-component)
  * [4. Deploy](#4-deploy)
  * [5. Verify](#5-verify)
* [Test end to end with SiteWise Edge](#test-end-to-end-with-sitewise-edge)
  * [Same core](#same-core)
  * [Different core](#different-core)
* [Cost](#cost)
* [Security](#security)
* [Cleanup](#cleanup)
* [Next steps](#next-steps)
* [Contributing and license](#contributing-and-license)

## Architecture

The **com.amazon.samples.OpcUaDataSim** component is a thin wrapper around a Docker container running the simulator application. The container image is built from the `docker/` directory and pushed to [Amazon ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html). The image is built as a multi-architecture manifest covering both `linux/amd64` (x86_64) and `linux/arm64` (aarch64/Graviton), so the same image URI works on either device. The component downloads the image with the help of the [Docker application manager](https://docs.aws.amazon.com/greengrass/v2/developerguide/docker-application-manager-component.html) component, and uses the [Token exchange service](https://docs.aws.amazon.com/greengrass/v2/developerguide/token-exchange-service-component.html) to obtain credentials to pull from ECR.

The simulator exposes an OPC UA server on TCP port 4840, published from Docker's bridge network, so that an OPC UA client such as SiteWise Edge can read its tags at `opc.tcp://localhost:4840/simulator/` on the same host. For runtime control, the simulator subscribes to and publishes on AWS IoT Core MQTT topics through the Greengrass Nucleus [IPC MQTT proxy](https://docs.aws.amazon.com/greengrass/v2/developerguide/ipc-iot-core-mqtt.html): the Nucleus owns the IoT Core connection and no device certificate is present in the container.

### Deployment topologies

There are two supported ways to consume the simulated data with SiteWise Edge.

#### Co-located (default)

The simulator and SiteWise Edge (the OPC UA collector and the publisher) run on the **same Greengrass core device**. The collector reads the simulator over the loopback interface at `opc.tcp://localhost:4840/simulator/`. This is the simplest setup and what the [Deploy the simulator](#deploy-the-simulator) and [Test end to end with SiteWise Edge](#test-end-to-end-with-sitewise-edge) walkthroughs set up.

```
                    Greengrass core device
  +---------------------------------------------------------------------+
  |  +--------------------+                      +---------------------+ |
  |  | Simulator (Docker) | <--connects to-----  | SiteWise Edge       | |
  |  | OPC UA server      |   localhost:4840     | collector/publisher | |
  |  |  :4840             | --tag values------>  |                     | |
  |  +--------------------+                      +----------+----------+ |
  +--------------------------------------------------------|------------+
                                                           v
                                                   SiteWise (cloud)

  IoT Core <-- MQTT via Greengrass Nucleus (IPC MQTT proxy) --> Simulator (control / status)
```

The simulator is the OPC UA *server*: it only listens on port 4840. The SiteWise Edge collector is the OPC UA *client*: it opens the connection, subscribes to the tags, and receives value updates over that connection. The simulator needs no knowledge of, or configuration for, the collector.

#### Separate cores

The simulator runs on one Greengrass core and SiteWise Edge runs on a **different** core (or any OPC UA client elsewhere on the network). In this case the SiteWise Edge OPC UA data source must point at the simulator host's IP address — `opc.tcp://<simulator-host-ip>:4840/simulator/` — and the network must allow inbound TCP 4840 from the SiteWise Edge host to the simulator host (security groups, firewall, routing).

```
   Simulator core                                    SiteWise Edge core
  +--------------------+                            +-----------------------+
  | Simulator (Docker) | <--connects to-----------  | SiteWise Edge         |
  | OPC UA server      |   opc.tcp://<sim-ip>:4840  |  collector/publisher  |
  |  :4840             | --tag values (OPC UA)--->  |                       |
  +--------------------+                            +-----------+-----------+
     ^ inbound TCP 4840 must be allowed                          v
       from the SiteWise Edge host                       SiteWise (cloud)

  IoT Core <-- MQTT via Greengrass Nucleus (IPC MQTT proxy) --> Simulator (control / status)
```

The simulator component itself is **identical** in both topologies — it only serves OPC UA and exchanges control messages through the Nucleus. The difference is entirely in how SiteWise Edge is configured (localhost vs. remote IP) and the network path between them. Because the simulator does not depend on SiteWise Edge, the simulator-only deployment has fewer component dependencies: just the Docker Application Manager and the Token Exchange Service (plus the Nucleus). SiteWise Edge and its EMQX/collector/publisher components are only required on whichever core runs SiteWise Edge.

## Prerequisites

### Greengrass core device

#### Platform

This component requires that the Greengrass core device run a Linux operating system. It supports the CPU architectures supported by both Greengrass and Docker (x86_64 and aarch64/arm64).

#### Edge runtime

The [Greengrass edge runtime needs to be deployed](https://docs.aws.amazon.com/greengrass/v2/developerguide/getting-started.html) to a suitable machine, virtual machine or EC2 instance, installed as a system service. Greengrass nucleus 2.12.0 or later is required (the recipe declares this dependency; tested on 2.17.0).

The core device must already have a deployment that targets its **thing** (not only a thing group). `deploy_component_version.py` revises that deployment and aborts if none exists; see [4. Deploy](#4-deploy).

#### Docker requirements

Your core device must [meet the requirements to run Docker containers using Docker Compose](https://docs.aws.amazon.com/greengrass/v2/developerguide/run-docker-container.html). `docker` must be on root's `PATH` (all lifecycle steps run with `RequiresPrivilege: true`), together with a Compose v2 implementation: the `docker compose` v2 plugin is preferred, and the standalone `docker-compose` v2 binary also works (the recipe detects which one is present). The Python-based `docker-compose` v1 is end-of-life and untested.

### Core device role

Assuming `publishBucketPrefix` in **component.json** is left unchanged, this component downloads artifacts from an S3 bucket named **opcua-data-sim-samples-REGION-ACCOUNT** (`publish_component.py` creates it on first publish), and pulls the container image from the Amazon ECR repository **opcua-data-sim**. Therefore your [Greengrass core device role](https://docs.aws.amazon.com/greengrass/v2/developerguide/device-service-role.html) must allow the **s3:GetObject** permission for that bucket and the ECR permissions to pull images from that repository.

Policy template to add to your device role (substituting correct values for ACCOUNT and REGION):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ArtifactAccess",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::opcua-data-sim-samples-REGION-ACCOUNT/*"
    },
    {
      "Sid": "ECRAuthToken",
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken"],
      "Resource": "*"
    },
    {
      "Sid": "ECRImagePull",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability"
      ],
      "Resource": "arn:aws:ecr:REGION:ACCOUNT:repository/opcua-data-sim"
    }
  ]
}
```

`ecr:GetAuthorizationToken` is an account-level action that does not operate on a repository, so it is the only statement that needs `Resource: "*"`; everything else is scoped to the `opcua-data-sim` repository and the artifact bucket.

If you use SiteWise Edge to ingest the simulated data, the device role also needs SiteWise ingestion permissions. See [Test end to end with SiteWise Edge](#test-end-to-end-with-sitewise-edge).

## Deploy the simulator

This section deploys the sample as-is (the shipped `artifacts/config.json`: one device, `Motor01`, with four tags). Changing devices and tags is covered in the Operations Guide under [Operate and customize](OperationsGuide.md#operate-and-customize). The region and account you pass to `build_image.sh`, the `region` in `component.json`, and the region of your AWS CLI credentials must all agree.

### Set up your developer machine

The build, publish, and deploy commands run from your developer machine, which needs:

- **AWS CLI**: [install the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) and configure credentials. The developer credentials need to create and push to the ECR repository, create and configure the S3 artifact bucket and upload to it (`s3:CreateBucket`, `s3:PutPublicAccessBlock`, `s3:PutBucketEncryption`, `s3:ListBucket` for `HeadBucket`, `s3:PutObject`), create Greengrass component versions and deployments (`greengrass:CreateComponentVersion`, `DescribeComponent`, `ListComponentVersions`, `CreateDeployment`, `GetDeployment`, `ListDeployments`), call `sts:GetCallerIdentity`, publish to the control topic (`iot:Publish`), and — for the SiteWise test — call the `iotsitewise` APIs used there; the AWS managed policies for those services cover this, or scope your own policy to these actions.
- **Docker**: Docker with [buildx](https://docs.docker.com/build/) is required to build and push the multi-architecture simulator image.
- **Python**: the scripts in this repository are Python 3 scripts and hence **python3** and **pip3** are required: Python 3.10 or newer (CI runs 3.12). Package dependencies can be resolved as follows (consider using a [virtual environment](https://docs.python.org/3/library/venv.html)):

  ```
  pip3 install -r requirements.txt
  ```

  [Boto3](https://docs.aws.amazon.com/boto3/latest/) is included in the package dependencies and therefore your machine requires appropriate [credentials](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html).

### 1. Build and push the Docker image

The simulator runs a custom application image, so you must build it and push it to a registry your core device can pull from (Amazon ECR). A helper script is provided:

```
cd docker
bash build_image.sh <region> <account-id> opcua-data-sim latest
```

By default this builds a single multi-architecture image (both `linux/amd64` and `linux/arm64`) and pushes it under one tag, so the same image URI runs on x86_64 and arm64 devices. To build for just one platform (faster, e.g. local testing), pass it as the last argument: `bash build_image.sh <region> <account-id> opcua-data-sim latest linux/arm64`. The script logs in to ECR, creates the repository (with image scanning enabled) if needed, sets up a buildx builder, and builds and pushes the image.

### 2. Set the image URI and region

Return to the repository root (`cd ..`); all remaining commands run from there.

Edit **artifacts/docker-compose.yml** and set the `image:` value to the URI you pushed in step 1. The build script prints this URI. (`publish_component.py` refuses to run while the placeholder URI is still in place.)

Set the AWS region in **component.json** (`region`). This is the region the component is published to and the region `deploy_component_version.py` deploys in; it must be the one you built the image in.

### 3. Publish the component

```
python3 publish_component.py
```

This packages `recipe.yaml` and `artifacts/` (config.json, docker-compose.yml), uploads the archive to S3 and registers the component version in the Greengrass cloud service — the equivalent of `gdk component build` + `gdk component publish` for readers who know GDK. On first publish it creates the bucket `opcua-data-sim-samples-<region>-<account>` (block public access on, SSE-S3 default encryption) and reminds you to grant the core device role `s3:GetObject` on it ([Core device role](#core-device-role)). It refuses to publish a version that already exists — bump `version` in `component.json` instead.

### 4. Deploy

The component can be deployed using [the console or the AWS CLI](https://docs.aws.amazon.com/greengrass/v2/developerguide/create-deployments.html) in the normal way. Alternatively, use the supplied **deploy_component_version.py** script, which adds the Docker Application Manager and Token Exchange Service dependencies to the existing deployment for the core device:

```
python3 deploy_component_version.py 1.0.0 MyCoreDeviceThingName
```

To find your core device's thing name, run `aws greengrassv2 list-core-devices` or open the core device in the Greengrass console. The script revises the latest deployment that targets the core device **thing** (not a thing group) and aborts with `No existing Thing deployment for this Core Device. Abort.` if none exists; create one in the console or with `aws greengrassv2 create-deployment --target-arn arn:aws:iot:<region>:<account>:thing/<thing>` first. It uses the region from `component.json` and waits up to 15 minutes for completion.

### 5. Verify

What success looks like (values from a live run with the shipped `config.json`):

1. `aws greengrassv2 list-installed-components --core-device-thing-name <thing-name>` lists `com.amazon.samples.OpcUaDataSim` version `1.0.0` with lifecycle state `RUNNING`.
2. The component log on the core device, `/greengrass/v2/logs/com.amazon.samples.OpcUaDataSim.log`, contains `Created 1 devices with 4 tags`, `Server running. Press Ctrl+C to stop.`, and `CONTROL: Connected to Greengrass IPC, subscribed to 'simulator/<thing-name>/control'`.
3. `docker ps` on the core device shows the container `opcua-data-sim` as `Up (healthy)` with `0.0.0.0:4840->4840/tcp`.
4. Publish a pause command (see [MQTT control](OperationsGuide.md#mqtt-control) for the `aws iot-data publish` form):

   ```
   aws iot-data publish --topic "simulator/<thing-name>/control" --payload '{"action":"pause"}' --cli-binary-format raw-in-base64-out --region <region>
   ```

   The component log shows `CONTROL: Simulation paused` and `CONTROL: Published ack 'pause' to simulator/<thing-name>/status`, and the periodic sample lines switch to `[sim ..h [PAUSED]] Motor01: ...` with frozen values. Publish `{"action":"resume"}` to continue. To see the ack itself, subscribe to `simulator/+/status` in the AWS IoT console MQTT test client before publishing.

## Test end to end with SiteWise Edge

The simulator works with SiteWise Edge in either of the [deployment topologies](#deployment-topologies).

### Same core

SiteWise Edge runs on the same core as the simulator. The steps below use the AWS CLI and create an [MQTT-enabled, V3 gateway](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/mqtt-enabled-v3-gateway.html). You can also create the gateway in the console ([SiteWise Edge gateway documentation](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/create-gateway-ggv2.html); untested with this sample); if you do, check afterwards with `aws greengrassv2 list-deployments --target-arn <thing ARN>` that the latest deployment still contains `com.amazon.samples.OpcUaDataSim`, because a console-created gateway deployment can supersede the one that installed the simulator.

1. Deploy this simulator component to the core (see [Deploy the simulator](#deploy-the-simulator)).
2. Create a SiteWise Edge gateway targeting the **same** Greengrass core device. A V3 gateway requires `--gateway-version 3` and the core's OS/architecture (`LINUX_AMD64`, `LINUX_AARCH64`, or `WINDOWS_AMD64`):

   ```bash
   aws iotsitewise create-gateway \
     --gateway-name opcua-sim-gateway \
     --gateway-platform "greengrassV2={coreDeviceThingName=<core-thing-name>,coreDeviceOperatingSystem=LINUX_AMD64}" \
     --gateway-version 3
   ```

   Note the `gatewayId` in the output.
3. Add the OPC UA data source and the cloud destination. Both are [gateway capability configurations](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/configure-opcua-source.html); the collector reads the simulator at `opc.tcp://localhost:4840/simulator/` with no security and anonymous authentication, and the publisher sends everything to SiteWise in real time:

   ```bash
   aws iotsitewise update-gateway-capability-configuration \
     --gateway-id <gateway-id> \
     --capability-namespace iotsitewise:opcuacollector:3 \
     --capability-configuration '{"sources":[{"name":"opcua-sim","endpoint":{"certificateTrust":{"type":"TrustAny"},"endpointUri":"opc.tcp://localhost:4840/simulator/","securityPolicy":"NONE","messageSecurityMode":"NONE","identityProvider":{"type":"Anonymous"},"nodeFilterRules":[]},"measurementDataStreamPrefix":"/opcua-sim","destination":{"type":"MQTT"}}]}'

   aws iotsitewise update-gateway-capability-configuration \
     --gateway-id <gateway-id> \
     --capability-namespace iotsitewise:publisher:3 \
     --capability-configuration '{"sources":[{"type":"MQTT"}],"destinations":[{"type":"SITEWISE_REALTIME","name":"sitewise-realtime","config":{"publishingOrder":"TIME_ORDER","enableCompression":true,"maxBatchWaitTime":"10s"},"filters":[{"type":"PATH","config":{"paths":["#"]}}]}]}'
   ```

   Both calls return `capabilitySyncStatus: OUT_OF_SYNC` until the gateway software is running on the core.
4. Deploy the SiteWise Edge components to the core. Creating the gateway with the CLI against an existing core does **not** create a Greengrass deployment for you, and a core device has only one effective deployment, so revise the existing deployment to contain **both** the simulator and the SiteWise Edge components. A V3 gateway needs the EMQX broker (with its TCP listener on loopback port 1883 and Greengrass auth bypassed), the OPC UA collector, and the publisher; Stream Manager and Secret Manager are pulled in as dependencies:

   ```bash
   aws greengrassv2 create-deployment --cli-input-json '{
     "targetArn": "arn:aws:iot:<region>:<account>:thing/<core-thing-name>",
     "deploymentName": "opcua-sim + SiteWise Edge V3",
     "components": {
       "aws.greengrass.DockerApplicationManager": {"componentVersion": "2.0.17"},
       "aws.greengrass.TokenExchangeService": {"componentVersion": "2.0.3"},
       "com.amazon.samples.OpcUaDataSim": {"componentVersion": "1.0.0"},
       "aws.greengrass.clientdevices.mqtt.EMQX": {
         "componentVersion": "2.0.4",
         "configurationUpdate": {"merge": "{\"emqxConfig\":{\"authorization\":{\"no_match\":\"allow\"},\"listeners\":{\"tcp\":{\"default\":{\"enabled\":true,\"enable_authn\":false}}}},\"authMode\":\"bypass\",\"dockerOptions\":\"-p 127.0.0.1:1883:1883 -v emqx-data:/opt/emqx/data -e EMQX_NODE__NAME=emqx@local\",\"requiresPrivilege\":\"true\"}"}
       },
       "aws.iot.SiteWiseEdgeCollectorOpcua": {"componentVersion": "3.2.1"},
       "aws.iot.SiteWiseEdgePublisher": {"componentVersion": "4.1.5"}
     },
     "deploymentPolicies": {"failureHandlingPolicy": "ROLLBACK", "componentUpdatePolicy": {"timeoutInSeconds": 60, "action": "NOTIFY_COMPONENTS"}}
   }'
   ```

   Use `aws greengrassv2 list-component-versions --arn arn:aws:greengrass:<region>:aws:components:<name>` to pick current versions. Once the deployment is `COMPLETED`, `aws greengrassv2 list-installed-components --core-device-thing-name <core-thing-name>` should show `com.amazon.samples.OpcUaDataSim`, `aws.greengrass.clientdevices.mqtt.EMQX`, `aws.iot.SiteWiseEdgeCollectorOpcua` and `aws.iot.SiteWiseEdgePublisher` as `RUNNING` (their dependencies are listed too), and `aws iotsitewise describe-gateway --gateway-id <gateway-id>` should show both capabilities `IN_SYNC`.
5. Data streams appear within about a minute as disassociated time series named `<prefix>/<device>/<tag>`, for example `/opcua-sim/Motor01/Temperature`:

   ```bash
   aws iotsitewise list-time-series --time-series-type DISASSOCIATED --alias-prefix /opcua-sim
   aws iotsitewise get-asset-property-value --property-alias /opcua-sim/Motor01/Temperature
   ```

   To see modeled data, create an asset model and asset, then bind the existing stream to the property with `aws iotsitewise associate-time-series-to-asset-property --alias /opcua-sim/Motor01/Temperature --asset-id <asset-id> --property-id <property-id>` (`update-asset-property --property-alias` fails with `ConflictingOperationException` once the disassociated stream exists).

### Different core

SiteWise Edge runs on a different core (or host) than the simulator.

1. Deploy this simulator component to the simulator core. It needs only the Docker Application Manager and Token Exchange Service dependencies — not SiteWise Edge.
2. Note the simulator host's IP address and ensure the SiteWise Edge host can reach it on TCP 4840 (security group / firewall ingress, and routing if in different subnets/VPCs).
3. On the SiteWise Edge gateway, add an OPC UA data source pointing at the simulator host: `opc.tcp://<simulator-host-ip>:4840/simulator/`, no authentication (the same `iotsitewise:opcuacollector:3` configuration as in the same-core steps, with `endpointUri` changed).
4. Data streams appear in the SiteWise console within minutes.

> **Security note:** The OPC UA endpoint is unauthenticated. Only expose port 4840 to trusted hosts on a private network — never to the public internet. Use security groups, host firewalls, and private subnets to restrict access to the SiteWise Edge host (or other trusted OPC UA clients). This simulator generates synthetic data only and is intended for development and demonstration, not production data collection. See [Security](#security) for more.

Whether SiteWise Edge runs on the same or another core, the device role of the core running **SiteWise Edge** needs SiteWise ingestion permissions. The OPC UA collector publishes each value by **property alias** (`measurementDataStreamPrefix` + `/<device>/<tag>`, e.g. `/opcua-sim/Motor01/Temperature`), and AWS authorizes alias-addressed writes against the `time-series` resource, so scope `BatchPutAssetPropertyValue` by the alias prefix you set in `measurementDataStreamPrefix` (substituting REGION and ACCOUNT):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SiteWiseGatewayRead",
      "Effect": "Allow",
      "Action": [
        "iotsitewise:Describe*",
        "iotsitewise:List*",
        "iotsitewise:Get*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SiteWiseIngestByAlias",
      "Effect": "Allow",
      "Action": ["iotsitewise:BatchPutAssetPropertyValue"],
      "Resource": "arn:aws:iotsitewise:REGION:ACCOUNT:time-series/*",
      "Condition": {
        "StringLike": { "iotsitewise:propertyAlias": "/opcua-sim/*" }
      }
    }
  ]
}
```

Only if you bind streams to assets (step 5 above) add a further `iotsitewise:BatchPutAssetPropertyValue` statement on the asset ARNs, `arn:aws:iotsitewise:REGION:ACCOUNT:asset/ASSET_ID` (one per asset from `aws iotsitewise list-assets`; do not use `asset/*` outside a short-lived sandbox). The [SiteWise Edge self-hosted gateway requirements](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/configure-gateway-ggv2.html) page is authoritative for the full set of permissions the gateway role needs (it also lists the `iot:*`, `logs:*` and `s3:GetObject`/`GetBucketLocation` actions that the Greengrass installer's role usually already grants). This policy was not exercised in the live test (that core's role was broader); verify with `aws iotsitewise list-time-series` after deployment. Both policy templates in this README (this one and the [Core device role](#core-device-role) example) are starting points: tighten them to the resources of your own deployment before use.

## Cost

This is a sample intended for development and demonstration. The sample code itself is provided at no cost, but deploying and running it uses AWS services that may incur charges. You are responsible for the cost of the AWS services used. Review the pricing pages below and use the [AWS Pricing Calculator](https://calculator.aws/) to estimate charges for your specific usage.

| AWS resource | When it applies | Cost driver | Pricing |
|---|---|---|---|
| [Amazon ECR](https://aws.amazon.com/ecr/pricing/) | Always (stores the simulator container image) | Image storage per GB-month and data transfer out | ECR pricing |
| [Amazon S3](https://aws.amazon.com/s3/pricing/) | Always (`publish_component.py` uploads component artifacts) | Object storage and requests (artifacts are small) | S3 pricing |
| [AWS IoT Greengrass](https://aws.amazon.com/greengrass/pricing/) | Always (core device management) | Per active Greengrass core device, per month | Greengrass pricing |
| [AWS IoT Core](https://aws.amazon.com/iot-core/pricing/) | Always when running on Greengrass (Nucleus connection plus the simulator's status/ack messages); none in local mode | Messages, connectivity minutes, and rules triggered | IoT Core pricing |
| [AWS IoT SiteWise](https://aws.amazon.com/iot-sitewise/pricing/) | Only if you ingest the simulated data with SiteWise Edge | Data ingestion, storage, and processing | SiteWise pricing |
| [Amazon EC2](https://aws.amazon.com/ec2/pricing/) | If your Greengrass core runs on EC2 | Instance hours, EBS storage, data transfer | EC2 pricing |

Costs scale with configuration. A faster `updateIntervalSeconds`, more devices/tags, and SiteWise ingestion of every tag all increase message volume and ingestion charges. To avoid ongoing charges when you are done, follow the steps in [Cleanup](#cleanup).

## Security

- **Unauthenticated OPC UA endpoint.** The simulator serves OPC UA on TCP 4840 with no authentication or encryption. Only expose this port to trusted hosts on a private network (security groups, host firewalls, private subnets). Never expose it to the public internet.
- **Synthetic data only.** The simulator generates fake sensor data. It is intended for development, testing, and demonstration — not for collecting or handling real production or sensitive data.
- **No device credentials in the container.** MQTT control goes through the Greengrass IPC MQTT proxy. The container only receives the per-component `SVCUID` token, whose permissions are limited to the two topics listed in the recipe's `accessControl`; no certificates or private keys are mounted. Certificate and key files (`*.pem`, `*.crt`, `*.key`) are also excluded from the repository via `.gitignore` — never commit credentials.
- **Hardened container.** The image runs as a non-root user (UID 1000), drops all Linux capabilities (`cap_drop: ALL`), sets `no-new-privileges`, and uses a read-only root filesystem with a tmpfs `/tmp`. It runs on Docker's bridge network with only TCP 4840 published, and its lifecycle is owned by Greengrass (compose runs in the foreground, so a crash is visible to the Nucleus). All of these flags were verified on a Greengrass core device (Ubuntu 22.04, Nucleus 2.17.0, standalone docker-compose v2.32.4) with the full control-channel test sequence; relax a flag only if your Docker runtime does not support it.
- **Least privilege.** Scope the Greengrass core device role to only the S3 bucket and ECR repository this component uses, and the SiteWise Edge gateway role to the alias prefix (and asset ARNs, if any) of your deployment. See [Core device role](#core-device-role), [MQTT control](OperationsGuide.md#mqtt-control), and [Test end to end with SiteWise Edge](#test-end-to-end-with-sitewise-edge) for example policies.
- **Reporting.** If you discover a potential security issue in this project, follow the process in [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) and do not create a public GitHub issue.

## Cleanup

Removing the component from a deployment does not remove the Docker image, the artifact caches, or the working directory from the core device. To stop incurring charges and remove everything this sample created:

1. Remove **com.amazon.samples.OpcUaDataSim** from the core device's deployment (console, AWS CLI, or revise the deployment without the component). Greengrass runs the recipe's `Shutdown` step, which stops the container.
2. On the core device, remove what Greengrass does not clean up itself:
   - From `/greengrass/v2/work/com.amazon.samples.OpcUaDataSim`, run `docker compose down` (or `docker-compose down`) if the container is still present, then `docker rmi <image-uri>` to delete the image.
   - Delete the working directory `/greengrass/v2/work/com.amazon.samples.OpcUaDataSim`.
   - Delete the artifact caches `/greengrass/v2/packages/artifacts/com.amazon.samples.OpcUaDataSim/` and `/greengrass/v2/packages/artifacts-unarchived/com.amazon.samples.OpcUaDataSim/`.
3. Delete the ECR repository `opcua-data-sim` (and its images).
4. Empty and delete the S3 artifact bucket `opcua-data-sim-samples-REGION-ACCOUNT`.
5. Delete the component versions from the Greengrass cloud service if you no longer need them (`aws greengrassv2 delete-component`).
6. If you created a SiteWise Edge gateway and assets for the simulated data, delete the gateway, the assets, and the asset models. Order matters: first stop the collector (revert the core to a deployment without the SiteWise Edge components) or the time series are recreated, then `disassociate-time-series-from-asset-property` → `delete-asset` → `delete-asset-model` → `aws iotsitewise delete-time-series --alias <alias>` for each stream (`/opcua-sim/Motor01/Temperature`, …).
7. If you deployed the SiteWise Edge V3 components with the deployment JSON above, remove the EMQX Docker volume on the core once the EMQX container is gone: `docker volume rm emqx-data`.

## Next steps

The [Operations Guide](OperationsGuide.md) covers the running simulator:

- Define your own devices, tags and data patterns in `artifacts/config.json`: [Configuration](OperationsGuide.md#configuration).
- Pause, resume and inject anomalies from AWS IoT Core: [MQTT control](OperationsGuide.md#mqtt-control).
- Diagnose a component that fails to start or has no MQTT control: [Troubleshooting](OperationsGuide.md#troubleshooting).
- Run the simulator locally, add patterns, types or control actions, and run the tests: [Development and extending](OperationsGuide.md#development-and-extending).
- See what each file in this repository is for: [Repository contents](OperationsGuide.md#repository-contents).

## Contributing and license

See [CONTRIBUTING](CONTRIBUTING.md) for how to report issues and submit changes, and [CHANGELOG](CHANGELOG.md) for release notes.

This sample is licensed under the MIT-0 License. See [LICENSE](LICENSE).

It relies on third-party packages that are provided under their own licenses and are retrieved from their upstream sources at build time (not redistributed in this repository). See [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES) for attributions, including a note on the LGPL-3.0-licensed `asyncua` dependency.
