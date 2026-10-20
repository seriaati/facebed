import logging
import os
from dataclasses import dataclass
from datetime import timedelta, timezone, tzinfo
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(Exception):
    pass


# accepted YAML types per option; exact match on purpose, a bool is not accepted where an int is expected
_YAML_TYPES: dict[str, tuple[type, ...]] = {
    'host': (str,),
    'port': (int,),
    'threads': (int,),
    'timezone': (int, float, str),
    'banned_users': (list,),
    'notifier_webhook': (str,),
    'proxy': (str,),
    'user_agent': (str,),
    'cookies_file': (str,),
    'log_level': (str,),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    host: str = '0.0.0.0'
    port: int = 9812
    threads: int = 8  # concurrent requests; each mostly waits on Facebook, so more than CPU cores is fine
    # fixed UTC offset in hours (7, 5.5) or an IANA zone name ('Asia/Bangkok') to follow DST
    timezone: int | float | str = 7
    banned_users: frozenset[str] = frozenset()
    notifier_webhook: str = ''
    proxy: str = ''
    user_agent: str = ''
    cookies_file: str = 'cookies.json'
    log_level: str = 'INFO'

    def __post_init__(self) -> None:
        self.tzinfo()
        if self.threads < 1:
            raise ConfigError('invalid config entry threads, expected at least 1')
        if self.log_level.upper() not in logging.getLevelNamesMapping():
            raise ConfigError(f'invalid log level {self.log_level}')

    def tzinfo(self) -> tzinfo:
        if isinstance(self.timezone, str):
            try:
                return ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise ConfigError(f'invalid timezone {self.timezone}') from e
        if not -12 <= self.timezone <= 14:
            raise ConfigError('invalid timezone offset')
        return timezone(timedelta(hours=self.timezone))

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError('config file must contain a mapping of options')
        for key, value in data.items():
            if type(value) not in _YAML_TYPES.get(key, ()):
                raise ConfigError(f'invalid config entry {key}')
        banned = data.get('banned_users', [])
        if not all(type(user) in (str, int) for user in banned):
            raise ConfigError('invalid config entry banned_users, expected a list of user ids')
        # unquoted ids load from YAML as ints, while Facebook ids are compared as strings
        return cls(**{**data, 'banned_users': frozenset(map(str, banned))})

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
