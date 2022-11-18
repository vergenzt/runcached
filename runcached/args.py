import logging
import shlex
import sys
from argparse import ZERO_OR_MORE
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from textwrap import dedent
from typing import List, Optional

from pytimeparse.timeparse import timeparse as pytimeparse

from .utils.dataclass_meta_argparse import ARGS, DataclassMetaArgumentParser, argparse_arg, dataclass_meta_argument_parser
from .utils.argparse import _IncrementAction, _ExtendEachAction, BlankLinesHelpFormatter, EnvArg


@dataclass
class VerbosityOpts(metaclass=DataclassMetaArgumentParser, add_help=False):
  verbosity: int = field(metadata={
    ARGS: [
      argparse_arg(
        '--quiet', '-q',
        action=_IncrementAction,
        increment=-10,
        default=logging.WARN,
        help='Decrease verbosity.',
      ),
      argparse_arg(
        '--verbose', '-v',
        action=_IncrementAction,
        increment=10,
        help='Increase verbosity.',
      ),
    ],
  })


def pytimeparse_or_int_seconds(s: str) -> timedelta:
  return timedelta(seconds=pytimeparse(s) or int(s))


@dataclass
class NonVerbosityOpts:
  """
  Runs the given command with caching of stdout and stderr.
  """

  ttl: timedelta = field(metadata={
    ARGS: [argparse_arg(
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
    ARGS: [argparse_arg(
      '--keep-failures', '-F',
      action='store_true',
      help=dedent('''
        Cache run results that exit non-zero. Does not cache these results by default.
      '''),
    )],
  })

  stdin: bool = field(metadata={
    ARGS: [
      argparse_arg(
        '--include-stdin', '-i',
        action='store_true',
        default=not sys.stdin.isatty(),
        help=dedent('''
          Include stdin when computing cache key. Defaults to true if stdin is not a TTY. If
          stdin is included, stdin will be read until EOF before executing anything.
        '''),
      ),
      argparse_arg(
        '--exclude-stdin', '-I',
        action='store_false',
        help=dedent('''
          Exclude stdin when computing cache key. Overrides -i.
        '''),
      ),
    ],
  })

  include_env: Optional[List[EnvArg]] = field(metadata={
    ARGS: [argparse_arg(
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
    ARGS: [argparse_arg(
      '--passthru-env', '-p',
      metavar='VAR[,...]',
      nargs=1,
      action=_ExtendEachAction,
      type=partial(EnvArg.from_env_args, assignment_allowed=True),
      default=EnvArg.from_env_args('HOME,PATH,TMPDIR'),
      help=dedent('''
        Pass named environment variable(s) through to command without caching them.
        Same format as -e. Any assignments override values from -e. Aggregates across
        all -p options. [defaults: %(default)s]
      '''),
    )],
  })

  exclude_env: Optional[List[EnvArg]] = field(metadata={
    ARGS: [argparse_arg(
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
    ARGS: [
      argparse_arg(
        '--shell', '-s',
        action='store_true',
        default=False,
        help=dedent('''
          Pass command to $SHELL for execution. [default: %(default)s]
        '''),
      ),
      argparse_arg(
        '--no-shell', '-S',
        action='store_false',
        help=dedent('''
          Do not pass command to $SHELL for execution. Overrides -s.
        '''),
      ),
    ],
  })

  shlex: bool = field(metadata={
    ARGS: [
      argparse_arg(
        '--shlex', '-l',
        action='store_true',
        default=False,
        help=dedent('''
          Re-quote command line args before passing to $SHELL. Only used if shell is
          true. [default: %(default)s]
        '''),
      ),
      argparse_arg(
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
    ARGS: [
      argparse_arg(
        '--strip-colors', '-C',
        action='store_true',
        default=not sys.stdout.isatty(),
        help=dedent('''
          Strip ANSI escape sequences when printing cached output. Defaults to true if
          stdout is not a TTY.
        '''),
      ),
      argparse_arg(
        '--no-strip-colors', '-c',
        action='store_false',
        help=dedent('''
          Do not strip ANSI escape sequences when printing cached output. Overrides -C.
        '''),
      ),
    ],
  })

  print_cache_path: bool = field(metadata={
    ARGS: [
      argparse_arg(
        '--print-cache-path', '-P', 
        action='store_true',
        help='Print the disk cache path to stdout and exit.'
      )
    ]
  })

  COMMAND: List[str] = field(metadata={
    ARGS: [
      argparse_arg(
        nargs=ZERO_OR_MORE,
        metavar='COMMAND',
      ),
    ],
  })


@dataclass
class CliArgs(NonVerbosityOpts, VerbosityOpts, metaclass=DataclassMetaArgumentParser):
  pass
