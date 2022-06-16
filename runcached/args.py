import logging
import sys
from argparse import REMAINDER, Action, ArgumentParser, Namespace
from dataclasses import dataclass, field, fields
from datetime import timedelta
from textwrap import dedent
from typing import Callable, ClassVar, List, Optional, Sequence, Tuple, Type

from pytimeparse.timeparse import timeparse as pytimeparse


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

  include_env: List[str] = field(metadata={
    ARGSPEC_KEY: [ArgSpec(
      '--include-env', '-e',
      metavar='VAR[,...]',
      nargs=1,
      action=_ExtendEachAction,
      type=lambda s: s.split(','),
      default=['HOME'],
      help=dedent('''
        Include named environment variables when computing cache key. Separate with commas.
        Wildcards allowed. Combines with -E. [default: %(default)s]
      '''),
    )],
  })

  exclude_env: str = field(metadata={
    ARGSPEC_KEY: [ArgSpec(
      '--exclude-env', '-E',
      metavar='VAR[,...]',
      nargs=1,
      action=_ExtendEachAction,
      type=lambda s: s.split(','),
      help=dedent('''
        Exclude named environment variables when computing cache key. Separate with commas.
        Wildcards allowed. Combines with -e.
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
    for field in fields(cls):
      add_arg_fns = field.metadata[cls.ARGSPEC_KEY]
      for add_arg_fn in add_arg_fns:
        add_arg_fn(parser, dest=field.name)

    known_args, _rest = parser.parse_known_args(argv)
    args = cls(**known_args.__dict__)
    return args, parser
