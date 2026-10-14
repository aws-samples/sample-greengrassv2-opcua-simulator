#!/bin/bash

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Builds the OPC UA simulator Docker image and pushes it to Amazon ECR.
#
# The simulator runs a custom application image, so it must be built and pushed
# to a registry that the Greengrass core device can pull from. By default this
# builds a single multi-architecture image manifest covering both linux/amd64
# (x86_64) and linux/arm64 (aarch64/Graviton), so the same image URI works on
# either device architecture.
#
# Prerequisites:
#   - Docker with buildx (a multi-arch builder; the script creates one if needed)
#   - AWS CLI configured with permissions for ECR
#
# Example execution (multi-arch, the default):
#   bash build_image.sh us-east-1 123456789012 opcua-data-sim latest
#
# Build for a single platform only (faster, e.g. for local testing):
#   bash build_image.sh us-east-1 123456789012 opcua-data-sim latest linux/arm64

set -euo pipefail

REGION=${1:-us-east-1}
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
docker buildx build --builder opcua-data-sim-builder --platform "${PLATFORM}" -t "${IMAGE}" --push .

echo "Pushed ${IMAGE}"
echo "Set this image URI in artifacts/docker-compose.yml"
