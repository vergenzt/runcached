import logging
import os
import shlex
import sys
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase
from subprocess import run
from typing import List, Mapping, Optional, cast

import appdirs
import diskcache

from .args import CliArgs


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


@dataclass(frozen=True)
class RunConfig:
  command: List[str]
  env: Mapping[str,str] = field(default_factory=dict)
  input: Optional[str] = None
  shell: bool = True
  custom_cache_key: Optional[str] = None

  def _run_without_caching(self) -> 'RunResult':
    started_at = datetime.now()
    result = run(
      args=' '.join(self.command) if self.shell else self.command,
      shell=self.shell,
      executable=os.environ.get('SHELL') if self.shell else None,
      env=self.env,
      input=self.input,
      text=True,
      capture_output=True,
    )
    return RunResult(started_at, result.returncode, result.stdout, result.stderr)

  def run_with_caching(self, cache: diskcache.Cache, args: CliArgs) -> 'RunResult':
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


def cli(argv = sys.argv[1:]) -> int:
  args, parser = CliArgs.parse(argv)
  if args.COMMAND[0] == '--':
    args.COMMAND = args.COMMAND[1:]

  logging.basicConfig(format='[runcached:%(levelname)s] %(message)s', level=args.verbosity)
  logging.debug(args)
  
  cfg = RunConfig(
    command = args.COMMAND,
    env = {
      env_var: env_var_value for env_var, env_var_value in os.environ.items()
      if (
        _included := any(( fnmatchcase(env_var, glob) for glob in args.include_env or [] ))
      )
      and not (
        _excluded := any(( fnmatchcase(env_var, glob) for glob in args.exclude_env or [] ))
      )
    },
    shell = args.shell,
    input = sys.stdin.read() if args.stdin else None,
  )

  cache_dir = appdirs.user_cache_dir(appname=__package__)
  cache = diskcache.Cache(cache_dir)

  if cfg.command:
    result = cfg.run_with_caching(cache, args)
    return result.write()
  else:
    parser.print_usage()
    return 1

if __name__=='__main__':
  sys.exit(cli())
