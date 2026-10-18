# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
API for component.json, the configuration file shared by libs/build_component.py,
publish_component.py and deploy_component_version.py.
"""

import json
import re

REQUIRED_KEYS = ('componentName', 'version', 'author', 'publishBucketPrefix', 'region')
VERSION_PATTERN = re.compile(r'^\d+\.\d+\.\d+$')


class ComponentConfig():
    """ API for the component configuration file """

    def __init__(self, path='component.json'):
        """ Loads the configuration file and validates that every required key is present """
        self.path = path
        try:
            with open(path, encoding="utf-8") as config_file:
                self.json = json.load(config_file)
        except FileNotFoundError:
            raise SystemExit(f'{path} not found. Run this script from the repository root.') from None
        except json.JSONDecodeError as e:
            raise SystemExit(f'{path} is not valid JSON: {e}') from None

        missing = [key for key in REQUIRED_KEYS if key not in self.json]
        if missing:
            raise SystemExit(f'{path} is missing required key(s): {", ".join(missing)}')

        if not VERSION_PATTERN.match(str(self.json['version'])):
            raise SystemExit(f'{path}: "version" must be MAJOR.MINOR.PATCH (for example 1.0.0), '
                             f'got "{self.json["version"]}"')

    def name(self):
        """ Gets the component name """
        return self.json['componentName']

    def version(self):
        """ Gets the component version """
        return self.json['version']

    def author(self):
        """ Gets the component author (recipe ComponentPublisher) """
        return self.json['author']

    def region(self):
        """ Gets the AWS region the component is published to and deployed in """
        return self.json['region']

    def image(self):
        """
        Gets the simulator image URI set by docker/build_image.sh, or None if absent.
        Not a required key: only build_component.py needs it, and it validates the value.
        """
        return self.json.get('image')

    def bucket_prefix(self):
        """ Gets the prefix of the S3 bucket that holds the component artifacts """
        return self.json['publishBucketPrefix']

    def bucket_name(self, account):
        """ Gets the full artifact bucket name: <prefix>-<region>-<account> """
        return f'{self.bucket_prefix()}-{self.region()}-{account}'

    def log_group_name(self):
        """ Gets the CloudWatch Logs group the Greengrass Log manager uploads this component's log to """
        return f'/aws/greengrass/UserComponent/{self.region()}/{self.name()}'
