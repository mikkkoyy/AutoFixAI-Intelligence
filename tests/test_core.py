from app.agents.orchestrator import AgentOrchestrator
from app.builder.project_builder import ProjectBuilder
from app.verification.verifier import ProjectVerifier

def test_builder_and_verifier(tmp_path):
    r=ProjectBuilder().build_python_project(tmp_path,'Demo')
    assert r.success
    assert ProjectVerifier().verify(r.project_path)==[]

def test_multi_agent_pipeline():
    results=AgentOrchestrator().run()
    assert len(results)==7
    assert all(x.status.value=='passed' for x in results)
