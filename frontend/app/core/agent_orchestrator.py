# Assuming OpenCodeAgent and AiderAgent are defined in agents module
from frontend.app.agents.opencode_agent import OpenCodeAgent
from frontend.app.agents.aider_agent import AiderAgent

class AgentOrchestrator:
    def __init__(self, workspace):
        self.workspace = workspace
        self.agents = {
            "OpenCode": OpenCodeAgent(workspace),
            "Aider": AiderAgent(workspace)
        }
