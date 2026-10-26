import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock
from src.db import DB
from src.config import Config
from src.media import MediaWorker

class ResourceTests(unittest.TestCase):
    def test_close_thread_keeps_other_connections(self):
        db=DB(Config()); own=Mock(); other=Mock()
        db._local.conn=own; db._all=[own,other]
        db.close_thread()
        own.close.assert_called_once(); other.close.assert_not_called()
        self.assertEqual(db._all,[other])
        db.close_thread()
        own.close.assert_called_once()

    def test_release_on_failure(self):
        db=Mock(); w=MediaWorker(Config(),db,None,None)
        w._one=Mock(side_effect=RuntimeError('fail'))
        with self.assertRaises(RuntimeError): w._one_and_release({})
        db.close_thread.assert_called_once()

    def test_backlog_has_round_limit(self):
        cfg=Config();cfg.media.max_items_per_round=3;cfg.media.workers=2
        db=Mock();db.reset_stuck_media.return_value=0;db.expire_stale_media.return_value=0
        db.claim_media.side_effect=lambda n: [{} for _ in range(n)]
        w=MediaWorker(cfg,db,None,None);w._one=Mock()
        w.drain()
        self.assertEqual(w._one.call_count,3)
        self.assertEqual([c.args[0] for c in db.claim_media.call_args_list],[2,1])
        self.assertEqual(db.close_thread.call_count,3)

    def test_stop_does_not_claim_new_work(self):
        import threading
        stop=threading.Event();stop.set()
        db=Mock();db.reset_stuck_media.return_value=0;db.expire_stale_media.return_value=0
        MediaWorker(Config(),db,None,None).drain(stop=stop)
        db.claim_media.assert_not_called()
