from asyncio import StreamReader, Task, create_task, run as async_run, wait
from enum import Enum
from io import StringIO
import logging
import os
import re
import shlex
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cached_property, partial
from hashlib import sha256
from signal import signal, SIGPIPE, SIG_DFL
from typing import IO, AsyncIterator, Callable, Dict, List, Mapping, Optional, TypeAlias, cast

import appdirs
import diskcache
from shellous import raw, sh, Runner

from .args import CliArgs, EnvArg


StrFilter: TypeAlias = Callable[[str], str]

# https://stackoverflow.com/a/14693789
STRIP_ANSI_ESCAPES: StrFilter = partial(re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])').sub, '')


class OutputDest(Enum):
  OUT = 'stdout'
  ERR = 'stderr'

  @property
  def io(self) -> IO[str]:
    return getattr(sys, self.value)

  def reader_for(self, proc: Runner) -> StreamReader:
    return getattr(proc, self.value)


@dataclass(frozen=True)
class Output:
  dest: OutputDest
  text: str

  @classmethod
  async def from_process(cls, proc: Runner) -> AsyncIterator['Output']:
    iters: Dict[str, AsyncIterator[bytes]] = { dest.value: aiter(dest.reader_for(proc)) for dest in OutputDest }
    nexts: Dict[str, Task[bytes]] = {}

    while iters:
      nexts.update({
        dest: create_task(anext(iter), name=dest)
        for dest, iter in iters.items() if dest not in nexts
      })

      nexts_done, _nexts_pending = await wait(nexts.values(), return_when='FIRST_COMPLETED')

      for next_done in nexts_done:
        next_dest = next_done.get_name()
        nexts.pop(next_dest)

        try:
          next_line = await next_done
          yield Output(OutputDest(next_dest), next_line.decode())
        except StopAsyncIteration:
          iters.pop(next_dest)

  def write(self, filter: StrFilter = str):
    self.dest.io.write(filter(self.text))


@dataclass(frozen=True)
class RunResult:
  started_at: datetime
  return_code: int
  outputs: List[Output]

  def replay_outputs(self, filter: StrFilter = str):
    for output in self.outputs:
      output.write(filter)


@dataclass(frozen=True)
class RunConfig:
  command: List[str]
  envs_for_cache: Mapping[str,str] = field(default_factory=dict)
  envs_for_passthru: Mapping[str,str] = field(default_factory=dict)
  input: Optional[str] = None
  shell: bool = False
  shlex: bool = False
  tty: bool = False
  strip_colors: bool = False

  @cached_property
  def _output_filter(self):
    return STRIP_ANSI_ESCAPES if self.strip_colors else (lambda s: s)

  async def _run_without_caching(self) -> 'RunResult':
    started_at = datetime.now()
    env = {
      **self.envs_for_cache,
      **self.envs_for_passthru,
    }
    cmd = (
      sh
      .set(
        exit_codes = range(255),
        inherit_env = False,
        pty = raw() if self.tty else False,
      )
      .env(**env)
      .stdin(
        sh.DEVNULL if self.input is None else StringIO(self.input) 
      )
      .stdout(sh.CAPTURE)
      .stderr(sh.CAPTURE)
      .result
    )(
      [
        env['SHELL'],
        '-c',
        (shlex.join if self.shlex else ' '.join)(self.command)
      ]
      if self.shell
      else self.command
    )
    logging.debug(repr(cmd))

    async with cmd as proc:
      outputs = []
      async for output in Output.from_process(proc):
        outputs.append(output)
        output.write(self._output_filter)

    return_code = proc.returncode

    return RunResult(
      started_at,
      return_code,
      outputs,
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

  async def run_with_caching(self, cache: diskcache.Cache, args: CliArgs) -> 'RunResult':
    logging.debug(self)
    min_started_at = datetime.now() - args.ttl

    if (result := cast(RunResult, cache.get(self._cacheable))) and result.started_at >= min_started_at:
      logging.info(f'Using cached result for {self} from {result.started_at}.')
      result.replay_outputs(self._output_filter)
    else:
      result = await self._run_without_caching()
      if result.return_code == 0 or args.keep_failures:
        cache.set(self._cacheable, result)
      else:
        logging.warn(f'Command returned {result.return_code} and --keep-failures not specified; refusing to cache.')
    return result


async def cli(argv: List[str] = sys.argv[1:]) -> int:
  logging.basicConfig(format='[runcached:%(levelname)s] %(message)s')
  if {'-v', '--verbose'} & set(_args_before_doubledash := argv[:(argv.index('--') if '--' in argv else -1)]) \
      or os.environ.get('RUNCACHED_VERBOSE') \
      or os.environ.get('RUNCACHED_v'):
    logging.getLogger().setLevel(logging.DEBUG)
    sys.addaudithook(lambda *a: print('[runcached:DEBUG]', *a, file=sys.stderr) if a[0] == 'subprocess.Popen' else None)

  # silently exit on broken pipes
  # https://stackoverflow.com/a/30091579
  signal(SIGPIPE, SIG_DFL)

  args, parser = CliArgs.parse(argv)

  logging.getLogger().setLevel(args.verbosity)
  logging.debug(args)

  envs_for_cache = EnvArg.filter_envvars(os.environ, args.include_env or [], args.exclude_env or [])
  envs_for_passthru = EnvArg.filter_envvars(os.environ, args.passthru_env or [], args.exclude_env or [])

  if args.shell:
    envs_for_cache['SHELL'] = os.environ.get('SHELL', 'sh')

  if args.tty and (term_val := os.environ.get('TERM')):
      envs_for_cache['TERM'] = term_val

  cfg = RunConfig(
    command = args.COMMAND,
    envs_for_cache = envs_for_cache,
    envs_for_passthru = envs_for_passthru,
    shell = args.shell,
    shlex = args.shlex,
    tty = args.tty,
    strip_colors = args.strip_colors,
    input = sys.stdin.read() if args.stdin else None,
  )

  cache_dir = appdirs.user_cache_dir(appname=__package__)
  cache = diskcache.Cache(cache_dir)

  if cfg.command:
    result = await cfg.run_with_caching(cache, args)
    return result.return_code
  else:
    parser.print_help()
    return 1

def main():
  sys.exit(async_run(cli()))

if __name__=='__main__':
  main()
