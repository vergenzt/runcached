import os
import re
from json import JSONEncoder, dumps
from pathlib import Path
from subprocess import PIPE, run
from typing import Any, Callable, Dict, Iterator, Tuple
from freezegun import freeze_time

import sh
from pytest import fixture
from syrupy.assertion import SnapshotAssertion


@fixture
def testenv(tmp_path: Path) -> Iterator[Dict[str, str]]:
  yield {
    'HOME': str(tmp_path),
    'PATH': os.environ['PATH'],
  }

@fixture
def cli(testenv: Dict[str, str]) -> Iterator[sh.Command]:
  yield sh.Command('runcached').bake(
    '--quiet',
    _in='',
    _env=testenv,
    _truncate_exc=False,
  )

res: Callable[[sh.RunningCommand], Tuple] = lambda of: (of.exit_code, of.stdout, of.stderr)

@fixture
def rc_random(cli: sh.Command):
  return cli.bake('--ttl=60s', '--', 'head', '-c5', '/dev/random')


def test_random_within_ttl(rc_random: sh.Command):
  assert res(rc_random()) == res(rc_random())

def test_random_beyond_ttl(rc_random: sh.Command):
  with freeze_time() as freezer:
    assert (init_res := res(rc_random())) == res(rc_random())
    freezer.tick(61)
    assert res(rc_random()) != init_res


def test_interleave(cli: sh.Command, snapshot: SnapshotAssertion):
  cmd = cli.bake('--', 'sh', '-c', 'echo foo; echo bar >&2; echo baz; echo boz >&2')
  assert res(cmd()) == res(cmd()) == snapshot()
    
