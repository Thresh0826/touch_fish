"""Screenshot geometry, fresh single-result searches and direct-copy recovery."""
import copy
from dataclasses import asdict
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.step_executor import StepContext
from errors import WDMSError
from moyu_tools.calibration import automatic, canvas_rect, fill_derived, refresh_marks, screen_point, screen_rect, target_at
from moyu_tools.direct_copy import inspect_target, target_matches, wait_target
from moyu_tools.model import Plan, Policy
from moyu_tools.engine import Engine
from moyu_tools.native import RemoteFiles, single_zip_result
from moyu_tools.runtime import make_request, rearm, verify_child, worker
from moyu_tools.settings import load_json, profile_digest, state_root, validate_profile
from moyu_tools.store import Store
from tests.moyu_fixtures import Clock, environment, sheet_profile, zip_bytes
from tests.test_moyu_native import VirtualRemote, regions
from tests.test_moyu_sheet_rows import NamedRemote, Page, row_plan


def simple_profile():
    value = sheet_profile()
    value["schema"] = 3
    value["remote"].pop("count")
    value["remote"]["mode"] = "single_zip"
    return value


class SimpleRemote(VirtualRemote):
    def __init__(self, clock):
        super().__init__(clock)
        self.keyword = "previous"
        self.filename = "unrelated_archive.zip"
        self.typed = None
        self.delay = 0
        self.unchanged_result = False
        self.multiple = False

    def type_text(self, root, text, context):
        self.typed = self.clock()
        super().type_text(root, text, context)

    def call(self, action, payload=None, context=None):
        if action == "ocr":
            self.actions.append(action)
            self.clock.sleep(.05)
            context.check()
            name = "old.zip" if self.typed is None or self.clock()-self.typed < self.delay or self.unchanged_result else self.filename
            return {"regions": regions(1, name)[:1]}
        if action == "file" and self.multiple:
            raise WDMSError("MOYU_CLIPBOARD_MULTIPLE", "synthetic multiple results")
        return super().call(action, payload, context)


class SnapshotGeometryTests(unittest.TestCase):
    def snapshot(self):
        value = simple_profile()
        controls = copy.deepcopy(list(value["wdms"][key] for key in ("start", "end", "contract_label", "contract", "query", "count_label", "count")))
        for item, text in zip(controls, ("", "", "合同编号", "", "查询(F)", "总行数", "1")):
            item["text"] = text
        controls[0]["class_name"] = controls[1]["class_name"] = "SysDateTimePick32"
        controls.append({**copy.deepcopy(value["wdms"]["query"]), "text": "查询条件", "rect": [10, 110, 875, 230]})
        return {"root": value["wdms"]["root"], "source_rect": [0, 0, 900, 700], "width": 450, "height": 350, "controls": controls}

    def test_scaled_negative_monitor_origin_and_reverse_drag(self):
        snap = {"source_rect": [-900, 120, 900, 1020], "width": 900, "height": 450}
        self.assertEqual(screen_point(snap, 450, 225), [0, 570])
        rect = screen_rect(snap, (500, 300), (100, 50))
        self.assertEqual(rect, [-700, 220, 100, 720])
        self.assertEqual(canvas_rect(snap, rect), [100, 50, 500, 300])
        for point in ((-1, 0), (900, 0), (0, 450)):
            with self.subTest(point=point), self.assertRaises(WDMSError):
                screen_point(snap, *point)

    def test_tiny_or_outside_regions_do_not_replace_a_selection(self):
        snap = self.snapshot()
        with self.assertRaises(WDMSError):
            screen_rect(snap, (10, 10), (11, 11))

    def test_native_candidates_keep_contract_number_distinct_from_other_identifiers(self):
        snap = self.snapshot()
        before = copy.deepcopy(snap)
        found = automatic(snap)
        self.assertEqual(found["contract"]["hwnd"], 109)
        self.assertEqual(found["start"]["hwnd"], 101)
        self.assertEqual(found["end"]["hwnd"], 102)
        self.assertEqual(found["count"]["hwnd"], 105)
        self.assertEqual(snap, before)
        self.assertIn("conditions", found)

    def test_ambiguous_contract_labels_remain_unselected(self):
        snap = self.snapshot()
        duplicate = copy.deepcopy(snap["controls"][2])
        duplicate["hwnd"] += 1000
        snap["controls"].append(duplicate)
        self.assertNotIn("contract", automatic(snap))

    def test_manual_target_chooses_smallest_containing_native_control(self):
        snap = self.snapshot()
        point = snap["controls"][3]["point"]
        self.assertEqual(target_at(snap, point)["hwnd"], 109)

    def test_refresh_keeps_manual_positions_but_rebinds_recreated_child_handles(self):
        snap = self.snapshot()
        old = {"contract": copy.deepcopy(snap["controls"][3]), "conditions": [10, 110, 875, 230]}
        snap["controls"][3]["hwnd"] = 999
        refreshed = refresh_marks(snap, old)
        self.assertEqual(refreshed["contract"]["hwnd"], 999)
        self.assertEqual(refreshed["contract"]["point"], old["contract"]["point"])
        self.assertEqual(old["contract"]["hwnd"], 109)

    def test_header_and_search_marking_derive_cell_and_static_band(self):
        snap = self.snapshot()
        header = {**copy.deepcopy(snap["controls"][0]), "point": [100, 340], "accessible_rect": [50, 330, 150, 350]}
        found = fill_derived(snap, {"header": header})
        self.assertEqual(found["cell"]["point"], [100, 360])
        found = fill_derived(snap, {"search": [20, 70, 400, 95]})
        self.assertEqual(found["anchor"], [20, 42, 200, 68])

    def test_simple_binding_requires_no_remote_count_rectangle(self):
        value = simple_profile()
        self.assertIs(validate_profile(value), value)
        value["remote"].pop("anchor")
        with self.assertRaises(WDMSError):
            validate_profile(value)


class SingleSearchTests(unittest.TestCase):
    def run_search(self, driver, seconds=30):
        remote = RemoteFiles(driver, simple_profile())
        with patch("moyu_tools.native.time.monotonic", driver.clock), patch("moyu_tools.native.time.sleep", driver.clock.sleep):
            return remote.search("001234", StepContext(seconds, lambda: False, driver.clock))

    def test_one_real_keyword_and_actual_different_filename(self):
        driver = SimpleRemote(Clock())
        selected = self.run_search(driver)
        self.assertEqual([x for x in driver.inputs if isinstance(x, str)], ["001234"])
        self.assertEqual(selected["name"], "unrelated_archive.zip")
        self.assertFalse(driver.zero_seen)

    def test_initial_many_files_need_no_dummy_search_or_manual_preparation(self):
        driver = SimpleRemote(Clock())
        original = driver.call
        def call(action, payload=None, context=None):
            if action == "ocr" and driver.typed is None:
                region = regions(1, "old.zip")[0]
                region["lines"].append({"text": "another.zip", "rect": [0, 20, 90, 38]})
                region["text"] += "\nanother.zip"
                return {"regions": [region]}
            return original(action, payload, context)
        driver.call = call
        selected = self.run_search(driver)
        self.assertEqual(selected["name"], driver.filename)
        self.assertEqual([x for x in driver.inputs if isinstance(x, str)], ["001234"])

    def test_empty_search_text_without_a_clipboard_sequence_change_is_accepted_as_baseline_only(self):
        driver = SimpleRemote(Clock())
        driver.keyword = ""
        original = driver.chord
        def chord(root, key, context, modifier=0x11):
            if key == 0x43 and driver.selection == "search" and driver.keyword == "":
                driver.inputs.append(key)
                return
            original(root, key, context, modifier)
        driver.chord = chord
        self.assertEqual(self.run_search(driver)["name"], driver.filename)

    def test_old_single_zip_must_change_after_different_keyword(self):
        driver = SimpleRemote(Clock())
        driver.delay = 5
        self.run_search(driver)
        self.assertGreater(driver.clock()-driver.typed, 7)

    def test_never_refreshed_old_result_times_out_without_copying_a_file(self):
        driver = SimpleRemote(Clock())
        driver.unchanged_result = True
        with self.assertRaises(WDMSError):
            self.run_search(driver, 9)
        self.assertNotIn("file", driver.actions)
        self.assertNotIn(0x56, driver.inputs)

    def test_multiple_clipboard_files_stop_without_paste(self):
        driver = SimpleRemote(Clock())
        driver.multiple = True
        with self.assertRaises(WDMSError) as caught:
            self.run_search(driver)
        self.assertEqual(caught.exception.code, "MOYU_CLIPBOARD_MULTIPLE")
        self.assertNotIn(0x56, driver.inputs)

    def test_empty_and_multiple_visible_results_are_not_treated_as_one_zip(self):
        self.assertEqual(single_zip_result(regions(0)[:1]), {})
        many = regions(1)[:1]
        many[0]["lines"] *= 2
        self.assertIsNone(single_zip_result(many))

    def test_same_name_conflict_is_checked_again_at_local_paste(self):
        with tempfile.TemporaryDirectory() as folder:
            driver = SimpleRemote(Clock())
            target = Path(folder)/driver.filename
            target.write_bytes(b"already here")
            selected = {"name": driver.filename, "size": 123, "sequence": 1}
            with self.assertRaises(WDMSError) as caught:
                RemoteFiles(driver, simple_profile()).paste(Path(folder), selected, StepContext(20, lambda: False, driver.clock))
            self.assertEqual(caught.exception.code, "MOYU_NAME_CONFLICT")
            self.assertNotIn(0x56, driver.inputs)
            self.assertEqual(target.read_bytes(), b"already here")

    def test_direct_paste_reuses_the_already_open_destination_window(self):
        with tempfile.TemporaryDirectory() as folder:
            driver = SimpleRemote(Clock())
            selected = {"name": driver.filename, "size": 123, "sequence": 1}
            RemoteFiles(driver, simple_profile()).paste(Path(folder), selected, StepContext(20, lambda: False, driver.clock))
            self.assertNotIn("open_folder", driver.actions)
            self.assertEqual(driver.inputs.count(0x56), 1)

    def test_duplicate_destination_windows_pause_without_opening_another(self):
        with tempfile.TemporaryDirectory() as folder:
            driver = SimpleRemote(Clock())
            original = driver.call
            def call(action, payload=None, context=None):
                result = original(action, payload, context)
                if action == "folder":
                    result["windows"] *= 2
                return result
            driver.call = call
            selected = {"name": driver.filename, "size": 123, "sequence": 1}
            with self.assertRaises(WDMSError):
                RemoteFiles(driver, simple_profile()).paste(Path(folder), selected, StepContext(20, lambda: False, driver.clock))
            self.assertNotIn("open_folder", driver.actions)
            self.assertNotIn(0x56, driver.inputs)


class DirectCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.engine, _, self.query, self.remote, self.clock, self.output = environment(self.folder)
        self.store = Store(self.folder/"direct")
        self.store.load_or_create(self.engine.plan, self.output, "synthetic-binding", copy_mode="direct")
        self.engine.store = self.store

    def test_zip_goes_straight_to_output_and_no_staging_is_created(self):
        self.assertEqual(self.engine.run(trial=True), "trial_passed")
        self.assertEqual([path.resolve() for path in self.remote.pastes], [self.output.resolve()])
        self.assertTrue((self.output/self.remote.name).exists())
        self.assertFalse((self.output/".moyu-staging").exists())
        self.assertEqual(self.store.value["segments"][0]["phase"], "done")

    def test_cancel_after_paste_then_resume_never_requeries_or_repastes(self):
        def stop():
            raise WDMSError("TASK_CANCELLED", "synthetic stop after paste")
        self.remote.after_paste = stop
        with self.assertRaises(WDMSError):
            self.engine.run(trial=True)
        self.assertEqual(self.store.value["segments"][0]["phase"], "copy_intent")
        self.remote.after_paste = None
        self.assertEqual(self.engine.run(trial=True), "trial_passed")
        self.assertEqual(self.query.clicks, 1)
        self.assertEqual(len(self.remote.pastes), 1)

    def test_ambiguous_paste_with_no_file_waits_and_does_not_paste_again(self):
        self.remote.paste = lambda folder, selected, context: None
        with self.assertRaises(WDMSError):
            self.engine.run(trial=True)
        self.assertEqual(self.store.value["segments"][0]["phase"], "copying")
        calls = []
        self.remote.paste = lambda *args: calls.append(args)
        with self.assertRaises(WDMSError):
            self.engine.run(trial=True)
        self.assertFalse(calls)
        self.assertEqual(self.query.clicks, 1)

    def test_existing_unknown_filename_is_never_overwritten(self):
        target = self.output/self.remote.name
        target.write_bytes(b"existing")
        with self.assertRaises(WDMSError) as caught:
            self.engine.run(trial=True)
        self.assertEqual(caught.exception.code, "MOYU_NAME_CONFLICT")
        self.assertFalse(self.remote.pastes)
        self.assertEqual(target.read_bytes(), b"existing")

    def test_other_files_in_destination_do_not_make_the_copy_ambiguous(self):
        (self.output/"other.zip").write_bytes(zip_bytes())
        self.assertEqual(self.engine.run(trial=True), "trial_passed")
        self.assertEqual(len(list(self.output.glob("*.zip"))), 2)

    def test_direct_task_cannot_be_opened_as_old_staged_progress(self):
        before = self.store.path.read_bytes()
        with self.assertRaises(WDMSError):
            Store(self.store.folder).load_or_create(self.engine.plan, self.output, "synthetic-binding")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_repeated_number_reuses_only_a_task_record_with_matching_file(self):
        plan = Plan(date(2024, 1, 1), date(2024, 1, 2), 1)
        store = Store(self.folder/"two-days")
        store.load_or_create(plan, self.output, "synthetic-binding", copy_mode="direct")
        self.engine.plan, self.engine.store = plan, store
        self.engine.run(trial=True)
        self.assertEqual(self.engine.run(), "completed")
        self.assertEqual(len(self.remote.pastes), 1)
        self.assertEqual(len(self.remote.searches), 1)

    def test_incomplete_zip_does_not_become_done_after_stable_size(self):
        target = self.output/"partial.zip"
        target.write_bytes(b"PK\x03\x04partial")
        with self.assertRaises(WDMSError):
            wait_target(target, None, StepContext(2, lambda: False, self.clock), clock=self.clock,
                        sleep=self.clock.sleep, stable_seconds=.2)

    def test_single_validation_pass_produces_receipt_and_detects_changed_file(self):
        target = self.output/"ready.zip"
        target.write_bytes(zip_bytes())
        receipt = inspect_target(target)
        self.assertTrue(target_matches(target, receipt))
        target.write_bytes(zip_bytes(b"changed"))
        self.assertFalse(target_matches(target, receipt))

    def test_write_handle_still_open_cannot_be_marked_complete_by_size_alone(self):
        target = self.output/"busy.zip"
        target.write_bytes(zip_bytes())
        with patch("moyu_tools.direct_copy.completed_reader", side_effect=PermissionError("synthetic active writer")):
            with self.assertRaises(WDMSError):
                wait_target(target, target.stat().st_size, StepContext(2, lambda: False, self.clock),
                            clock=self.clock, sleep=self.clock.sleep, stable_seconds=.2)


class SimpleRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name).resolve()
        self.env = patch.dict("os.environ", {"LOCALAPPDATA": str(self.folder/"state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = self.folder/"output"
        self.output.mkdir()
        self.config = {"plan": row_plan().document(), "policy": asdict(Policy(initial_wait=3, settle=1, query_timeout=15)),
                       "output": str(self.output), "task": ""}

    def test_old_settings_and_portable_binding_are_not_overwritten_by_new_mode(self):
        old = self.folder/"state"/"MoyuTools"/"sheet_rows"
        old.mkdir(parents=True)
        (old/"settings.json").write_bytes(b"synthetic previous settings")
        path, request = make_request(self.config, simple_profile(), trial=True)
        self.assertEqual((old/"settings.json").read_bytes(), b"synthetic previous settings")
        self.assertEqual(state_root().name, "sheet_rows_simple")
        self.assertIn(state_root(), path.parents)
        self.assertEqual(request["profile"]["schema"], 3)

    def test_old_staging_progress_is_rejected_before_new_request_files(self):
        task = self.folder/"old-task"
        Store(task).load_or_create(row_plan(), self.output, "old-binding")
        before = (task/"progress.json").read_bytes()
        self.config["task"] = str(task)
        with self.assertRaises(WDMSError) as caught:
            make_request(self.config, simple_profile(), trial=True)
        self.assertEqual(caught.exception.code, "MOYU_COPY_MODE_CHANGED")
        self.assertEqual((task/"progress.json").read_bytes(), before)
        self.assertFalse(list(task.glob("*.request.json")))

    def task_with_copy(self, data):
        _, request = make_request(self.config, simple_profile(), trial=True)
        self.config["task"] = request["task"]
        profile = simple_profile()
        store = Store(request["task"])
        store.load_or_create(row_plan(), self.output, profile_digest(profile), profile["wdms"]["conditions_hash"], "direct")
        store.update(0, "copying", count=1, number="001234", destination_absent=True,
                     selected={"name": "archive.zip", "size": len(zip_bytes()), "sequence": 5})
        (self.output/"archive.zip").write_bytes(data)
        return store

    def test_human_recovery_keeps_partial_target_as_a_named_backup(self):
        store = self.task_with_copy(b"part")
        rearm(self.config, simple_profile())
        row = load_json(store.path)["segments"][0]
        self.assertEqual(Path(row["recovery_backups"][-1]).read_bytes(), b"part")
        self.assertFalse((self.output/"archive.zip").exists())
        self.assertEqual(row["phase"], "queried")

    def test_complete_file_requires_normal_continue_instead_of_recopy(self):
        store = self.task_with_copy(zip_bytes())
        with self.assertRaises(WDMSError) as caught:
            rearm(self.config, simple_profile())
        self.assertEqual(caught.exception.code, "MOYU_COPY_PENDING")
        self.assertEqual((self.output/"archive.zip").read_bytes(), zip_bytes())
        self.assertEqual(load_json(store.path)["segments"][0]["phase"], "copying")

    def test_failed_recovery_rename_preserves_original_and_blocks_next_actions(self):
        store = self.task_with_copy(b"part")
        with patch.object(Path, "rename", side_effect=PermissionError("synthetic still locked")), self.assertRaises(PermissionError):
            rearm(self.config, simple_profile())
        self.assertTrue(load_json(store.path)["segments"][0]["rearm_pending"])
        self.assertEqual((self.output/"archive.zip").read_bytes(), b"part")

    def test_new_worker_route_generates_direct_receipt_without_any_staging(self):
        path, request = make_request(self.config, simple_profile(), trial=True)
        clock, remote = Clock(), NamedRemote()
        page = Page(clock)
        parent = Mock()
        parent.alive.return_value = True
        def assemble(plan, policy, store, query, files, **kwargs):
            return Engine(plan, policy, store, query, files, clock=clock, sleep=clock.sleep, wall=clock, **kwargs)
        with patch("moyu_tools.runtime.sys.platform", "win32"), patch("moyu_tools.bridge.Bridge"), \
                patch("moyu_tools.bridge.ParentHandle", return_value=parent), patch("moyu_tools.native.Driver"), \
                patch("moyu_tools.native.LocalQuery", return_value=page.query()), \
                patch("moyu_tools.native.RemoteFiles", return_value=remote), patch("moyu_tools.runtime.Engine", side_effect=assemble), \
                patch("moyu_tools.direct_copy.completed_reader", side_effect=lambda target: Path(target).open("rb")):
            self.assertEqual(worker(path), 0)
        self.assertEqual(verify_child(0, request), "trial_passed")
        self.assertFalse((self.output/".moyu-staging").exists())
        self.assertTrue((self.output/"actual_001111.zip").exists())
        self.assertEqual(load_json(Path(request["task"])/"progress.json")["identity"]["copy_mode"], "direct")

    def test_environment_checker_can_use_old_runtime_without_changing_it(self):
        from scripts.check_moyu_runtime import main
        old = self.folder/"old-portable"/"WDMS_Cloud_Project"
        with patch("moyu_tools.portable.check", return_value={"bundled_runtime_checked": True}) as check:
            self.assertEqual(main(["--runtime-project", str(old)]), 0)
        check.assert_called_once_with(old)
        self.assertFalse(old.exists())
        self.assertTrue((state_root()/"portable_startup.json").exists())
