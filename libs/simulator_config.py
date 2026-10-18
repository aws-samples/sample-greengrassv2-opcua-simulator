# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Basic checks for the simulator's device/tag configuration (artifacts/config.json,
or the simulatorConfig component configuration value), used by the developer
scripts before a config reaches a device. The simulator validates each config
fully (types, numbers, duplicate names) before it applies it.
"""

import json

# deploy_component_version.py warns (but continues) above this size: the config
# travels inside the deployment document and is stored on the device.
SIZE_WARNING_BYTES = 100 * 1024


def shape_error(cfg):
    """
    Returns why ``cfg`` does not have the required top-level shape, or None.
    The shape is an object with a "devices" list of objects that each have a
    "name" and a "tags" list.
    """
    if not isinstance(cfg, dict):
        return f'the config must be a JSON object, got {type(cfg).__name__}'
    devices = cfg.get('devices')
    if not isinstance(devices, list):
        return '"devices" must be a list'
    for i, device in enumerate(devices):
        if not isinstance(device, dict):
            return f'devices[{i}] must be an object'
        if not isinstance(device.get('name'), str) or not device['name']:
            return f'devices[{i}] needs a non-empty "name"'
        if not isinstance(device.get('tags'), list):
            return f'device "{device["name"]}" needs a "tags" list'
    return None


def counts(cfg):
    """ (number of devices, number of tags) of a config that passed shape_error """
    return len(cfg['devices']), sum(len(device['tags']) for device in cfg['devices'])


def load(path):
    """ Loads and shape-checks a simulator config file; exits with a readable message on error """
    try:
        with open(path, encoding='utf-8') as config_file:
            cfg = json.load(config_file)
    except FileNotFoundError:
        raise SystemExit(f'Simulator config {path} not found') from None
    except json.JSONDecodeError as e:
        raise SystemExit(f'Simulator config {path} is not valid JSON: {e}') from None
    error = shape_error(cfg)
    if error:
        raise SystemExit(f'Simulator config {path} is invalid: {error}')
    return cfg
