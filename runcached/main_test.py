import subprocess
from unittest.mock import Mock

from pytest import fixture
from pytest_mock import MockerFixture

from .main import run_cached


@fixture
def popen(mocker: MockerFixture):
  return mocker.spy(subprocess, 'Popen')


def test_main(popen: Mock):
  run_cached(['echo', 'foo'])
  assert popen.call_count == 1
  run_cached(['echo', 'foo'])
  assert popen.call_count == 1

