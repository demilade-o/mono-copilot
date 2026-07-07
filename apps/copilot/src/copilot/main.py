import os
from contextlib import asynccontextmanager

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import (
    Dir,
    Manifest,
    SandboxAgent,
    SandboxRunConfig,
)
from agents.sandbox.capabilities import Capabilities, Skills
from agents.sandbox.entries.base import BaseEntry
from agents.sandbox.entries import File, GitRepo
from agents.sandbox.manifest import Environment
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions
from docker import from_env as docker_from_env
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pathlib import Path
from pydantic import BaseModel
import uvicorn

load_dotenv()


def _runtime_shim(binary: str) -> File:
    binary_path = f"/usr/local/bin/{binary}"
    content = (
        "#!/bin/sh\n"
        f"if [ -x {binary_path!r} ]; then\n"
        f"  exec {binary_path!r} \"$@\"\n"
        "fi\n"
        f"echo '{binary} is not installed in this Docker sandbox image.' >&2\n"
        "echo 'Set SANDBOX_DOCKER_IMAGE to an image that includes uv and bun.' >&2\n"
        "exit 127\n"
    )
    return File(content=content.encode("utf-8"))


def _docker_image() -> str:
    return os.getenv("SANDBOX_DOCKER_IMAGE", "mono-copilot:latest")


def _skills_repo() -> GitRepo:
    return GitRepo(
        repo=os.getenv("SANDBOX_SKILLS_REPO", "mosesgameli/myagentskills"),
        ref=os.getenv("SANDBOX_SKILLS_REF", "main"),
        subpath=os.getenv("SANDBOX_SKILLS_SUBPATH", ".agents/skills"),
    )


def _sandbox_manifest() -> Manifest:
    entries: dict[str | Path, BaseEntry] = {
        "tools/": Dir(),
        "tools/bin/": Dir(),
        "tools/bin/uv": _runtime_shim("uv"),
        "tools/bin/bun": _runtime_shim("bun"),
        "tmp/": Dir(),
        ".bun/": Dir(),
    }

    return Manifest(
        entries=entries,
        environment=Environment(
            value={
                "PATH": "tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "TMPDIR": "tmp",
                "BUN_INSTALL": ".bun",
                "BUN_TMPDIR": "tmp",
            }
        ),
    )


agent = SandboxAgent(
    name="Sandbox Coding Agent",
    instructions=(
        "You are a coding agent operating inside a sandbox workspace. "
        "Inspect the workspace before making assumptions. "
        "You have a PERSISTENT project workspace at /workspace/projects that survives across requests. "
        "Each project lives in its own folder: /workspace/projects/<project-name>/. "
        "When asked to create or work on a project, create or reuse /workspace/projects/<project-name>/ "
        "and save that project's documents inside it (for example project-brd.md and project-prd.md). "
        "Before creating a new project, list /workspace/projects first to see what already exists and avoid duplicates. "
        "When a user asks you to run a one-off script, execute it in the sandbox and report the result. "
        "Runtime policy: run Python scripts with './tools/bin/uv run python <script_or_flags>' and run JavaScript/TypeScript with './tools/bin/bun' (for example './tools/bin/bun run', './tools/bin/bun <file>.js', or './tools/bin/bun <file>.ts'). "
        "Prefer these runtimes over direct python/node execution unless the command fails and you explain why. "
        "For executed commands, include: the exact command, exit status, stdout, and stderr in your response. "
        "Keep answers concise, factual, and focused on implementation details."
    ),
    default_manifest=_sandbox_manifest(),
    capabilities=[
        *Capabilities.default(),
        Skills(
            from_=_skills_repo(),
            skills_path=os.getenv("SANDBOX_SKILLS_PATH", ".agents/skills"),
        ),
    ],
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create ONE sandbox when the server starts and reuse it for every request.

    Because the same container stays alive for the life of the server process,
    anything written under /workspace (for example /workspace/projects/<name>/)
    persists across requests instead of being destroyed after each call.

    Note: this persistence lasts as long as the server (and its container) is
    running. Stopping the server tears the sandbox down. See the notes in chat
    for how to make it survive restarts later if you want that.
    """
    docker_client = DockerSandboxClient(docker_from_env())
    sandbox = await docker_client.create(
        manifest=_sandbox_manifest(),
        options=DockerSandboxClientOptions(image=_docker_image()),
    )
    try:
        async with sandbox:
            # One-time preflight: confirm uv + bun exist in the image.
            preflight = await sandbox.exec(
                "./tools/bin/uv --version && ./tools/bin/bun --version", shell=True
            )
            if preflight.exit_code != 0:
                stderr = preflight.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    "Docker sandbox image is missing uv and/or bun. "
                    "Set SANDBOX_DOCKER_IMAGE to an image that includes both runtimes "
                    "(for example mono-copilot:latest). "
                    f"Preflight error: {stderr}"
                )

            # Make sure the persistent projects directory exists.
            await sandbox.exec("mkdir -p /workspace/projects", shell=True)

            # Hand the live session to the request handlers.
            app.state.sandbox = sandbox
            print("Sandbox ready. Persistent workspace at /workspace/projects")
            yield
    finally:
        app.state.sandbox = None
        try:
            await docker_client.delete(sandbox)
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)


class AgentRequest(BaseModel):
    user_input: str


class AgentResponse(BaseModel):
    response: str


def _mask_secret(value: str | None) -> str:
    if not value:
        return "<missing>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


@app.get("/")
def read_root() -> dict[str, str]:
    return {"Hello": "World"}


@app.post("/agent", response_model=AgentResponse)
async def run_agent(request: AgentRequest) -> AgentResponse:
    sandbox = getattr(app.state, "sandbox", None)
    if sandbox is None:
        raise HTTPException(
            status_code=503,
            detail="Sandbox is not ready yet. Wait a moment after startup and try again.",
        )

    try:
        result = await Runner.run(
            agent,
            request.user_input,
            run_config=RunConfig(
                sandbox=SandboxRunConfig(session=sandbox),
                workflow_name="Copilot docker sandbox coding agent",
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Agent execution failed: {exc}") from exc

    return AgentResponse(response=str(result.final_output))


def run() -> None:
    environment = os.getenv("APP_ENV", "production").lower()

    if environment == "development":
        print("Running in development mode with hot reload enabled.")
        uvicorn.run("copilot.main:app", host="127.0.0.1", port=8000, reload=True)
    else:
        print("Running in production mode.")
        uvicorn.run("copilot.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()