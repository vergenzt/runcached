import logging
import os
import re
import shlex
import sys
from argparse import REMAINDER, Action, ArgumentParser, HelpFormatter, Namespace
from dataclasses import dataclass, field, fields
from datetime import timedelta
from fnmatch import fnmatchcase
from functools import partial
from logging import debug
from textwrap import dedent
from typing import Callable, ClassVar, Dict, List, Mapping, Optional, Tuple, Type, cast

from pytimeparse.timeparse import timeparse as pytimeparse
from trycast import isassignable


def pytimeparse_or_int_seconds(s: str) -> timedelta:
  return timedelta(seconds=pytimeparse(s) or int(s))


# https://stackoverflow.com/a/29485128
class BlankLinesHelpFormatter(HelpFormatter):
  def _split_lines(self, text, width):
    return super()._split_lines(text, width) + ['']


@dataclass
class EnvArg:
  envvar: str
  assigned_value: Optional[str] = None

  def matches(self, envvar: str) -> bool:
    return fnmatchcase(envvar, self.envvar)

  @staticmethod
  def filter_envvars(envvars: Mapping[str, str], inclusions: List['EnvArg'], exclusions: List['EnvArg']) -> Mapping[str, str]:
    assignments = { arg.envvar: arg.assigned_value for arg in inclusions if arg.assigned_value is not None }
    return {
      name: assignments.get(name, val)
      for name, val in envvars.items()
      if any(arg.matches(name) for arg in inclusions)
      and not any(arg.matches(name) for arg in exclusions)
    }

  @classmethod
  def from_env_arg(cls, envarg: str, assignment_allowed: bool = False) -> 'EnvArg':
    if assignment_allowed:
      (envarg_shlexed,) = shlex.split(envarg) # unnest shell quotes; should always only be one value
      (envvar, assigned_value) = envarg_shlexed.split('=', maxsplit=1)
      return cls(envvar, assigned_value)
    else:
      return cls(envarg)

  @classmethod
  def from_env_args(cls, arg: str, assignment_allowed: bool = False) -> List['EnvArg']:
    envargs = filter(','.__ne__, shlex.shlex(arg, posix=True, punctuation_chars=','))
    return list(map( cls.from_env_arg, envargs ))


class _ExtendEachAction(Action):
  def __call__(self, parser: ArgumentParser, namespace: Namespace, args: List[List[EnvArg]], option_string: Optional[str] = None):
    for arg in args:
      _values = cast(List[EnvArg], getattr(namespace, self.dest, None) or [])
      _values.extend(arg)
      setattr(namespace, self.dest, _values)


@dataclass
class CliArgs:
  """
  Runs the given COMMAND with caching of stdout and stderr.
  """

  ARGSPEC_KEY: ClassVar[object] = object()
  ArgSpec: ClassVar[Callable[..., Callable[[ArgumentParser], Action]]] = lambda *a, **kw: lambda self, *a2, **kw2: self.add_argument(*a, *a2, **kw, **kw2)

  ttl: timedelta = field(metadata={
    ARGSPEC_KEY: [ArgSpec(
      '--ttl', '-t',
      metavar='DURATION',
      type=pytimeparse_or_int_seconds,
      default='1d',
      help=dedent('''
        Max length of time for which to cache command results.
        Format: https://pypi.org/project/pytimeparse [default: %(default)s]
      '''),
    )],
  })

  keep_failures: bool = field(metadata={
    ARGSPEC_KEY: [ArgSpec(
      '--keep-failures', '-F',
      action='store_true',
      help=dedent('''
        Cache run results that exit non-zero. Does not cache these results by default.
      '''),
    )],
  })

  stdin: bool = field(metadata={
    ARGSPEC_KEY: [
      ArgSpec(
        '--include-stdin', '-i',
        action='store_true',
        default=not sys.stdin.isatty(),
        help=dedent('''
          Include stdin when computing cache key. Defaults to true if stdin is not a TTY. If
          stdin is included, stdin will be read until EOF before executing anything.
        '''),
      ),
      ArgSpec(
        '--exclude-stdin', '-I',
        action='store_false',
        help=dedent('''
          Exclude stdin when computing cache key. Overrides -i.
        '''),
      ),
    ],
  })

  include_env: Optional[List[EnvArg]] = field(metadata={
    ARGSPEC_KEY: [ArgSpec(
      '--include-env', '-e',
      metavar='VAR[,...]',
      nargs=1,
      action=_ExtendEachAction,
      type=partial(EnvArg.from_env_args, assignment_allowed=True),
      help=dedent('''
        Include named environment variable(s) when running command and when computing
        cache key. Separate with commas or spaces. Escape separators with shell-style
        quoting. May assign new value with VAR=value, or forward existing value by
        simply naming VAR. Wildcards allowed when declaring without assignment.
        Aggregates across all -e options.
      '''),
    )],
  })

  passthru_env: Optional[List[EnvArg]] = field(metadata={
    ARGSPEC_KEY: [ArgSpec(
      '--passthru-env', '-p',
      metavar='VAR[,...]',
      nargs=1,
      action=_ExtendEachAction,
      type=partial(EnvArg.from_env_args, assignment_allowed=True),
      default='HOME,PATH,TMPDIR',
      help=dedent('''
        Pass named environment variable(s) through to command without caching them.
        Same format as -e. Any assignments override values from -e. Aggregates across
        all -p options. [defaults: %(default)s]
      '''),
    )],
  })

  exclude_env: Optional[List[EnvArg]] = field(metadata={
    ARGSPEC_KEY: [ArgSpec(
      '--exclude-env', '-E',
      metavar='VAR[,...]',
      nargs=1,
      action=_ExtendEachAction,
      type=partial(EnvArg.from_env_args, assignment_allowed=False),
      help=dedent('''
        Do not pass named environment variable(s) through to command, nor include them
        when computing cache key. Same format as -e and -p except assignments are
        disallowed. Aggregates across all -E options, and overrides -e and -p.
      '''),
    )],
  })

  shell: bool = field(metadata={
    ARGSPEC_KEY: [
      ArgSpec(
        '--shell', '-s',
        action='store_true',
        default=False,
        help=dedent('''
          Pass COMMAND to $SHELL for execution. [default: %(default)s]
        '''),
      ),
      ArgSpec(
        '--no-shell', '-S',
        action='store_false',
        help=dedent('''
          Do not pass COMMAND to $SHELL for execution. Overrides -s.
        '''),
      ),
    ],
  })

  shlex: bool = field(metadata={
    ARGSPEC_KEY: [
      ArgSpec(
        '--shlex', '-l',
        action='store_true',
        default=False,
        help=dedent('''
          Re-quote command line args before passing to $SHELL. Only used if shell is
          true. [default: %(default)s]
        '''),
      ),
      ArgSpec(
        '--no-shlex', '-L',
        action='store_false',
        help=dedent('''
          Do not re-quote command line args before passing to $SHELL. You may need to
          embed additional quoting ensure the shell correctly interprets the command.
        '''),
      ),
    ],
  })

  strip_colors: bool = field(metadata={
    ARGSPEC_KEY: [
      ArgSpec(
        '--strip-colors', '-C',
        action='store_true',
        default=not sys.stdout.isatty(),
        help=dedent('''
          Strip ANSI escape sequences when printing cached output. Defaults to true if
          stdout is not a TTY.
        '''),
      ),
      ArgSpec(
        '--no-strip-colors', '-c',
        action='store_false',
        help=dedent('''
          Do not strip ANSI escape sequences when printing cached output.
        '''),
      ),
    ],
  })

  verbosity: int = field(metadata={
    ARGSPEC_KEY: [
      ArgSpec(
        '--quiet', '-q',
        action='store_const',
        const=logging.WARN,
        default=logging.INFO,
        help='Set log level to warnings only.',
      ),
      ArgSpec(
        '--verbose', '-v',
        action='store_const',
        const=logging.DEBUG,
        help='Set log level to debug.',
      ),
    ],
  })

  COMMAND: List[str] = field(metadata={
    ARGSPEC_KEY: [ArgSpec(
      nargs=REMAINDER,
      metavar='COMMAND',
    )],
  })

  @classmethod
  def parse(cls: Type['CliArgs'], argv = sys.argv[1:]) -> Tuple['CliArgs', ArgumentParser]:
    parser = ArgumentParser(description=cls.__doc__, formatter_class=BlankLinesHelpFormatter)
    actions: List[Action] = [
      add_arg_fn(parser, dest=field.name)
      for field in fields(cls) 
      for add_arg_fn in field.metadata[cls.ARGSPEC_KEY]
    ]
    known_args, _rest = parser.parse_known_args(argv)
    if (cmd := getattr(known_args, 'COMMAND')) and cmd[0] == '--':
      cmd.pop(0)

    extra_argvs = []
    for k,v in os.environ.items():
      if k.startswith('RUNCACHED_'):
        debug('Environment var %s=%s', k, v)

    opt_actions: Dict[str, Action] = {
      option_string: action
      for action in actions
      for option_string in action.option_strings
    }
    opts_envized: Dict[str, str] = {
      opt: (
        # require envvar to match case for single-letter options
        envized_opt if len(envized_opt.strip('_')) == 1
        else envized_opt.upper()
      )
      for opt in opt_actions.keys()
      if (envized_opt := re.sub('[^a-zA-Z0-9]+', '_', opt))
    }

    for opt, envized_opt in opts_envized.items():
      action = opt_actions[opt]

      if val := os.environ.get('RUNCACHED' + envized_opt):
        extra_argvs.append(opt)
        if action.nargs is None or action.nargs:
          extra_argvs.append(val)

      if (cmd := getattr(known_args, 'COMMAND')) \
        and (cmd_first_word := cmd[0]) \
        and (cmd_specific_val := os.environ.get('RUNCACHED' + envized_opt + '__' + cmd_first_word)):

        extra_argvs.append(opt)
        if action.nargs is None or action.nargs:
          extra_argvs.append(cmd_specific_val)

    if extra_argvs:
      debug('Extra args from env vars: %s', extra_argvs)

    for extra_argv in reversed(extra_argvs):
      argv.insert(0, extra_argv)

    known_args, _rest = parser.parse_known_args(argv)
    if (cmd := getattr(known_args, 'COMMAND')) and cmd[0] == '--':
      cmd.pop(0)
    args = cls(**known_args.__dict__)

    return args, parser

  def __post_init__(self):
    for field in fields(self):
      assert isassignable(val := getattr(self, field.name), field.type), f'{field.name} {val} should be a {field.type}!'
