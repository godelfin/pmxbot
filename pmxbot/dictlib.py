import os

import yaml
from jaraco.collections import ItemsAsAttributes


class ConfigDict(ItemsAsAttributes, dict):
    @classmethod
    def from_yaml(cls, filename):
        with open(filename, encoding="utf-8") as f:
            return cls(yaml.load(f, Loader=EnvironmentLoader))

    def to_yaml(self, filename):
        with open(filename, "w", encoding="utf-8") as f:
            yaml.safe_dump(dict(self), f)


class EnvironmentLoader(yaml.SafeLoader):
    """Safe YAML with explicit environment-variable references."""


EnvironmentLoader.add_constructor(
    "!env", lambda loader, node: os.environ.get(loader.construct_scalar(node), "")
)
