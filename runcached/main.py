"""
"""

import logging
import os
import sys
from dataclasses import dataclass, field, replace, fields
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from subprocess import DEVNULL, PIPE, run
from typing import List, Mapping, Optional, TypedDict, cast
from functools import partial

from argparse import ArgumentParser, BooleanOptionalAction

import appdirs
import diskcache
import docopt
from pytimeparse.timeparse import timeparse
from strongtyping.strong_typing import MatchTypedDict



RUNCACHE_KEY_ENV_VAR = 'RUNCACHE_KEY'


@dataclass
class CliArgs:
  '''
  Usage: runcached [options] [--] COMMAND...

  Runs the given COMMAND with caching of stdout and stderr.
  '''

  COMMAND: List[str] = field(metadata=dict(
    help='The command to run',
    nargs='+',
    action='append',
  ))

  ttl: timedelta = field(
    metadata=dict(
      option_strings=['-t', '--ttl'],
      type=lambda ttl: timedelta(seconds=timeparse(ttl)),
      metavar='DURATION',
      help='Max length of time for which to cache command results. Format: https://pypi.org/project/pytimeparse',
      default='60s',
    )
  )

  custom_key: bool = field(
    metadata=dict(
      option_strings=['-k', '--custom-key'],
      help=f'''
        Before computing cache key, pre-invoke COMMAND with special environment variable
        ${RUNCACHE_KEY_ENV_VAR} non-empty. Resulting stdout is included in computation
        of cache key in addition to COMMAND/stdin/env vars according to other options.
      ''',
      default=False,
      action=BooleanOptionalAction,
    )
  )

  keep_failures: bool = field(
    metadata=dict(
      option_strings=['-f', '--keep-failures'],
      help='Cache run results that exit non-zero.',
      default=False,
    )
  )

  include_stdin: bool = field(
    metadata=dict(
      option_strings=['-i', '--include-stdin'],
      help='''
        Include stdin when computing cache key. Defaults to true if stdin is not a TTY. If
        stdin is included, stdin will be read until EOF before executing anything.
      ''',
      action=BooleanOptionalAction,
    ),
    default_factory=lambda: not sys.stdin.isatty(),
  )

  include_env: List[str] = field(
    metadata=dict(
      option_strings=['-e', '--include-env'],
      help='Include named environment variables when computing cache key. Separate with commas. Wildcards allowed.',
      type=partial(str.split, sep=',')
    )
  )

  exclude_env: List[str] = field(
    metadata=dict(
      option_strings=['-E', '--exclude-env'],
      help='Excluded named environment variables when computing cache key. Separate with commas. Wildcards allowed.',
      type=partial(str.split, sep=',')
    )
  )

  shell: bool = field(
    metadata=dict(
      option_strings=['-s', '--shell'],
      help='Passes COMMAND to $SHELL for execution.',
      type=BooleanOptionalAction,
    )
  )

  quiet: bool = field(
    metadata=dict(
      option_strings=['-q', '--quiet'],
      help='Supress log messages from runcached.',
      default=False,
    ),
  )

  verbose: bool = field(
    metadata=dict(
      option_strings=['-v', '--verbose'],
      help='Show extra log messages from runcached.',
      default=False,
    )
  )

  @classmethod
  def parse(cls: Type[CliArgs], prog: str = sys.argv[0], argv: List[str] = sys.argv[1:]) -> CliArgs:
    parser = ArgumentParser(prog=prog, usage=cls.__doc__)
    for field in fields(cls):
      parser.add_argument(dest=field.name, type=field.type, **field.metadata)
    return cls(**parser.parse_known_args(argv))


@dataclass(frozen=True)
class RunConfig:
  cmd: List[str]
  env: Mapping[str,str] = field(default_factory=dict)
  input: Optional[str] = field(default=None, repr=False)
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
    if args['--custom-key'] and self.custom_cache_key is None:
      return self._compute_custom_cache_key().run_with_caching(cache, args)
    else:
      logging.debug(self)

      ttl = timedelta(seconds=timeparse(args['--ttl']))
      min_started_at = datetime.now() - ttl

      result: RunResult
      cached_result = cast(RunResult, cache.get(self))

      if cached_result and cached_result.started_at >= min_started_at:
        result = cached_result
        logging.info(f'Using cached result for {self} from {result.started_at}.')
      else:
        if cached_result:
          logging.info(f'Last cached {self} at {cached_result.started_at}, which is more than {args["--ttl"]} old. Recomputing...')
        else:
          logging.info(f'No cached result found for {self}. Computing...')

        result = self._run_without_caching()

        if result.return_code == 0 or args['--keep-failures']:
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
  args = cli_args(argv)
  logging.basicConfig(
    format='[runcached] %(message)s',
    level=logging.DEBUG if args['--verbose'] else logging.WARNING if args['--quiet'] else logging.INFO,
  )
  logging.debug(args)
  
  is_shell = not args['--no-shell']
  cfg = RunConfig(
    shell = is_shell,
    cmd = [' '.join(args['COMMAND'])] if is_shell else args['COMMAND'],
    env = {
      env_var: env_var_value for env_var, env_var_value in os.environ.items()
      if (
        _included := any(( fnmatchcase(env_var, glob) for glob in (args['--include-env'] or '').split(',') ))
      )
      and not (
        _excluded := any(( fnmatchcase(env_var, glob) for glob in (args['--exclude-env'] or '').split(',') ))
      )
    },
    input = sys.stdin.read() if (not sys.stdin.isatty() or args['--include-stdin']) and not args['--exclude-stdin'] else None,
  )

  cache_dir = appdirs.user_cache_dir(appname=__package__)
  cache = diskcache.Cache(cache_dir)

  result = cfg.run_with_caching(cache, args)
  return result.write()


if __name__=='__main__':
  sys.exit(cli())
