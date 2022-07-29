import logging
import os
import re
import shlex
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from functools import cached_property, partial
from hashlib import sha256
from typing import IO, Callable, List, Mapping, Optional, cast

import appdirs
import diskcache
import sh

from .args import CliArgs, EnvArg


# https://stackoverflow.com/a/14693789
STRIP_ANSI_ESCAPES: Callable[[str], str] = partial(re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])').sub, '')


class OutputDest(Enum):
  OUT = 'stdout'
  ERR = 'stderr'

  @property
  def io(self) -> IO[str]:
    return getattr(sys, self.value)


@dataclass(frozen=True)
class OutputLine:
  dest: OutputDest
  text: str

  def write(self, filter: Callable[[str], str] = str) -> 'OutputLine':
    self.dest.io.write(filter(self.text))
    return self


@dataclass(frozen=True)
class RunResult:
  started_at: datetime
  return_code: int
  output_lines: List[OutputLine]

  def replay(self, filter: Callable[[str], str] = str):
    for line in self.output_lines:
      line.write(filter)


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

  @property
  def _output_filter(self) -> Callable[[str], str]:
    return STRIP_ANSI_ESCAPES if self.strip_colors else str

  def _args(self) -> List[str]:
    if self.shell:
      joiner: Callable[[List[str]], str] = shlex.join if self.shlex else ' '.join
      return [os.environ['SHELL'], '-c', joiner(self.command)]
    else:
      return self.command

  def _run_without_caching(self) -> 'RunResult':
    started_at = datetime.now()
    args = self._args()
    output_lines: List[OutputLine] = list()
    command = sh.Command(args[0]).bake(*args[1:])
    process: sh.RunningCommand = command(
      _env={ **self.envs_for_cache, **self.envs_for_passthru },
      _in=self.input,
      _out=lambda line: output_lines.append(OutputLine(OutputDest.OUT, line).write(self._output_filter)),
      _err=lambda line: output_lines.append(OutputLine(OutputDest.ERR, line).write(self._output_filter)),
      _truncate_exc=False,
    )

    try:
      process.wait()
    except sh.ErrorReturnCode:
      pass

    assert isinstance(process.exit_code, int)
    return RunResult(started_at, process.exit_code, output_lines)

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
      result.replay(self._output_filter)

    elif result := self._run_without_caching():
      if result.return_code == 0 or args.keep_failures:
        cache.set(self._cacheable, result)
      else:
        logging.warn(f'Command returned {result.return_code} and --keep-failures not specified; refusing to cache.')

    return result


def _sh_command_info_to_debug(log: logging.LogRecord):
  'Redirect all INFO messages to DEBUG'
  if log.levelno == logging.INFO:
    log.levelno, log.levelname = logging.DEBUG, 'DEBUG'
    logger = logging.getLogger('sh.command')
    if logger.isEnabledFor(logging.DEBUG):
      logger.handle(log)
    return False
  else:
    return True


def cli(argv = sys.argv[1:]) -> int:
  logging.basicConfig(format='[runcached:%(levelname)s] %(message)s')
  logging.getLogger('sh.command').addFilter(_sh_command_info_to_debug)

  # short circuit so we can debug the argument/environment parsing itself
  if {'-v', '--verbose'} & set(argv) or os.environ.get('RUNCACHED_VERBOSE') or os.environ.get('RUNCACHED_v'):
    logging.getLogger().setLevel(logging.DEBUG)

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
    return cfg.run_with_caching(cache, args).return_code
  else:
    parser.print_help()
    return 1

if __name__=='__main__':
  sys.exit(cli())
