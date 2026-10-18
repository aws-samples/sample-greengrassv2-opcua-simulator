# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Builds the Greengrass component locally: renders recipe.yaml with the values from
component.json (including the simulator image URI in "image") and the contents of
artifacts/config.json (as the default simulatorConfig component configuration),
and zips the artifacts/ directory. Output goes to build/ (gitignored).

publish_component.py runs this automatically, so the normal flow is a single
command: python3 publish_component.py. Run this script on its own to inspect the
rendered recipe and archive under build/ without AWS access.

Before building, the user should:
1) Build and push the simulator Docker image with docker/build_image.sh, which
   also sets "image" (the pushed image URI) and "region" in component.json.
2) Edit artifacts/config.json with the desired devices and tags.
3) Set the desired version in component.json.

Example execution (from the repository root):
python3 -m libs.build_component
"""

import argparse
import json
import os
import shutil
import sys
from libs import simulator_config as simulator_config_lib
from libs.component_config import ComponentConfig

DIRECTORY_ARTIFACTS = 'artifacts/'
FILE_SIMULATOR_CONFIG = 'artifacts/config.json'
PLACEHOLDER_SIMULATOR_CONFIG = '$SIMULATOR_CONFIG'
DIRECTORY_BUILD = 'build/'
DIRECTORY_BUILD_ARTIFACTS = 'build/artifacts/'
FILE_RECIPE_TEMPLATE = 'recipe.yaml'
FILE_RECIPE = 'build/recipes/recipe.yaml'
FILE_ZIP_BASE = 'opcua-data-sim'
FILE_ZIP_EXT = 'zip'
PLACEHOLDER_IMAGE_MARKERS = ('<ACCOUNT_ID>', '<REGION>')


def read_image_uri(config):
    """ Reads the simulator image URI from component.json, failing fast on a missing or placeholder value """
    image = config.image()
    if not image or any(marker in image for marker in PLACEHOLDER_IMAGE_MARKERS):
        sys.exit(f'{config.path} has no simulator image URI yet ("image" is missing or still the placeholder). '
                 'Run bash docker/build_image.sh <region> first; it pushes the image and sets "image" for you '
                 '(the build and publish_component.py refuse to run until then).')
    return image


def render_recipe(config, image_uri):
    """
    Fills the component name, version, author and Docker image into the recipe
    template, and artifacts/config.json into DefaultConfiguration.simulatorConfig.
    The s3://BUCKET_NAME/... artifact URI is left for publish_component.py,
    which knows the real bucket name once the account id is resolved.
    """
    with open(FILE_RECIPE_TEMPLATE, encoding="utf-8") as recipe_template_file:
        recipe_str = recipe_template_file.read()

    recipe_str = recipe_str.replace('COMPONENT_NAME', config.name())
    recipe_str = recipe_str.replace('COMPONENT_VERSION', config.version())
    recipe_str = recipe_str.replace('COMPONENT_AUTHOR', config.author())
    recipe_str = recipe_str.replace('$DOCKER_IMAGE', image_uri)
    # Last, so text inside the config is never mistaken for a placeholder. JSON is
    # valid YAML flow syntax, so the value lands in the recipe as an object.
    simulator_config = simulator_config_lib.load(FILE_SIMULATOR_CONFIG)
    recipe_str = recipe_str.replace(PLACEHOLDER_SIMULATOR_CONFIG, json.dumps(simulator_config))
    return recipe_str


def create_recipe(config):
    """ Creates the component recipe, filling in the component details and Docker image """
    print(f'Creating recipe {FILE_RECIPE}')
    recipe_str = render_recipe(config, read_image_uri(config))
    os.makedirs(os.path.dirname(FILE_RECIPE), exist_ok=True)
    with open(FILE_RECIPE, 'w', encoding="utf-8") as recipe_file:
        recipe_file.write(recipe_str)
    print('Created recipe')
    return FILE_RECIPE


def create_artifacts(config):
    """ Creates the artifacts archive as a ZIP file and returns its path """
    base_name = os.path.join(DIRECTORY_BUILD_ARTIFACTS, config.name(), config.version(), FILE_ZIP_BASE)
    zip_path = f'{base_name}.{FILE_ZIP_EXT}'
    print(f'Creating artifacts archive {zip_path}')
    shutil.make_archive(base_name, FILE_ZIP_EXT, DIRECTORY_ARTIFACTS)
    print('Created artifacts archive')
    return zip_path


def build(config):
    """ Builds the component; returns (recipe path, artifacts zip path) """
    return create_recipe(config), create_artifacts(config)


def main():
    """ Builds the component described by component.json """
    parser = argparse.ArgumentParser(
        description='Render recipe.yaml and zip artifacts/ into build/ (publish_component.py runs this for you)')
    parser.add_argument('--config', default='component.json',
                        help='Path to the component configuration file (default: component.json)')
    args = parser.parse_args()
    build(ComponentConfig(args.config))


if __name__ == '__main__':
    main()
