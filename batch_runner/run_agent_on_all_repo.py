"""
Batch runner for both the autonomous code review agent (agent.py) and the
zero-shot baseline (zeroshot.py).

Iterates over a CSV of GitHub URLs (format:
https://github.com/<owner>/<repo>/pull/<pr>/files/<commit_sha>#r<comment_id>),
and for each row:
  1. locates the JSON file containing the data (path/patch) for the comment
  2. checks out the corresponding commit in the local repository
  3. runs the selected review script (agent.py or zeroshot.py via --agent-script),
     passing a run label (e.g., "Run_3")
  4. reads the final_output.txt file written by the agent or zero-shot script
  5. parses the raw output and saves it as a structured JSON file

Usage:
    # Run the autonomous code review agent:
    python3 run_agent_all_repo.py --csv urls.csv --agent-script ../run_agent/agent.py

    # Run the zero-shot baseline:
    python3 run_agent_all_repo.py --csv urls.csv --agent-script ../run_agent/zeroshot.py

    (check argparse / run with --help to see more arguments)
"""

import json
import subprocess
import os
import re
import csv
import argparse


# ---------------------------------------------------------------------------
# Agent / Zero-shot output parsing
# ---------------------------------------------------------------------------

def clean_line(line):
    line = line.strip()
    line = re.sub(r'^\\*"+', '', line)
    line = re.sub(r'\\*"+$', '', line)
    line = re.sub(r'^(?:\\+t|\s)+', '', line)
    line = line.lstrip('\\')
    line = re.sub(r'\\+"', '"', line)
    return line.strip()

def parse_agent_output(raw_text, path=None):
    result = {"issue_found": False, "reviews": []}
    
    # Convert literal escaped newlines into actual newline characters
    raw_text = raw_text.replace('\\n', '\n')

    issue_match = re.search(r"Issue:\s*(Yes|No)", raw_text, re.IGNORECASE)
    if issue_match:
        result["issue_found"] = issue_match.group(1).lower() == "yes"
        if not result["issue_found"]:
            return result

    header_pattern = re.compile(r"(Review comment\s*#\s*\d+):\s*", re.IGNORECASE)
    headers = list(header_pattern.finditer(raw_text))

    for i, h in enumerate(headers):
        title = h.group(1).strip()
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw_text)
        section = raw_text[start:end]

        # Strip optional outer curly braces if present
        section = re.sub(r'^[\s\n]*\{', '', section)
        section = re.sub(r'\}[\s\n]*$', '', section)

        # Use DOTALL regexes to extract blocks regardless of internal whitespace/newlines
        code_part = ""
        comment_part = ""
        
        # Match everything between "Code snippet reviewed:" and "Review comment:"
        code_match = re.search(r"Code snippet(?:s)? reviewed:\s*(.*?)(?=Review comment|Comment|$)", section, re.IGNORECASE | re.DOTALL)
        if code_match:
            code_part = code_match.group(1)

        # Match everything after "Review comment:"
        comment_match = re.search(r"(?:Review comment(?:s)?|Comment):\s*(.*)", section, re.IGNORECASE | re.DOTALL)
        if comment_match:
            comment_part = comment_match.group(1)

        # Extract bulleted items from a text block
        def extract_bullets(text_block):
            items = []
            for line in text_block.split('\n'):
                line = line.strip()
                # Match any bullet character: -, –, —, *, •
                if re.match(r'^[-–—*•]', line):
                    # Strip the bullet character and trailing whitespace
                    cleaned = re.sub(r'^[-–—*•]\s*', '', line).strip()
                    if cleaned:
                        items.append(cleaned)
            return items

        snippets = extract_bullets(code_part)
        comments = extract_bullets(comment_part)

        result["reviews"].append({
            "review_title": title,
            "snippets_reviewed": [clean_line(s) for s in snippets],
            "comment": comments
        })

    return result


# ---------------------------------------------------------------------------
# GitHub URL parsing -> owner / repo / pr / commit / comment_id
# ---------------------------------------------------------------------------

URL_PATTERN = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)/files/([0-9a-fA-F]+)#r(\d+)"
)


def parse_github_url(url):
    match = URL_PATTERN.match(url.strip())
    if not match:
        return None
    owner, repo, pr_number, commit_sha, comment_id = match.groups()
    return {
        "owner": owner,
        "repo": repo,
        "pr_number": pr_number,
        "commit_sha": commit_sha,
        "comment_id": comment_id,
    }


# ---------------------------------------------------------------------------
# Git checkout
# ---------------------------------------------------------------------------

def git_checkout_commit(repo_path, commit_sha):
    r = subprocess.run(["git", "checkout", "--force", commit_sha],
                        cwd=repo_path, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"   ❌ git checkout failed: {r.stderr.strip()}")
        return False
    return True


# ---------------------------------------------------------------------------
# Single CSV row processing
# ---------------------------------------------------------------------------

def process_row(url, jsons_dir, repos_base_dir, agent_script_abs, output_dir,
                 agent_output_filename, run_index, dry_run=False, parse_only = False):
    info = parse_github_url(url)
    if info is None:
        print(f"⚠️  Unrecognized URL, skipping: {url}")
        return False

    owner, repo = info["owner"], info["repo"]
    pr_number, commit_sha, comment_id = info["pr_number"], info["commit_sha"], info["comment_id"]

    json_filename = f"{owner}__{repo}__pr{pr_number}__c{comment_id}.json"
    json_path = os.path.join(jsons_dir, json_filename)

    if not os.path.exists(json_path):
        print(f"⚠️  Data file not found: {json_path}, skipping.")
        return False

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    path = data.get("path")
    patch = data.get("patch")

    repo_path = os.path.join(repos_base_dir, repo)
    if not os.path.isdir(repo_path):
        print(f"⚠️  Repository directory not found: {repo_path}, skipping.")
        return False

    print(f"→ Checking out commit {commit_sha[:10]} in {repo_path}")
    if not git_checkout_commit(repo_path, commit_sha):
        print(f"❌ Skipping {json_filename} due to checkout error.")
        return False

    # The target script (agent.py or zeroshot.py) expects only a run LABEL (not a full path)
    # in --path_output. Using that label, it creates <agent_runs_base>/runs/<run_label>/
    # and writes the .txt output file inside it. We reconstruct the same path here to read it back.
    run_label = f"Run_{run_index}"
    agent_runs_base = os.path.dirname(agent_script_abs)  # e.g. .../run_agent

    repo_display_name = f"{owner}/{repo}"          # for logging/display only
    repo_path_abs = os.path.abspath(repo_path)     # actual path passed to the script

    if not parse_only:


        if dry_run:
            expected_output = os.path.join(agent_runs_base, "runs", run_label, agent_output_filename)
            cmd = ["python3", agent_script_abs,
                "--repo", repo_path_abs,
                "--path", str(path),
                "--patch", "<patch omitted, " + str(len(patch or "")) + " chars>",
                "--path_output", run_label]
            print("🧪 DRY RUN — command that would be executed:")
            print(f"   cwd: {repo_path}")
            print("   " + " ".join(cmd))
            print(f"   expected output at: {expected_output}")
            return True

        print(f"Running agent on {repo_display_name} for file {path} (run label: {run_label})...")
        try:
            subprocess.run(
                [
                    "python3", agent_script_abs,
                    "--repo", repo_path_abs,
                    "--path", path,
                    "--patch", patch,
                    "--path_output", run_label,
                ],
                cwd=repo_path,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            print(f"❌ Agent exited with return code {e.returncode} for {json_filename}")
            return True  # advance the counter anyway since the run label was consumed

    else:
        print(f"🔎 Parse-only on {repo_display_name}, file {path} (run label: {run_label})")

    agent_output_path = os.path.join(agent_runs_base, "runs", run_label, agent_output_filename)
    if not os.path.exists(agent_output_path):
        print(f"⚠️  File {agent_output_path} was not found for {json_filename}.")
        return True

    with open(agent_output_path, "r", encoding="utf-8") as f:
        raw_output = f.read().strip()

    if raw_output.endswith('EOF"}'):
        raw_output = raw_output[:-len('EOF"}')]
    raw_output = raw_output.replace('\\n', '\n')

    parsed_data = parse_agent_output(raw_output, path)

    os.makedirs(output_dir, exist_ok=True)
    out_name = f"structured_review__{owner}__{repo}__pr{pr_number}__c{comment_id}.json"
    out_path = os.path.join(output_dir, out_name)
    with open(out_path, "w", encoding="utf-8") as f_json:
        json.dump(parsed_data, f_json, indent=4, ensure_ascii=False)

    print(f"✅ Saved: {out_path}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Batch runner for both the autonomous code review agent (agent.py) and the zero-shot baseline (zeroshot.py) across multiple PRs/repos"
    )
    parser.add_argument("--csv", default="urls.csv", help="CSV file with a single column of GitHub URLs")
    parser.add_argument("--jsons-dir", default=".", help="Directory containing <owner>__<repo>__pr<N>__c<id>.json files")
    parser.add_argument("--repos-base-dir", default="../agent_workspace", help="Base directory containing the cloned repositories")
    parser.add_argument("--agent-script", default="../run_agent/agent.py",
                         help="Path to the review script to execute (e.g., ../run_agent/agent.py for the agent or ../run_agent/zeroshot.py for the zero-shot baseline)")
    parser.add_argument("--agent-output-filename", default="final_output.txt",
                         help="Name of the output file written by the script inside run_agent/runs/Run_xxxx/")
    parser.add_argument("--output-dir", default="structured_reviews",
                         help="Directory where structured JSON outputs will be saved")
    parser.add_argument("--dry-run", action="store_true",
                         help="Parse URL + checkout commit without running the script; only print the command")
    parser.add_argument("--parse-only", action="store_true",
                     help="Do not run the script; only read and parse existing final_output.txt files")
    args = parser.parse_args()

    # Resolve absolute path now: the script is executed with cwd set to the target repository,
    # so a relative path would resolve against the wrong directory otherwise.
    agent_script_abs = os.path.abspath(args.agent_script)

    with open(args.csv, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = [row[0].strip() for row in reader if row and row[0].strip().startswith("http")]

    print(f"Found {len(rows)} URLs to process.\n")

    run_counter = 0
    for i, url in enumerate(rows, 1):
        print(f"=== [{i}/{len(rows)}] {url} ===")
        used = process_row(
            url,
            jsons_dir=args.jsons_dir,
            repos_base_dir=args.repos_base_dir,
            agent_script_abs=agent_script_abs,
            output_dir=args.output_dir,
            agent_output_filename=args.agent_output_filename,
            run_index=run_counter,
            dry_run=args.dry_run,
            parse_only = args.parse_only,
        )
        if used:
            run_counter += 1
        print()


if __name__ == "__main__":
    main()