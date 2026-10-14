import pytest

import lumida
from lumida.cli import main


def test_version_is_set() -> None:
    assert lumida.__version__


def test_cli_version_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == lumida.__version__


def test_user_errors_are_one_line_not_a_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lumida import cli
    from lumida.ingestion.sources import SourceNotFoundError

    def boom(_args: object) -> int:
        raise SourceNotFoundError("no data source named 'nope'")

    monkeypatch.setattr(cli, "_dispatch", boom)
    assert main(["version"]) == 1
    assert capsys.readouterr().err.strip() == "error: no data source named 'nope'"
