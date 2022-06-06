"""
Usage: runcached [-q | -v] [options] [--] COMMAND...

"""

from collections import namedtuple
from functools import partial
import inspect
import logging
import operator
import os
import sys
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from subprocess import DEVNULL, PIPE, run
from textwrap import dedent
from tkinter import W
from typing import Any, Callable, ClassVar, Dict, List, Literal, Mapping, Optional, ParamSpec, Tuple, TypeAlias, TypedDict, cast

import appdirs
import diskcache
from pytimeparse import parse as pytimeparse

from configargparse import ArgumentParser


add_arg_fn = lambda *a, **k1: lambda parser, **k2: ArgumentParser.add_argument(parser, *a, **k1, **k2)


@dataclass(frozen=True)
class CliArgs:
  """
  Runs the given COMMAND with caching of stdout and stderr.
  """
  ADD_ARGS: ClassVar = object()

  @classmethod
  def _get_parser(cls) -> ArgumentParser:
    parser = ArgumentParser(description=cls.__doc__)
    for field in fields(cls):
      for add_arg_fn in field.metadata.get(cls.ADD_ARGS, []):
        add_arg_fn(parser, dest=field.name)

    return parser

  @classmethod
  def from_args(cls) -> 'CliArgs':
    parser = cls._get_parser()
    parsed = parser.parse_args()
    return cls(**vars(parsed))

  command: List[str] = field(metadata={
    ADD_ARGS: [add_arg_fn(
      metavar='CMD',
      type=str,
      nargs='+',
      help='The command to run. Passed to $SHELL if --shell is true, otherwise run as an executable.',
    )],
  })

  ttl: timedelta = field(metadata={
    ADD_ARGS: [add_arg_fn(
      '-t', '--ttl',
      metavar='DURATION',
      type=lambda s: timedelta(seconds=pytimeparse(s)),
      default='60s',
      help='Max length of time for which to cache command results. Format: https://pypi.org/project/pytimeparse',
    )],
  })

  custom_key: bool = field(metadata={
    ADD_ARGS: [add_arg_fn(
      '-k', '--custom-key',
      action='store_true',
      help=dedent('''
        Before computing cache key, pre-invoke COMMAND with special environment variable
        ${RUNCACHE_KEY} non-empty. Resulting stdout is included in computation
        of cache key in addition to COMMAND/stdin/env vars according to other options.
      '''),
    )],
  })

  keep_failures: bool = field(metadata={
    ADD_ARGS: [add_arg_fn(
      '-F', '--keep-failures',
      action='store_true',
      help='Cache run results that exit non-zero.',
    )],
  })

  stdin: bool = field(metadata={
    ADD_ARGS: [
      add_arg_fn(
        '-i', '--include-stdin',
        action='store_true',
        default=lambda: not sys.stdin.isatty(),
        help='Include stdin when computing cache key. If true, stdin will be read until EOF before executing anything.',
      ),
      add_arg_fn(
        '-I', '--exclude-stdin',
        action='store_false',
        help='Exclude stdin when computing cache key. Overrides -i.',
      )
    ],
  })

  class EnvFilterer(argparse.Action):
    filter: Callable[[str, str], bool]

    def __init__(self, filter, **kwargs):
      self.filter = filter
      super().__init__(
        metavar='VAR,...',
        type=lambda s: s.split(sep=','),
        **kwargs
      )

    def __call__(self, _parser, namespace, values, _option_string):
      curr_env: Mapping[str,str] = getattr(namespace, self.dest, os.environ)
      next_env: Mapping[str,str] = {
        var: val
        for var, val in curr_env.items()
        if values and any((
          self.filter(var, glob)
          for arg_value in (values if isinstance(values, list) else [values])
          for glob in str(arg_value).split(',')
        ))
      }
      setattr(namespace, self.dest, next_env)


  env: Mapping[str,str] = field(metadata={
    ADD_ARGS: [
      add_arg_fn(
        '-e', '--include-env',
        action=EnvFilterer,
        filter=fnmatchcase,
        help='Include named environment variables when computing cache key. Separate with commas. Wildcards allowed.',
      ),
      add_arg_fn(
        '-E', '--exclude-env',
        action=EnvFilterer,
        filter=lambda var, glob: not fnmatchcase(var, glob),
        help='Exclude named environment variables when computing cache key. Separate with commas. Wildcards allowed.',
      ),
    ],
  })

  shell: bool = field(metadata={
    ADD_ARGS: [
      add_arg_fn(
        '-s', '--shell',
        action='store_true',
        help='Pass COMMAND to $SHELL for execution.',
        default=True,
      ),
      add_arg_fn(
        '-S', '--no-shell',
        action='store_false',
        help='Do not pass COMMAND to $SHELL for execution. Overrides -s.',
      )
    ],
  })

  verbosity: int = field(metadata={
    ADD_ARGS: [
      add_arg_fn(
        '-q', '--quiet',
        action='store_const',
        const=logging.WARNING,
        help='Set log level to warnings only.',
      ),
      add_arg_fn(
        '-v', '--verbose',
        action='store_const',
        const=logging.DEBUG,
        help='Set log level to debug.',
      ),
    ],
  })


RUNCACHE_KEY = 'RUNCACHE_KEY'


@dataclass(frozen=True)
class RunConfig:
  cmd: List[str]
  env: Mapping[str,str] = field(default_factory=dict)
  input: Optional[str] = None
  shell: bool = True
  custom_cache_key: Optional[str] = None

  def _run_without_caching(self) -> 'RunResult':
    started_at = datetime.now()
    result = run(
      args=' '.join(self.cmd) if self.shell else self.cmd,
      shell=self.shell,
      executable=os.environ.get('SHELL') if self.shell else None,
      env=self.env,
      input=self.input,
      text=True,
      capture_output=True,
    )
    return RunResult(started_at, result.returncode, result.stdout, result.stderr)

  def _compute_custom_cache_key(self) -> 'RunConfig':
    with_env_marker = replace(self, env={ **self.env, RUNCACHE_KEY: '1' })
    logging.debug('Generating custom cache key using %s', with_env_marker)
    custom_cache_key = with_env_marker._run_without_caching().stdout
    return replace(self, custom_cache_key=custom_cache_key)

  def run_with_caching(self, cache: diskcache.Cache, args: CliArgs) -> 'RunResult':
    if args.custom_key and self.custom_cache_key is None:
      return self._compute_custom_cache_key().run_with_caching(cache, args)
    else:
      logging.debug(self)

      min_started_at = datetime.now() - args.ttl

      if (result := cast(RunResult, cache.get(self))) and result.started_at >= min_started_at:
        logging.info(f'Using cached result for {self} from {result.started_at}.')
      elif result := self._run_without_caching():
        if result.return_code == 0 or args.keep_failures:
          cache.set(self, result)
        else:
          logging.warn(f'Command returned {result.return_code} and --keep-failures not specified; refusing to cache.')

      return result


@dataclass(frozen=True)
class RunResult:
  started_at: datetime
  return_code: int
  stdout: Optional[str] = None
  stderr: Optional[str] = None

  def write(self) -> int:
    sys.stdout.write(self.stdout) if self.stdout else None
    sys.stderr.write(self.stderr) if self.stderr else None
    return self.return_code


def cli(argv = sys.argv[1:]) -> int:
  args = CliArgs.from_args()
  logging.basicConfig(format='[runcached] %(message)s', level=args.verbosity)
  logging.debug(args)
  
  cfg = RunConfig(
    shell = args.shell,
    cmd = args.command,
    env = args.env,
    input = sys.stdin.read() if args.stdin else None,
  )

  cache_dir = appdirs.user_cache_dir(appname=__package__)
  cache = diskcache.Cache(cache_dir)

  result = cfg.run_with_caching(cache, args)
  return result.write()


if __name__=='__main__':
  sys.exit(cli())
