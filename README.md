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
  * [Developer machine](#developer-machine)
* [Deploy the simulator](#deploy-the-simulator)
  * [1. Clone the repository and install dependencies](#1-clone-the-repository-and-install-dependencies)
  * [2. Build and push the Docker image](#2-build-and-push-the-docker-image)
  * [3. Publish the component](#3-publish-the-component)
  * [4. Update the core device role](#4-update-the-core-device-role)
  * [5. Deploy](#5-deploy)
  * [6. Verify](#6-verify)
* [Test end to end with SiteWise Edge](#test-end-to-end-with-sitewise-edge)
  * [Same core](#same-core)
  * [Different core](#different-core)
  * [SiteWise permissions](#sitewise-permissions)
* [Cost](#cost)
* [Security](#security)
* [Cleanup](#cleanup)
* [Next steps](#next-steps)
* [Contributing and license](#contributing-and-license)

## Architecture

The **com.amazon.samples.OpcUaDataSim** component is a thin wrapper around a Docker container running the simulator application. The container image is built from the `docker/` directory and pushed to [Amazon ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html). The image is built as a multi-architecture manifest covering both `linux/amd64` (x86_64) and `linux/arm64` (aarch64/Graviton), so the same image URI works on either device. The component downloads the image with the help of the [Docker application manager](https://docs.aws.amazon.com/greengrass/v2/developerguide/docker-application-manager-component.html) component, and uses the [Token exchange service](https://docs.aws.amazon.com/greengrass/v2/developerguide/token-exchange-service-component.html) to obtain credentials to pull from ECR.

The recipe starts the container with a plain `docker run` (no Docker Compose), mounting `config.json` from the component artifact. The simulator exposes an OPC UA server on TCP port 4840. By default the port is published only on the core's loopback address (`127.0.0.1:4840`), so an OPC UA client such as SiteWise Edge on the same host reads its tags at `opc.tcp://localhost:4840/simulator/` and nothing else on the network can reach the unauthenticated endpoint. For runtime control, the simulator subscribes to and publishes on AWS IoT Core MQTT topics through the Greengrass Nucleus [IPC MQTT proxy](https://docs.aws.amazon.com/greengrass/v2/developerguide/ipc-iot-core-mqtt.html): the Nucleus owns the IoT Core connection and no device certificate is present in the container.

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

The simulator runs on one Greengrass core and SiteWise Edge runs on a **different** core (or any OPC UA client elsewhere on the network). In this case the simulator must publish TCP 4840 on a network address of its host (`--publish-address`, see [Different core](#different-core)), the SiteWise Edge OPC UA data source must point at that address — `opc.tcp://<simulator-host-ip>:4840/simulator/` — and the network must allow inbound TCP 4840 from the SiteWise Edge host only (security groups, firewall, routing).

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

The simulator component is the same in both topologies — it only serves OPC UA and exchanges control messages through the Nucleus. The differences are the address TCP 4840 is published on (loopback by default, a host address for a separate core), how SiteWise Edge is configured (localhost vs. remote IP), and the network path between them. Because the simulator does not depend on SiteWise Edge, the simulator-only deployment has fewer component dependencies: just the Docker Application Manager and the Token Exchange Service (plus the Nucleus); the deploy script also adds the Log manager to upload the simulator's log to CloudWatch Logs. SiteWise Edge and its EMQX/collector/publisher components are only required on whichever core runs SiteWise Edge.

## Prerequisites

### Greengrass core device

#### Platform

This component requires that the Greengrass core device run a Linux operating system. It supports the CPU architectures supported by both Greengrass and Docker (x86_64 and aarch64/arm64).

#### Edge runtime

The [Greengrass edge runtime needs to be deployed](https://docs.aws.amazon.com/greengrass/v2/developerguide/getting-started.html) to a suitable machine, virtual machine or EC2 instance, installed as a system service. Greengrass nucleus 2.12.0 or later is required (the recipe declares this dependency; tested on 2.17.0).

#### Docker requirements

Your core device must [meet the requirements to run Docker containers](https://docs.aws.amazon.com/greengrass/v2/developerguide/run-docker-container.html). The only host requirement is Docker: `docker` must be on root's `PATH` (all lifecycle steps run with `RequiresPrivilege: true`). Docker Compose is not needed; the recipe starts the container with `docker run`.

### Developer machine

The build, publish, and deploy commands run from your developer machine, which needs:

- **AWS CLI**: [Install the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) and configure credentials.
- **AWS IAM permissions**: Your developer credentials need access to Amazon ECR (create repository, push images), Amazon S3 (create bucket, upload artifacts), AWS IoT Greengrass (create component versions and deployments), Amazon CloudWatch Logs (create log group, set retention), AWS STS (get caller identity), AWS IoT Core (publish to MQTT topics), and IoT SiteWise (create gateway, configure data sources). The AWS managed policies for these services cover the required actions, or scope a custom policy to just what the scripts use.
- **Docker**: Docker with [buildx](https://docs.docker.com/build/) is required to build and push the multi-architecture simulator image.
- **Python**: Python 3.10 or newer (CI runs 3.12).

## Deploy the simulator

This section deploys the sample as-is (the shipped `artifacts/config.json`: one device, `Motor01`, with four tags). Changing devices and tags is covered in the Operations Guide under [Operate and customize](docs/OperationsGuide.md#operate-and-customize). Use credentials for the same AWS account throughout; `build_image.sh` records the region in `component.json`, so the later scripts use it automatically.

### 1. Clone the repository and install dependencies

```bash
git clone https://github.com/aws-samples/sample-greengrassv2-opcua-simulator.git
cd sample-greengrassv2-opcua-simulator
pip3 install -r requirements.txt
```

Consider using a [virtual environment](https://docs.python.org/3/library/venv.html).

### 2. Build and push the Docker image

Run from the repository root, replacing `<region>` with the AWS Region to deploy in (for example, `eu-west-1`):

```bash
bash docker/build_image.sh <region>
```

The script builds the simulator image for both `linux/amd64` and `linux/arm64` (one image URI runs on x86_64 and arm64 devices), creates the `opcua-data-sim` ECR repository with image scanning enabled if needed, and pushes the image. It then points the component at what it pushed, so there is nothing to edit by hand:

- **component.json** `region` is set to the region you passed. `publish_component.py` and `deploy_component_version.py` use this region.
- **component.json** `image` is set to the pushed image URI. `publish_component.py` writes it into the recipe.

The account defaults to that of your current AWS credentials. Without a region argument, the script uses the `region` already in `component.json`. To build one platform only (faster), use `bash docker/build_image.sh <region> "" opcua-data-sim latest linux/arm64`.

### 3. Publish the component

```
python3 publish_component.py
```

This packages `recipe.yaml` and `artifacts/` (config.json), uploads the archive to S3 and registers the component version in the Greengrass cloud service — the equivalent of `gdk component build` + `gdk component publish` for readers who know GDK. On first publish it creates the bucket `opcua-data-sim-samples-<region>-<account>` (block public access on, SSE-S3 default encryption). It also creates the CloudWatch Logs group `/aws/greengrass/UserComponent/<region>/com.amazon.samples.OpcUaDataSim` with 7-day retention. It refuses to publish a version that already exists — bump `version` in `component.json` instead.

The script also generates **build/device-role-policy.json** with the exact IAM policy your core device needs. Apply it in the next step.

> **Tip:** To view the new component, open the [AWS IoT console](https://console.aws.amazon.com/iot/home) in the same region and choose **Greengrass devices** > **Components**. It's listed under **My components** as `com.amazon.samples.OpcUaDataSim`, version `1.0.0`.

### 4. Update the core device role

The component downloads artifacts from the S3 bucket created in step 3 and pulls the container image from Amazon ECR, the Log manager uploads the simulator's log to CloudWatch Logs, and SiteWise Edge on the same core writes the simulated data to AWS IoT SiteWise. Your [Greengrass core device role](https://docs.aws.amazon.com/greengrass/v2/developerguide/device-service-role.html) (the token exchange role) needs permissions for all four.

`publish_component.py` generated the policy at **build/device-role-policy.json** with your actual bucket, repository, log group, and SiteWise ARNs (the CloudWatch Logs statement is scoped to the simulator's log group; ingestion is scoped to the `/opcua-sim/*` alias prefix). Add it to your token exchange role (replace `YOUR_ROLE_NAME` with your actual role name, commonly `GreengrassV2TokenExchangeRole`):

```bash
aws iam put-role-policy \
  --policy-name OpcUaSimulatorAccess \
  --policy-document file://build/device-role-policy.json \
  --role-name YOUR_ROLE_NAME
```

If you applied an earlier version of this policy, re-run the same command: `put-role-policy` overwrites the inline policy `OpcUaSimulatorAccess`, adding the `CloudWatchLogsUpload` statement. The policy file only contains that statement if step 3 ran with the current scripts. Because step 3 refuses an existing version, bump `version` in `component.json` (or delete the old component version) and publish again first.

If SiteWise Edge runs on a [separate core](#separate-cores), the two SiteWise statements belong on that core's role instead; see [SiteWise permissions](#sitewise-permissions).

### 5. Deploy

Replace `<thing-name>` with your core device's thing name (shown in the Greengrass console under **Core devices**, or by `aws greengrassv2 list-core-devices`):

```bash
python3 deploy_component_version.py 1.0.0 <thing-name>
```

The script deploys to that core device only, using the region in `component.json`:

- It adds the simulator, plus the Docker Application Manager and Token Exchange Service it depends on, to the deployment that targets the device's thing. If the thing has no deployment yet (typical when the Greengrass installer put the device in a thing group), it creates one.
- It adds the [Log manager](https://docs.aws.amazon.com/greengrass/v2/developerguide/log-manager-component.html) (`aws.greengrass.LogManager`) and configures it to upload only the simulator's log to CloudWatch Logs every 60 seconds. If the deployment already has the Log manager, its version and settings (including its upload interval) are kept and only the simulator's entry is added. The Log manager is a nucleus plugin, so adding it restarts the nucleus once.
- It never changes thing group deployments, so other devices in the group are unaffected, and components the device gets from a group (such as the Greengrass CLI) keep running alongside the simulator.
- It waits up to 15 minutes for the deployment to complete.
- If SiteWise Edge runs on the core and this deployment installs a new simulator version (which replaces the container), it prints how to restart the SiteWise Edge OPC UA collector in case SiteWise stops receiving values. See [Troubleshooting](docs/OperationsGuide.md#troubleshooting).

Options: `--publish-address <ip>` publishes the OPC UA port beyond the core's loopback address, only for a [separate SiteWise Edge core](#different-core); `--config my-config.json` deploys your own devices and tags as component configuration, which the running simulator applies live without a new component version; `--reset-config` returns to the shipped default. See [Change the configuration](docs/OperationsGuide.md#change-the-configuration).

You can also deploy the component from [the console or the AWS CLI](https://docs.aws.amazon.com/greengrass/v2/developerguide/create-deployments.html); include the Docker Application Manager and Token Exchange Service and, for CloudWatch Logs, the Log manager.

### 6. Verify

In the [AWS IoT console](https://console.aws.amazon.com/iot/home), choose **Greengrass devices** > **Core devices** and your core device: `com.amazon.samples.OpcUaDataSim` is listed as **Running**.

Within about a minute, the CloudWatch log group `/aws/greengrass/UserComponent/<region>/com.amazon.samples.OpcUaDataSim` shows `Server running`.

For detailed checks (CLI, container health and hardening, MQTT pause/resume), see [Verify a deployment](docs/OperationsGuide.md#verify-a-deployment).

## Test end to end with SiteWise Edge

The simulator works with SiteWise Edge in either of the [deployment topologies](#deployment-topologies).

### Same core

SiteWise Edge runs on the same core as the simulator. `setup_sitewise_edge.py` sets up an [MQTT-enabled, V3 gateway](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/mqtt-enabled-v3-gateway.html) with one command. You can also create the gateway in the console ([SiteWise Edge gateway documentation](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/create-gateway-ggv2.html); untested with this sample); if you do, check afterwards with `aws greengrassv2 list-deployments --target-arn <thing ARN>` that the latest deployment still contains `com.amazon.samples.OpcUaDataSim`, because a console-created gateway deployment can supersede the one that installed the simulator.

These steps continue from [Deploy the simulator](#deploy-the-simulator): the simulator is already running on the core, and SiteWise Edge is added alongside it.

1. Run the setup script from the repository root with the core's thing name:

   ```bash
   python3 setup_sitewise_edge.py <core-thing-name>
   ```

   The script reuses the SiteWise gateway already registered for the core, or creates `opcua-sim-gateway` for the core's OS and architecture. It then configures the OPC UA data source (the simulator at `opc.tcp://localhost:4840/simulator/`, no security, anonymous, streams prefixed `/opcua-sim`) and the real-time SiteWise destination. Next, it adds the EMQX broker (loopback port 1883 only), the OPC UA collector and the publisher to the core's thing deployment, keeping the simulator and every other component as they are. Finally, it waits for the deployment, waits until both gateway capabilities are `IN_SYNC`, and lists the data streams it finds. It runs safely more than once: SiteWise components already in the deployment keep their versions. The script stops with a message if the thing deployment does not contain the simulator yet.

2. Data streams appear within a few minutes as disassociated time series named `<prefix>/<device>/<tag>`, for example `/opcua-sim/Motor01/Temperature`. The script prints them. If none have arrived yet, check later with:

   ```bash
   aws iotsitewise list-time-series --time-series-type DISASSOCIATED --alias-prefix /opcua-sim
   aws iotsitewise get-asset-property-value --property-alias /opcua-sim/Motor01/Temperature
   ```

   In the AWS IoT SiteWise console, the streams are listed under **Data streams** and the gateway under **Edge gateways**. To see modeled data, create an asset model and asset, then bind the existing stream to the property with `aws iotsitewise associate-time-series-to-asset-property --alias /opcua-sim/Motor01/Temperature --asset-id <asset-id> --property-id <property-id>` (`update-asset-property --property-alias` fails with `ConflictingOperationException` once the disassociated stream exists).

### Different core

SiteWise Edge runs on a different core (or host) than the simulator. The simulator core is the one you deployed to in [Deploy the simulator](#deploy-the-simulator); it needs no SiteWise Edge components.

1. Publish the simulator's OPC UA port on the simulator host's private IP address. By default it is published on `127.0.0.1` only, which other hosts cannot reach. Redeploy with the host's address (`0.0.0.0` publishes on all interfaces; prefer the single private address):

   ```bash
   python3 deploy_component_version.py 1.0.0 <simulator-thing-name> --publish-address <simulator-host-private-ip>
   ```

   The address is stored in the deployment, so later deploys keep it; pass `--publish-address 127.0.0.1` to return to loopback only. The container reads the address when it starts: the script prints the restart command to run on the simulator core (`sudo /greengrass/v2/bin/greengrass-cli component restart --names com.amazon.samples.OpcUaDataSim`). If SiteWise Edge also runs there, restart its collector afterwards ([Troubleshooting](docs/OperationsGuide.md#troubleshooting)).
2. Allow inbound TCP 4840 to the simulator host **from the SiteWise Edge host only** (security group or host firewall), and make sure the two hosts can route to each other.
3. On the SiteWise Edge gateway, add an OPC UA data source pointing at the simulator host: `opc.tcp://<simulator-host-ip>:4840/simulator/`, no authentication (the same `iotsitewise:opcuacollector:3` configuration as in the same-core steps, with `endpointUri` changed).
4. Data streams appear in the SiteWise console within minutes.

> **Security note:** The OPC UA endpoint is unauthenticated. Only expose port 4840 to trusted hosts on a private network — never to the public internet. Use security groups, host firewalls, and private subnets to restrict access to the SiteWise Edge host (or other trusted OPC UA clients). This simulator generates synthetic data only and is intended for development and demonstration, not production data collection. See [Security](#security) for more.

### SiteWise permissions

The core running **SiteWise Edge** needs permission to write to AWS IoT SiteWise. The OPC UA collector publishes each value by **property alias** (`measurementDataStreamPrefix` + `/<device>/<tag>`, e.g. `/opcua-sim/Motor01/Temperature`), and AWS authorizes alias-addressed writes against the `time-series` resource, so `BatchPutAssetPropertyValue` is scoped by that alias prefix.

- **Same core:** nothing extra to do. The `SiteWiseGatewayConfigRead` and `SiteWiseIngestByAlias` statements are already in **build/device-role-policy.json**, which you applied in [step 4](#4-update-the-core-device-role).
- **Different core:** apply just those two statements to the SiteWise Edge core's role:

  ```bash
  jq '{Version, Statement: [.Statement[] | select(.Sid | startswith("SiteWise"))]}' \
    build/device-role-policy.json > build/sitewise-role-policy.json
  aws iam put-role-policy \
    --policy-name OpcUaSimulatorSiteWise \
    --policy-document file://build/sitewise-role-policy.json \
    --role-name SITEWISE_CORE_ROLE_NAME
  ```

If you change `measurementDataStreamPrefix`, pass the same value to `python3 publish_component.py --sitewise-alias-prefix <prefix>` so the generated policy matches.

If you bind streams to assets (step 2 of [Same core](#same-core)), add a further `iotsitewise:BatchPutAssetPropertyValue` statement on the asset ARNs, `arn:aws:iotsitewise:REGION:ACCOUNT:asset/ASSET_ID` (one per asset from `aws iotsitewise list-assets`; do not use `asset/*` outside a short-lived sandbox). The [SiteWise Edge self-hosted gateway requirements](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/configure-gateway-ggv2.html) page is authoritative for the full set of permissions the gateway role needs (it also lists the `iot:*`, `logs:*` and `s3:GetObject`/`GetBucketLocation` actions that the Greengrass installer's role usually already grants). The generated policy is a starting point: tighten it to the resources of your own deployment before use, and verify ingestion with `aws iotsitewise list-time-series` after deployment.

## Cost

This is a sample intended for development and demonstration. The sample code itself is provided at no cost, but deploying and running it uses AWS services that may incur charges. You are responsible for the cost of the AWS services used. Review the pricing pages below and use the [AWS Pricing Calculator](https://calculator.aws/) to estimate charges for your specific usage.

| AWS resource | When it applies | Cost driver | Pricing |
|---|---|---|---|
| [Amazon ECR](https://aws.amazon.com/ecr/pricing/) | Always (stores the simulator container image) | Image storage per GB-month and data transfer out | ECR pricing |
| [Amazon S3](https://aws.amazon.com/s3/pricing/) | Always (`publish_component.py` uploads component artifacts) | Object storage and requests (artifacts are small) | S3 pricing |
| [AWS IoT Greengrass](https://aws.amazon.com/greengrass/pricing/) | Always (core device management) | Per active Greengrass core device, per month | Greengrass pricing |
| [AWS IoT Core](https://aws.amazon.com/iot-core/pricing/) | Always when running on Greengrass (Nucleus connection plus the simulator's status/ack messages); none in local mode | Messages, connectivity minutes, and rules triggered | IoT Core pricing |
| [Amazon CloudWatch Logs](https://aws.amazon.com/cloudwatch/pricing/) | Always when deployed with `deploy_component_version.py` (the Log manager uploads the simulator's log) | Log ingestion per GB and storage per GB-month; the log group keeps 7 days of events | CloudWatch pricing |
| [AWS IoT SiteWise](https://aws.amazon.com/iot-sitewise/pricing/) | Only if you ingest the simulated data with SiteWise Edge | Data ingestion, storage, and processing | SiteWise pricing |
| [Amazon EC2](https://aws.amazon.com/ec2/pricing/) | If your Greengrass core runs on EC2 | Instance hours, EBS storage, data transfer | EC2 pricing |

Costs scale with configuration. A faster `updateIntervalSeconds`, more devices/tags, and SiteWise ingestion of every tag all increase message volume and ingestion charges. To avoid ongoing charges when you are done, follow the steps in [Cleanup](#cleanup).

## Security

- **Unauthenticated OPC UA endpoint, loopback by default.** The simulator serves OPC UA on TCP 4840 with no authentication or encryption. By default the port is published only on `127.0.0.1`, so it is reachable only from the core itself. `--publish-address` opens it to other hosts; do that only on a private network, restrict TCP 4840 to the specific OPC UA clients (security groups, host firewalls, private subnets), and never expose it to the public internet.
- **Synthetic data only.** The simulator generates fake sensor data. It is intended for development, testing, and demonstration — not for collecting or handling real production or sensitive data.
- **No device credentials in the container.** MQTT control goes through the Greengrass IPC MQTT proxy. The container only receives the per-component `SVCUID` token, whose permissions are limited to the two topics listed in the recipe's `accessControl`; no certificates or private keys are mounted. Certificate and key files (`*.pem`, `*.crt`, `*.key`) are also excluded from the repository via `.gitignore` — never commit credentials.
- **Hardened container.** The image runs as a non-root user (UID 1000), drops all Linux capabilities (`--cap-drop ALL`), sets `no-new-privileges`, and uses a read-only root filesystem with a tmpfs `/tmp`. It runs on Docker's bridge network with only TCP 4840 published, on `127.0.0.1` by default, and its lifecycle is owned by Greengrass (the recipe's `docker run` stays in the foreground, so a crash is visible to the Nucleus). `docker inspect` on a live Greengrass core (Amazon Linux 2023 on EC2, Nucleus 2.18.3, Docker 25.0.14, no Docker Compose) confirmed `ReadonlyRootfs=true`, `CapDrop=[ALL]`, `SecurityOpt=[no-new-privileges:true]`, `User=1000:1000`, a tmpfs `/tmp`, `AutoRemove=true`, `config.json` mounted read-only, and a healthy container with TCP 4840 published as `127.0.0.1:4840` (not reachable on the host's private IP until `--publish-address` was set), pulling from ECR with only the generated role policy. MQTT control and SiteWise Edge were not exercised in that run. Relax a flag only if your Docker runtime does not support it.
- **Least privilege.** Scope the Greengrass core device role to only the S3 bucket, ECR repository and CloudWatch Logs log group this component uses, and the SiteWise Edge gateway role to the alias prefix (and asset ARNs, if any) of your deployment. See [step 4](#4-update-the-core-device-role), [MQTT control](docs/OperationsGuide.md#mqtt-control), and [Test end to end with SiteWise Edge](#test-end-to-end-with-sitewise-edge) for example policies.
- **Reporting.** If you discover a potential security issue in this project, follow the process in [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) and do not create a public GitHub issue.

## Cleanup

Removing the component from a deployment does not remove the Docker image, the artifact caches, or the working directory from the core device. To stop incurring charges and remove everything this sample created:

1. Remove **com.amazon.samples.OpcUaDataSim** from the core device's deployment (console, AWS CLI, or revise the deployment without the component). Greengrass runs the recipe's `Shutdown` step, which stops the container. Also remove `aws.greengrass.LogManager` if no other component on the device needs it.
2. On the core device, remove what Greengrass does not clean up itself:
   - Run `docker rm -f opcua-data-sim` if the container is still present, then `docker rmi <image-uri>` to delete the image.
   - Delete the working directory `/greengrass/v2/work/com.amazon.samples.OpcUaDataSim`.
   - Delete the artifact caches `/greengrass/v2/packages/artifacts/com.amazon.samples.OpcUaDataSim/` and `/greengrass/v2/packages/artifacts-unarchived/com.amazon.samples.OpcUaDataSim/`.
3. Delete the ECR repository `opcua-data-sim` (and its images).
4. Empty and delete the S3 artifact bucket `opcua-data-sim-samples-REGION-ACCOUNT`.
5. Delete the CloudWatch log group (after step 1, so the Log manager does not recreate it): `aws logs delete-log-group --log-group-name /aws/greengrass/UserComponent/<region>/com.amazon.samples.OpcUaDataSim`.
6. Delete the component versions from the Greengrass cloud service if you no longer need them (`aws greengrassv2 delete-component`).
7. If you set up SiteWise Edge, run `python3 setup_sitewise_edge.py <core-thing-name> --delete`. This removes the EMQX, collector and publisher components from the thing deployment (the simulator stays) and deletes the core's gateway. Then delete any assets, asset models and time series: `disassociate-time-series-from-asset-property` → `delete-asset` → `delete-asset-model` → `aws iotsitewise delete-time-series --alias <alias>` for each stream (`/opcua-sim/Motor01/Temperature`, …).
8. If you set up SiteWise Edge, remove the EMQX Docker volume on the core once the EMQX container is gone: `docker volume rm emqx-data`.

## Next steps

The [Operations Guide](docs/OperationsGuide.md) covers the running simulator:

- Define your own devices, tags and data patterns, live through component configuration (`deploy_component_version.py --config`) or as the shipped default in `artifacts/config.json`: [Configuration](docs/OperationsGuide.md#configuration).
- Pause, resume and inject anomalies from AWS IoT Core: [MQTT control](docs/OperationsGuide.md#mqtt-control).
- Diagnose a component that fails to start or has no MQTT control: [Troubleshooting](docs/OperationsGuide.md#troubleshooting).
- Run the simulator locally, add patterns, types or control actions, and run the tests: [Development and extending](docs/OperationsGuide.md#development-and-extending).
- See what each file in this repository is for: [Repository contents](docs/OperationsGuide.md#repository-contents).

## Contributing and license

See [CONTRIBUTING](CONTRIBUTING.md) for how to report issues and submit changes, and [CHANGELOG](CHANGELOG.md) for release notes.

This sample is licensed under the MIT-0 License. See [LICENSE](LICENSE).

It relies on third-party packages that are provided under their own licenses and are retrieved from their upstream sources at build time (not redistributed in this repository). See [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES) for attributions, including a note on the LGPL-3.0-licensed `asyncua` dependency.
