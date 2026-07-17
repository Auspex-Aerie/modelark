import pytest

from modelark import __version__
from modelark import cli


def test_cli_reports_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out == f"modelark {__version__}\n"
