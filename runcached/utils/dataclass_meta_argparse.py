r'''
Tools to help build ArgumentParsers out of dataclass metadata.

To use:

 1. Create a dataclass whose fields you'd like to populate from `argparse` arguments,
  set its metaclass to `DataclassMetaArgumentParser`, and pass any additional `ArgumentParser`
  keyword args to the metaclass.

 2. Associate command line arguments with dataclass fields by adding lists of `argparse_args`
  to fields' `metadata` dicts under the `ARGS` key. (An `argparse_arg` is essentially a
  `functools.partial` on `ArgumentParser.add_argument`, but where the actual parser instance
  is added last.)

 3. Apply any additional customizations to the ArgumentParser after the class is declared through
  the class's `argument_parser` attribute.

 4. Get an instance of your dataclass from parsed command line arguments via the class's
  new `from_args` method.

Example:

>>> @argument_parser_from_dataclass_meta(prog='my-cli')
... @dataclass
... class MyCliArgs:
...   'My CLI app that does a thing'
...   my_arg: int = field(metadata={
...   ARGS: [argparse_arg(
...     '--myarg', '-m',
...     metavar='INT',
...     type=int,
...     default=5,
...     help='my integer argument',
...   )],
...   })
...   remainder: List[str] = field(metadata={
...   ARGS: [argparse_arg(
...     nargs=argparse.ZERO_OR_MORE,
...     metavar='STR',
...     help='remaining string arguments',
...   )],
...   })
>>> MyCliArgs.from_args(['--myarg', '7', 'foo', 'bar', 'baz'])
MyCliArgs(my_arg=7, remainder=['foo', 'bar', 'baz'])
>>> MyCliArgs.argument_parser.print_help()
usage: my-cli [-h] [--myarg INT] [STR ...]
<BLANKLINE>
positional arguments:
  STR          remaining string arguments
<BLANKLINE>
options:
  -h, --help       show this help message and exit
  --myarg INT, -m INT  my integer argument
'''

import os
import re
import sys
from argparse import Action, ArgumentParser, Namespace
from dataclasses import dataclass, field, fields, is_dataclass
from logging import debug
from typing import Callable, Concatenate, Dict, Generic, List, Mapping, Optional, ParamSpec, Protocol, Tuple, Type, TypeVar, Union, overload


T = TypeVar('T', bound=ArgumentParser)
P = ParamSpec('P')
R = TypeVar('R', ArgumentParser, Action)


def _partial_instance_method(source_fn: Callable[Concatenate[T, P], R]) -> Callable[P, Callable[[T], R]]:
  def outer(*outer_args: P.args, **outer_kwargs: P.kwargs) -> Callable[[T], R]:
    def inner(self: T, **kwargs) -> R:
      return source_fn(self, *outer_args, **outer_kwargs, **kwargs)
    return inner
  return outer


argparse_arg: Callable[..., Callable[[ArgumentParser], Action]]
argparse_arg = _partial_instance_method(ArgumentParser.add_argument)
argparse_arg.__doc__ = '''
  An `argparse_arg` is a saved set of arguments to `ArgumentParser.add_argument`, for
  later application to a specific `ArgumentParser` instance.

  Instantiate one by calling `argparse_arg(...)` with the same arguments you would give
  to the `add_argument` method of an `ArgumentParser` instance.

  Exception: You should not specify a `dest`; this will be supplied to `add_argument`
  later with the name of the dataclass field whose metadata the `argparse_arg` is a
  member of.

  >>> my_arg_fn = argparse_arg('-f', '--foo', action='store_true', help='bar')
  >>> parser = ArgumentParser(prog='baz')
  >>> action = my_arg_fn(parser)
  >>> parser.print_help()
  usage: baz [-h] [-f]
  <BLANKLINE>
  options:
    -h, --help  show this help message and exit
    -f, --foo   bar
'''

ARGS = object()


def default_env_from_arg(s: str) -> str:
  subbed = re.sub(r'[^a-zA-Z0-9]+', '_', s).strip('_')
  return subbed if len(subbed) == 1 else subbed.upper()

def default_arg_from_env(s: str) -> str:
  subbed = re.sub(r'[^a-zA-Z0-9]+', '-', s).strip('-')
  return f'-{subbed}' if len(subbed) == 1 else f'--{subbed.lower()}'


class DataclassMetaArgumentParser(type, ArgumentParser):
  def __new__(cls, name, bases, attrs, **kwargs):
    return super().__new__(cls, name, bases, attrs)

  def __init__(
    subcls, name, bases, attrs, /, *,

    include_env: bool = True,
    env_prefix: Optional[str] = None,
    env_from_arg_fn: Callable[[str], str] = default_env_from_arg,
    arg_from_env_fn: Callable[[str], str] = default_arg_from_env,
    validate_field_types: bool = False,

    **kwargs
  ):

    subcls.include_env = include_env
    subcls.env_prefix = env_prefix
    subcls.env_from_arg_fn = env_from_arg_fn
    subcls.arg_from_env_fn = arg_from_env_fn
    subcls.validate_field_types = validate_field_types

    kwargs.setdefault('description', subcls.__doc__)
    ArgumentParser.__init__(subcls, **kwargs)
    type.__init__(subcls, name, bases, attrs)

    subcls._dataclass_args_added: bool = False

    if validate_field_types:
      old_post_init = getattr(subcls, '__post_init__', lambda *a, **kw: None)
      new_post_init = lambda inst, *a, **kw: (subcls._validate_types(inst), old_post_init(inst, *a, **kw))
      setattr(subcls, '__post_init__', new_post_init)

  @staticmethod
  def _validate_types(inst):
    from trycast import isassignable
    for field in fields(inst):
      field_val = getattr(inst, field.name)
      assert isassignable(field_val, field.type), f'{field.name} {field_val} should be a {field.type}!'

  # instance method on metaclass = classmethod
  def _add_dataclass_arguments(cls):
    if not cls._dataclass_args_added:
      for field in fields(cls):
        for add_arg_fn in field.metadata[ARGS]:
          add_arg_fn(cls, dest=field.name)
      cls._dataclass_args_added = True

  # instance method on metaclass = classmethod
  def _extra_args_from_env(cls, env: Mapping[str, str] = os.environ) -> List[str]:
    prefix: str = cls.env_prefix or cls.env_from_arg_fn(cls.prog)

    envized_opts: Dict[str, str] = {
      cls.env_from_arg_fn(opt): opt
      for opt in cls._option_string_actions.keys()
    }

    extra_args: List[str] = []
    for envvar, envvar_val in env.items():
      if not envvar.startswith(prefix + '_') or not envvar_val:
        continue

      envized_opt = envvar[len(prefix)+1:]
      if envized_opt not in envized_opts:
        raise ValueError(
          f'Envvar {envvar}: Unrecognized option {repr(envized_opt)}. Must be one of {set(envized_opts.keys())}.')

      opt = envized_opts[envized_opt]
      action = cls._option_string_actions[opt]

      extra_arg = [
        opt] + ([envvar_val] if action.nargs is None or action.nargs else [])
      extra_args = extra_arg + extra_args  # prepend

      debug(f'Extra arg from env var {envvar}: {extra_arg}')

    return extra_args

  def parse_known_args(cls: Type[T], argv: Optional[List[str]] = None) -> Tuple[T, List[str]]:
    cls._add_dataclass_arguments()

    if argv is None:
      argv = (cls._extra_args_from_env() if cls.include_env else []) + sys.argv[1:]

    args_ns, remaining_args = super().parse_known_args(argv)
    args = cls(**args_ns.__dict__)
    return args, remaining_args


P = ParamSpec('P')
Q = ParamSpec('Q')


class Decorator(Protocol[T]):
  @overload
  def __call__(self, cls: Type[T], /) -> Type[T]: ...

class DecoratorWrapper(Generic[P, T]):
  @overload
  def __call__(self, cls: Type[T]=None, /, *a: P.args, **kwargs: P.kwargs) -> Decorator[T]: ...
  @overload
  def __call__(self, cls: Type[T], /) -> Type[T]: ...


ArgumentParserP = ParamSpec('ArgumentParserP')


def _param_spec_wrapper(ArgumentParser: Callable[ArgumentParserP, ArgumentParser]) -> DecoratorWrapper[ArgumentParserP, T]:

  class dataclass_meta_argument_parser:
    def __new__(cls, deco_cls: Type[T]=None, /, **kwargs):
      self = super().__new__(cls)(deco_cls, **kwargs)
      self.argparser_args: dict[str] = {}
      return self(deco_cls, **kwargs)

    def __call__(self, deco_cls: Type[T]=None, /, **kwargs):
      if deco_cls:
        class subcls(deco_cls, metaclass=DataclassMetaArgumentParser, **self.argparser_args): pass
        return subcls
      else:
        self.argparser_args = kwargs
        return self


  return dataclass_meta_argument_parser


dataclass_meta_argument_parser = _param_spec_wrapper(ArgumentParser)
