from app.builder.project_builder import ProjectBuilder
from app.verification.verifier import ProjectVerifier
from app.agents.orchestrator import AgentOrchestrator


def test_builder_and_verifier(tmp_path):
    result = ProjectBuilder().build_python_project(tmp_path, "Demo")
    assert result.success
    assert ProjectVerifier().verify(result.project_path) == []


def test_agent_pipeline():
    results = AgentOrchestrator().run()
    assert len(results) == 7
    assert all(item.status.value == "passed" for item in results)
