import pytest

import ai_data_engineer
from ai_data_engineer.cli import main


def test_version_is_set() -> None:
    assert ai_data_engineer.__version__


def test_cli_version_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == ai_data_engineer.__version__
