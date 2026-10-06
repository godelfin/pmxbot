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


def construct_environment(loader: EnvironmentLoader, node: yaml.Node) -> str:
    if not isinstance(node, yaml.ScalarNode):
        node_type = getattr(node, 'id', type(node).__name__)
        raise yaml.constructor.ConstructorError(
            None,
            None,
            f"expected a scalar node, but found {node_type}",
            node.start_mark,
        )
    return os.environ.get(loader.construct_scalar(node), "")


EnvironmentLoader.add_constructor("!env", construct_environment)
