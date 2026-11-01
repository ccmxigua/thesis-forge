"""An unsuccessful restore must never destroy its only recovery copy."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import artifact_io as io


class RollbackRecoveryTests(unittest.TestCase):
    def test_second_failure_keeps_original_backup_and_restores_other_output(self):
        with tempfile.TemporaryDirectory() as directory:
            r=Path(directory);out=r/'out.docx';report=r/'report.json';a=r/'new.docx';b=r/'new.json'
            out.write_bytes(b'old docx');report.write_bytes(b'old report');a.write_bytes(b'new docx');b.write_bytes(b'new report')
            real=io.os.replace
            def fail(src,dst):
                if Path(src)==b or (str(src).endswith('.backup') and Path(dst)==out):
                    raise OSError('injected publication/restore failure')
                return real(src,dst)
            with patch.object(io.os,'replace',side_effect=fail):
                with self.assertRaises(io.ArtifactRollbackError) as caught:io.commit_files([(a,out),(b,report)])
            self.assertEqual(report.read_bytes(),b'old report')
            recovery=caught.exception.recovery_backups
            self.assertIn(str(out),recovery);self.assertEqual(Path(recovery[str(out)]).read_bytes(),b'old docx')
            self.assertIn('rollback_failed',str(caught.exception))

    def test_ordinary_failure_restores_all_old_bytes_without_leftover_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            r=Path(directory);out=r/'out';report=r/'report';a=r/'a';b=r/'b'
            for p,data in [(out,b'old1'),(report,b'old2'),(a,b'new1'),(b,b'new2')]:p.write_bytes(data)
            real=io.os.replace
            def fail(src,dst):
                if Path(src)==b:raise OSError('one failure')
                return real(src,dst)
            with patch.object(io.os,'replace',side_effect=fail):
                with self.assertRaises(OSError):io.commit_files([(a,out),(b,report)])
            self.assertEqual(out.read_bytes(),b'old1');self.assertEqual(report.read_bytes(),b'old2')
            self.assertFalse(list(r.glob('*.backup')))
