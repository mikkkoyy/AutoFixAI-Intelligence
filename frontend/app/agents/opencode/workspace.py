"""OpenCode workspace handling — resolve working directory from Explorer context."""

from pathlib import Path


class OpenCodeWorkspace:
    """Determine the correct working directory for OpenCode.

    OpenCode must run with the currently selected Explorer workspace
    as its working directory, NOT the AutoFix repo root.
    """

    def resolve(
        self,
        explorer_path: Path | None,
        project_root: Path,
    ) -> Path:
        """Return the best working directory for OpenCode.

        Args:
            explorer_path: The currently selected path in Explorer (file or dir).
            project_root: The AutoFix AI Studio repo root (fallback).

        Returns:
            A directory path to use as OpenCode's cwd.
        """
        if explorer_path and explorer_path.exists():
            if explorer_path.is_dir():
                return explorer_path
            return explorer_path.parent

        return project_root

    def current_file_context(
        self,
        editor_file_path: str | None,
    ) -> str | None:
        """Return the current file path for context, or None."""
        if editor_file_path:
            p = Path(editor_file_path)
            if p.exists():
                return str(p)
        return None
