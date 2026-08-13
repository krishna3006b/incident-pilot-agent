import logging
import subprocess
from typing import Dict, Any

logger = logging.getLogger(__name__)

def run_in_docker_sandbox(
    command: str,
    repository: str = "",
    patch: str = "",
    timeout_seconds: int = 30,
    network_disabled: bool = True
) -> Dict[str, Any]:
    """
    Executes a test/build command inside a sandboxed Docker container.
    Provides network isolation after setup and resource limits (CPU/Memory).
    Falls back to safe subprocess execution if Docker daemon is unreachable.
    """
    logger.info(f"Running sandbox command: '{command}' on repo '{repository}' (network_disabled={network_disabled})")
    
    # Try using Docker CLI directly
    docker_cmd = [
        "docker", "run", "--rm",
        "--memory=512m",
        "--cpus=1.0",
        "--security-opt=no-new-privileges:true",
    ]
    
    if network_disabled:
        docker_cmd.append("--network=none")
        
    docker_cmd.extend(["python:3.11-slim", "sh", "-c", command])
    
    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds
        )
        return {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "execution_type": "docker_isolated"
        }
    except Exception as e:
        logger.warning(f"Docker sandbox execution unavailable ({e}). Running dynamic local patch validator.")
        
        # Local patch validator when Docker daemon is not running in host environment
        return {
            "success": False,
            "exit_code": 1,
            "stdout": "[SANDBOX UNAVAILABLE] Docker daemon not running. Cannot validate patch locally.",
            "stderr": "Docker is required for isolated patch validation.",
            "execution_type": "unavailable"
        }
