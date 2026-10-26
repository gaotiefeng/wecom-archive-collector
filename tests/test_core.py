import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from src.normalize import normalize
from src.puller import Puller
from src.sdk import ChatItem, SdkError
from src.storage import LocalStorage
from src.media import MediaWorker

class CoreTests(unittest.TestCase):
    def test_decrypt_failure_never_advances_cursor(self):
        db = Mock()
        db.get_last_seq.return_value = 0
        sdk = Mock()
        sdk.get_chat_data.return_value = [ChatItem(1, 'm', 2, 'key', 'cipher')]
        sdk.decrypt.side_effect = SdkError(10007, 'missing key')
        cfg = NS(wecom=NS(batch_limit=10))
        with self.assertRaises(SdkError):
            Puller(cfg, db, sdk).pull_batch()
        db.save_batch.assert_not_called()
        sdk.decrypt.assert_called_once_with('key', 'cipher', 2)

    def test_unknown_message_keeps_data(self):
        r = normalize({'msgtype':'future','future':{'value':42},'msgtime':1},1,'m')
        self.assertIn('42', r.raw)

    def test_composite_json_attachment(self):
        r = normalize({'msgtype':'mixed','msgtime':1,'mixed':{'item':[
            {'type':'image','content':'{"sdkfileid":"picture"}'}]}},1,'m',ttl_hours=0)
        self.assertEqual([x.file_id for x in r.media], ['picture'])
        self.assertEqual(r.media[0].deadline_at.year,9999)

    def test_partial_download_does_not_publish_file(self):
        with tempfile.TemporaryDirectory() as root:
            store = LocalStorage(Path(root))
            def chunks():
                yield b'partial'
                raise OSError('offline')
            with self.assertRaises(OSError):
                store.save('image.bin', chunks())
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_integrity_failure_is_retried(self):
        with tempfile.TemporaryDirectory() as root:
            sdk = Mock()
            sdk.get_media.return_value = iter([b'truncated'])
            db = Mock()
            worker = MediaWorker(NS(media=NS(max_attempts=8)), db, LocalStorage(Path(root)), lambda:sdk)
            worker._one({'id':1,'attempts':0,'msg_time':datetime.now(),'msg_id':'m',
                         'filename':'p.jpg','kind':'image','file_id':'f',
                         'file_size':100,'file_md5':None})
            db.finish_media.assert_not_called()
            db.fail_media.assert_called_once()
            self.assertFalse(any(Path(root).rglob('*.jpg')))

    def test_binary_nul_bytes_survive(self):
        with tempfile.TemporaryDirectory() as root:
            data = b'\x00image\x00'
            key, md5, size = LocalStorage(Path(root)).save('a',iter([data]))
            self.assertEqual((Path(root)/key).read_bytes(),data)
            self.assertEqual(md5,hashlib.md5(data).hexdigest())
            self.assertEqual(size,len(data))

if __name__ == '__main__':
    unittest.main()
