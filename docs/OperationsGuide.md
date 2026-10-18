# Operations Guide: OPC UA Simulator for AWS IoT Greengrass V2

The [README](../README.md) covers deploying the **com.amazon.samples.OpcUaDataSim** component, testing it end to end with SiteWise Edge, and cleaning up. This guide covers configuring, controlling, troubleshooting and extending the running simulator.

## Table of Contents
* [Operate and customize](#operate-and-customize)
  * [Configuration](#configuration)
    * [Change the configuration](#change-the-configuration)
    * [Config fields](#config-fields)
    * [Device and tag fields](#device-and-tag-fields)
    * [Data types](#data-types)
    * [Tag patterns](#tag-patterns)
    * [Device naming](#device-naming)
  * [MQTT control](#mqtt-control)
* [Troubleshooting](#troubleshooting)
  * [Verify a deployment](#verify-a-deployment)
* [Development and extending](#development-and-extending)
  * [Run locally](#run-locally)
  * [Extending the simulator](#extending-the-simulator)
  * [Static analysis](#static-analysis)
  * [Unit tests](#unit-tests)
  * [Updating dependencies](#updating-dependencies)
* [Repository contents](#repository-contents)

## Operate and customize

### Configuration

The simulator config defines the OPC UA endpoint, update interval, and devices with their tags. Under Greengrass it is the component configuration value `simulatorConfig`; its default is the contents of **artifacts/config.json**, which `libs/build_component.py` puts into the recipe's `DefaultConfiguration`. At startup the simulator reads `simulatorConfig` over IPC; if it is missing or invalid, it logs a warning and uses the bind-mounted `artifacts/config.json` instead (locally, the file is the only source). No code change or image rebuild is needed for anything in this section.

#### Change the configuration

**Live, without a new component version.** Write a config file in the same format as `artifacts/config.json` and deploy it as component configuration:

```bash
python3 deploy_component_version.py <version> <thing-name> --config my-config.json
```

`<version>` is the component version already on the device (or a newer one). The script resets `/simulatorConfig` and merges `{"simulatorConfig": <your file>}`; reset runs before merge, so the result is a full replace and devices or keys missing from your file disappear. `--reset-config` instead returns `simulatorConfig` to the recipe default. Without either flag, the script keeps a `simulatorConfig` merge recorded in the thing's current deployment, so redeploying does not lose your config; with none recorded it resets `simulatorConfig` to the default of the version being deployed. The script warns when the config is over 100 KB.

In the console: **Greengrass devices** > **Deployments** > the deployment that targets your thing > **Revise** > keep the components > **Configure components** > select `com.amazon.samples.OpcUaDataSim` > **Configure component**. Put `/simulatorConfig` in **Reset paths** (for a full replace) and `{"simulatorConfig": { ...your config... }}` in **Configuration to merge**, then deploy. Merges replace lists rather than merging them, so without the reset path a merged `devices` list still replaces the whole default list, but other keys you leave out keep their current values. To return to the default, put `/simulatorConfig` in **Reset paths** with nothing to merge.

The running simulator applies the change within seconds, without a container restart: it adds, removes and changes OPC UA nodes for devices and tags, and applies `updateIntervalSeconds`, `timeAcceleration` and `writableTags`. The pause state is kept, and so are injected anomalies on tags that still exist, unless a tag's type changes and the anomaly value no longer fits the new type. A changed tag keeps its OPC UA node when its type is unchanged; a tag whose type changes gets a new node. `endpoint` and `controlTopic` cannot change live: the simulator logs a warning, keeps the current values until the component restarts, and lists the setting in the `restartRequired` field of the `config_applied` event. An invalid config is rejected with an `ERROR` log line giving the reason, and the current config keeps running. Each outcome is published on the status topic as a `config_applied` or `config_rejected` event (see [MQTT control](#mqtt-control)).

**Change the shipped default.** Edit `artifacts/config.json`, bump `version` in `component.json`, then publish ([3. Publish the component](../README.md#3-publish-the-component)) and deploy the new version. `deploy_component_version.py` resets `simulatorConfig` to the new default unless the deployment carries a custom `simulatorConfig` (pass `--reset-config` to drop it). Greengrass keeps a component's configuration across version changes, so when deploying from the console, add `/simulatorConfig` to **Reset paths** for the new default to take effect.

#### Config fields

| Field | Required | Description |
|---|---|---|
| `endpoint` | Required | OPC UA server endpoint (shipped value: `opc.tcp://0.0.0.0:4840/simulator/`) |
| `updateIntervalSeconds` | Required | How often values update (shipped value: `1.0`) |
| `timeAcceleration` | Required | Simulated time speed — 60 means 1 real minute = 1 simulated hour (shipped value: `60`) |
| `devices` | Required | Array of device definitions (see below) |
| `controlTopic` | Optional | MQTT topic to receive control commands on (default: `simulator/<thingName>/control`). The status topic is always `simulator/<thingName>/status`. |
| `writableTags` | Optional | Default `false`. When `true`, OPC UA clients may write tag values. |

The four required keys have no defaults. The simulator checks every config before it uses it (required keys present, `updateIntervalSeconds` and `timeAcceleration` positive numbers, unique device names and unique tag names per device, supported tag types): an invalid config file makes it exit at startup with `CONFIG: Invalid config file: <reason>`, and an invalid live update is rejected.

#### Device and tag fields

Each entry in `devices` has a `name` (required; see [Device naming](#device-naming)) and a `tags` array (default `[]`). Each tag accepts the keys below; defaults apply when a key is omitted.

| Tag field | Default | Used by |
|---|---|---|
| `name` | required | all |
| `type` | `"double"` | all (see [Data types](#data-types)) |
| `pattern` | `"steady"` | all (see [Tag patterns](#tag-patterns)); an unknown pattern is **not** rejected — the tag silently yields `0.0` |
| `base` | `50.0` | `sine`, `steady`, `drift` |
| `amplitude` | `10.0` | `sine` |
| `noise` | `0.5` | `sine`, `steady`, `drift` |
| `phaseOffset` | `0.0` | `sine` |
| `onProbability` | `0.5` | `duty_cycle` |
| `driftRate` | `0.01` | `drift` |
| `min` | `0.0` | `drift` |
| `max` | `100.0` | `drift` |
| `states` | `["ON", "OFF"]` | `state_machine` |
| `transitionProbability` | `0.003` | `state_machine` |
| `requiresTag` | none | all; names a **boolean** tag **of the same device** — while it is false, double/int tags read 0 and boolean tags read false |

#### Data types

| OPC UA Type | Config value | Common use |
|---|---|---|
| Double | `"double"` | Sensor readings (temp, pressure, flow) |
| Boolean | `"boolean"` | Equipment state (running, fault) |
| String | `"string"` | Status codes, mode names |
| Int64 | `"int"` | Counters, batch numbers |

These four values are the only ones accepted (`type` defaults to `"double"` when omitted); any other value makes the config invalid with `Tag <name>: unsupported type '<value>'`.

#### Tag patterns

| Pattern | Parameters | Description |
|---|---|---|
| `sine` | base, amplitude, noise, phaseOffset | Daily sine cycle (one cycle per 24 simulated hours) |
| `steady` | base, noise | Constant with Gaussian noise |
| `drift` | base, driftRate, noise, min, max | Random walk within bounds |
| `duty_cycle` | onProbability | Boolean on/off; `onProbability` sets the initial state and biases occasional re-samples — the state toggles randomly about every 200 updates |
| `state_machine` | states, transitionProbability | String state cycling |

Tags can reference other tags via `"requiresTag"` — e.g., a fan's RPM outputs zero when its Running tag is false. The referenced tag must be a `boolean` tag of the same device.

#### Device naming

Device names support `/` paths to create nested OPC UA folders:

```
"name": "Line1/Motor01"
-> Root -> Line1 -> Motor01 -> [tags]
```

### MQTT control

The component subscribes to the control topic and publishes status through the Greengrass Nucleus using the [IPC MQTT proxy](https://docs.aws.amazon.com/greengrass/v2/developerguide/ipc-iot-core-mqtt.html). It authenticates to the Nucleus with the per-component `SVCUID` token that the Nucleus hands to the recipe's lifecycle script, so no device certificate is used by the container.

| Topic | Direction | Purpose |
|---|---|---|
| `simulator/{thingName}/control` | IoT Core -> Simulator | Receive commands |
| `simulator/{thingName}/status` | Simulator -> IoT Core | Status and command acks |

| Command | Payload | Effect |
|---|---|---|
| Pause | `{"action": "pause"}` | Freezes all OPC UA values |
| Resume | `{"action": "resume"}` | Resumes normal simulation |
| Inject anomaly | `{"action": "inject_anomaly", "device": "Motor01", "tag": "Temperature", "value": 200.0}` | Overrides a tag with a fixed value |
| Clear anomaly | `{"action": "clear_anomaly", "device": "Motor01", "tag": "Temperature"}` | Removes override |
| Clear all | `{"action": "clear_all"}` | Clears all overrides |

Publishing a command:

```
aws iot-data publish \
  --topic "simulator/<thing-name>/control" \
  --payload '{"action":"pause"}' \
  --cli-binary-format raw-in-base64-out \
  --region <region>
```

Messages on the status topic have three shapes. Once, after the subscription succeeds, the simulator announces itself:

```json
{"status": "online", "thingName": "<thing-name>", "controlTopic": "simulator/<thing-name>/control"}
```

For every JSON object received on the control topic it publishes an ack — including for unknown actions and rejected values:

```json
{"ack": "<action>", "thingName": "<thing-name>"}
```

After each `simulatorConfig` change from a deployment ([Change the configuration](#change-the-configuration)) it publishes a config event. `devices` and `tags` count the configuration now running: the new one after `config_applied`, the retained one after `config_rejected`, which also carries the reason:

```json
{"event": "config_applied", "thingName": "<thing-name>", "devices": 1, "tags": 4}
{"event": "config_applied", "thingName": "<thing-name>", "devices": 1, "tags": 4, "restartRequired": ["endpoint"]}
{"event": "config_rejected", "thingName": "<thing-name>", "devices": 1, "tags": 4, "reason": "duplicate device name 'Motor01'"}
```

An ack confirms receipt, not success — check the component log for `CONTROL:` warnings. To watch the status topic, subscribe to `simulator/+/status` (or `simulator/<thing-name>/status`) in the AWS IoT console MQTT test client; alternatively tail `/greengrass/v2/logs/com.amazon.samples.OpcUaDataSim.log` on the core for `Published ack` lines.

The recipe's `accessControl` grants the component exactly two operations, enforced by the Nucleus: `aws.greengrass#SubscribeToIoTCore` on `simulator/+/control` and `aws.greengrass#PublishToIoTCore` on `simulator/+/status`. Reading and watching its own configuration ([`GetConfiguration` and `SubscribeToConfigurationUpdate`](https://docs.aws.amazon.com/greengrass/v2/developerguide/ipc-component-configuration.html)) needs no grant. The core device's [AWS IoT policy](https://docs.aws.amazon.com/iot/latest/developerguide/iot-policies.html) must also allow the core to publish, subscribe, and receive on those topics; the policy created by Greengrass automatic provisioning does. To limit the grant to a single core, change the resources to `simulator/{iot:thingName}/control` and `simulator/{iot:thingName}/status` and enable the Nucleus [`interpolateComponentConfiguration`](https://docs.aws.amazon.com/greengrass/v2/developerguide/greengrass-nucleus-component.html#greengrass-nucleus-component-configuration) option.

## Troubleshooting

Component log on the core device:

```
/greengrass/v2/logs/com.amazon.samples.OpcUaDataSim.log
```

The Log manager also uploads it to CloudWatch Logs every 60 seconds, in log group `/aws/greengrass/UserComponent/<region>/com.amazon.samples.OpcUaDataSim`.

Container logs:

```
docker logs opcua-data-sim
```

Common issues:

- **Wrong image architecture**: an `exec format error` indicates the image architecture does not match the device. This only happens if you built for a single platform; rebuild with the default multi-arch build (omit the last argument of `build_image.sh`) or pass the device's platform as the fifth argument, then publish a new version.
- **SiteWise stops receiving values after a new simulator version**: deploying a new component version replaces the simulator container, which resets the collector's OPC UA connection. The SiteWise Edge OPC UA collector reconnects and keeps rediscovering the nodes, but can keep a subscription the new server rejects (its log shows `Bad_SubscriptionIdInvalid`, and its `IncomingValuesCount` metric stays at 0). Restart the collector on the core:

  ```bash
  sudo /greengrass/v2/bin/greengrass-cli component restart --names aws.iot.SiteWiseEdgeCollectorOpcua
  ```

  Values resume within about a minute. `deploy_component_version.py` prints this hint when a deployment restarts the simulator on a core that runs the collector. Live configuration changes (`--config`, `--reset-config`) keep the server running, so they do not cause this: the collector picks up added and removed tags on its own within about 30 seconds.
- **Image pull failure**: confirm the device role has ECR pull permissions and that the Token Exchange Service is in the deployment.
- **No logs in CloudWatch**: check that `aws.greengrass.LogManager` is in the deployment, that the token exchange role has the `CloudWatchLogsUpload` statement from `build/device-role-policy.json` ([README step 4](../README.md#4-update-the-core-device-role)), and that the device clock is within 15 minutes of real time (CloudWatch Logs rejects requests signed with a skewed clock).
- **MQTT control disabled**: control is only available when the simulator runs as a Greengrass component; the OPC UA server still runs normally without it. Check the component log for `CONTROL:` messages:
  - `CONTROL: Greengrass IPC not available` means the `SVCUID` or the IPC socket path was not forwarded into the container — check that the recipe's `Run` step passes both to `docker run` (`-e`) and that its socket bind mount (`-v`) points at the path the Nucleus reports.
  - `CONTROL: Could not connect to Greengrass IPC — MQTT control disabled` follows five `CONTROL: IPC connect attempt N/5 failed: ...` warnings, 5 s apart. Unlike the subscribe path below, the connect is **not** retried afterwards; fix the cause (IPC socket path, Nucleus not running) and restart the component (redeploy, or `sudo systemctl restart greengrass`).
  - `CONTROL: IPC subscribe attempt N to '<topic>' failed (UnauthorizedError: ...)` means the topic does not match the recipe's `accessControl` resources (for example a custom `controlTopic` outside `simulator/+/control`). The simulator keeps retrying the subscription with backoff, so control recovers on its own once a deployment corrects the configuration.
  - `CONTROL: IPC subscription stream closed — reconnecting` means the Nucleus closed an established subscription (for example an IPC connection drop). The simulator opens a new IPC connection and re-subscribes with the same backoff; no restart is needed.
  - `CONTROL: Invalid value for <device>/<tag>` means an `inject_anomaly` value did not match the tag's data type or did not fit its OPC UA variant (`double` must be a finite number, `int` must be within the Int64 range). The message is ignored and the simulation keeps running.
- **Configuration change not applied**: check the component log for `CONFIG:` messages. `CONFIG: Rejected configuration update (<reason>)` means the deployed `simulatorConfig` is invalid; the previous config keeps running. `CONFIG: Component configuration not usable (<reason>); using the config file instead` at startup means the simulator fell back to the bind-mounted `artifacts/config.json`. `CONFIG: endpoint changes need a component restart` means the rest of the change was applied but the endpoint was not; restart the component (for example `sudo /greengrass/v2/bin/greengrass-cli component restart --names com.amazon.samples.OpcUaDataSim` if the Greengrass CLI is installed, or `sudo systemctl restart greengrass`).

### Verify a deployment

Detailed checks after [README step 5](../README.md#5-deploy). What success looks like with the shipped `config.json` (expected values; the Log manager, CloudWatch Logs and MQTT pause/resume checks have not yet been verified in a live run):

1. `aws greengrassv2 list-installed-components --core-device-thing-name <thing-name>` lists `com.amazon.samples.OpcUaDataSim` version `1.0.0` with lifecycle state `RUNNING`, and `aws.greengrass.LogManager`.
2. The component log contains `Created 1 devices with 4 tags`, `Server running. Press Ctrl+C to stop.`, and `CONTROL: Connected to Greengrass IPC, subscribed to 'simulator/<thing-name>/control'`.
   - On the core device: `sudo tail -f /greengrass/v2/logs/com.amazon.samples.OpcUaDataSim.log`.
   - In CloudWatch Logs (uploaded every 60 seconds): log group `/aws/greengrass/UserComponent/<region>/com.amazon.samples.OpcUaDataSim`, log stream `/<yyyy>/<mm>/<dd>/thing/<thing-name>`. For example: `aws logs filter-log-events --log-group-name /aws/greengrass/UserComponent/<region>/com.amazon.samples.OpcUaDataSim --filter-pattern '"Server running"' --region <region>`.
3. `docker ps` on the core device shows the container `opcua-data-sim` as `Up (healthy)` with `127.0.0.1:4840->4840/tcp` (or the address set with `--publish-address`).
4. The container hardening is in place: `docker inspect opcua-data-sim --format '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.Config.User}}'` prints `true [ALL] [no-new-privileges:true] 1000:1000`.
5. MQTT pause/resume: publish `{"action":"pause"}` to `simulator/<thing-name>/control` as shown in [MQTT control](#mqtt-control). The component log shows `CONTROL: Simulation paused` and `CONTROL: Published ack 'pause' to simulator/<thing-name>/status`, and the periodic sample lines switch to `[sim ..h [PAUSED]] Motor01: ...` with frozen values. Publish `{"action":"resume"}` to continue. To see the ack itself, subscribe to `simulator/+/status` in the AWS IoT console MQTT test client before publishing.

## Development and extending

### Run locally

The simulator can be run locally without Greengrass for development. Install the dependencies from the repository root, then start the simulator:

```
pip3 install -r requirements.txt
cd docker
python3 simulator.py ../artifacts/config.json
```

MQTT control and live configuration updates are disabled in local mode (there is no Greengrass Nucleus to provide IPC); the config file is the only config source. The OPC UA server runs normally on port 4840.

### Extending the simulator

Everything in [Configuration](#configuration) is config-only. The following need a change to `docker/simulator.py` (plus a unit test under `tests/`), a new image, and a new component version:

- **New data pattern**: add an `elif self.pattern == "<name>"` branch in `TagState.generate` and cache its parameters (with defaults) in `TagState.__init__`.
- **New data type**: add it to `UA_TYPE_MAP` and `INITIAL_VALUES`, and teach `_validate_override_value` and `coerce_value` how to check and convert it.
- **New control action**: add an `action == "<name>"` branch in `handle_control_message` (acks are published automatically).
- **New runtime dependency**: add it to `docker/requirements.in`, re-lock as described in [Updating dependencies](#updating-dependencies); the Dockerfile installs with `--require-hashes`, so an unlocked dependency fails the image build.

Then release the change: run `bash docker/build_image.sh` (it updates `image` in `component.json`) → bump `version` in `component.json` → `python3 publish_component.py` → `python3 deploy_component_version.py <version> <thing-name>` ([5. Deploy](../README.md#5-deploy)). Always bump the version: re-publishing the **same** version leaves the previously extracted `artifacts-unarchived` directory in place on devices that already have it (Greengrass treats a version's artifacts as immutable), so the container keeps mounting the stale `config.json` and the old image keeps running.

### Static analysis

```
pylint libs docker tests *.py
```

### Unit tests

Unit tests for the simulator application live under `tests/`. Install the dependencies from `requirements.txt` and run:

```
pytest --cov=docker --cov=libs
```

The same commands run automatically in CI (see `.github/workflows/ci.yml`).

### Updating dependencies

`docker/requirements.txt` (runtime, installed in the image) and `requirements-dev.txt` (lint and test tools) are lock files with hashes, compiled with [uv](https://github.com/astral-sh/uv) from `docker/requirements.in` and `requirements-dev.in`. Edit the `.in` files, then regenerate the locks:

```
uv pip compile --universal --generate-hashes --python-version 3.10 docker/requirements.in -o docker/requirements.txt
uv pip compile --universal --generate-hashes --python-version 3.10 requirements-dev.in -o requirements-dev.txt
```

The Dockerfile installs with `pip install --require-hashes`, so a hand-edited or unhashed `docker/requirements.txt` fails the image build.

## Repository contents

| Item                          | Description                                                                                           |
| ----------------------------- | ----------------------------------------------------------------------------------------------------- |
| /.github                      | GitHub Actions CI workflow (lint and tests) and Dependabot configuration.                            |
| /artifacts                    | Greengrass V2 component artifacts that run on the Greengrass edge runtime (config.json). |
| /docker                       | Docker build context for the simulator image (Dockerfile, simulator.py, build helper, runtime dependency input `requirements.in` and its hashed lock `requirements.txt`). |
| /docs                         | This guide (`OperationsGuide.md`) and the architecture diagram (`architecture.drawio`, rendered as `architecture.png`). |
| /libs                         | Python modules used by the developer-machine scripts: `build_component.py` (renders `recipe.yaml` and zips `artifacts/` into `build/`; run by `publish_component.py`, or on its own with `python3 -m libs.build_component`), `component_config.py`, `simulator_config.py`. |
| /tests                        | Pytest unit tests for the simulator application and the build/publish scripts.                       |
| component.json                | Component name, version, author, artifact bucket prefix, region and simulator image URI used by the build, publish and deploy scripts. |
| publish_component.py          | Packages the component, uploads the artifacts to S3 and registers the component version in the Greengrass cloud service (boto3). |
| deploy_component_version.py   | Deploys a component version to a Greengrass core device, optionally with a simulator config (`--config`) or a non-loopback OPC UA publish address (`--publish-address`). |
| setup_sitewise_edge.py        | Sets up (or with `--delete`, removes) a SiteWise Edge gateway, its OPC UA source and publisher, and its components on the simulator's core. |
| recipe.yaml                   | Greengrass V2 component recipe template.                                                              |
| requirements.txt              | Developer-machine install file: pulls in the two hashed locks (see [Updating dependencies](#updating-dependencies)). |
| requirements-dev.in / .txt    | Developer tooling (pylint, pytest, coverage) input and its hashed lock, constrained to the runtime lock. |
| .pylintrc / .coveragerc / .gitignore | Lint and coverage settings used by CI, and the ignore list for local build output.             |
| README.md                     | Getting started: deploy, test with SiteWise Edge, clean up. This guide covers configure, control, troubleshoot and extend. |
| CHANGELOG.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md, LICENSE, NOTICE, THIRD-PARTY-LICENSES | Project metadata: release notes ([CHANGELOG](../CHANGELOG.md)), contribution guidelines, code of conduct, license, and attributions. |
