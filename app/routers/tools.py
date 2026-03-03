"""Tools router — security tool registry and management."""
import logging
import subprocess
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.tool_registry import ToolCategory, ToolRegistry
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tools", tags=["tools"])


class ToolInfo(BaseModel):
    """Information about a security tool."""
    name: str
    display_name: str
    description: str
    category: str
    is_pro: bool
    requires_docker: bool
    install_command: str | None = None
    run_command: str | None = None


class ToolListResponse(BaseModel):
    """Response for GET /tools — list available tools."""
    tools: list[ToolInfo]
    total: int


class ToolStatusResponse(BaseModel):
    """Response for tool status check."""
    name: str
    installed: bool
    version: str | None = None
    error: str | None = None


class ToolRunResponse(BaseModel):
    """Response for tool execution."""
    name: str
    success: bool
    output: str | None = None
    error: str | None = None
    duration_seconds: float | None = None


# Seed default tools
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
        "name": "gitleaks",
        "display_name": "Gitleaks",
        "description": "Scan repositories for secrets, passwords, and tokens",
        "category": ToolCategory.SAST,
        "install_command": "brew install gitleaks || go install github.com/gitleaks/gitleaks/v8@latest",
        "run_command": "gitleaks detect --source",
        "requires_docker": False,
        "is_pro": True,
    },
    {
        "name": "bandit",
        "display_name": "Bandit",
        "description": "Python-specific security issue scanner",
        "category": ToolCategory.SAST,
        "install_command": "pip install bandit",
        "run_command": "bandit -r",
        "requires_docker": False,
        "is_pro": False,
    },
    {
        "name": "zap",
        "display_name": "OWASP ZAP",
        "description": "Web application security scanner",
        "category": ToolCategory.DAST,
        "install_command": "pip install zaproxy",
        "run_command": "zap-baseline.py -t",
        "requires_docker": True,
        "is_pro": True,
    },
    {
        "name": "nikto",
        "display_name": "Nikto",
        "description": "Web server scanner for vulnerabilities",
        "category": ToolCategory.DAST,
        "install_command": "brew install nikto || apt-get install nikto",
        "run_command": "nikto -h",
        "requires_docker": False,
        "is_pro": True,
    },
    {
        "name": "nmap",
        "display_name": "Nmap",
        "description": "Network port scanner",
        "category": ToolCategory.DAST,
        "install_command": "brew install nmap || apt-get install nmap",
        "run_command": "nmap -sV",
        "requires_docker": False,
        "is_pro": False,
    },
]


async def _seed_tools(db: AsyncSession) -> None:
    """Seed default tools if they don't exist."""
    for tool_data in DEFAULT_TOOLS:
        result = await db.execute(
            select(ToolRegistry).where(ToolRegistry.name == tool_data["name"])
        )
        existing = result.scalar_one_or_none()
        if not existing:
            tool = ToolRegistry(**tool_data)
            db.add(tool)
    await db.commit()


@router.get(
    "",
    response_model=ToolListResponse,
    summary="List available security tools",
)
async def list_tools(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Returns list of all available security tools."""
    await _seed_tools(session)

    result = await session.execute(select(ToolRegistry))
    tools = result.scalars().all()

    # Filter tools based on user plan
    if current_user.plan != "pro":
        tools = [t for t in tools if not t.is_pro]

    return ToolListResponse(
        tools=[
            ToolInfo(
                name=t.name,
                display_name=t.display_name,
                description=t.description,
                category=t.category.value,
                is_pro=t.is_pro,
                requires_docker=t.requires_docker,
                install_command=t.install_command,
                run_command=t.run_command,
            )
            for t in tools
        ],
        total=len(tools),
    )


@router.get(
    "/{tool_name}/status",
    response_model=ToolStatusResponse,
    summary="Check if a tool is installed",
)
async def get_tool_status(
    tool_name: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Check if a security tool is installed and get its version."""
    result = await session.execute(
        select(ToolRegistry).where(ToolRegistry.name == tool_name)
    )
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tool '{tool_name}' not found",
        )

    if tool.is_pro and current_user.plan != "pro":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This tool requires a Pro subscription",
        )

    # Try to run --version to check if installed
    try:
        cmd = tool.run_command.split()[0] if tool.run_command else tool_name
        proc = subprocess.run(
            [cmd, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        installed = proc.returncode == 0
        version = proc.stdout.strip() if installed else None
    except Exception as e:
        installed = False
        version = None

    return ToolStatusResponse(
        name=tool_name,
        installed=installed,
        version=version,
    )


@router.post(
    "/{tool_name}/install",
    summary="Install a security tool",
)
async def install_tool(
    tool_name: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Install a security tool using its install command."""
    # Check if user has pro access for pro tools
    if current_user.plan != "pro":
        result = await session.execute(
            select(ToolRegistry).where(
                ToolRegistry.name == tool_name,
                ToolRegistry.is_pro == True,
            )
        )
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This tool requires a Pro subscription",
            )

    result = await session.execute(
        select(ToolRegistry).where(ToolRegistry.name == tool_name)
    )
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tool '{tool_name}' not found",
        )

    if not tool.install_command:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No install command available for '{tool_name}'",
        )

    # Run install command
    try:
        proc = subprocess.run(
            tool.install_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        success = proc.returncode == 0
        if not success:
            return {
                "success": False,
                "tool": tool_name,
                "error": proc.stderr or "Installation failed",
            }
        return {
            "success": True,
            "tool": tool_name,
            "message": "Tool installed successfully",
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Installation timed out",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Installation failed: {str(e)}",
        )


@router.post(
    "/{tool_name}/run",
    response_model=ToolRunResponse,
    summary="Run a security tool on a project",
)
async def run_tool(
    tool_name: str,
    project_path: str = ".",
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Run a security tool on a project directory."""
    # Check if user has pro access for pro tools
    if current_user.plan != "pro":
        result = await session.execute(
            select(ToolRegistry).where(
                ToolRegistry.name == tool_name,
                ToolRegistry.is_pro == True,
            )
        )
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This tool requires a Pro subscription",
            )

    result = await session.execute(
        select(ToolRegistry).where(ToolRegistry.name == tool_name)
    )
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tool '{tool_name}' not found",
        )

    if not tool.run_command:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No run command available for '{tool_name}'",
        )

    import time
    start_time = time.time()

    # Build run command
    cmd = f"{tool.run_command} {project_path}"

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        duration = time.time() - start_time

        return ToolRunResponse(
            name=tool_name,
            success=proc.returncode == 0,
            output=proc.stdout[:10000] if proc.stdout else None,
            error=proc.stderr[:1000] if proc.stderr else None,
            duration_seconds=duration,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Tool execution timed out",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Tool execution failed: {str(e)}",
        )


# ---------------------------------------------------------------------------
# T-CLI-P11: SonarQube dedicated endpoint (Pro-only)
# ---------------------------------------------------------------------------

class SonarQubeRunRequest(BaseModel):
    project_key: str
    source_path: str = "."
    sonar_host: str = "http://localhost:9000"
    sonar_token: str | None = None  # falls back to SONAR_TOKEN env var
    use_docker: bool = True         # prefer Docker sonar-scanner-cli image


class SonarQubeRunResponse(BaseModel):
    success: bool
    dashboard_url: str | None = None
    via: str | None = None
    error: str | None = None
    note: str | None = None


@router.post(
    "/sonarqube/run",
    response_model=SonarQubeRunResponse,
    summary="Run SonarQube analysis on a project directory (Pro-only)",
)
async def run_sonarqube(
    body: SonarQubeRunRequest,
    current_user: User = Depends(get_current_user),
):
    """
    T-CLI-P11: Execute a SonarQube Community Edition analysis.

    Strategy (in order):
      1. Local sonar-scanner CLI if available.
      2. Docker image sonarsource/sonar-scanner-cli:latest.

    Pro-only feature. Requires SONAR_TOKEN env var or body.sonar_token.
    """
    import os
    import time

    if current_user.plan != "pro":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SonarQube analysis is a Pro-only feature. Upgrade at pakalon.com/pricing.",
        )

    import pathlib
    source = pathlib.Path(body.source_path).resolve()
    if not source.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Source path does not exist: {body.source_path}",
        )

    token = body.sonar_token or os.environ.get("SONAR_TOKEN", "")
    project_key = body.project_key.lower().replace(" ", "_")
    dashboard_url = f"{body.sonar_host}/dashboard?id={project_key}"

    # --- Attempt 1: local sonar-scanner ---
    try:
        result = subprocess.run(
            [
                "sonar-scanner",
                f"-Dsonar.projectKey={project_key}",
                f"-Dsonar.sources={str(source)}",
                f"-Dsonar.host.url={body.sonar_host}",
                f"-Dsonar.login={token}",
                "-Dsonar.scm.disabled=true",
            ],
            capture_output=True, text=True, timeout=300,
            cwd=str(source),
        )
        if result.returncode == 0:
            return SonarQubeRunResponse(
                success=True,
                dashboard_url=dashboard_url,
                via="sonar-scanner-cli",
                note=f"Analysis complete. View at {dashboard_url}",
            )
        else:
            err = result.stderr[:500] or result.stdout[:500]
    except FileNotFoundError:
        err = "sonar-scanner CLI not found"
    except subprocess.TimeoutExpired:
        err = "sonar-scanner timed out"
    except Exception as _e:
        err = str(_e)

    if not body.use_docker:
        return SonarQubeRunResponse(success=False, error=err)

    # --- Attempt 2: Docker sonar-scanner-cli ---
    docker_check = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
    if docker_check.returncode != 0:
        return SonarQubeRunResponse(
            success=False,
            error=f"sonar-scanner unavailable ({err}) and Docker not running",
        )

    try:
        docker_result = subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{str(source)}:/usr/src",
                "--network", "host",
                "sonarsource/sonar-scanner-cli:latest",
                f"-Dsonar.projectKey={project_key}",
                "-Dsonar.sources=/usr/src",
                f"-Dsonar.host.url={body.sonar_host}",
                f"-Dsonar.login={token or 'admin'}",
                "-Dsonar.scm.disabled=true",
            ],
            capture_output=True, text=True, timeout=360,
        )
        if docker_result.returncode == 0:
            return SonarQubeRunResponse(
                success=True,
                dashboard_url=dashboard_url,
                via="docker",
                note=f"Analysis complete (Docker). Dashboard: {dashboard_url}",
            )
        return SonarQubeRunResponse(
            success=False,
            via="docker",
            error=docker_result.stderr[:500] or docker_result.stdout[:500],
        )
    except subprocess.TimeoutExpired:
        return SonarQubeRunResponse(success=False, error="Docker sonar-scanner timed out")
    except Exception as _e:
        return SonarQubeRunResponse(success=False, error=str(_e))


# ---------------------------------------------------------------------------
# T-IMG-01: Image generation endpoint (Pro-only)
# ---------------------------------------------------------------------------

class ImageGenRequest(BaseModel):
    prompt: str
    size: str = "1024x1024"
    quality: str = "standard"
    style: str = "natural"
    project_dir: str = "."


class ImageGenResponse(BaseModel):
    ok: bool
    url: str | None = None
    local_path: str | None = None
    provider: str | None = None
    revised_prompt: str | None = None
    size: str | None = None
    error: str | None = None
    plan_blocked: bool = False


@router.post(
    "/generate-image",
    response_model=ImageGenResponse,
    summary="Generate an AI image from a text prompt (Pro-only)",
)
async def generate_image(
    body: ImageGenRequest,
    current_user: User = Depends(get_current_user),
):
    """
    T-IMG-01: Generate an AI image using DALL-E 3 / Stability AI / Replicate.
    Pro-only feature.
    """
    import sys
    import pathlib as _pl

    if current_user.plan != "pro":
        return ImageGenResponse(
            ok=False,
            plan_blocked=True,
            error=(
                "Image generation is a Pro-only feature. "
                "Upgrade at https://pakalon.com/pricing to unlock it."
            ),
        )

    if not body.prompt or len(body.prompt.strip()) < 3:
        return ImageGenResponse(ok=False, error="Prompt must be at least 3 characters.")

    # Attempt to import the Python image_gen tool
    _tools_path = str(_pl.Path(__file__).resolve().parents[2] / "python" / "tools")
    if _tools_path not in sys.path:
        sys.path.insert(0, _tools_path)

    try:
        from image_gen import ImageGenTool, PlanBlockedError, NoProviderAvailableError  # type: ignore

        tool = ImageGenTool(user_plan=current_user.plan, project_dir=body.project_dir)
        result = tool.generate(
            body.prompt,
            size=body.size,
            quality=body.quality,
            style=body.style,
        )
        return ImageGenResponse(
            ok=True,
            url=result.get("url"),
            local_path=result.get("local_path"),
            provider=result.get("provider"),
            revised_prompt=result.get("prompt", body.prompt),
            size=result.get("size", body.size),
        )
    except Exception as exc:
        logger.exception("Image generation failed: %s", exc)
        return ImageGenResponse(ok=False, error=str(exc))

