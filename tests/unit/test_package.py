import pytest

import ai_data_engineer
from ai_data_engineer.cli import main


def test_version_is_set() -> None:
    assert ai_data_engineer.__version__


def test_cli_version_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == ai_data_engineer.__version__


def test_user_errors_are_one_line_not_a_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_data_engineer import cli
    from ai_data_engineer.ingestion.sources import SourceNotFoundError

    def boom(_args: object) -> int:
        raise SourceNotFoundError("no data source named 'nope'")

    monkeypatch.setattr(cli, "_dispatch", boom)
    assert main(["version"]) == 1
    assert capsys.readouterr().err.strip() == "error: no data source named 'nope'"
