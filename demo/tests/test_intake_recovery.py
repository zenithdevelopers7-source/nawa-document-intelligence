from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nawa_demo.intake_jobs import IntakeJobs
from nawa_demo.intake_pipeline import SyntheticIntakePipeline
from nawa_demo.intake_recovery import inspect_storage
from nawa_demo.intake_store import LocalIntakeStore


class RecoveryTests(unittest.TestCase):
    def test_unreferenced_identifier_symlink_is_reported_and_preserved(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = LocalIntakeStore(root / 'files')
            jobs = IntakeJobs(root / 'jobs.db')
            name = 'b' * 32
            link = store.directory / name
            try:
                link.symlink_to(root / 'missing-target')
            except (OSError, NotImplementedError):
                self.skipTest('Creating symlinks is unavailable')
            report = inspect_storage(store, jobs)
            self.assertEqual(report['unsafe_entries'], [name])
            self.assertTrue(link.is_symlink())

    def test_unreferenced_identifier_directory_is_reported_and_preserved(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = LocalIntakeStore(root / 'files')
            jobs = IntakeJobs(root / 'jobs.db')
            name = 'a' * 32
            (store.directory / name).mkdir()
            report = inspect_storage(store, jobs)
            self.assertEqual(report['unsafe_entries'], [name])
            self.assertEqual(report['orphaned_files'], [])
            self.assertTrue((store.directory / name).is_dir())

    def test_offline_inventory_reports_discrepancies_without_deletion(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = LocalIntakeStore(root / 'files')
            jobs = IntakeJobs(root / 'jobs.db')
            pipeline = SyntheticIntakePipeline(store, jobs)
            good, _ = pipeline.submit('a', 'good.txt', b'good')
            missing, _ = pipeline.submit('a', 'missing.txt', b'missing')
            corrupt, _ = pipeline.submit('a', 'corrupt.txt', b'original')
            orphan, _ = store.save('orphan.txt', b'leftover')
            (store.directory / missing).unlink()
            (store.directory / corrupt).write_bytes(b'changed')
            (store.directory / 'unexpected').write_bytes(b'leave alone')
            report = inspect_storage(store, jobs)
            self.assertEqual(report['orphaned_files'], [orphan])
            self.assertEqual(report['missing_files'], [missing])
            self.assertEqual(report['corrupt_files'], [corrupt])
            self.assertEqual(report['unrecognized_entries'], ['unexpected'])
            self.assertTrue((store.directory / orphan).exists())
            self.assertTrue((store.directory / good).exists())


if __name__ == '__main__':
    unittest.main()
