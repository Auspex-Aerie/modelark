"""The actual browser refusal formatter exposes the affected drive safely."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_refusal_formatter_reports_drive_and_reason_as_text():
    node = shutil.which('node')
    if node is None:
        pytest.skip('node needed for actual JavaScript formatter test')
    source = (Path(__file__).resolve().parents[1] / 'modelark/web/static/proposal.js').read_text()
    function = source[source.index('  function refusalText('):source.index('  function setStartGate(')]
    result = dict(code='DRIVE_SERIAL_REPAIR_REQUIRED',
                  evidence=dict(drive='drive-01', reason='<script>historical bridge proof</script>'),
                  actions=['inspect_serial_identity', 'repair_serial_identity', 'preview_again'])
    output = subprocess.run([node, '-e', function + '\nconsole.log(refusalText(' + json.dumps(result) + '));'],
                            capture_output=True, text=True, check=True).stdout
    assert 'drive-01' in output
    assert '<script>historical bridge proof</script>' in output
    assert 'inspect_serial_identity' in output
    assert 'repair_serial_identity' in output
    # The formatter returns text, not HTML. Review errors use the text sink.
    assert 'state.textContent = refusalText(status)' in source
    assert 'innerHTML = refusalText' not in source
