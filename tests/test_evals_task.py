import tempfile
import unittest
from pathlib import Path

from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from evals.fixtures import build_suricata_fixture
from evals.task import EvalCaseInputs, run_skill_eval


def _write_skill(skill_dir: Path) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("---\nname: eval-test-skill\n---\n\nYou are a test analyst.\n")


class RunSkillEvalTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_final_text_output_and_creates_pristine_task(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            skill_dir = workspace / "skill"
            _write_skill(skill_dir)
            data_source = workspace / "data-source"
            data_source.mkdir()
            build_suricata_fixture(data_source)

            async def stream_respond(messages: list[ModelMessage], info: AgentInfo):
                yield "no findings"

            inputs = EvalCaseInputs(
                skill_dir=str(skill_dir),
                prompt="Summarize the log.",
                model="test",
                workspace=workspace,
                model_override=FunctionModel(stream_function=stream_respond),
            )

            output = await run_skill_eval(inputs)

            self.assertEqual(output, "no findings")
            task_dirs = [p for p in workspace.iterdir() if p.name.startswith("task-")]
            self.assertEqual(len(task_dirs), 1)

    async def test_propagates_task_failures_uncaught(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            skill_dir = workspace / "skill"
            _write_skill(skill_dir)
            data_source = workspace / "data-source"
            data_source.mkdir()
            build_suricata_fixture(data_source)

            async def stream_crash(messages: list[ModelMessage], info: AgentInfo):
                raise RuntimeError("simulated provider crash")
                yield  # pragma: no cover -- unreachable, keeps this an async generator

            inputs = EvalCaseInputs(
                skill_dir=str(skill_dir),
                prompt="Summarize the log.",
                model="test",
                workspace=workspace,
                model_override=FunctionModel(stream_function=stream_crash),
            )

            with self.assertRaisesRegex(RuntimeError, "simulated provider crash"):
                await run_skill_eval(inputs)


if __name__ == "__main__":
    unittest.main()
