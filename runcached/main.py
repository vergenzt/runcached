"""
Usage: runcached [options] [--] COMMAND...

Runs the given COMMAND with caching of stdout and stderr.

Options:

  -c, --cache-for
    Length of time for which to cache command results.
    Format: https://pypi.org/project/pytimeparse [default: 60s]

  -K, --generate-key
    If specified, first runs COMMAND with special environment variable $RUNCACHED_KEYGEN
    non-empty, with resulting stdout used as cache key for subsequent "live" command
    run.

  -I, --exclude-stdin
    Exclude stdin when computing cache key. Included by default if stdin is not a TTY.
    If stdin is included, full contents of stdin will be read before executing anything.

  -e, --include-env=VAR,...
    Include named environment variables when computing cache key.
    Separate with commas. Wildcards allowed. [default: *]
  -E, --exclude-env=VAR,...
    Exclude named environment variables () when computing cache key.
    Separate with commas. Wildcards allowed.

  -S, --no-shell
    By default, COMMAND is passed to the shell for execution. This option reverts that
    by runnning the first part of COMMAND as the executable and passing the remainder as
    arguments.

  -q, --quiet
    Suppress cache-hit notification on stderr.

"""

from fnmatch import fnmatchcase
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from sys import stdin
from typing import Any, Callable, List, Mapping, MutableMapping, Optional, TextIO, Tuple, Type, TypeVar, TypedDict, Union, cast
from xml.etree.ElementInclude import include

import appdirs
import diskcache
import docopt
from pytimeparse.timeparse import timeparse


CliArgs = TypedDict('CliArgs', {
  '--cache-for': str,
  '--generate-key': bool,
  '--exclude-stdin': bool,
  '--include-env': str,
  '--exclude-env': str,
  '--no-shell': bool,
  '--quiet': bool,
  'COMMAND': List[str],
})


def cli_args(argv: Optional[List[str]] = None) -> CliArgs:
  args = docopt.docopt(str(__doc__), argv)
  assert isinstance(args, CliArgs)
  return args


@dataclass(frozen=True)
class RunConfig:
  cmd: List[str]
  use_shell: bool = True
  env: Mapping[str,str] = {}
  stdin: str = ''
  cache_key: Optional[Any] = None


def cli():
  args = cli_args()
  cfg = RunConfig(args['COMMAND'])

  def _replace_cfg(**kw):
    nonlocal cfg
    cfg = replace(cfg, **kw)

  if args['--include-env'] or args['--exclude-env']:
    _replace_cfg(env = {
      env_var: env_var_value
      for env_var, env_var_value in os.environ
      if (
        included := any(( fnmatchcase(env_var, glob) for glob in args['--include-env'].split(',') ))
      )
      and not (
        excluded := any(( fnmatchcase(env_var, glob) for glob in args['--exclude-env'].split(',') ))
      )
    })

  cache_dir = appdirs.user_cache_dir(appname=__package__)
  cache = diskcache.Cache(cache_dir)



if __name__=='__main__':
  cli()
