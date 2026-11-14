#!/usr/bin/env python3
"""
Fallback patch extraction utility: extracts the file diff introduced by a specific
commit directly from a local Git repository and outputs it as a JSON-encoded string.
"""

import argparse
import subprocess
import sys
import json

def extract_patch(repo_path, commit_sha, file_path):
    """
    Extracts the diff (patch) of a specific file introduced by a specific commit.
    The ^! operator instructs Git to show the difference between the commit and its parent.
    """
    cmd = [
        "git", "-C", repo_path, 
        "diff", f"{commit_sha}^!", 
        "--", file_path
    ]
    
    try:
        # Execute the git command and capture the output
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout.strip()
        
    except subprocess.CalledProcessError as e:
        print(f"❌ Git error: {e.stderr.strip()}", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("❌ Error: 'git' command not found on the system.", file=sys.stderr)
        return None

def main():
    parser = argparse.ArgumentParser(description="Extracts the patch of a file at a specific commit from a local repository.")
    parser.add_argument("--repo", required=True, help="Absolute or relative path to the repository directory")
    parser.add_argument("--commit", required=True, help="The commit hash (e.g., 936dacd745fe15850342c9f07e5330a80c84c194)")
    parser.add_argument("--file", required=True, help="The path of the modified file (e.g., src/test/java/...)")
    
    args = parser.parse_args()
    
    patch = extract_patch(args.repo, args.commit, args.file)
    
    if patch:
        print(json.dumps(patch))
    elif patch == "":
        print(f"⚠️ No patch found. File '{args.file}' may not have been modified in commit {args.commit[:7]}, or the path is incorrect.", file=sys.stderr)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()