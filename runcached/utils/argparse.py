import shlex
from argparse import Action, ArgumentParser, HelpFormatter, Namespace
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Generic, List, Mapping, Optional, TypeVar, cast


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


T = TypeVar('T')

class _ExtendEachAction(Action, Generic[T]):
  def __call__(self, parser: ArgumentParser, namespace: Namespace, args: List[List[T]], option_string: Optional[str] = None):
    for arg in args:
      _values = cast(List[T], getattr(namespace, self.dest, None) or [])
      _values.extend(arg)
      setattr(namespace, self.dest, _values)


class _IncrementAction(Action):
  def __init__(self, *, increment: int = 1, **action_args):
    super().__init__(nargs=0, **action_args)
    self.increment = increment

  def __call__(self, parser, namespace, values, option_string=None):
      count = getattr(namespace, self.dest, None)
      if count is None:
          count = 0
      setattr(namespace, self.dest, count + self.increment)

