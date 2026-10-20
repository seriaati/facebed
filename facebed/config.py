import os
from dataclasses import dataclass, field, fields
from typing import Any, Self, get_origin

import yaml


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    host: str = '0.0.0.0'
    port: int = 9812
    timezone: int = 7
    banned_users: list[str] = field(default_factory=list)
    notifier_webhook: str = ''
    proxy: str = ''
    user_agent: str = ''

    def __post_init__(self) -> None:
        if self.timezone < -12 or self.timezone > 14:
            raise ConfigError('invalid timezone offset')

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError('config file must contain a mapping of options')
        expected = {f.name: get_origin(f.type) or f.type for f in fields(cls)}
        for key, value in data.items():
            # exact type match on purpose: a bool is not accepted where an int is expected
            if key not in expected or type(value) is not expected[key]:
                raise ConfigError(f'invalid config entry {key}')
        return cls(**data)

    @classmethod
    def load(cls, path: str) -> Self:
        if not os.path.isfile(path):
            raise ConfigError(f'config file {path} not found or is not a file')
        if not os.access(path, os.R_OK):
            raise ConfigError(f'config file {path} not readable')
        with open(path, encoding='utf-8') as f:
            return cls.from_dict(yaml.safe_load(f))


_config = Config()


def get_config() -> Config:
    return _config


def set_config(config: Config) -> None:
    global _config  # noqa: PLW0603 -- replaced once at startup, read everywhere via get_config()
    _config = config
