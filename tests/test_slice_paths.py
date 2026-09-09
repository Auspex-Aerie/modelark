import os
from pathlib import Path

import pytest

from modelark.slice.paths import canonical_attachment, utf8_size
from modelark.slice.transaction import TransferRefusal


def test_attachment_normalization_is_lexical_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / 'target'
    target.mkdir()
    (tmp_path / 'link').symlink_to(target, target_is_directory=True)
    result = canonical_attachment('missing/../link')
    assert result == tmp_path / 'link'
    assert result != result.resolve()
    assert canonical_attachment(result) == result


def test_attachment_expands_home_without_requiring_it_to_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'expanduser', lambda self: tmp_path / str(self)[2:])
    assert canonical_attachment('~/missing/../usb') == tmp_path / 'usb'


def test_attachment_retains_host_filesystem_encoding(tmp_path):
    path = tmp_path / os.fsdecode(b'usb-\xff')
    assert canonical_attachment(path) == path


@pytest.mark.parametrize('value', ['plain', '模型/é', '😀'])
def test_output_size_counts_utf8_bytes(value):
    assert utf8_size(value) == len(value.encode('utf-8'))


@pytest.mark.parametrize('value', ['bad-\udcff', '\ud800'])
def test_output_size_refuses_surrogates(value):
    with pytest.raises(TransferRefusal) as caught:
        utf8_size(value)
    assert caught.value.code == 'DESTINATION_LAYOUT_UNSUPPORTED'
