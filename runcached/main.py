import logging
import os
import re
import shlex
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cached_property, partial
from hashlib import sha256
from random import randbytes
from subprocess import DEVNULL, PIPE, Popen
from tempfile import TemporaryDirectory
from typing import Callable, List, Mapping, Optional, Sequence, Tuple, cast

import appdirs
import diskcache

from .args import CliArgs, EnvArg, NonVerbosityOpts, VerbosityOpts


# https://stackoverflow.com/a/14693789
ANSI_ESCAPE = re.compile(br'''
    \x1B  # ESC
    (?:   # 7-bit C1 Fe (except CSI)
        [@-Z\\-_]
    |     # or [ for CSI, followed by a control sequence
        \[
        [0-?]*  # Parameter bytes
        [ -/]*  # Intermediate bytes
        [@-~]   # Final byte
    )
''', re.VERBOSE)

STRIP_ANSI_ESCAPES: Callable[[bytes], bytes] = partial(ANSI_ESCAPE.sub, '')


@dataclass(frozen=True)
class RunResult:
  started_at: datetime
  return_code: int
  output: Sequence[Tuple[int, bytes]]

  @classmethod
  def record(cls, args: List[str], *, display: bool = True, **recorder_kwargs) -> 'RunResult':
    with TemporaryDirectory() as tmpdir:
      while os.path.exists(script_fifo := tmpdir + '/runcached-fifo-' + randbytes(8).hex()): pass
      os.mkfifo(script_fifo)

      # NOTE: this usage assumes MacOS `script` command.
      recorder_args = [
        'script',
        '-F', # always flush output immediately
        '-r', # record timestamps & interleave stdout/stderr
        '-q', # be quiet about start & end (relevant if display == True)
        script_fifo,
        'bash', '-c',
        shlex.join(args)

        # `script` doesn't differentiate between errput vs output; so we manually
        # prepend a specifier to every line
        + ' 2> >(sed s/^/E:/ >&2)'
        + ' | '' sed s/^/O:/'
      ]

      started_at = datetime.now()
      recorded_output: Sequence[Tuple[int, bytes]] = []

      with Popen(recorder_args, stdout=DEVNULL, stderr=DEVNULL, **recorder_kwargs) as record_ps:

        replayer_args = [
          'script',
          '-p', # replay
          '-d', # without sleeping between timestamped output records
          '-q', # be quiet about start & end
          script_fifo
        ]
        with Popen(replayer_args, stdout=PIPE, stderr=DEVNULL) as replay_ps:
          while raw_line := replay_ps.stdout.readline():
            logging.debug(repr(raw_line))

            dest_key, line = raw_line.split(b':', 1, )
            dest = { b'O': sys.stdout, b'E': sys.stderr }[dest_key].buffer
            dest_fileno = dest.fileno()
            recorded_output.append((dest_fileno, line))

            if display:
              dest.write(line)
              dest.flush()

      return cls(
        started_at,
        record_ps.returncode,
        recorded_output,
      )

  def replay(self):
    'Replay cached recording to stdout/stderr and return cached exit code.'
    dests = {}
    for fileno, line in self.output:
      dest = dests[fileno] if fileno in dests else dests.setdefault(fileno, open(fileno, 'wb'))
      dest.write(line)
      dest.flush()



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

  def _run_without_caching(self, display: bool = False) -> 'RunResult':
    cmd: List[str]
    if self.shell:
      shell = os.environ.get('SHELL')
      shell_cmd = (shlex.join if self.shlex else ' '.join)(self.command)
      cmd = [ shell, '-c', shell_cmd ]
    else:
      cmd = self.command

    env={ **self.envs_for_cache, **self.envs_for_passthru }

    return RunResult.record(cmd, display=display, env=env)

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

  def run_with_caching(self, cache: diskcache.Cache, args: CliArgs, display: bool = True) -> int:
    logging.debug(self)
    min_started_at = datetime.now() - args.ttl

    if (result := cast(RunResult, cache.get(self._cacheable))) and result.started_at >= min_started_at:
      logging.info(f'Using cached result for {self} from {result.started_at}.')
      result.replay()

    elif result := self._run_without_caching(display=display):
      logging.debug(result)

      if result.return_code == 0 or args.keep_failures:
        cache.set(self._cacheable, result)
      else:
        logging.warn(f'Command returned {result.return_code} and --keep-failures not specified; refusing to cache.')

    return result.return_code

def cli(argv: List[str] = sys.argv[1:]) -> int:
  verbosity: int = VerbosityOpts.parse_known_args()

  logging.basicConfig(level=verbosity, format='[runcached:%(levelname)s] %(message)s')
  sys.addaudithook(lambda *a: (logging.debug(shlex.join(a[1][1])), logging.debug(str(a))) if a[0] == 'subprocess.Popen' else None)

  args = CliArgs.parse_args(argv)

  logging.getLogger().setLevel(args.verbosity)
  logging.debug(args)

  cache_dir = appdirs.user_cache_dir(appname=__package__)
  cache = diskcache.Cache(cache_dir)

  if args.print_cache_path:
    print(cache_dir)
    return 0

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

  if cfg.command:
    return cfg.run_with_caching(cache, args, display=True)
  else:
    CliArgs.print_help()
    return 1

def main():
  sys.exit(cli())

if __name__=='__main__':
  main()
