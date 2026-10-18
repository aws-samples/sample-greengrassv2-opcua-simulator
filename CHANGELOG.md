# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-01

### Added

- Initial release of **com.amazon.samples.OpcUaDataSim**, an OPC UA Simulator for AWS IoT Greengrass V2:
  - Greengrass V2 component that runs an OPC UA server in a Docker container, with devices and tags defined in `artifacts/config.json` (no code changes needed) and five value patterns: `sine`, `steady`, `drift`, `duty_cycle`, `state_machine`; data types `double`, `boolean`, `string`, `int`; nested folders via `/` in device names; tag dependencies via `requiresTag`.
  - Live configuration through the `simulatorConfig` component configuration (default: `artifacts/config.json`, built into the recipe): `deploy_component_version.py --config <file>` (or a console configuration merge) replaces devices and tags without a new component version or container restart, `--reset-config` returns to the default, and the simulator publishes `config_applied` / `config_rejected` on the status topic; the bind-mounted file remains the fallback.
  - Runtime MQTT control from AWS IoT Core (pause, resume, inject anomaly, clear anomaly, clear all, with status/ack publishing) through the Greengrass IPC MQTT proxy; the recipe's `accessControl` grants exactly `aws.greengrass#SubscribeToIoTCore` on `simulator/+/control` and `aws.greengrass#PublishToIoTCore` on `simulator/+/status`, and no device certificate is present in the container.
  - Hardened container: non-root UID 1000, `--cap-drop ALL`, `no-new-privileges`, read-only root filesystem with a tmpfs `/tmp`, bridge network with only TCP 4840 published, on `127.0.0.1` by default (`--publish-address` for OPC UA clients on other hosts), `HEALTHCHECK`; multi-architecture image (`linux/amd64`, `linux/arm64`) built and pushed by `docker/build_image.sh`; the recipe starts the container with a plain foreground `docker run` (Docker is the only host requirement; no Docker Compose) so Greengrass owns the container lifecycle.
  - Hashed dependency locks (`docker/requirements.txt`, `requirements-dev.txt`) compiled with uv and installed with `--require-hashes`; base image pinned by digest.
  - Boto3 developer scripts: `publish_component.py` (builds the component, creates the artifact bucket with public access blocked and SSE-S3 encryption, uploads the archive, registers the component version and waits until it is DEPLOYABLE; refuses to overwrite an existing version) and `deploy_component_version.py` (revises the core device's thing deployment, or creates one if the device only has thing group deployments, which it never changes; adds the Docker Application Manager, Token Exchange Service and Log manager; `--config` / `--reset-config` for live configuration; prints how to restart the SiteWise Edge OPC UA collector when a new simulator version replaces the container on a core that runs it).
  - CloudWatch Logs upload of the simulator's component log through `aws.greengrass.LogManager` (added by `deploy_component_version.py`; simulator log only, every 60 seconds) to `/aws/greengrass/UserComponent/<region>/com.amazon.samples.OpcUaDataSim`, which `publish_component.py` creates with 7-day retention; the generated device role policy grants the four CloudWatch Logs actions on that log group only.
  - `setup_sitewise_edge.py`: one-command SiteWise Edge setup on the simulator's core (creates or reuses a V3 gateway, configures the OPC UA collector and publisher, adds EMQX, the collector and the publisher to the thing deployment, waits for `IN_SYNC` and lists the data streams); `--delete` undoes it. Instructions also cover a separate SiteWise Edge core, and the generated device role policy includes the scoped SiteWise statements.
  - Pytest unit test suite (simulator application, server integration, build and publish scripts) and a GitHub Actions CI workflow running pylint and pytest with SHA-pinned actions and least-privilege `contents: read` permissions; Dependabot configuration.

[Unreleased]: https://github.com/aws-samples/sample-greengrassv2-opcua-simulator/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/aws-samples/sample-greengrassv2-opcua-simulator/releases/tag/v1.0.0
