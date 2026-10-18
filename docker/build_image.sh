#!/bin/bash

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Builds the OPC UA simulator Docker image, pushes it to Amazon ECR, and points
# the component at it.
#
# The simulator runs a custom application image, so it must be built and pushed
# to a registry that the Greengrass core device can pull from. By default this
# builds a single multi-architecture image manifest covering both linux/amd64
# (x86_64) and linux/arm64 (aarch64/Graviton), so the same image URI works on
# either device architecture.
#
# After a successful push the script updates component.json so the rest of the
# flow needs no hand edits:
#   - "region" is set to REGION
#   - "image"  is set to the pushed image URI (libs/build_component.py writes it into the recipe)
#
# Usage (any directory; arguments are positional and all optional):
#   bash docker/build_image.sh [REGION] [ACCOUNT] [REPOSITORY] [TAG] [PLATFORM]
#
#   REGION      default: "region" in component.json
#   ACCOUNT     default: account of the current AWS credentials
#   REPOSITORY  default: opcua-data-sim
#   TAG         default: latest
#   PLATFORM    default: linux/amd64,linux/arm64 (pass one, e.g. linux/arm64, for a faster build)
#
# Prerequisites:
#   - Docker with buildx (a multi-arch builder; the script creates one if needed)
#   - AWS CLI configured with permissions for ECR
#   - python3 (used to update component.json)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPONENT_JSON="${REPO_ROOT}/component.json"

REGION=${1:-$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["region"])' "${COMPONENT_JSON}")}
ACCOUNT=${2:-$(aws sts get-caller-identity --query Account --output text)}
REPOSITORY=${3:-opcua-data-sim}
TAG=${4:-latest}
PLATFORM=${5:-linux/amd64,linux/arm64}

REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${REPOSITORY}:${TAG}"

echo "Building and pushing ${IMAGE} for ${PLATFORM}"

# Authenticate Docker to ECR
aws ecr get-login-password --region "${REGION}" | \
  docker login --username AWS --password-stdin "${REGISTRY}"

# Create the repository if it does not already exist
aws ecr describe-repositories --repository-names "${REPOSITORY}" --region "${REGION}" >/dev/null 2>&1 || \
  aws ecr create-repository \
    --repository-name "${REPOSITORY}" \
    --region "${REGION}" \
    --image-scanning-configuration scanOnPush=true

# Ensure a buildx builder capable of multi-platform builds exists
if ! docker buildx inspect opcua-data-sim-builder >/dev/null 2>&1; then
  echo "Creating multi-arch buildx builder"
  docker buildx create --name opcua-data-sim-builder --driver docker-container --bootstrap
fi

# Build for the target platform(s) and push a single (multi-arch) manifest.
# --builder targets the named builder without changing the user's default.
docker buildx build --builder opcua-data-sim-builder --platform "${PLATFORM}" \
  -t "${IMAGE}" --push "${SCRIPT_DIR}"

echo "Pushed ${IMAGE}"

# Point the component at the pushed image and region. Other keys keep their
# values and order.
python3 - "${COMPONENT_JSON}" "${REGION}" "${IMAGE}" <<'EOF'
import json
import sys

component_json, region, image = sys.argv[1:4]

with open(component_json, encoding='utf-8') as f:
    config = json.load(f)
config['region'] = region
config['image'] = image
with open(component_json, 'w', encoding='utf-8') as f:
    json.dump(config, f, indent=2)
    f.write('\n')

print(f'Set "region": "{region}" and "image": "{image}" in {component_json}')
EOF
