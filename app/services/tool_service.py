"""Tool service — manages security tools registry and operations."""
import logging
import subprocess
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_registry import ToolCategory, ToolRegistry

logger = logging.getLogger(__name__)


# Default tools to seed the registry
DEFAULT_TOOLS = [
    {
        "name": "semgrep",
        "display_name": "Semgrep",
        "description": "Fast static analysis tool for finding bugs and security issues",
        "category": ToolCategory.SAST,
        "install_command": "pip install semgrep",
        "run_command": "semgrep --config=auto",
        "requires_docker": False,
        "is_pro": True,
    },
    {
        "name": "bandit",
        "display_name": "Bandit",
        "description": "Security issue scanner for Python code",
        "category": ToolCategory.SAST,
        "install_command": "pip install bandit",
        "run_command": "bandit -r",
        "requires_docker": False,
        "is_pro": False,
    },
    {
        "name": "gitleaks",
        "display_name": "Gitleaks",
        "description": "Scan for secrets, keys, and tokens in repositories",
        "category": ToolCategory.SAST,
        "install_command": "brew install gitleaks || go install github.com/gitleaks/gitleaks/v8@latest",
        "run_command": "gitleaks detect --source",
        "requires_docker": False,
        "is_pro": True,
    },
    {
        "name": "sonarqube",
        "display_name": "SonarQube",
        "description": "Code quality and security analysis platform",
        "category": ToolCategory.SAST,
        "install_command": "docker run -d --name sonarqube -p 9000:9000 sonarqube",
        "run_command": "sonarscanner",
        "requires_docker": True,
        "is_pro": True,
    },
    {
        "name": "zap",
        "display_name": "OWASP ZAP",
        "description": "Dynamic application security testing proxy",
        "category": ToolCategory.DAST,
        "install_command": "pip install zap-baseline",
        "run_command": "zap-baseline.py -t {target}",
        "requires_docker": False,
        "is_pro": True,
    },
    {
        "name": "nikto",
        "display_name": "Nikto",
        "description": "Web server vulnerability scanner",
        "category": ToolCategory.DAST,
        "install_command": "brew install nikto || apt-get install nikto",
        "run_command": "nikto -h {target}",
        "requires_docker": False,
        "is_pro": True,
    },
    {
        "name": "sqlmap",
        "display_name": "sqlmap",
        "description": "Automatic SQL injection and database takeover tool",
        "category": ToolCategory.DAST,
        "install_command": "git clone --depth 1 https://github.com/sqlmapproject/sqlmap.git",
        "run_command": "python sqlmap/sqlmap.py -u {target}",
        "requires_docker": False,
        "is_pro": True,
    },
    {
        "name": "nmap",
        "display_name": "Nmap",
        "description": "Network exploration and security scanning",
        "category": ToolCategory.DAST,
        "install_command": "brew install nmap || apt-get install nmap",
        "run_command": "nmap -sV --open",
        "requires_docker": False,
        "is_pro": False,
    },
]


async def seed_default_tools(db: AsyncSession) -> None:
    """Seed the database with default security tools."""
    for tool_data in DEFAULT_TOOLS:
        result = await db.execute(
            select(ToolRegistry).where(ToolRegistry.name == tool_data["name"])
        )
        existing = result.scalar_one_or_none()

        if not existing:
            tool = ToolRegistry(**tool_data)
            db.add(tool)

    await db.commit()


async def list_tools(
    db: AsyncSession,
    category: ToolCategory | None = None,
    is_pro: bool | None = None,
) -> list[dict[str, Any]]:
    """List all available security tools."""
    query = select(ToolRegistry)
    if category:
        query = query.where(ToolRegistry.category == category)
    if is_pro is not None:
        query = query.where(ToolRegistry.is_pro == is_pro)

    result = await db.execute(query)
    tools = result.scalars().all()

    return [
        {
            "name": t.name,
            "display_name": t.display_name,
            "description": t.description,
            "category": t.category.value,
            "requires_docker": t.requires_docker,
            "is_pro": t.is_pro,
            "install_command": t.install_command,
            "run_command": t.run_command,
        }
        for t in tools
    ]


async def get_tool(
    name: str,
    db: AsyncSession,
) -> dict[str, Any] | None:
    """Get a specific tool by name."""
    result = await db.execute(
        select(ToolRegistry).where(ToolRegistry.name == name)
    )
    tool = result.scalar_one_or_none()

    if not tool:
        return None

    return {
        "name": tool.name,
        "display_name": tool.display_name,
        "description": tool.description,
        "category": tool.category.value,
        "requires_docker": tool.requires_docker,
        "is_pro": tool.is_pro,
        "install_command": tool.install_command,
        "run_command": tool.run_command,
    }


async def check_tool_installed(name: str) -> bool:
    """Check if a tool is installed and available on the system."""
    tool_commands = {
        "semgrep": "semgrep",
        "bandit": "bandit",
        "gitleaks": "gitleaks",
        "zap": "zap-baseline.py",
        "nikto": "nikto",
        "sqlmap": "sqlmap.py",
        "nmap": "nmap",
    }

    command = tool_commands.get(name)
    if not command:
        return False

    try:
        result = subprocess.run(
            ["which", command],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False
