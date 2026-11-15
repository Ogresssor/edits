import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from edits.core import Store, sha256, validate_plan, save
from edits.analysis import require_coverage


def plan():
    return dict(id='story-01', source='movie.mkv', rating='strong', segments=[dict(start=1, end=3)],
                story={k:'Concrete editorial reason' for k in ('hook','development','payoff','context','selection_reason')},
                captions=[dict(start=0, end=1, text='Понятная реплика')])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root / 'work')
        self.file = self.root / 'video.mp4'; self.file.write_bytes(b'original export')
        self.digest = sha256(self.file)
        self.store.register(self.file, 'story-01', 'revision-a', dict(passed=True,sha256=self.digest))
        self.when = (datetime.now(timezone.utc)+timedelta(days=5)).replace(hour=6,minute=0,second=0,microsecond=0)

    def test_complete_plan(self):
        self.assertEqual(validate_plan(plan()), 2)

    def test_missing_payoff(self):
        p=plan(); del p['story']['payoff']
        with self.assertRaises(ValueError): validate_plan(p)

    def test_bad_segments(self):
        for segments in ([dict(start=float('nan'),end=4)], [dict(start=3,end=1)],
                         [dict(start=2,end=4),dict(start=3,end=5)]):
            p=plan(); p['segments']=segments
            with self.assertRaises(ValueError): validate_plan(p)

    def test_caption_injection(self):
        p=plan(); p['captions'][0]['text']=r'{\pos(0,0)}oops'
        with self.assertRaises(ValueError): validate_plan(p)

    def test_requires_approval(self):
        with self.assertRaises(ValueError): self.store.enqueue(self.digest,self.when)

    def test_requires_human_review(self):
        with self.assertRaises(ValueError): self.store.approve(self.digest,'user',False)

    def test_changed_export_invalidates_approval(self):
        self.store.approve(self.digest,'user',True); self.file.write_bytes(b'new export')
        with self.assertRaises(ValueError): self.store.enqueue(self.digest,self.when)

    def test_unique_story_and_time(self):
        self.store.approve(self.digest,'user',True); self.store.enqueue(self.digest,self.when)
        with self.assertRaises(sqlite3.IntegrityError): self.store.enqueue(self.digest,self.when)
        other=self.root/'other.mp4'; other.write_bytes(b'another revision'); digest=sha256(other)
        self.store.register(other,'story-01','b',dict(passed=True,sha256=digest)); self.store.approve(digest,'user',True)
        with self.assertRaises(sqlite3.IntegrityError): self.store.enqueue(digest,self.when+timedelta(days=1))

    def test_slot_must_be_moscow_and_future(self):
        self.store.approve(self.digest,'user',True)
        for when in [self.when.replace(tzinfo=None),self.when+timedelta(minutes=1),self.when-timedelta(days=10)]:
            with self.assertRaises(ValueError): self.store.enqueue(self.digest,when)

    def test_full_map_required(self):
        path=self.root/'map.json'
        save(path,dict(source_sha256='abc',complete_technical_scan=True,scenes=[dict(start=0,end=5)]))
        require_coverage(path,'abc',5)
        with self.assertRaises(ValueError): require_coverage(path,'abc',10)


if __name__ == '__main__': unittest.main()
