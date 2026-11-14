#!/usr/bin/env python3
"""
extract_review_units_batch.py

Batch version of extract_pr_review_units.py: reads a CSV where each row
references ONE individual human review comment, and writes ONE output JSON
per row/comment (not per PR) — because two different comments on the same
PR (even same file, even same commit hash in the URL) are two distinct
reviews and need to be evaluated independently.

To avoid redundant API calls, PR-level data (metadata, review comments) 
and Commit-level data (file diffs at the exact time of the comment) are 
fetched ONCE and cached in memory; the per-comment output files are then 
built from that cache.

Each output JSON looks like:
    {
      "repo": "...", "pr_number": ..., "pr_title": "...",
      "base_sha": "...", "head_sha": "...", "clone_url": "...",
      "comment_commit_sha": "...", 
      "path": "path/to/File.java",
      "patch": "@@ ... full diff of this file at that specific commit ...",
      "patch_truncated": false,
      "target_comment": {
          "comment_id": 1430117695, "commit_id": "...", "original_commit_id": "...",
          "body": "...", "diff_hunk": "...",
          "line": 42, "user": "...", "created_at": "..."
      },
      "all_human_comments_on_file": [ ... ]
    }

RESUMABLE: if the output file for a comment already exists, it's skipped
(unless --overwrite). Safe to Ctrl-C and rerun, or rerun after a rate limit.

USAGE
-----
    export GITHUB_TOKEN=github_pat_xxxxxxxxxxxx
    python3 extract_review_units_batch.py commits.csv output_dir/
    python3 extract_review_units_batch.py commits.csv output_dir/ --limit 5
    python3 extract_review_units_batch.py commits.csv output_dir/ \
        --repo-col repo_name --pr-col pr_number
"""

import argparse
import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = None

GITHUB_API = "https://api.github.com"
PR_URL_RE = re.compile(r"github\.com/([^/\s\"]+)/([^/\s\"]+)/pull/(\d+)")
COMMENT_ID_RE = re.compile(r"#r(\d+)")


def load_comment_rows_from_csv(csv_path, repo_col, pr_col):
    """
    Returns a list of dicts: {repo, pr_number, comment_id}, one per input row
    that could be parsed. Rows without a resolvable comment_id are skipped.
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        raw_rows = list(csv.reader(f))
    if not raw_rows:
        return [], 0

    first_row_text = ",".join(raw_rows[0])
    headerless = bool(PR_URL_RE.search(first_row_text))

    results = []
    unparsed = 0

    def try_structured(header, data_rows):
        out = []
        if repo_col in header and pr_col in header:
            ri, pi = header.index(repo_col), header.index(pr_col)
            for row in data_rows:
                if len(row) <= max(ri, pi):
                    continue
                repo, pr = row[ri].strip(), row[pi].strip()
                cid_match = COMMENT_ID_RE.search(",".join(row))
                if repo and pr and cid_match:
                    try:
                        out.append({"repo": repo, "pr_number": int(pr),
                                    "comment_id": int(cid_match.group(1))})
                    except ValueError:
                        pass
        return out

    if not headerless:
        header = raw_rows[0]
        data_rows = raw_rows[1:]
        results = try_structured(header, data_rows)
        if results:
            unparsed = len(data_rows) - len(results)
            return results, unparsed
    else:
        data_rows = raw_rows

    # URL-scan fallback
    for row in data_rows:
        row_text = ",".join(row)
        pr_match = PR_URL_RE.search(row_text)
        cid_match = COMMENT_ID_RE.search(row_text)
        if pr_match and cid_match:
            owner, repo_name, pr_number = pr_match.group(1), pr_match.group(2), int(pr_match.group(3))
            results.append({"repo": f"{owner}/{repo_name}", "pr_number": pr_number,
                             "comment_id": int(cid_match.group(1))})
        elif row_text.strip():
            unparsed += 1

    return results, unparsed


def github_request_all_pages(url, token):
    results = []
    while url:
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if token:
            req.add_header("Authorization", f"Bearer {token}")

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, context=SSL_CONTEXT) as resp:
                    body = json.loads(resp.read().decode())
                    results.extend(body)
                    remaining = resp.headers.get("X-RateLimit-Remaining")
                    if remaining is not None and int(remaining) < 3:
                        reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
                        sleep_s = max(0, reset - time.time()) + 1
                        print(f"    [rate limit low, sleeping {sleep_s:.0f}s]", file=sys.stderr)
                        time.sleep(sleep_s)
                    url = None
                    for part in resp.headers.get("Link", "").split(","):
                        if 'rel="next"' in part:
                            url = part.split(";")[0].strip().strip("<>")
                    break
            except urllib.error.HTTPError as e:
                if e.code == 403 and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
    return results


def github_request_single(url, token):
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode())


def fetch_pr_data(repo, pr_number, token):
    """Fetch PR metadata and comments (file listing removed to save API calls)."""
    owner, repo_name = repo.split("/")

    pr_meta = github_request_single(
        f"{GITHUB_API}/repos/{owner}/{repo_name}/pulls/{pr_number}", token
    )
    
    comments = github_request_all_pages(
        f"{GITHUB_API}/repos/{owner}/{repo_name}/pulls/{pr_number}/comments?per_page=100",
        token,
    )
    comments_by_id = {c["id"]: c for c in comments}
    comments_by_path = {}
    for c in comments:
        comments_by_path.setdefault(c["path"], []).append(c)

    return {
        "pr_meta": pr_meta,
        "comments_by_id": comments_by_id,
        "comments_by_path": comments_by_path,
    }

def fetch_compare_files(repo, base_sha, commit_sha, token):
    """Fetch the cumulative diff of the PR up to the specific commit when the review was made."""
    owner, repo_name = repo.split("/")
    url = f"{GITHUB_API}/repos/{owner}/{repo_name}/compare/{base_sha}...{commit_sha}"
    compare_data = github_request_single(url, token)
    return {f["filename"]: f for f in compare_data.get("files", [])}


def shape_comment(c):
    return {
        "comment_id": c["id"],
        "commit_id": c.get("commit_id"),
        "original_commit_id": c.get("original_commit_id"),
        "body": c.get("body"),
        "diff_hunk": c.get("diff_hunk"),
        "line": c.get("line"),
        "original_line": c.get("original_line"),
        "start_line": c.get("start_line"),
        "side": c.get("side"),
        "user": c.get("user", {}).get("login"),
        "created_at": c.get("created_at"),
        "in_reply_to_id": c.get("in_reply_to_id"),
    }


def build_comment_unit(repo, pr_number, comment_id, pr_data, commit_files_by_path, commit_sha):
    comment = pr_data["comments_by_id"].get(comment_id)
    if comment is None:
        raise KeyError(f"comment_id {comment_id} not found among this PR's review comments")

    path = comment["path"]
    f = commit_files_by_path.get(path)
    if f is None:
        raise KeyError(f"file '{path}' (from comment {comment_id}) not found in the diff for commit {commit_sha}")

    pr_meta = pr_data["pr_meta"]
    siblings = pr_data["comments_by_path"].get(path, [])

    return {
        "repo": repo,
        "pr_number": pr_number,
        "pr_title": pr_meta.get("title"),
        "base_sha": pr_meta["base"]["sha"],
        "head_sha": pr_meta["head"]["sha"],
        "base_ref": pr_meta["base"]["ref"],
        "head_ref": pr_meta["head"]["ref"],
        "clone_url": pr_meta["base"]["repo"]["clone_url"],
        "comment_commit_sha": commit_sha,
        "path": path,
        "previous_filename": f.get("previous_filename"),
        "status": f.get("status"),
        "additions": f.get("additions"),
        "deletions": f.get("deletions"),
        "patch": f.get("patch"),
        "patch_truncated": "patch" not in f,
        "target_comment": shape_comment(comment),
        "all_human_comments_on_file": [shape_comment(c) for c in siblings],
    }


def safe_filename(repo, pr_number, comment_id):
    return f"{repo.replace('/', '__')}__pr{pr_number}__c{comment_id}.json"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path")
    ap.add_argument("output_dir")
    ap.add_argument("--repo-col", default="repo_name")
    ap.add_argument("--pr-col", default="pr_number")
    ap.add_argument("--limit", type=int, default=None, help="process only the first N rows")
    ap.add_argument("--overwrite", action="store_true", help="reprocess even if output already exists")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("WARNING: no GITHUB_TOKEN set, limited to 60 req/hour unauthenticated.",
              file=sys.stderr)

    os.makedirs(args.output_dir, exist_ok=True)
    summary_path = os.path.join(args.output_dir, "_summary.csv")

    rows, unparsed = load_comment_rows_from_csv(args.csv_path, args.repo_col, args.pr_col)
    if not rows:
        print("ERROR: could not extract any (repo, pr_number, comment_id) rows from this CSV.",
              file=sys.stderr)
        sys.exit(1)
    if unparsed:
        print(f"WARNING: {unparsed} row(s) had no resolvable comment_id and were skipped.",
              file=sys.stderr)

    # de-dupe identical rows only (same PR + same comment), preserve order
    seen = set()
    unique_rows = []
    for r in rows:
        key = (r["repo"], r["pr_number"], r["comment_id"])
        if key not in seen:
            seen.add(key)
            unique_rows.append(r)

    print(f"{len(rows)} rows parsed -> {len(unique_rows)} distinct comments "
          f"across {len({(r['repo'], r['pr_number']) for r in unique_rows})} PRs", file=sys.stderr)

    if args.limit:
        unique_rows = unique_rows[:args.limit]
        print(f"Limiting to first {len(unique_rows)} rows", file=sys.stderr)

    pr_cache = {}      # (repo, pr_number) -> pr_data
    commit_cache = {}  # (repo, commit_sha) -> files_by_path
    summary_rows = []

    for i, r in enumerate(unique_rows, 1):
        repo, pr_number, comment_id = r["repo"], r["pr_number"], r["comment_id"]
        out_path = os.path.join(args.output_dir, safe_filename(repo, pr_number, comment_id))
        print(f"[{i}/{len(unique_rows)}] {repo}#{pr_number} comment {comment_id}", file=sys.stderr)

        if os.path.exists(out_path) and not args.overwrite:
            print("  skipped (already exists)", file=sys.stderr)
            summary_rows.append({"repo": repo, "pr_number": pr_number, "comment_id": comment_id,
                                  "status": "skipped_exists", "error": ""})
            continue

        pr_key = (repo, pr_number)
        try:
            # 1. Ensure PR metadata and comments are loaded
            if pr_key not in pr_cache:
                pr_cache[pr_key] = fetch_pr_data(repo, pr_number, token)
            
            # 2. Extract the comment to identify its specific commit SHA
            comment = pr_cache[pr_key]["comments_by_id"].get(comment_id)
            if not comment:
                raise KeyError(f"comment_id {comment_id} not found in PR {pr_number}")
            
            # original_commit_id is the exact commit the user left the review on
            commit_sha = comment.get("original_commit_id") or comment.get("commit_id")
            if not commit_sha:
                raise ValueError(f"comment_id {comment_id} lacks both original_commit_id and commit_id")

            commit_key = (repo, commit_sha)
            
            # 3. Ensure the exact commit state is loaded
            if commit_key not in commit_cache:
                base_sha = pr_cache[pr_key]["pr_meta"]["base"]["sha"]
                commit_cache[commit_key] = fetch_compare_files(repo, base_sha, commit_sha, token)

            # 4. Build output using the cumulative diff at the time of the review
            unit = build_comment_unit(repo, pr_number, comment_id, pr_cache[pr_key], 
                                      commit_cache[commit_key], commit_sha)
            
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(unit, fh, indent=2, ensure_ascii=False)
            
            print("  ok", file=sys.stderr)
            summary_rows.append({"repo": repo, "pr_number": pr_number, "comment_id": comment_id,
                                  "status": "ok", "error": ""})
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            summary_rows.append({"repo": repo, "pr_number": pr_number, "comment_id": comment_id,
                                  "status": "error", "error": str(e)})

    write_header = not os.path.exists(summary_path)
    with open(summary_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["repo", "pr_number", "comment_id", "status", "error"])
        if write_header:
            writer.writeheader()
        writer.writerows(summary_rows)

    n_ok = sum(1 for r in summary_rows if r["status"] == "ok")
    n_err = sum(1 for r in summary_rows if r["status"] == "error")
    n_skip = sum(1 for r in summary_rows if r["status"] == "skipped_exists")
    print(f"\nDone: {n_ok} ok, {n_err} errors, {n_skip} skipped. Summary: {summary_path}",
          file=sys.stderr)


if __name__ == "__main__":
    main()