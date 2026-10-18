# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Amazon Linux 2023 minimal is used as the base image because it is actively
# maintained and patched by AWS and carries far fewer OS-package CVEs than the
# general-purpose Debian-based python:*-slim images.
#
# The base image is pinned by the manifest-list digest of the 2023-minimal tag
# so builds are reproducible and the same FROM line serves linux/amd64 and
# linux/arm64. To refresh the pin to the current tag, run:
#   docker buildx imagetools inspect public.ecr.aws/amazonlinux/amazonlinux:2023-minimal
# and copy the top-level "Digest:" value below.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:5c546c873970e79e5c9152cd9c8a9c7013824352bc89071d21feb15ad68496aa

# Install Python 3.11 and pip. Clean the package cache to keep the image small.
RUN microdnf install -y python3.11 \
    && microdnf clean all \
    && rm -rf /var/cache/dnf

WORKDIR /app

# Bootstrap pip via ensurepip (single location) and upgrade it to the latest
# release, so no older, vulnerable pip metadata lingers in the image.
# requirements.txt is a hashed lock file generated from requirements.in, so
# --require-hashes rejects any artifact that does not match the recorded hashes.
COPY requirements.txt .
RUN python3.11 -m ensurepip --upgrade \
    && python3.11 -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python3.11 -m pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy the simulator application
COPY simulator.py .

# Run as a numeric non-root user. The Greengrass Nucleus makes the IPC socket
# world read/write, so the container UID does not need to match ggc_user.
USER 1000:1000

# OPC UA server port
EXPOSE 4840

# Health check: verify the OPC UA server is accepting TCP connections on 4840.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3.11 -c "import socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(0 if s.connect_ex(('127.0.0.1',4840))==0 else 1)"

ENTRYPOINT ["python3.11", "-u", "simulator.py"]
