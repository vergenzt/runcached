import logging
import os
import re
import shlex
import sys
from argparse import REMAINDER, Action, ArgumentParser, Namespace
from dataclasses import dataclass, field, fields
from datetime import timedelta
from logging import debug
from textwrap import dedent
from typing import Callable, ClassVar, Dict, List, Optional, Sequence, Tuple, Type

from pytimeparse.timeparse import timeparse as pytimeparse
from trycast import isassignable


class _ExtendEachAction(Action):
  def __call__(self, parser: ArgumentParser, namespace: Namespace, values: Sequence[str], option_string: Optional[str] = None):
    for next_value in values:
      _values = getattr(namespace, self.dest, None) or []
      _values.extend(next_value)
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
      type=lambda s: timedelta(seconds=pytimeparse(s)),
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

  include_env: Optional[List[str]] = field(metadata={
    ARGSPEC_KEY: [ArgSpec(
      '--include-env', '-e',
      metavar='VAR[,...]',
      nargs=1,
      action=_ExtendEachAction,
      type=lambda s: [t for t in shlex.shlex(s, posix=True, punctuation_chars=',') if t != ','],
      default=['HOME'],
      help=dedent('''
        Include named environment variable(s) when computing cache key. Separate with
        commas or spaces. Escape separators with shell-style quoting. May assign new
        value with VAR=value, or include existing by simply naming VAR. Wildcards
        allowed when declaring simple names. Aggregates across default and across all -e
        options. [default: %(default)s]
      '''),
    )],
  })

  exclude_env: Optional[List[str]] = field(metadata={
    ARGSPEC_KEY: [ArgSpec(
      '--exclude-env', '-E',
      metavar='VAR[,...]',
      nargs=1,
      action=_ExtendEachAction,
      type=lambda s: s.split(','),
      help=dedent('''
        Exclude named environment variables when computing cache key. Same format as -e.
        Wildcards allowed. Aggregates across all -E options, and overrides -e.
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
    parser = ArgumentParser(description=cls.__doc__)
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
