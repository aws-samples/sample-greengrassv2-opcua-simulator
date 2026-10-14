# Operations Guide: OPC UA Simulator for AWS IoT Greengrass V2

The [README](README.md) covers deploying the **com.amazon.samples.OpcUaDataSim** component, testing it end to end with SiteWise Edge, and cleaning up. This guide covers configuring, controlling, troubleshooting and extending the running simulator.

## Table of Contents
* [Operate and customize](#operate-and-customize)
  * [Configuration](#configuration)
    * [Config fields](#config-fields)
    * [Device and tag fields](#device-and-tag-fields)
    * [Data types](#data-types)
    * [Tag patterns](#tag-patterns)
    * [Device naming](#device-naming)
  * [MQTT control](#mqtt-control)
* [Troubleshooting](#troubleshooting)
* [Development and extending](#development-and-extending)
  * [Run locally](#run-locally)
  * [Extending the simulator](#extending-the-simulator)
  * [Static analysis](#static-analysis)
  * [Unit tests](#unit-tests)
  * [Updating dependencies](#updating-dependencies)
* [Repository contents](#repository-contents)

## Operate and customize

### Configuration

The simulator reads **artifacts/config.json**, which defines the OPC UA endpoint, update interval, and devices with their tags. Edit it to define your own devices and tags, then rebuild and publish the component ([3. Publish the component](README.md#3-publish-the-component)) and deploy the new version. No code change or image rebuild is needed for anything in this section.

#### Config fields

| Field | Required | Description |
|---|---|---|
| `endpoint` | Required | OPC UA server endpoint (shipped value: `opc.tcp://0.0.0.0:4840/simulator/`) |
| `updateIntervalSeconds` | Required | How often values update (shipped value: `1.0`) |
| `timeAcceleration` | Required | Simulated time speed — 60 means 1 real minute = 1 simulated hour (shipped value: `60`) |
| `devices` | Required | Array of device definitions (see below) |
| `controlTopic` | Optional | MQTT topic to receive control commands on (default: `simulator/<thingName>/control`). The status topic is always `simulator/<thingName>/status`. |
| `writableTags` | Optional | Default `false`. When `true`, OPC UA clients may write tag values. |

The four required keys have no defaults: the simulator exits with a `KeyError` at startup if one is missing.

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

These four values are the only ones accepted (`type` defaults to `"double"` when omitted); the simulator exits at startup with `Tag <name>: unsupported type '<value>'` for anything else.

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

Messages on the status topic have two shapes. Once, after the subscription succeeds, the simulator announces itself:

```json
{"status": "online", "thingName": "<thing-name>", "controlTopic": "simulator/<thing-name>/control"}
```

For every JSON object received on the control topic it publishes an ack — including for unknown actions and rejected values:

```json
{"ack": "<action>", "thingName": "<thing-name>"}
```

An ack confirms receipt, not success — check the component log for `CONTROL:` warnings. To watch the status topic, subscribe to `simulator/+/status` (or `simulator/<thing-name>/status`) in the AWS IoT console MQTT test client; alternatively tail `/greengrass/v2/logs/com.amazon.samples.OpcUaDataSim.log` on the core for `Published ack` lines.

The recipe's `accessControl` grants the component exactly two operations, enforced by the Nucleus: `aws.greengrass#SubscribeToIoTCore` on `simulator/+/control` and `aws.greengrass#PublishToIoTCore` on `simulator/+/status`. The core device's [AWS IoT policy](https://docs.aws.amazon.com/iot/latest/developerguide/iot-policies.html) must also allow the core to publish, subscribe, and receive on those topics; the policy created by Greengrass automatic provisioning does. To limit the grant to a single core, change the resources to `simulator/{iot:thingName}/control` and `simulator/{iot:thingName}/status` and enable the Nucleus [`interpolateComponentConfiguration`](https://docs.aws.amazon.com/greengrass/v2/developerguide/greengrass-nucleus-component.html#greengrass-nucleus-component-configuration) option.

## Troubleshooting

Component log on the core device:

```
/greengrass/v2/logs/com.amazon.samples.OpcUaDataSim.log
```

Container logs:

```
docker logs opcua-data-sim
```

Common issues:

- **Wrong image architecture**: an `exec format error` indicates the image architecture does not match the device. This only happens if you built for a single platform; rebuild with the default multi-arch build (omit the last argument of `build_image.sh`) or pass the device's platform as the fifth argument, then publish a new version.
- **Image pull failure**: confirm the device role has ECR pull permissions and that the Token Exchange Service is in the deployment.
- **MQTT control disabled**: control is only available when the simulator runs as a Greengrass component; the OPC UA server still runs normally without it. Check the component log for `CONTROL:` messages:
  - `CONTROL: Greengrass IPC not available` means the `SVCUID` or the IPC socket path was not forwarded into the container — check that the recipe's `Run` step exports both and that the socket bind mount in `docker-compose.yml` points at the path the Nucleus reports.
  - `CONTROL: Could not connect to Greengrass IPC — MQTT control disabled` follows five `CONTROL: IPC connect attempt N/5 failed: ...` warnings, 5 s apart. Unlike the subscribe path below, the connect is **not** retried afterwards; fix the cause (IPC socket path, Nucleus not running) and restart the component (redeploy, or `sudo systemctl restart greengrass`).
  - `CONTROL: IPC subscribe attempt N to '<topic>' failed (UnauthorizedError: ...)` means the topic does not match the recipe's `accessControl` resources (for example a custom `controlTopic` outside `simulator/+/control`). The simulator keeps retrying the subscription with backoff, so control recovers on its own once a deployment corrects the configuration.
  - `CONTROL: IPC subscription stream closed — reconnecting` means the Nucleus closed an established subscription (for example an IPC connection drop). The simulator opens a new IPC connection and re-subscribes with the same backoff; no restart is needed.
  - `CONTROL: Invalid value for <device>/<tag>` means an `inject_anomaly` value did not match the tag's data type or did not fit its OPC UA variant (`double` must be a finite number, `int` must be within the Int64 range). The message is ignored and the simulation keeps running.

## Development and extending

### Run locally

The simulator can be run locally without Greengrass for development. Install the dependencies from the repository root, then start the simulator:

```
pip3 install -r requirements.txt
cd docker
python3 simulator.py ../artifacts/config.json
```

MQTT control is disabled in local mode (there is no Greengrass Nucleus to provide IPC). The OPC UA server runs normally on port 4840.

### Extending the simulator

Everything in [Configuration](#configuration) is config-only. The following need a change to `docker/simulator.py` (plus a unit test under `tests/`), a new image, and a new component version:

- **New data pattern**: add an `elif self.pattern == "<name>"` branch in `TagState.generate` and cache its parameters (with defaults) in `TagState.__init__`.
- **New data type**: add it to `UA_TYPE_MAP` and `INITIAL_VALUES`, and teach `_validate_override_value` and `coerce_value` how to check and convert it.
- **New control action**: add an `action == "<name>"` branch in `handle_control_message` (acks are published automatically).
- **New runtime dependency**: add it to `docker/requirements.in`, re-lock as described in [Updating dependencies](#updating-dependencies); the Dockerfile installs with `--require-hashes`, so an unlocked dependency fails the image build.

Then release the change: run `docker/build_image.sh` → set the new image URI in `artifacts/docker-compose.yml` → bump `version` in `component.json` → `python3 publish_component.py` → `python3 deploy_component_version.py <version> <thing-name>` ([4. Deploy](README.md#4-deploy)). Always bump the version: re-publishing the **same** version leaves the previously extracted `artifacts-unarchived` directory in place on devices that already have it (Greengrass treats a version's artifacts as immutable), so the Install step copies stale files and the old image keeps running.

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
| /artifacts                    | Greengrass V2 component artifacts that run on the Greengrass edge runtime (docker-compose.yml, config.json). |
| /docker                       | Docker build context for the simulator image (Dockerfile, simulator.py, build helper, runtime dependency input `requirements.in` and its hashed lock `requirements.txt`). |
| /libs                         | Python library shared by the developer-machine scripts (`component_config.py`).                      |
| /tests                        | Pytest unit tests for the simulator application and the build/publish scripts.                       |
| build_component.py            | Renders `recipe.yaml` and zips `artifacts/` into `build/`; run automatically by `publish_component.py`. |
| component.json                | Component name, version, author, artifact bucket prefix and region used by the publish and deploy scripts. |
| publish_component.py          | Packages the component, uploads the artifacts to S3 and registers the component version in the Greengrass cloud service (boto3). |
| deploy_component_version.py   | Deploys a component version to a Greengrass core device.                                             |
| recipe.yaml                   | Greengrass V2 component recipe template.                                                              |
| requirements.txt              | Developer-machine install file: pulls in the two hashed locks (see [Updating dependencies](#updating-dependencies)). |
| requirements-dev.in / .txt    | Developer tooling (pylint, pytest, coverage) input and its hashed lock, constrained to the runtime lock. |
| .pylintrc / .coveragerc / .gitignore | Lint and coverage settings used by CI, and the ignore list for local build output.             |
| README.md / OperationsGuide.md | Getting started (deploy, test, clean up) and this guide (configure, control, troubleshoot, extend).                                      |
| CHANGELOG.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md, LICENSE, NOTICE, THIRD-PARTY-LICENSES | Project metadata: release notes ([CHANGELOG](CHANGELOG.md)), contribution guidelines, code of conduct, license, and attributions. |
