import os
import re
from pathlib import Path
from subprocess import PIPE, run
from typing import Dict, Iterator

from pytest import fixture


@fixture
def testenv(tmp_path: Path) -> Iterator[Dict[str, str]]:
  yield {
    'HOME': str(tmp_path),
    'PATH': os.environ.get('PATH'),
  }


def test_main(testenv: Dict[str, str]):
  result = run(['runcached', '--', 'echo', 'foo'], text=True, input='', stdout=PIPE, stderr=PIPE, env=testenv)
  assert result.returncode == 0
  assert result.stdout == 'foo\n'
  assert result.stderr == ''

  result = run(['runcached', '--', 'echo', 'foo'], text=True, input='', stdout=PIPE, stderr=PIPE, env=testenv)
  assert result.returncode == 0
  assert result.stdout == 'foo\n'
  assert re.match(string=result.stderr, pattern='^' + re.escape("[runcached:INFO] Using cached result") + '.*$')

