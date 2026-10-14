# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-01

### Added

- Initial release of **com.amazon.samples.OpcUaDataSim**, an OPC UA Simulator for AWS IoT Greengrass V2:
  - Greengrass V2 component that runs an OPC UA server in a Docker container, with devices and tags defined in `artifacts/config.json` (no code changes needed) and five value patterns: `sine`, `steady`, `drift`, `duty_cycle`, `state_machine`; data types `double`, `boolean`, `string`, `int`; nested folders via `/` in device names; tag dependencies via `requiresTag`.
  - Runtime MQTT control from AWS IoT Core (pause, resume, inject anomaly, clear anomaly, clear all, with status/ack publishing) through the Greengrass IPC MQTT proxy; the recipe's `accessControl` grants exactly `aws.greengrass#SubscribeToIoTCore` on `simulator/+/control` and `aws.greengrass#PublishToIoTCore` on `simulator/+/status`, and no device certificate is present in the container.
  - Hardened container: non-root UID 1000, `cap_drop: ALL`, `no-new-privileges`, read-only root filesystem with a tmpfs `/tmp`, bridge network with only TCP 4840 published, `HEALTHCHECK`; multi-architecture image (`linux/amd64`, `linux/arm64`) built and pushed by `docker/build_image.sh`; compose runs in the foreground so Greengrass owns the container lifecycle.
  - Hashed dependency locks (`docker/requirements.txt`, `requirements-dev.txt`) compiled with uv and installed with `--require-hashes`; base image pinned by digest.
  - Boto3 developer scripts: `publish_component.py` (builds the component, creates the artifact bucket with public access blocked and SSE-S3 encryption, uploads the archive, registers the component version and waits until it is DEPLOYABLE; refuses to overwrite an existing version) and `deploy_component_version.py` (revises the core device's thing deployment, adding the Docker Application Manager and Token Exchange Service dependencies).
  - SiteWise Edge instructions for the same-core and separate-core topologies, with scoped example IAM policies for the core device role and the gateway role.
  - Pytest unit test suite (simulator application, server integration, build and publish scripts) and a GitHub Actions CI workflow running pylint and pytest with SHA-pinned actions and least-privilege `contents: read` permissions; Dependabot configuration.

[Unreleased]: https://github.com/aws-samples/sample-greengrassv2-opcua-simulator/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/aws-samples/sample-greengrassv2-opcua-simulator/releases/tag/v1.0.0
