"""Internal AutoFix worker adapters.

These are intentionally not user-facing modes. They are selected by the
internal worker router when AutoFix needs a coding worker and may fall back
between OpenCode, DeepSeek and Copilot.
"""

from .copilot_worker import CopilotWorker
from .deepseek_worker import DeepSeekWorker
from .ollama_worker import OllamaWorker
from .opencode_worker import OpenCodeWorker

__all__ = ["OpenCodeWorker", "DeepSeekWorker", "CopilotWorker", "OllamaWorker"]
