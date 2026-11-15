"""Fault-injection tests: no Adobe processes are launched or stopped."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from editbot.ae import bridge


class RenderGuards(unittest.TestCase):
    def setUp(self):
        parent=ROOT/'review/spiderman_six_for_approval/integrity_review/guard_tests'
        parent.mkdir(parents=True,exist_ok=True)
        self.folder=Path(tempfile.mkdtemp(dir=parent)).resolve()
        assert self.folder.is_relative_to(parent.resolve())
        self.engine=bridge.AfterEffects(self.folder,self.folder/'logs')
        self.output=self.folder/'video.mp4'
        self.project=self.folder/'project.aep';self.project.write_bytes(b'project')

    def tearDown(self):
        absolute=self.folder.resolve()
        assert absolute.is_relative_to((ROOT/'review/spiderman_six_for_approval/integrity_review/guard_tests').resolve())
        shutil.rmtree(absolute)

    def run_fake(self,code=0,duration=20):
        def launch(*args,**kwargs):
            self.output.write_bytes(b'x'*150000)
            kwargs['stdout'].write(b'simulated aerender result\n')
            return SimpleNamespace(returncode=code,wait=lambda timeout:code,kill=lambda:None)
        with patch.object(bridge.subprocess,'Popen',side_effect=launch), \
             patch.object(bridge,'ae_running',return_value=False), \
             patch.object(bridge,'kill_leftovers'), \
             patch.object(bridge.time,'sleep'), \
             patch.object(bridge.ffmpeg,'probe',return_value=SimpleNamespace(duration=duration,fps=24)):
            return self.engine.render(self.project,self.output,expected_duration=20)

    def test_existing_file_cannot_be_accepted_as_new_render(self):
        self.output.write_bytes(b'old result')
        with patch.object(bridge.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(bridge.AEError,'уже существует'):
                self.engine.render(self.project,self.output)
            launch.assert_not_called()

    def test_nonzero_exit_rejects_even_large_output_and_preserves_log(self):
        with self.assertRaisesRegex(bridge.AEError,'кодом 7'):
            self.run_fake(code=7)
        self.assertEqual(len(list((self.folder/'logs').glob('render_*.log'))),1)
        self.assertFalse(self.output.with_suffix('.render.json').exists())

    def test_short_render_rejected_despite_zero_exit(self):
        with self.assertRaisesRegex(bridge.AEError,'Неполный рендер'):
            self.run_fake(duration=8)
        self.assertEqual(len(list((self.folder/'logs').glob('render_*.log'))),1)

    def test_success_has_receipt_and_correct_duration(self):
        self.assertEqual(self.run_fake(),self.output)
        receipt=json.loads(self.output.with_suffix('.render.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['returncode'],0)
        self.assertEqual(receipt['duration'],20)
        self.assertEqual(receipt['expected_duration'],20)
        self.assertFalse(list((self.folder/'logs').glob('render_*.log')))


if __name__=='__main__':unittest.main(verbosity=2)
