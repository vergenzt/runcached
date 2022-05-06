"""
Usage: runcached [options] [--] COMMAND...

Runs the given COMMAND with caching of stdout and stderr.

Options:

  -c, --cache-for=DURATION
    Length of time for which to cache command results.
    Format: https://pypi.org/project/pytimeparse [default: 60s]

  -K, --custom-key
    Before computing cache key, pre-invoke COMMAND with special environment variable
    ${RUNCACHED_CUSTOM_KEY_GEN} non-empty. Resulting stdout is included in computation
    of cache key in addition to COMMAND/stdin/env vars according to other options.

  -i, --include-stdin
    Include stdin when computing cache key. Defaults to true if stdin is not a TTY. If
    stdin is included, stdin will be read until EOF before executing anything.
  -I, --exclude-stdin
    Exclude stdin when computing cache key. Overrides -i.

  -e, --include-env=VAR,...
    Include named environment variables when computing cache key. Separate with commas.
    Wildcards allowed. [default: *]
  -E, --exclude-env=VAR,...
    Exclude named environment variables when computing cache key. Separate with commas.
    Wildcards allowed. [default: ]

  -s, --shell
    Pass COMMAND to $SHELL for execution. [default: True]
  -S, --no-shell
    Do not pass COMMAND to $SHELL for execution. Overrides -s.

  -q, --quiet
    Suppress cache-hit notification on stderr.

"""

import os
import sys
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from subprocess import PIPE, run
from typing import List, Mapping, Optional, TypedDict, cast

import appdirs
import diskcache
import docopt
from pytimeparse.timeparse import timeparse
from strongtyping.strong_typing import MatchTypedDict


CliArgs = TypedDict('CliArgs', {
  '--cache-for': str,
  '--custom-key': bool,
  '--include-stdin': bool,
  '--exclude-stdin': bool,
  '--include-env': str,
  '--exclude-env': str,
  '--shell': bool,
  '--no-shell': bool,
  '--quiet': bool,
  '--': bool,
  'COMMAND': List[str],
})


RUNCACHED_CUSTOM_KEY_GEN = 'RUNCACHED_CUSTOM_KEY_GEN'


def cli_args(argv: Optional[List[str]] = None) -> CliArgs:
  doc = str(__doc__).format(RUNCACHED_CUSTOM_KEY_GEN=RUNCACHED_CUSTOM_KEY_GEN)
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

  def run(self) -> 'RunResult':
    result = run(args=self.cmd, env=self.env, stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True, check=True, shell=self.shell)
    return RunResult(self, result.stdout, result.stderr)

  def compute_custom_cache_key(self) -> 'RunConfig':
    with_env_marker = replace(self, env={ **self.env, RUNCACHED_CUSTOM_KEY_GEN: '1' })
    custom_cache_key = with_env_marker.run().stdout
    return replace(self, custom_cache_key=custom_cache_key)


@dataclass(frozen=True)
class RunResult:
  config: RunConfig
  stdout: Optional[str] = None
  stderr: Optional[str] = None


def cli():
  args = cli_args()

  cfg = RunConfig(
    cmd = args['COMMAND'],
    env = {
      env_var: env_var_value for env_var, env_var_value in os.environ.items()
      if (
        _included := any(( fnmatchcase(env_var, glob) for glob in args['--include-env'].split(',') ))
      )
      and not (
        _excluded := any(( fnmatchcase(env_var, glob) for glob in args['--exclude-env'].split(',') ))
      )
    },
    input = sys.stdin.read() if args['--include-stdin'] and not args['--exclude-stdin'] else None,
    shell = args['--shell'] and not args['--no-shell'],
  )

  if args['--custom-key']:
    cfg = cfg.compute_custom_cache_key()

  cache_dir = appdirs.user_cache_dir(appname=__package__)
  cache = diskcache.Cache(cache_dir)

  # TODO: caching


if __name__=='__main__':
  cli()
