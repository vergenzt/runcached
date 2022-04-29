"""
Run shell commands with output caching.
"""

import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, List, MutableMapping, Tuple, Type, TypeVar, Union

import appdirs
import clize
import diskcache


T = TypeVar('T')
Cmd = Union[str, List[str]]
Out = subprocess.CompletedProcess


def disk_cache() -> MutableMapping[Any, Any]:
  return diskcache.Cache(appdirs.user_cache_dir(__package__)) # type: ignore


def run_cached(cmd: Cmd, key: Callable[[Cmd], T]=lambda x:x, cache: MutableMapping[T, Out]=disk_cache()) -> Out:
  return subprocess.run(cmd, capture_output=True)


def cli(*cmd: str, out=True, err=False, env=False, keep_failures=False):
  ...


if __name__=='__main__':
  clize.run(cli)
