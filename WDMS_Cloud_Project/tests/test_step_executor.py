import math
import unittest

from core.step_executor import Step, StepExecutor
from errors import WDMSError


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class StepExecutorTests(unittest.TestCase):
    def test_steps_run_in_order_and_return_success_results(self):
        clock = FakeClock()
        order = []
        events = []
        steps = [
            Step("first", lambda context: order.append("action-1") or 1,
                 lambda value: order.append("verify-1") or value == 1, 3),
            Step("second", lambda context: order.append("action-2") or 2,
                 lambda value: order.append("verify-2") or value == 2, 3),
        ]
        results = StepExecutor(emit=lambda event, fields: events.append((event, fields)), clock=clock).run(steps)
        self.assertEqual(order, ["action-1", "verify-1", "action-2", "verify-2"])
        self.assertEqual([(item.name, item.status, item.value) for item in results], [
            ("first", "succeeded", 1), ("second", "succeeded", 2),
        ])
        self.assertEqual([event for event, _ in events], [
            "step_started", "step_succeeded", "step_started", "step_succeeded",
        ])
        self.assertEqual(set(events[0][1]), {"step", "status", "elapsed_seconds", "error_code"})

    def test_verify_non_true_stops_later_steps(self):
        later = []
        with self.assertRaises(WDMSError) as caught:
            StepExecutor().run([
                Step("unconfirmed", lambda context: object(), lambda value: 1, 2),
                Step("later", lambda context: later.append(True), lambda value: True, 2),
            ])
        self.assertEqual(caught.exception.code, "STEP_UNCONFIRMED")
        self.assertEqual(later, [])

    def test_action_exception_message_does_not_leak_original_text(self):
        def fail(context):
            raise ValueError("secret customer detail")

        with self.assertRaises(WDMSError) as caught:
            StepExecutor().run([Step("safe step", fail, lambda value: True, 2)])
        self.assertEqual(caught.exception.code, "STEP_FAILED")
        self.assertIn("safe step", caught.exception.message)
        self.assertIn("ValueError", caught.exception.message)
        self.assertNotIn("secret customer detail", caught.exception.message)

    def test_action_wdms_error_is_preserved(self):
        original = WDMSError("BUSINESS_STOP", "stable message")

        def fail(context):
            raise original

        with self.assertRaises(WDMSError) as caught:
            StepExecutor().run([Step("known failure", fail, lambda value: True, 2)])
        self.assertIs(caught.exception, original)

    def test_verification_wdms_error_is_preserved(self):
        original = WDMSError("VERIFY_STOP", "stable verification message")

        def verify(value):
            raise original

        with self.assertRaises(WDMSError) as caught:
            StepExecutor().run([Step("verify failure", lambda context: "value", verify, 2)])
        self.assertIs(caught.exception, original)

    def test_startup_cancel_does_not_run_action(self):
        actions = []
        with self.assertRaises(WDMSError) as caught:
            StepExecutor(cancelled=lambda: True).run([
                Step("first", lambda context: actions.append(True), lambda value: True, 2),
            ])
        self.assertEqual(caught.exception.code, "TASK_CANCELLED")
        self.assertEqual(actions, [])

    def test_cancel_during_action_skips_verify_and_following_steps(self):
        cancelled = [False]
        verified = []
        later = []

        def action(context):
            cancelled[0] = True
            return "done"

        with self.assertRaises(WDMSError) as caught:
            StepExecutor(cancelled=lambda: cancelled[0]).run([
                Step("first", action, lambda value: verified.append(value) or True, 2),
                Step("later", lambda context: later.append(True), lambda value: True, 2),
            ])
        self.assertEqual(caught.exception.code, "TASK_CANCELLED")
        self.assertEqual(verified, [])
        self.assertEqual(later, [])

    def test_deadline_boundary_times_out_before_verify(self):
        clock = FakeClock()
        verified = []

        def action(context):
            self.assertEqual(context.remaining(), 1)
            clock.advance(1)
            self.assertEqual(context.remaining(), 0)
            return "done"

        with self.assertRaises(WDMSError) as caught:
            StepExecutor(clock=clock).run([
                Step("deadline", action, lambda value: verified.append(value) or True, 1),
            ])
        self.assertEqual(caught.exception.code, "STEP_TIMEOUT")
        self.assertEqual(verified, [])

    def test_keyboard_interrupt_becomes_cancelled_status(self):
        events = []

        def interrupt(context):
            raise KeyboardInterrupt()

        with self.assertRaises(WDMSError) as caught:
            StepExecutor(emit=lambda event, fields: events.append((event, fields))).run([
                Step("interrupt", interrupt, lambda value: True, 2),
            ])
        self.assertEqual(caught.exception.code, "TASK_CANCELLED")
        self.assertEqual(events[-1][0], "step_failed")
        self.assertEqual(events[-1][1]["status"], "cancelled")

    def test_all_steps_are_validated_before_any_action(self):
        actions = []
        invalid_steps = [
            [Step("first", lambda context: actions.append(True), lambda value: True, 1),
             Step("first", lambda context: None, lambda value: True, 1)],
            [Step("first", lambda context: actions.append(True), lambda value: True, 1),
             Step("nan", lambda context: None, lambda value: True, math.nan)],
            [Step("first", lambda context: actions.append(True), lambda value: True, 1),
             Step("infinity", lambda context: None, lambda value: True, math.inf)],
            [Step("first", lambda context: actions.append(True), lambda value: True, 1),
             Step("boolean", lambda context: None, lambda value: True, True)],
            [Step("first", lambda context: actions.append(True), lambda value: True, 1),
             Step("zero", lambda context: None, lambda value: True, 0)],
            [Step("first", lambda context: actions.append(True), lambda value: True, 1),
             Step("negative", lambda context: None, lambda value: True, -1)],
            [Step("first", lambda context: actions.append(True), lambda value: True, 1),
             Step("bad action", None, lambda value: True, 1)],
            [Step("first", lambda context: actions.append(True), lambda value: True, 1),
             Step("bad verify", lambda context: None, None, 1)],
            [Step("first", lambda context: actions.append(True), lambda value: True, 1),
             Step("", lambda context: None, lambda value: True, 1)],
        ]
        for steps in invalid_steps:
            with self.subTest(step=steps[-1].name):
                with self.assertRaises(WDMSError) as caught:
                    StepExecutor().run(steps)
                self.assertEqual(caught.exception.code, "STEP_INVALID")
        self.assertEqual(actions, [])

    def test_emit_failure_stops_without_a_second_failure_callback(self):
        calls = []

        def emit(event, fields):
            calls.append(event)
            raise OSError("not displayed")

        with self.assertRaises(WDMSError) as caught:
            StepExecutor(emit=emit).run([
                Step("event failure", lambda context: None, lambda value: True, 1),
            ])
        self.assertEqual(caught.exception.code, "STEP_EVENT_FAILED")
        self.assertEqual(calls, ["step_started"])

    def test_failed_event_callback_is_not_retried(self):
        calls = []

        def emit(event, fields):
            calls.append(event)
            if event == "step_failed":
                raise OSError("not displayed")

        with self.assertRaises(WDMSError) as caught:
            StepExecutor(emit=emit).run([
                Step("action failure", lambda context: (_ for _ in ()).throw(ValueError("hidden")),
                     lambda value: True, 1),
            ])
        self.assertEqual(caught.exception.code, "STEP_EVENT_FAILED")
        self.assertEqual(calls, ["step_started", "step_failed"])


if __name__ == "__main__":
    unittest.main()
