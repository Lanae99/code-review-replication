from __future__ import annotations

import argparse
import os
from pathlib import Path
import textwrap
from dotenv import load_dotenv
from openai import OpenAI

SCRIPT_DIR = Path(__file__).resolve().parent

# Default model used when MSWEA_MODEL_NAME is not set in the .env file or environment.
DEFAULT_MODEL = "gpt-5.4-mini"


def setup_environment() -> tuple[str, dict[str, str]]:
  """
    Loads .env, verifies the API key, and prepares the environment dictionary.
    """
  load_dotenv(SCRIPT_DIR / ".env")

  if "OPENAI_API_KEY" not in os.environ:
    raise RuntimeError(
        "Missing OPENAI_API_KEY. Define it in .env or in the environment."
    )

  model = os.environ.get("MSWEA_MODEL_NAME", DEFAULT_MODEL)

  # Prepare child environment dictionary
  child_env = os.environ.copy()
  child_env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
  child_env["MSWEA_MODEL_NAME"] = model
  child_env["MSWEA_CONFIGURED"] = "1"

  return model, child_env


def zero_shot_revision(
    client: OpenAI,
    model: str,
    rel_path: str,
    diff: str,
    file_content: str | None,
) -> str:
  # 1. SYSTEM PROMPT: General verification rules and required output format
  system_prompt = textwrap.dedent("""
    You are an expert software engineer performing a code review. 
    Your objective is to identify issues, related but NOT LIMITED to: correctness (e.g. bugs, logical flaws), architectural weaknesses, maintainability, readability, documentation or testability. 

    VERIFICATION RULES (CRITICAL): 
    1. STATIC ANALYSIS ONLY: You must rely entirely on a rigorous static analysis of the provided diff and file context. Carefully read and evaluate every single modified or added line.
    2. LOGICAL DEDUCTION: If you suspect a logical flaw, you MUST report it based on rigorous logical deduction, explaining exactly how it could fail or degrade the system based purely on the provided code.
    3. ZERO-TOLERANCE FOR FLAWS (CRITICAL): Do NOT assume overly broad conditions, weak logic, or missing validations are "intentional" or "by design". If your analysis reveals a scenario where the code matches too broadly, behaves incorrectly in edge-cases, or lacks necessary architectural safeguards, you MUST output 'Issue: Yes'. Do not excuse or justify bad practices.
    4. VISIBLE MITIGATIONS (THE EXCEPTION): If the provided code explicitly shows that the edge-case is handled (e.g., a suspected case-sensitivity issue is visibly handled by an `equalsIgnoreCase` method in the diff or immediately surrounding context), you MUST DISMISS the flaw and NOT report it. Do not invent mitigations that are not visible in the provided text.
    5. EXHAUSTIVE SEARCH: Do not stop regardless of how many issues you have already found. You must continuously evaluate the modified lines in the diff across different categories. Your review is only complete when the entire diff has been thoroughly evaluated and absolutely zero suspected flaws remain unchecked.
    
    FINAL OUTPUT INSTRUCTIONS:
    You must structure your final answer EXACTLY as requested in the final-output protocol.
    Under 'Code snippets reviewed', you must output the EXACT, verbatim lines of code from the diff you are referring to, enclosed in quotes. Do not modify the text.

    Issue: <Yes | No>

    Review comment #1:
    {
    Code snippet reviewed:
    - "<Exact, verbatim lines of code from the diff. Do not modify the text.>"

    Review comment:
    - <Clear description of the verified issue, why it fails or degrades the system, and a concrete recommendation>
    }
    """).strip()

  # 2. USER PROMPT: Dynamically inject the file path, full file content
  # (for context beyond the diff), and the diff itself.
  if file_content is not None:

    user_prompt = textwrap.dedent(f"""
        You are reviewing a specific change in the file: {rel_path}

        Full current content of the file, for context:
        <file>
        {file_content}
        </file>

        The file has been subject to the following code change:
        <diff>
        {diff}
        </diff>
        """).strip()
  else:
    # Fallback: unable to read the full file content from disk
    # (e.g., deleted file or invalid path).
    return False

  response = client.chat.completions.create(
      model=model,
      messages=[
          {"role": "system", "content": system_prompt},
          {"role": "user", "content": user_prompt},
      ],
      temperature=0.2,
  )

  return response.choices[0].message.content.strip(), system_prompt, user_prompt


def main():
  parser = argparse.ArgumentParser(
      description=(
          "Zero-shot code review baseline runner, invoked by the batch runner"
          " script."
      )
  )
  parser.add_argument(
      "--repo",
      type=Path,
      required=True,
      help=(
          "Absolute path to the local repository (the commit checkout is"
          " already performed by the batch runner script)."
      ),
  )
  parser.add_argument(
      "--path",
      type=str,
      required=True,
      help=(
          "Relative path of the file within the repository referenced by the"
          " diff."
      ),
  )
  parser.add_argument(
      "--patch",
      type=str,
      required=True,
      help="Content of the diff to be reviewed by the model.",
  )
  parser.add_argument(
      "--path_output",
      type=str,
      required=True,
      help=(
          "Run label (e.g., 'Run_265'): determines the output folder "
          "runs/<path_output>/ where the result is written."
      ),
  )

  args = parser.parse_args()

  model_name, child_env = setup_environment()
  client = OpenAI(api_key=child_env["OPENAI_API_KEY"])

  api_model_name = (
      model_name.split("/", 1)[-1] if "/" in model_name else model_name
  )

  # Full path on the filesystem: local repository (already checked out at the target
  # commit by the batch runner) + relative path of the file inside the repository.
  full_path = args.repo / args.path
  diff = args.patch or ""

  try:
    file_content = full_path.read_text(encoding="utf-8")
  except (FileNotFoundError, UnicodeDecodeError, IsADirectoryError) as e:
    print(
        f"⚠️  Unable to read full file from {full_path} ({e}); proceeding with"
        " diff only."
    )
    file_content = None

  print(
      f"Starting zero-shot review with model: {model_name} on file"
      f" {args.path}..."
  )

  revised_content, system_prompt, user_prompt = zero_shot_revision(
      client, api_model_name, args.path, diff, file_content
  )

  # The batch runner expects this script to create <script_dir>/runs18/<path_output>/
  # and write the output file inside it (default "final_output.txt").
  run_dir = SCRIPT_DIR / "runs" / args.path_output
  run_dir.mkdir(parents=True, exist_ok=True)

  output_path = run_dir / "final_output.txt"
  output_path.write_text(revised_content, encoding="utf-8")

  # Also save the exact prompt sent to the model (system + user) for reproducibility.
  prompt_path = run_dir / "prompt.txt"
  prompt_text = (
      "=== SYSTEM PROMPT ===\n"
      f"{system_prompt}\n\n"
      "=== USER PROMPT ===\n"
      f"{user_prompt}\n"
  )
  prompt_path.write_text(prompt_text, encoding="utf-8")

  # print(f"Review completed. Saved to: {output_path}")
  print(f"Prompt saved to: {prompt_path}")


if __name__ == "__main__":
  main()