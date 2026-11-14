#!/usr/bin/env python3
"""
Generic mini-swe-agent runner

mini-swe-agent: https://mini-swe-agent.com/latest/

Edit the constants in the CONFIGURATION section, then run:

    python3 agent.py --repo <repo_path> --path <file_path> --patch <diff> --path_output <run_label>

Expected .env file where to set OPENAI key and model:
    OPENAI_API_KEY=sk-...
    MSWEA_MODEL_NAME=openai/gpt-5.4-mini   # optional
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

parser = argparse.ArgumentParser(description="Code Review Agent")
    
parser.add_argument("--repo", required=True, help="Repository path or name (e.g., skylot/jadx)")
parser.add_argument("--path", required=True, help="Relative path to the target file")
parser.add_argument("--patch", required=True, help="Diff containing the code changes")
parser.add_argument("--path_output", required=True, help="Output run directory label")
    
args = parser.parse_args()
    
repo_name = args.repo
file_path = args.path
patch_content = args.patch
output_path = args.path_output

SCRIPT_DIR = Path(__file__).resolve().parent

# Directory where the agent should operate.
WORKDIR = SCRIPT_DIR / repo_name

# Task definition.
TASK = f"""
You are an expert software engineer performing a code review. You are reviewing a specific change in the file: {file_path}

The file has been subject to the following code change:
<diff>
{patch_content}
</diff>

Your objective is to identify issues, related but NOT LIMITED to: correctness (e.g. bugs, logical flaws), architectural weaknesses, maintainability, readability, documentation or testability. 



VERIFICATION RULES (CRITICAL): 
1. You MUST explore the repository with bash command turns (e.g., `grep -rn`, `cat`, `find`) to investigate the codebase BEFORE concluding.
2. ENVIRONMENT FALLBACK: If a build/test command fails with an environment or version error, IGNORE IT. Do not attempt to fix the environment. Fall back to static analysis with grep/cat/find.
3. LOGICAL DEDUCTION: If you suspect a logical flaw that is not covered by existing tests, you MUST report it based on rigorous logical deduction, explaining exactly how it could fail.
4. ZERO-TOLERANCE FOR FLAWS (CRITICAL): Do NOT assume overly broad conditions, weak test assertions, or missing test fixtures are "intentional" or "by design". If your static analysis reveals a scenario where the code matches too broadly, behaves incorrectly in edge-cases, or lacks necessary architectural files (e.g., missing .smali test files), you MUST output 'Issue: Yes'. Do not excuse or justify bad practices.
5. VERIFIED MITIGATIONS (THE EXCEPTION): If your bash exploration explicitly proves that the codebase actively handles your suspected edge-case under the hood (e.g., a suspected case-sensitivity issue is actually handled by an `equalsIgnoreCase` method, or bounds are checked downstream), you MUST DISMISS the flaw and NOT report it.
6. EXHAUSTIVE SEARCH: Do not stop regardless of how many issues you have already found (whether 1, 3, or more). You must continuously evaluate the modified lines in the diff across different categories. Your search is only complete when the entire diff has been evaluated and absolutely zero suspected flaws remain. For every suspect found, rigorously verify it (using the file or repository context if needed), report it if valid, and immediately resume scanning the diff.


FINAL OUTPUT INSTRUCTIONS:
You must structure your final answer EXACTLY as requested in the final-output protocol. To ensure you do not skip your logical deduction, you MUST include a <scratchpad> block INSIDE your final output, exactly as shown in the required format.

Under 'Code snippets reviewed', you must output the EXACT, verbatim lines of code from the diff you are referring to, enclosed in quotes. Do not modify the text.
""".strip()

# The required final answer format after COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT.
OUTPUT_FORMAT = """
Issue: <Yes | No>

Review comment #1:
{
Code snippet reviewed:
- "<Exact, verbatim lines of code from the diff. Do not modify the text.>"

Review comment:
- <Clear description of the verified issue, why it fails or degrades the system, and a concrete recommendation>
}

Review comment #2:
{...}

(Repeat one block per distinct issue found. There is no fixed number of 
review comments: output as many blocks as there are real issues, and no more. 
If you find only one issue, output only one block. If you find none, 
write "Issue: No" and stop.)
""".strip()

# Output directory for trajectories/logs.
RUNS_ROOT_DIR = SCRIPT_DIR / "runs20"
RUN_NAME = output_path # None = timestamped run directory, or set e.g. "my_run_001"

# Model and agent limits.
DEFAULT_MODEL = "openai/gpt-5.6-luna"
MAX_STEPS = 20       # to set accordingly with the task
COST_LIMIT = 1.00   # to set accordingly with the task

# For non-interactive subprocess runs, YOLO should usually be True.
YOLO = True
# Print the agent's live stdout/stderr while it runs.
# When False, output is still saved to live.log but not shown in the terminal.
VERBOSE_AGENT_OUTPUT = False


# ============================================================
# mini-swe-agent config
# ============================================================

AGENT_SYSTEM_TEMPLATE = """
You are a careful autonomous agent that completes tasks by using bash commands.
You can inspect files and run shell commands in the current working directory.
Be concise, avoid destructive actions, and follow the requested final-output protocol exactly.
""".strip()

AGENT_INSTANCE_TEMPLATE = """
Please complete this task:

{{task}}

Operating rules:
- Every assistant step must use the bash tool.
- Work only in the current working directory unless the task explicitly says otherwise.
- Prefer read-only inspection commands when the task does not require edits.
- Do not modify files unless the task explicitly asks for modifications.
- Do not create patches unless the task explicitly asks for code changes.

Final-output protocol:
- When you have collected enough evidence, finish by executing exactly one final bash command.
- The final bash command must print COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT on the first line.
- The same final bash command must print the final answer after that first line.
- Use this exact shape for the final command:

cat <<'EOF'
COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
{{output_format}}
EOF

Do not run any commands after the final submission command.
""".strip()


# ============================================================
# Environment
# ============================================================

def load_environment() -> str:
    """Load .env and return the configured model name."""
    load_dotenv(SCRIPT_DIR / ".env")

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Missing OPENAI_API_KEY. Define it in .env or in the environment.")

    return os.getenv("MSWEA_MODEL_NAME", DEFAULT_MODEL)


# ============================================================
# Prompt
# ============================================================

def build_generic_prompt(user_task: str, output_format: str) -> str:
    """Create the task prompt, including the strict final-output format."""
    return f"""
Goal:
{user_task.strip()}

Output format:
{output_format.strip()}
""".strip()


def render_instance_template(template: str, output_format: str) -> str:
    """Fill placeholders that mini-swe-agent itself will not know about."""
    return template.replace("{{output_format}}", output_format.strip())


# ============================================================
# Trajectory/output helpers
# ============================================================

FINAL_SENTINEL = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def iter_strings(obj: Any):
    """Yield all string values recursively from a JSON-like object."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from iter_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_strings(item)


def extract_after_sentinel(text: str) -> str | None:
    """Return everything after the final-output sentinel in a command/log string."""
    if FINAL_SENTINEL not in text:
        return None
    after = text.split(FINAL_SENTINEL, 1)[1]
    return after.strip()


def extract_final_output_from_trajectory(trajectory_file: Path) -> str | None:
    """
    Extract the submitted final output from a mini-swe-agent trajectory.

    This searches every string in the trajectory for COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
    and returns the text after the last occurrence. This is more robust than relying on
    one specific trajectory schema.
    """
    if not trajectory_file.exists():
        return None

    try:
        data = json.loads(trajectory_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    matches = []
    for text in iter_strings(data):
        extracted = extract_after_sentinel(text)
        if extracted:
            matches.append(extracted)

    return matches[-1] if matches else None


def extract_final_output_from_log(log_file: Path) -> str | None:
    """Fallback final-output extraction from the live log."""
    if not log_file.exists():
        return None

    text = log_file.read_text(encoding="utf-8", errors="replace")
    index = text.rfind(FINAL_SENTINEL)
    if index == -1:
        return None

    return text[index + len(FINAL_SENTINEL):].strip()


def save_final_output_if_available(
    trajectory_file: Path,
    live_log_file: Path,
    final_output_file: Path,
) -> None:
    """Save the submitted final answer in a plain text file, when available."""
    final_output = extract_final_output_from_trajectory(trajectory_file)
    if final_output is None:
        final_output = extract_final_output_from_log(live_log_file)

    if final_output is None:
        print(f"No submitted final output found in trajectory/log.")
        return

    final_output_file.write_text(final_output.rstrip() + "\n", encoding="utf-8")


# ============================================================
# Runner
# ============================================================

def run_agent() -> int:
    """Run mini-swe-agent and save prompt, trajectory, live log, and final output."""
    model = load_environment()
    workdir = WORKDIR.resolve()

    if not workdir.exists():
        raise FileNotFoundError(f"Working directory does not exist: {workdir}")

    run_name = RUN_NAME or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    run_dir = (RUNS_ROOT_DIR / run_name).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    prompt_file = run_dir / "prompt.txt"
    trajectory_file = run_dir / "trajectory.traj.json"
    live_log_file = run_dir / "live.log"
    final_output_file = run_dir / "final_output.txt"

    prompt = build_generic_prompt(TASK, OUTPUT_FORMAT)
    prompt_file.write_text(prompt + "\n", encoding="utf-8")

    instance_template = render_instance_template(AGENT_INSTANCE_TEMPLATE, OUTPUT_FORMAT)

    cmd = [
        "mini",
        "--task",
        prompt,
        "--exit-immediately",
        "--cost-limit",
        str(COST_LIMIT),
        "-c",
        "mini.yaml",
        "-c",
        str(SCRIPT_DIR / "mini_config.yaml"),
        "-c",
        f"agent.system_template={AGENT_SYSTEM_TEMPLATE}",
        "-c",
        f"agent.instance_template={instance_template}",
        "-c",
        f"agent.step_limit={MAX_STEPS}",
        "-o",
        str(trajectory_file),
    ]

    if YOLO:
        cmd.append("--yolo")

    child_env = os.environ.copy()
    child_env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
    child_env["MSWEA_MODEL_NAME"] = model
    child_env["MSWEA_CONFIGURED"] = "1"

    print(f"Running mini-swe-agent in: {workdir}")
    print(f"Run directory: {run_dir}")
    print(f"Model: {model}")
    print(f"Max steps: {MAX_STEPS}")
    print(f"YOLO: {YOLO}")

    with live_log_file.open("w", encoding="utf-8") as log_file:
        
        process = subprocess.Popen(
            cmd,
            cwd=workdir,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            env=child_env,
        )

        assert process.stdout is not None

        for line in process.stdout:
            if VERBOSE_AGENT_OUTPUT:
                print(line, end="")
            log_file.write(line)
            log_file.flush()

        return_code = process.wait()

    save_final_output_if_available(trajectory_file, live_log_file, final_output_file)

    status = "success" if return_code == 0 else f"failed exit_code={return_code}"

    metadata = {
        "status": status,
        "return_code": return_code,
        "workdir": str(workdir),
        "model": model,
        "cost_limit": COST_LIMIT,
        "max_steps": MAX_STEPS,
        "yolo": YOLO,
        "task": TASK,
        "output_format": OUTPUT_FORMAT,
        "prompt_file": str(prompt_file),
        "trajectory_file": str(trajectory_file),
        "live_log_file": str(live_log_file),
        "final_output_file": str(final_output_file),
    }
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Finished with status: {status}.")
    print(f"Trajectory: {trajectory_file}")
    print(f"Live log: {live_log_file}")
    print(f"Final output: {final_output_file}")

    return return_code


if __name__ == "__main__":
    raise SystemExit(run_agent())