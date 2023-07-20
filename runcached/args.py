import logging
import os
import re
import shlex
import sys
from argparse import ZERO_OR_MORE, Action, ArgumentParser, HelpFormatter, Namespace
from dataclasses import dataclass, field, fields
from datetime import timedelta
from fnmatch import fnmatchcase
from functools import partial
from logging import debug
from textwrap import dedent
from typing import Callable, ClassVar, Dict, Iterator, List, Mapping, NamedTuple, Optional, Set, Tuple, Type, cast

from pytimeparse.timeparse import timeparse as pytimeparse
from trycast import isassignable


def pytimeparse_or_int_seconds(s: str) -> timedelta:
  return timedelta(seconds=pytimeparse(s) or int(s))


# https://stackoverflow.com/a/29485128
class BlankLinesHelpFormatter(HelpFormatter):
  def _split_lines(self, text, width):
    return super()._split_lines(text, width) + ['']


class EnvArg(NamedTuple):
  name: str
  assigned_value: Optional[str]

  @staticmethod
  def filter_envvars(envvars: Mapping[str, str], includes: Dict[str, Optional[str]], exclude_pats: Set[str]) -> Mapping[str, str]:
    assignments = { k: v for k, v in includes.items() if v is not None }
    include_pats = set(includes.keys() - assignments.keys())
    return {
      k: v for k, v in (envvars | assignments).items()
      if     (k in include_pats or any(fnmatchcase(k, pat) for pat in include_pats))
      if not (k in exclude_pats or any(fnmatchcase(k, pat) for pat in exclude_pats))
    }

  @classmethod
  def from_env_arg(cls, envarg: str, assignment_allowed: bool = False) -> 'EnvArg':
    (envarg_shlexed,) = shlex.split(envarg) # unnest shell quotes; should always only be one value
    if '=' in envarg_shlexed:
      name, assigned_value = envarg_shlexed.split('=', maxsplit=1)
      if not assignment_allowed:
        raise ValueError(f'Assignment not allowed in this context: {envarg}')
      elif not re.match(name, r'\w+'):
        raise ValueError(f'Cannot assign value to envvars with wildcards: {envarg}')
      else:
        return cls(name, assigned_value)
    else:
      return cls(envarg, None)

  @classmethod
  def from_env_args(cls, arg: str, *a, **k) -> List['EnvArg']:
    envargs = filter(','.__ne__, shlex.shlex(arg, posix=True, punctuation_chars=','))
    return [cls.from_env_arg(envarg, *a, **k) for envarg in envargs]


class _ExtendEachAction(Action):
  def __call__(self, parser: ArgumentParser, namespace: Namespace, args: List[List[EnvArg]], option_string: Optional[str] = None):
    for arg in args:
      _values = cast(List[EnvArg], getattr(namespace, self.dest, None) or [])
      _values.extend(arg)
      setattr(namespace, self.dest, _values)


@dataclass
class CliArgs:
  """
  Runs the given command with caching of stdout and stderr.
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

  tty: bool = field(metadata={
    ARGSPEC_KEY: [
       ArgSpec(
        '--tty', '-y',
        action='store_true',
        default=False,
        help=dedent('''
          Allocate a pseudo-TTY for the command. Often determines whether command
          outputs color or not. See also --strip-colors/-C. [default: %(default)s]
        '''),
      ),
      ArgSpec(
        '--no-tty', '-Y',
        action='store_false',
        help=dedent('''
          Do not allocate a pseudo-TTY. Overrides -y.
        '''),
      ),
    ]
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
      default=EnvArg.from_env_args('HOME,PATH,TMPDIR'),
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
          Pass command to $SHELL for execution. [default: %(default)s]
        '''),
      ),
      ArgSpec(
        '--no-shell', '-S',
        action='store_false',
        help=dedent('''
          Do not pass command to $SHELL for execution. Overrides -s.
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
          Do not strip ANSI escape sequences when printing cached output. Overrides -C.
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
    ARGSPEC_KEY: [
      ArgSpec(
        type=lambda arg: [arg], # start a list
        metavar='COMMAND',
      ),
      ArgSpec(
        nargs=ZERO_OR_MORE,
        action='extend',
        metavar='ARGS',
      )
    ],
  })

  @classmethod
  def _get_argument_parser(cls) -> ArgumentParser:
    parser = ArgumentParser(description=cls.__doc__, formatter_class=BlankLinesHelpFormatter)
    [
      add_arg_fn(parser, dest=field.name)
      for field in fields(cls) 
      for add_arg_fn in field.metadata[cls.ARGSPEC_KEY]
    ]
    return parser

  @classmethod
  def _get_command(cls, argv: List[str]) -> List[str]:
    args, _rest = cls._get_argument_parser().parse_known_args(argv)
    cmd: List[str] = args.COMMAND[1:] if args.COMMAND[0] == '--' else args.COMMAND
    return cmd

  @staticmethod
  def _envize_string(s: str, keep_case: Callable[[str],bool] = lambda _: True) -> str:
    subbed = re.sub(r'[^a-zA-Z0-9]+', '_', s).strip('_')
    return subbed if keep_case(subbed) else subbed.upper()

  _ENVVAR_RE = r'''(?x)
    ^
      RUNCACHED_
      (?P<envized_opt> [a-zA-Z0-9]+ (?:_[a-zA-Z0-9]+)* ) # option part does not allow double underscores
      (?: __ (?P<envized_cmd> [\w\*]+) )? # cmd part allows double underscores
    $
  '''

  @classmethod
  def _get_arguments_from_env(cls, parser: ArgumentParser, cmd: List[str]) -> Iterator[str]:
    envized_cmd: str = '__'.join(map(cls._envize_string, cmd))
    debug(f'Envized command: {envized_cmd}')

    envized_opts: Dict[str, str] = {
      cls._envize_string(opt, keep_case=lambda s: len(s) == 1): opt
      for opt in parser._option_string_actions.keys()
    }

    for envvar, envvar_val in sorted(os.environ.items()):
      if not envvar.startswith('RUNCACHED_') or not envvar_val:
        continue

      if not (match := re.match(cls._ENVVAR_RE, envvar)):
        raise ValueError(f'Envvar {envvar}: Could not parse. Should match {repr(cls._ENVVAR_RE)}.')

      if (_envized_opt := match['envized_opt']) not in envized_opts:
        raise ValueError(f'Envvar {envvar}: Unrecognized option {repr(_envized_opt)}. Must be one of {envized_opts.keys()}.')

      if (_envized_cmd := match['envized_cmd']) and not envized_cmd.startswith(_envized_cmd):
        debug(f'Envvar {envvar}: envized command does not start with {repr(_envized_cmd)}; skipping.')
        continue

      opt = envized_opts[_envized_opt]
      action = parser._option_string_actions[opt]

      extra_args = [opt] + ([envvar_val] if action.nargs is None or action.nargs else [])
      debug(f'Extra args from env var {envvar}: {extra_args}')
      yield from extra_args

  @classmethod
  def parse(cls: Type['CliArgs'], argv: List[str] = sys.argv[1:]) -> Tuple['CliArgs', ArgumentParser]:
    parser = cls._get_argument_parser()
    cmd = cls._get_command(argv)
    argv = list(cls._get_arguments_from_env(parser, cmd)) + argv
    args_namespace = parser.parse_args(argv)
    args = cls(**args_namespace.__dict__)
    return args, parser

  def __post_init__(self):
    for field in fields(self):
      assert isassignable(val := getattr(self, field.name), field.type), f'{field.name} {val} should be a {field.type}!'
