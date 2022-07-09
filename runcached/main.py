import logging
import os
import re
import shlex
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from fnmatch import fnmatchcase
from functools import cached_property
from hashlib import sha256
from subprocess import run
from typing import List, Mapping, Optional, cast

import appdirs
import diskcache
from more_itertools import only, partition

from .args import CliArgs


@dataclass(frozen=True)
class RunResult:
  started_at: datetime
  return_code: int
  stdout: Optional[str] = None
  stderr: Optional[str] = None

  def write(self) -> int:
    for f, val in [(sys.stdout, self.stdout), (sys.stderr, self.stderr)]:
      if val:
        try:
          f.write(val)
        except BrokenPipeError:
          pass
    return self.return_code


@dataclass(frozen=True)
class RunConfig:
  command: List[str]
  env: Mapping[str,str] = field(default_factory=dict)
  input: Optional[str] = None
  shell: bool = False
  shlex: bool = False
  custom_cache_key: Optional[str] = None

  def _run_without_caching(self) -> 'RunResult':
    started_at = datetime.now()
    result = run(
      args=(shlex.join if self.shlex else ' '.join)(self.command) if self.shell else self.command,
      shell=self.shell,
      executable=os.environ.get('SHELL') if self.shell else None,
      env=self.env,
      input=self.input,
      text=True,
      capture_output=True,
    )
    return RunResult(started_at, result.returncode, result.stdout, result.stderr)

  @cached_property
  def _cacheable(self) -> 'RunConfig':
    return replace(self, env={
      k: sha256(v.encode('utf-8')).hexdigest()
      for k, v in self.env.items()
    })

  def run_with_caching(self, cache: diskcache.Cache, args: CliArgs) -> 'RunResult':
    logging.debug(self)
    min_started_at = datetime.now() - args.ttl

    if (result := cast(RunResult, cache.get(self._cacheable))) and result.started_at >= min_started_at:
      logging.info(f'Using cached result for {self} from {result.started_at}.')
    elif result := self._run_without_caching():
      if result.return_code == 0 or args.keep_failures:
        cache.set(self._cacheable, result)
      else:
        logging.warn(f'Command returned {result.return_code} and --keep-failures not specified; refusing to cache.')

    return result


def cli(argv = sys.argv[1:]) -> int:
  logging.basicConfig(format='[runcached:%(levelname)s] %(message)s')
  if {'-v', '--verbose'} & set(argv):
    logging.getLogger().setLevel(logging.DEBUG)
    sys.addaudithook(lambda *a: print('[runcached:DEBUG]', *a, file=sys.stderr) if a[0] == 'subprocess.Popen' else None)

  args, parser = CliArgs.parse(argv)
  if args.COMMAND[0] == '--':
    args.COMMAND = args.COMMAND[1:]

  logging.getLogger().setLevel(args.verbosity)
  logging.debug(args)

  env_forwards, env_assigns = partition(re.compile(r'^\w+=').match, args.include_env)
  envs_forwarded = {
    env_var: env_val for env_var, env_val in os.environ.items()
    if any((
      fnmatchcase(env_var, glob) for glob in env_forwards or []
    ))
    or (
      args.shell and env_var == 'SHELL'
    )
  }
  envs_assigned = {
    tokens[0]: tokens[2]
    for env_assign in env_assigns
    if (tokens := list(shlex.shlex(env_assign, posix=True, punctuation_chars='=')))
  }
  envs_included = envs_forwarded | envs_assigned
  envs_remaining = {
    env_var: env_val for env_var, env_val in envs_included.items()
    if not any((
      fnmatchcase(env_var, glob) for glob in args.exclude_env or []
    ))
  }

  cfg = RunConfig(
    command = args.COMMAND,
    env = envs_remaining,
    shell = args.shell,
    shlex = args.shlex,
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
