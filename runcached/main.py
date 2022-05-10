"""
Usage: runcached [-q | -v] [options] [--] COMMAND...

Runs the given COMMAND with caching of stdout and stderr.

Options:

  -t, --ttl=DURATION
    Max length of time for which to cache command results.
    Format: https://pypi.org/project/pytimeparse [default: 60s]

  -k, --custom-key
    Before computing cache key, pre-invoke COMMAND with special environment variable
    ${RUNCACHE_KEY} non-empty. Resulting stdout is included in computation
    of cache key in addition to COMMAND/stdin/env vars according to other options.

  -F, --keep-failures
    Cache run results that exit non-zero. Does not cache these results by default.

  -i, --include-stdin
    Include stdin when computing cache key. Defaults to true if stdin is not a TTY. If
    stdin is included, stdin will be read until EOF before executing anything.
  -I, --exclude-stdin
    Exclude stdin when computing cache key. Overrides -i.

  -e, --include-env=VAR,...
    Include named environment variables when computing cache key. Separate with commas.
    Wildcards allowed. [default: ]
  -E, --exclude-env=VAR,...
    Exclude named environment variables when computing cache key. Separate with commas.
    Wildcards allowed. [default: ]

  -S, --no-shell
    Do not pass COMMAND to $SHELL for execution. Overrides -s.

  -q, --quiet
    Set log level to warnings only.
  -v, --verbose
    Set log level to debug.

"""

import logging
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from subprocess import DEVNULL, PIPE, run
from typing import List, Mapping, Optional, TypedDict, cast

import appdirs
import diskcache
import docopt
from pytimeparse.timeparse import timeparse
from strongtyping.strong_typing import MatchTypedDict


CliArgs = TypedDict('CliArgs', {
  '--ttl': str,
  '--custom-key': bool,
  '--keep-failures': bool,
  '--include-stdin': bool,
  '--exclude-stdin': bool,
  '--include-env': str,
  '--exclude-env': str,
  '--no-shell': bool,
  '--quiet': bool,
  '--verbose': bool,
  '--': bool,
  'COMMAND': List[str],
})


RUNCACHE_KEY = 'RUNCACHE_KEY'


def cli_args(argv: Optional[List[str]] = None) -> CliArgs:
  doc = str(__doc__).format(RUNCACHE_KEY=RUNCACHE_KEY)
  args = docopt.docopt(doc, argv)
  validated_args = MatchTypedDict(CliArgs)(args) # raises error if TypedDict is inconsistent with value
  return cast(CliArgs, validated_args)


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
    if args['--custom-key'] and self.custom_cache_key is None:
      return self._compute_custom_cache_key().run_with_caching(cache, args)
    else:
      logging.debug(self)

      ttl = timedelta(seconds=timeparse(args['--ttl']))
      min_started_at = datetime.now() - ttl

      if (result := cast(RunResult, cache.get(self))) and result.started_at >= min_started_at:
        logging.info(f'Using cached result for {self} from {result.started_at}.')
      elif result := self._run_without_caching():
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
  
  cfg = RunConfig(
    shell = (is_shell := not args['--no-shell']),
    cmd = [' '.join(args['COMMAND'])] if is_shell else args['COMMAND'],
    env = {
      env_var: env_var_value for env_var, env_var_value in os.environ.items()
      if (
        _included := any(( fnmatchcase(env_var, glob) for glob in args['--include-env'].split(',') ))
      )
      and not (
        _excluded := any(( fnmatchcase(env_var, glob) for glob in args['--exclude-env'].split(',') ))
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
