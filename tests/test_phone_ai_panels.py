"""The phone page's Stuart transcript: one panel per caller.

There used to be ONE panel (#aiTranscript), so a second Stuart call ringing
the same person took it over and the first caller's transcript and "Take this
call" button vanished while that caller was still talking to Stuart. Each
ring now clones #aiTranscriptTemplate into #aiTranscripts.

The behaviour itself is JavaScript; these checks pin the page shape it relies
on, and syntax-check the script when Node is available.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PHONE = Path(__file__).resolve().parents[1] / 'rinq' / 'web' / 'templates' / 'phone.html'
BLOCK_START = '// ---- Stuart (AI receptionist) live transcript'
BLOCK_END = "device.on('incoming', async (call) => {"


def _source():
    return PHONE.read_text(encoding='utf-8')


def test_template_parses(app):
    app.jinja_env.parse(_source())


def test_no_single_panel_ids_left():
    src = _source()
    for old in ('id="aiTranscript"', 'aiTranscriptLines', 'aiTranscriptReason',
                'aiTranscriptTitle', 'aiTakeBtn', 'aiFollowToken', 'hideAiTranscript'):
        assert old not in src, f"{old} is a leftover of the single shared panel"


def test_panel_template_has_every_part_the_script_reads():
    src = _source()
    assert 'id="aiTranscripts"' in src
    tpl = re.search(r'<template id="aiTranscriptTemplate">(.*?)</template>', src, re.S)
    assert tpl, "the per-caller panel template is missing"
    block = src[src.index(BLOCK_START):src.index(BLOCK_END)]
    for cls in set(re.findall(r"querySelector\('\.([\w-]+)'\)", block)):
        assert f'{cls}' in tpl.group(1), f".{cls} is read by the script but not in the template"


def test_ai_block_is_valid_javascript(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    src = _source()
    js = tmp_path / 'ai_block.js'
    js.write_text(src[src.index(BLOCK_START):src.index(BLOCK_END)], encoding='utf-8')
    result = subprocess.run([node, '--check', str(js)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
