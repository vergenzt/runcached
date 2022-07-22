import logging
import os
import re
import shlex
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cached_property, partial
from hashlib import sha256
from subprocess import run
from typing import Callable, List, Mapping, Optional, cast

import appdirs
import diskcache

from .args import CliArgs, EnvArg


# https://stackoverflow.com/a/14693789
STRIP_ANSI_ESCAPES: Callable[[str], str] = partial(re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])').sub, '')


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
  envs_for_cache: Mapping[str,str] = field(default_factory=dict)
  envs_for_passthru: Mapping[str,str] = field(default_factory=dict)
  input: Optional[str] = None
  shell: bool = False
  shlex: bool = False
  strip_colors: bool = False
  custom_cache_key: Optional[str] = None

  def _run_without_caching(self) -> 'RunResult':
    started_at = datetime.now()
    result = run(
      args=(shlex.join if self.shlex else ' '.join)(self.command) if self.shell else self.command,
      shell=self.shell,
      executable=os.environ.get('SHELL') if self.shell else None,
      env={ **self.envs_for_cache, **self.envs_for_passthru},
      input=self.input,
      text=True,
      capture_output=True,
    )
    return RunResult(
      started_at,
      result.returncode,
      STRIP_ANSI_ESCAPES(result.stdout) if self.strip_colors else result.stdout,
      result.stderr
    )

  @cached_property
  def _cacheable(self) -> 'RunConfig':
    return replace(
      self,
      envs_for_passthru={},
      envs_for_cache={
        k: sha256(v.encode('utf-8')).hexdigest()
        for k, v in self.envs_for_cache.items()
      },
    )

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
  if {'-v', '--verbose'} & set(argv) or os.environ.get('RUNCACHED_VERBOSE') or os.environ.get('RUNCACHED_v'):
    logging.getLogger().setLevel(logging.DEBUG)
    sys.addaudithook(lambda *a: print('[runcached:DEBUG]', *a, file=sys.stderr) if a[0] == 'subprocess.Popen' else None)

  args, parser = CliArgs.parse(argv)

  logging.getLogger().setLevel(args.verbosity)
  logging.debug(args)

  envs_for_cache = EnvArg.filter_envvars(os.environ, args.include_env or [], args.exclude_env or [])
  envs_for_passthru = EnvArg.filter_envvars(os.environ, args.passthru_env or [], args.exclude_env or [])

  if args.shell:
    envs_for_cache = { **envs_for_cache, 'SHELL': os.environ.get('SHELL') }

  cfg = RunConfig(
    command = args.COMMAND,
    envs_for_cache = envs_for_cache,
    envs_for_passthru = envs_for_passthru,
    shell = args.shell,
    shlex = args.shlex,
    strip_colors = args.strip_colors,
    input = sys.stdin.read() if args.stdin else None,
  )

  cache_dir = appdirs.user_cache_dir(appname=__package__)
  cache = diskcache.Cache(cache_dir)

  if cfg.command:
    result = cfg.run_with_caching(cache, args)
    return result.write()
  else:
    parser.print_help()
    return 1

if __name__=='__main__':
  sys.exit(cli())
