import json
from pathlib import Path
from typing import Optional
from AIRA.core.memory_db import memory_db
from AIRA.core.logging import get_logger

logger = get_logger("tools")

SKILLS_DIR = Path(__file__).parent.parent / "skills"


class Skill:
    def __init__(self, name: str, description: str, version: str,
                 instructions: str, tools: list[str] = None,
                 requirements: list[str] = None):
        self.name = name
        self.description = description
        self.version = version
        self.instructions = instructions
        self.tools = tools or []
        self.requirements = requirements or []

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "instructions": self.instructions,
            "tools": self.tools,
            "requirements": self.requirements,
        }


class SkillManager:
    def __init__(self):
        self.skills: dict[str, Skill] = {}

    async def discover_skills(self):
        if SKILLS_DIR.exists():
            for skill_dir in SKILLS_DIR.iterdir():
                if skill_dir.is_dir():
                    manifest = skill_dir / "skill.json"
                    if manifest.exists():
                        try:
                            data = json.loads(manifest.read_text(encoding="utf-8"))
                            skill = Skill(**data)
                            self.skills[skill.name] = skill
                            logger.info(f"Discovered skill: {skill.name}")
                        except Exception as e:
                            logger.error(f"Failed to load skill {skill_dir.name}: {e}")

        built_in = [
            Skill(
                name="coding",
                description="Writing and editing code",
                version="1.0.0",
                instructions="Write clean, well-structured code following best practices.",
                tools=["file_read", "file_write", "terminal"],
            ),
            Skill(
                name="debugging",
                description="Analyzing and fixing errors",
                version="1.0.0",
                instructions="Systematically identify the root cause of errors and propose fixes.",
                tools=["file_read", "terminal", "memory"],
            ),
            Skill(
                name="git",
                description="Version control operations",
                version="1.0.0",
                instructions="Manage git repositories safely. Always check status before changes.",
                tools=["git", "terminal"],
            ),
            Skill(
                name="research",
                description="Information gathering and analysis",
                version="1.0.0",
                instructions="Thoroughly research topics using available tools and knowledge.",
                tools=["file_read", "terminal", "memory"],
            ),
            Skill(
                name="windows",
                description="Windows system operations",
                version="1.0.0",
                instructions="Handle Windows-specific system operations and PowerShell commands.",
                tools=["terminal", "file_read", "file_write"],
            ),
        ]
        for s in built_in:
            if s.name not in self.skills:
                self.skills[s.name] = s
                await self._store_skill_db(s)

    async def _store_skill_db(self, skill: Skill):
        try:
            await memory_db.execute(
                """INSERT OR REPLACE INTO skills (id, name, description, version, instructions, tools, requirements, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                (skill.name, skill.name, skill.description, skill.version,
                 skill.instructions, json.dumps(skill.tools), json.dumps(skill.requirements)),
            )
        except Exception:
            pass

    def get_skill(self, name: str) -> Optional[Skill]:
        return self.skills.get(name)

    def list_skills(self) -> list[dict]:
        return [s.to_dict() for s in self.skills.values()]

    def get_skill_names(self) -> list[str]:
        return list(self.skills.keys())
