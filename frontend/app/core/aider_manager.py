import os
import json

# Assuming load_config and Aider are defined elsewhere
def load_config(path):
    with open(path, 'r') as f:
        return json.load(f)

class Aider:
    def __init__(self, config, workspace):
        self.config = config
        self.workspace = workspace

class AiderManager:
    def __init__(self, workspace):
        self.workspace = workspace
        self.aider = self.detect_aider()

    def detect_aider(self):
        # Check for Aider installation and config
        config_path = os.path.join(self.workspace, "aider_config.json")
        if os.path.exists(config_path):
            config = load_config(config_path)
            if config.get("model") == "openrouter":
                return Aider(config, workspace=self.workspace)
        return None  # Fallback if not found
