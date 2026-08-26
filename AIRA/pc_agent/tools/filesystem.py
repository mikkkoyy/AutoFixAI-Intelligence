import os
import shutil
from pathlib import Path
from typing import Any
from AIRA.core.logging import get_logger

logger = get_logger("pc_agent")


class FilesystemTool:
    name = "filesystem"
    description = "Controlled filesystem operations"
    permission_level = "read"

    def __init__(self, config: dict = None):
        config = config or {}
        allowed = config.get("allowed_paths", [])
        self.allowed_paths = [Path(p).resolve() for p in allowed] if allowed else None
        self.max_file_size = config.get("max_file_size", 10 * 1024 * 1024)

    def _check_path(self, path: str) -> tuple[bool, Path]:
        import os
        p = Path(path).resolve()
        if self.allowed_paths is not None:
            for allowed in self.allowed_paths:
                try:
                    p.relative_to(allowed)
                    return True, p
                except ValueError:
                    continue
            return False, p
        return True, p

    def _validate_no_traversal(self, path: str) -> tuple[bool, str]:
        import os
        normalized = os.path.normpath(path)
        if ".." in normalized.split(os.sep):
            return False, "Path traversal not allowed"
        return True, "OK"

    async def execute(self, action: str, **kwargs) -> dict:
        actions = {
            "list_directory": self._list_directory,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "create_directory": self._create_directory,
            "copy": self._copy,
            "move": self._move,
            "delete": self._delete,
            "exists": self._exists,
            "file_info": self._file_info,
            "search": self._search,
        }
        handler = actions.get(action)
        if not handler:
            return {"success": False, "error": f"Unknown filesystem action: {action}"}
        return await handler(**kwargs)

    async def _list_directory(self, path: str = ".", pattern: str = "*", **kw) -> dict:
        ok, p = self._check_path(path)
        if not ok:
            return {"success": False, "error": f"Access denied: {path}"}
        if not p.exists():
            return {"success": False, "error": f"Directory not found: {path}"}
        if not p.is_dir():
            return {"success": False, "error": f"Not a directory: {path}"}
        try:
            items = []
            for item in sorted(p.glob(pattern)):
                items.append({
                    "name": item.name,
                    "path": str(item),
                    "is_dir": item.is_dir(),
                    "size": item.stat().st_size if item.is_file() else 0,
                })
            return {"success": True, "result": items}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _read_file(self, path: str = "", encoding: str = "utf-8", **kw) -> dict:
        ok, p = self._check_path(path)
        if not ok:
            return {"success": False, "error": f"Access denied: {path}"}
        if not p.exists():
            return {"success": False, "error": f"File not found: {path}"}
        if p.stat().st_size > self.max_file_size:
            return {"success": False, "error": f"File too large ({p.stat().st_size} bytes)"}
        try:
            content = p.read_text(encoding=encoding)
            return {"success": True, "result": content}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _write_file(self, path: str = "", content: str = "", encoding: str = "utf-8", **kw) -> dict:
        ok, p = self._check_path(path)
        if not ok:
            return {"success": False, "error": f"Access denied: {path}"}
        protected = [".env", "auth_token", "secrets", ".pc_agent"]
        for pp in protected:
            if pp in str(p).lower():
                return {"success": False, "error": f"Write to protected path blocked: {path}"}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding=encoding)
            logger.info(f"File written: {path} ({len(content)} chars)")
            return {"success": True, "result": f"Written {len(content)} chars to {path}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _create_directory(self, path: str = "", **kw) -> dict:
        ok, p = self._check_path(path)
        if not ok:
            return {"success": False, "error": f"Access denied: {path}"}
        try:
            p.mkdir(parents=True, exist_ok=True)
            return {"success": True, "result": f"Directory created: {path}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _copy(self, source: str = "", destination: str = "", **kw) -> dict:
        ok_s, ps = self._check_path(source)
        ok_d, pd = self._check_path(destination)
        if not ok_s:
            return {"success": False, "error": f"Access denied to source: {source}"}
        if not ok_d:
            return {"success": False, "error": f"Access denied to destination: {destination}"}
        try:
            if ps.is_dir():
                shutil.copytree(str(ps), str(pd))
            else:
                pd.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(ps), str(pd))
            return {"success": True, "result": f"Copied {source} to {destination}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _move(self, source: str = "", destination: str = "", **kw) -> dict:
        ok_s, ps = self._check_path(source)
        ok_d, pd = self._check_path(destination)
        if not ok_s:
            return {"success": False, "error": f"Access denied to source: {source}"}
        if not ok_d:
            return {"success": False, "error": f"Access denied to destination: {destination}"}
        try:
            shutil.move(str(ps), str(pd))
            return {"success": True, "result": f"Moved {source} to {destination}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _delete(self, path: str = "", **kw) -> dict:
        ok, p = self._check_path(path)
        if not ok:
            return {"success": False, "error": f"Access denied: {path}"}
        if not p.exists():
            return {"success": False, "error": f"Not found: {path}"}
        try:
            if p.is_dir():
                shutil.rmtree(str(p))
            else:
                p.unlink()
            return {"success": True, "result": f"Deleted: {path}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _exists(self, path: str = "", **kw) -> dict:
        p = Path(path).resolve()
        return {"success": True, "result": p.exists()}

    async def _file_info(self, path: str = "", **kw) -> dict:
        ok, p = self._check_path(path)
        if not ok:
            return {"success": False, "error": f"Access denied: {path}"}
        if not p.exists():
            return {"success": False, "error": f"Not found: {path}"}
        try:
            stat = p.stat()
            return {
                "success": True,
                "result": {
                    "name": p.name,
                    "path": str(p),
                    "is_file": p.is_file(),
                    "is_dir": p.is_dir(),
                    "size": stat.st_size,
                    "extension": p.suffix,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _search(self, path: str = ".", pattern: str = "*", max_results: int = 50, **kw) -> dict:
        ok, p = self._check_path(path)
        if not ok:
            return {"success": False, "error": f"Access denied: {path}"}
        try:
            results = []
            for item in p.rglob(pattern):
                results.append({
                    "name": item.name,
                    "path": str(item),
                    "is_dir": item.is_dir(),
                    "size": item.stat().st_size if item.is_file() else 0,
                })
                if len(results) >= max_results:
                    break
            return {"success": True, "result": results}
        except Exception as e:
            return {"success": False, "error": str(e)}
