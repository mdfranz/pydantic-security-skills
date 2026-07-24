import signal
import tempfile
import time
import unittest
from pathlib import Path

from skill_runner.audit import RunInterrupted, install_sigterm_handler, restore_sigterm_handler
from skill_runner.config import RunOptions
from skill_runner.run_core import RunSetup
from skill_runner.session import RunSession, SessionState


class FakeAudit:
    def __init__(self):
        self.events = []
        self.closed = False

    def event(self, kind, **fields):
        self.events.append((kind, fields))

    def close(self):
        self.closed = True


class FakeSink:
    def __init__(self):
        self.messages = []

    def status(self, message):
        self.messages.append(message)

    def emit(self, kind, **fields):
        pass


class FakeResult:
    def __init__(self, output):
        self.output = output

    def all_messages(self):
        return []


class FakeAgent:
    def __init__(self, outcomes, *, sleep_seconds: float = 0):
        self.outcomes = iter(outcomes)
        self.prompts = []
        self.sleep_seconds = sleep_seconds

    def run_sync(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResult(outcome)


class RunSessionTests(unittest.TestCase):
    def make_setup(
        self, workspace: Path, agent: FakeAgent, audit: FakeAudit, *, options: RunOptions | None = None
    ) -> RunSetup:
        return RunSetup(
            task_id="default",
            skill_name="demo",
            ws_path=workspace,
            run_stamp="stamp",
            model="provider:model",
            audit=audit,
            previous_sigterm_handler=signal.getsignal(signal.SIGTERM),
            agent=agent,
            run_metadata={"task_id": "default", "run_id": "run"},
            initial_prompt="first",
            prompt_prefix="inventory\n\n",
            options=options or RunOptions(model="provider:model", workspace=workspace),
        )

    def test_first_prompt_prefix_and_completed_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = FakeAudit()
            agent = FakeAgent(["one", "two"])
            setup = self.make_setup(Path(directory), agent, audit)

            with RunSession(setup, FakeSink(), checkpoint_each_turn=False) as session:
                session.submit_sync("first")
                session.submit_sync("second")
                session.complete()

            self.assertEqual(agent.prompts, ["inventory\n\nfirst", "second"])
            self.assertEqual(session.state, SessionState.COMPLETED)
            self.assertEqual(audit.events[-1], ("run_end", {"status": "completed"}))
            self.assertTrue(audit.closed)

    def test_interactive_first_turn_carries_initial_phase_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            audit = FakeAudit()
            options = RunOptions(model="provider:model", workspace=workspace, interactive=True)
            setup = self.make_setup(workspace, FakeAgent(["one", "two"]), audit, options=options)
            session = RunSession(setup, FakeSink(), checkpoint_each_turn=False)

            self.assertEqual(session._run_kwargs()["metadata"]["interactive_phase"], "initial")
            session.submit_sync("first")
            self.assertEqual(session._run_kwargs()["metadata"]["interactive_phase"], "followup")
            session.close()

    def test_later_failure_marks_whole_session_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = FakeAudit()
            agent = FakeAgent(["one", ValueError("second turn failed")])
            setup = self.make_setup(Path(directory), agent, audit)
            session = RunSession(setup, FakeSink(), checkpoint_each_turn=True)

            session.submit_sync("first")
            with self.assertRaisesRegex(ValueError, "second turn failed"):
                session.submit_sync("second")
            session.close()

            self.assertEqual(session.state, SessionState.FAILED)
            self.assertEqual(audit.events[-1], ("run_end", {"status": "failed"}))

    def test_closing_an_inflight_session_records_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = FakeAudit()
            setup = self.make_setup(Path(directory), FakeAgent(["unused"]), audit)

            with RunSession(setup, FakeSink(), checkpoint_each_turn=True) as session:
                session._begin_turn("first")

            self.assertEqual(session.state, SessionState.INTERRUPTED)
            self.assertIn(
                ("interrupted", {"reason": "session closed while a turn was running"}),
                audit.events,
            )
            self.assertEqual(audit.events[-1], ("run_end", {"status": "failed"}))

    def test_max_run_seconds_interrupts_a_stuck_turn(self):
        previous = install_sigterm_handler()
        try:
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                audit = FakeAudit()
                agent = FakeAgent(["never reached"], sleep_seconds=1)
                options = RunOptions(model="provider:model", workspace=workspace, max_run_seconds=0.05)
                setup = self.make_setup(workspace, agent, audit, options=options)
                session = RunSession(setup, FakeSink(), checkpoint_each_turn=False)

                with self.assertRaises(RunInterrupted):
                    session.submit_sync("first")
                session.close()

                self.assertEqual(session.state, SessionState.INTERRUPTED)
                self.assertEqual(audit.events[-1], ("run_end", {"status": "failed"}))
        finally:
            restore_sigterm_handler(previous)


if __name__ == "__main__":
    unittest.main()
