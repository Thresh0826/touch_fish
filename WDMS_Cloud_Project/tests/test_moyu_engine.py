from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from errors import WDMSError
from moyu_tools.files import inspect_zip, publish
from moyu_tools.model import Plan, Policy, zip_name
from moyu_tools.store import Store
from tests.moyu_fixtures import environment, profile, zip_bytes


class MoyuModelTests(unittest.TestCase):
    def test_default_118_inclusive_contiguous_segments_cover_leap_year(self):
        segments = list(Plan().segments())
        self.assertEqual(len(segments), 118)
        self.assertEqual(segments[0].start, date(2023, 11, 1))
        self.assertEqual(segments[-1].start, date(2026, 1, 28))
        self.assertEqual(segments[-1].end, date(2026, 1, 31))
        days = [s.start+timedelta(days=i) for s in segments for i in range((s.end-s.start).days+1)]
        self.assertEqual(len(days), 823)
        self.assertEqual(len(set(days)), 823)
        self.assertIn(date(2024, 2, 29), days)
        self.assertTrue(all(b == a+timedelta(days=1) for a, b in zip(days, days[1:])))

    def test_single_day_and_date_max(self):
        self.assertEqual(len(list(Plan(date.max, date.max).segments())), 1)

    def test_invalid_dates_and_span(self):
        for args in [(date(2025, 1, 2), date(2025, 1, 1), 7), (date.today(), date.today(), True),
                     (date.today(), date.today(), 0)]:
            with self.subTest(args=args), self.assertRaises(WDMSError):
                Plan(*args)

    def test_invalid_budgets_and_initial_wait_not_timeout(self):
        self.assertEqual(Policy().initial_wait, 30)
        self.assertEqual(Policy().query_timeout, 600)
        for options in [dict(initial_wait=float('nan')), dict(copy_timeout=True), dict(query_timeout=30), dict(poll=6)]:
            with self.subTest(options=options), self.assertRaises(WDMSError):
                Policy(**options)

    def test_filename_preserves_actual_numeric_name_and_rejects_traversal(self):
        self.assertEqual(zip_name("00999.zip"), "00999.zip")
        for name in ("../x.zip", r"..\x.zip", "C:x.zip", "x.zip:ads", "CON.zip", "x.exe", "x.zip "):
            with self.subTest(name=name), self.assertRaises(WDMSError):
                zip_name(name)


class MoyuEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.e, self.s, self.q, self.r, self.c, self.out = environment(self.tmp.name)

    def test_real_zip_trial_observes_busy_and_stability_and_preserves_leading_zero(self):
        self.assertEqual(self.e.run(trial=True), "trial_passed")
        self.assertGreaterEqual(self.c(), 4)
        self.assertEqual(self.q.clicks, 1)
        self.assertEqual(self.r.searches, ["001234"])
        self.assertTrue((self.out/"987654.zip").exists())
        row = self.s.value["segments"][0]
        self.assertEqual(row["file"], inspect_zip(self.out/"987654.zip"))
        self.assertEqual(row["phase"], "done")

    def test_full_run_requires_actual_zip_trial_before_any_query(self):
        with self.assertRaisesRegex(WDMSError, "单段试跑"):
            self.e.run()
        self.assertEqual(self.q.clicks, 0)

    def test_empty_result_only_completes_after_busy_cycle(self):
        self.q.count = 0
        self.assertEqual(self.e.run(trial=True), "trial_empty")
        self.assertEqual(self.s.value["segments"][0]["phase"], "empty")
        self.assertFalse(self.s.value["trial_passed"])
        self.assertEqual(self.r.searches, [])

    def test_idle_unchanged_old_result_never_confirms(self):
        self.q.never_busy = True
        with self.assertRaises(WDMSError) as caught:
            self.e.run(trial=True)
        self.assertEqual(caught.exception.code, "STEP_TIMEOUT")
        self.assertEqual(self.q.clicks, 1)
        self.assertFalse(self.r.searches)
        self.assertEqual(self.s.value["segments"][0]["phase"], "querying")

    def test_slow_query_continues_beyond_initial_wait(self):
        self.q.busy_seconds = 6
        self.e.run(trial=True)
        self.assertGreaterEqual(self.c(), 7)
        self.assertEqual(self.q.clicks, 1)

    def test_readonly_timeout_restarts_observation_without_requery(self):
        original = self.q.snapshot
        failed = [False]
        def snapshot(segment, context):
            if not failed[0]:
                failed[0] = True
                raise WDMSError('MOYU_READ_PENDING', 'synthetic read timeout')
            return original(segment, context)
        with patch.object(self.q, 'snapshot', side_effect=snapshot):
            self.e.run(trial=True)
        self.assertEqual(self.q.clicks, 1)
        self.assertEqual(len(self.r.pastes), 1)

    def test_wall_clock_jump_does_not_shorten_minimum_wait(self):
        self.e.wall = lambda: 10**12
        self.e.run(trial=True)
        self.assertGreaterEqual(self.c(), self.e.policy.initial_wait+self.e.policy.settle)

    def test_increased_initial_wait_on_resume_does_not_reuse_shorter_wait_as_full(self):
        from dataclasses import replace
        self.s.update(0, 'querying', busy_seen=True, submitted_wall=0, initial_wait_seconds=3)
        self.q.started = -100
        self.e.policy = replace(self.e.policy, initial_wait=6)
        self.e.run(trial=True)
        self.assertGreaterEqual(self.c(), 4)
        self.assertEqual(self.q.clicks, 0)

    def test_changed_other_query_conditions_pause_before_remote_search(self):
        with patch.object(self.q, 'guard_conditions', side_effect=WDMSError('MOYU_CONDITIONS_CHANGED', 'changed')):
            with self.assertRaises(WDMSError):
                self.e.run(trial=True)
        self.assertFalse(self.r.searches)

    def test_different_numbers_can_reuse_same_task_file_only_after_checksum_match(self):
        plan = Plan(date(2025, 1, 1), date(2025, 1, 14), 7)
        self.e, self.s, self.q, self.r, self.c, self.out = environment(Path(self.tmp.name)/'different', plan)
        self.e.run(trial=True)
        self.q.number = '005678'
        self.e.run()
        self.assertEqual(self.r.searches, ['001234', '005678'])
        self.assertTrue(self.s.value['segments'][1]['reused'])

    def test_window_response_does_not_replace_busy_evidence(self):
        self.q.never_busy = True
        self.q.never_responds = False
        with self.assertRaises(WDMSError):
            self.e.run(trial=True)
        self.assertFalse(self.r.pastes)

    def test_unresponsive_window_never_reads_cached_result(self):
        self.q.never_responds = True
        with patch.object(self.q, "snapshot", wraps=self.q.snapshot) as snapshot:
            with self.assertRaises(WDMSError):
                self.e.run(trial=True)
            snapshot.assert_not_called()

    def test_multiple_rows_pause_without_search(self):
        self.q.count = 2
        with self.assertRaises(WDMSError) as caught:
            self.e.run(trial=True)
        self.assertEqual(caught.exception.code, "MOYU_MULTIPLE_ROWS")
        self.assertEqual(self.r.searches, [])

    def test_incomplete_or_labelled_number_never_searches(self):
        for number in ("", "送货单号001234", "001234 567", None):
            with self.subTest(number=number):
                self.q.number = number
                self.s.update(0, "pending")
                with self.assertRaises(WDMSError):
                    self.e.run(trial=True)
        self.assertEqual(self.r.searches, [])

    def test_cancel_during_query_keeps_interval_and_never_reclicks_on_resume(self):
        self.e.cancelled = lambda: self.c() > 0.5
        with self.assertRaises(WDMSError):
            self.e.run(trial=True)
        self.e.cancelled = lambda: False
        self.e.run(trial=True)
        self.assertEqual(self.q.clicks, 1)

    def test_unknown_submission_requires_explicit_human_recovery(self):
        self.s.update(0, "query_intent", busy_seen=False, submitted_wall=0)
        self.q.started = -100
        with self.assertRaises(WDMSError) as caught:
            self.e.run(trial=True)
        self.assertEqual(caught.exception.code, "MOYU_QUERY_UNCERTAIN")
        self.assertEqual(self.q.clicks, 0)

    def test_saving_intent_fails_before_click(self):
        original = self.s.update
        def update(index, phase, **fields):
            if phase == "query_intent":
                raise OSError("synthetic disk full")
            return original(index, phase, **fields)
        with patch.object(self.s, "update", side_effect=update), self.assertRaises(OSError):
            self.e.run(trial=True)
        self.assertEqual(self.q.clicks, 0)

    def test_restart_after_paste_reconciles_without_second_search_or_paste(self):
        def interrupt():
            raise WDMSError("TASK_CANCELLED", "synthetic interruption after paste")
        self.r.after_paste = interrupt
        with self.assertRaises(WDMSError):
            self.e.run(trial=True)
        self.assertEqual(self.s.value["segments"][0]["phase"], "copy_intent")
        # Simulate process restart by loading the actual atomic disk journal.
        loaded = Store(self.s.folder)
        loaded.load_or_create(self.e.plan, self.out, "synthetic-binding")
        self.e.store = loaded
        self.e.run(trial=True)
        self.assertEqual(len(self.r.searches), 1)
        self.assertEqual(len(self.r.pastes), 1)

    def test_restart_after_publication_reconciles_existing_verified_receipt(self):
        def crash(*args, **kwargs):
            publish(*args, **kwargs)
            raise WDMSError("SYNTHETIC_CRASH", "after publication")
        with patch('moyu_tools.engine.publish', side_effect=crash), self.assertRaises(WDMSError):
            self.e.run(trial=True)
        self.assertEqual(self.s.value["segments"][0]["phase"], "verified")
        self.e.run(trial=True)
        self.assertEqual(len(self.r.pastes), 1)

    def test_unrelated_existing_name_is_not_overwritten_even_if_valid_zip(self):
        old = zip_bytes(b"old delivery")
        (self.out/self.r.name).write_bytes(old)
        with self.assertRaises(WDMSError) as caught:
            self.e.run(trial=True)
        self.assertEqual(caught.exception.code, "MOYU_NAME_CONFLICT")
        self.assertEqual((self.out/self.r.name).read_bytes(), old)

    def test_corrupt_crc_never_publishes(self):
        data = bytearray(self.r.data)
        start = 30+len("synthetic.txt")
        data[start] ^= 1
        self.r.data = bytes(data)
        with self.assertRaises(WDMSError):
            self.e.run(trial=True)
        self.assertFalse((self.out/self.r.name).exists())

    def test_partial_copy_is_waited_for_and_not_repeated(self):
        data = self.r.data
        def paste(folder, selected, context):
            path = folder/selected['name']
            path.write_bytes(data[:20])
            ready = self.c()+1
            self.c.hooks.append(lambda now: path.write_bytes(data) if now >= ready and path.stat().st_size < len(data) else None)
            self.r.pastes.append(folder)
        with patch.object(self.r, 'paste', side_effect=paste):
            self.e.run(trial=True)
        self.assertEqual(len(self.r.pastes), 1)
        self.assertEqual((self.out/self.r.name).read_bytes(), data)

    def test_known_duplicate_number_reuses_verified_task_file(self):
        plan = Plan(date(2025, 1, 1), date(2025, 1, 14), 7)
        self.e, self.s, self.q, self.r, self.c, self.out = environment(Path(self.tmp.name)/"two", plan)
        self.e.run(trial=True)
        self.e.run()
        self.assertEqual(self.q.clicks, 2)
        self.assertEqual(len(self.r.pastes), 1)
        self.assertTrue(self.s.value['segments'][1]['reused'])

    def test_changed_completed_file_blocks_resume_before_further_query(self):
        self.e.run(trial=True)
        (self.out/self.r.name).write_bytes(zip_bytes(b"changed"))
        with self.assertRaises(WDMSError):
            self.e.run()
        self.assertEqual(self.q.clicks, 1)

    def test_crash_after_done_before_trial_flag_reconciles_without_new_query(self):
        plan = Plan(date(2025, 1, 1), date(2025, 1, 14), 7)
        self.e, self.s, self.q, self.r, self.c, self.out = environment(Path(self.tmp.name)/'gate', plan)
        self.e.run(trial=True)
        self.s.value.update(trial_passed=False, status='running')
        self.s.commit()
        self.assertEqual(self.e.run(trial=True), 'trial_passed')
        self.assertEqual(self.q.clicks, 1)


    def test_changed_identity_and_corrupt_progress_refused(self):
        with self.assertRaises(WDMSError):
            Store(self.s.folder).load_or_create(self.e.plan, self.out, "new binding")
        self.s.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(WDMSError):
            Store(self.s.folder).load_or_create(self.e.plan, self.out, "synthetic-binding")

    def test_rebind_cannot_unlock_new_layout_using_only_old_done_receipt(self):
        plan=Plan(date(2025,1,1),date(2025,1,14))
        self.e,self.s,self.q,self.r,self.c,self.out=environment(Path(self.tmp.name)/'rebind',plan)
        self.e.run(trial=True)
        self.s.rebind('new-binding')
        self.e.run(trial=True)
        self.assertEqual(self.q.clicks,2)
        self.assertEqual(len(self.r.searches),2)
        self.assertEqual(len(self.r.pastes),2)
        self.assertTrue(self.s.value['trial_passed'])

    def test_copy_reconciliation_after_rebind_does_not_pretend_new_query_trial_passed(self):
        plan=Plan(date(2025,1,1),date(2025,1,14))
        self.e,self.s,self.q,self.r,self.c,self.out=environment(Path(self.tmp.name)/'rebound-copy',plan)
        def stop():
            raise WDMSError('TASK_CANCELLED','synthetic after paste')
        self.r.after_paste=stop
        with self.assertRaises(WDMSError):
            self.e.run(trial=True)
        self.r.after_paste=None
        self.s.rebind('new-binding')
        self.assertEqual(self.e.run(trial=True),'trial_pending')
        self.assertFalse(self.s.value['trial_passed'])
        self.assertEqual(self.q.clicks,1)
        self.assertEqual(len(self.r.pastes),1)
        self.assertEqual(self.e.run(trial=True),'trial_passed')

    def test_corrupt_interrupted_copy_can_be_archived_and_explicitly_retried(self):
        from dataclasses import asdict
        from moyu_tools.runtime import rearm
        from moyu_tools.settings import profile_digest
        p=profile()
        self.e,self.s,self.q,self.r,self.c,self.out=environment(Path(self.tmp.name)/'retry',binding_digest=profile_digest(p),
                                                            condition_signature=p['wdms']['conditions_hash'])
        self.r.data=b'not a complete zip'
        with self.assertRaises(WDMSError):
            self.e.run(trial=True)
        config={'plan':self.e.plan.document(),'policy':asdict(self.e.policy),'output':str(self.out),'task':str(self.s.folder)}
        with patch.dict('os.environ',{'LOCALAPPDATA':str(Path(self.tmp.name)/'state')}):
            rearm(config,p)
        self.e.store=Store(self.s.folder)
        self.e.store.load_or_create(self.e.plan,self.out,profile_digest(p),p['wdms']['conditions_hash'])
        self.r.data=zip_bytes()
        self.assertEqual(self.e.run(trial=True),'trial_passed')
        self.assertEqual(self.q.clicks,1)
        self.assertEqual(len(self.r.pastes),2)
        backup=Path(self.e.store.value['segments'][0]['recovery_backups'][-1])
        self.assertEqual((backup/self.r.name).read_bytes(),b'not a complete zip')

    def test_progress_write_is_atomic_and_retains_last_completed_interval(self):
        self.e.run(trial=True)
        data = json.loads(self.s.path.read_text(encoding='utf-8'))
        self.assertTrue(data['trial_passed'])
        self.assertEqual(data['segments'][0]['phase'], 'done')
        self.assertFalse(list(self.s.folder.glob('*.tmp')))
