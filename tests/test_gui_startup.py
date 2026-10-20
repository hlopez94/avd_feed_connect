"""Exercise imports in fresh interpreters, as the production launcher does."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('module', ['avd_feed_gui', 'avd_feed_connect.gui.localization'])
def test_fresh_import_pins_gtk_before_loading(module):
    gi = pytest.importorskip('gi')
    try:
        gi.require_version('Gtk', '4.0')
        if module == 'avd_feed_gui':
            gi.require_version('WebKit', '6.0')
    except ValueError:
        pytest.skip('Required GUI typelibs are unavailable')
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    result = subprocess.run(
        [sys.executable, '-c',
         'import warnings, gi\n'
         'warnings.filterwarnings("error", message=".*Gtk.*", category=gi.PyGIWarning)\n'
         f'import {module}\n'
         'assert gi.get_required_version("Gtk") == "4.0"\n'],
        env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
