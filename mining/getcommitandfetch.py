"""
Fetches and checks out the exact review commit (original_commit_id) for PR comment URLs
that do not embed the commit SHA directly in the URL path.
"""

import os
import re
import requests
import time

def get_commit_sha(url, github_token=None):
    """Calls the GitHub API to retrieve the exact commit hash associated with the review comment."""
    # Regex captures: 1=owner, 2=repo, 3=pr_number, 4=comment_id
    match = re.search(r'github\.com/([^/]+)/([^/]+)/pull/(\d+)/files/#r(\d+)', url)
    
    if not match:
        return None, None, "Invalid URL"

    owner = match.group(1)
    repo = match.group(2)
    comment_id = match.group(4)
    
    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/comments/{comment_id}"
    
    headers = {"Accept": "application/vnd.github.v3+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
        
    try:
        response = requests.get(api_url, headers=headers)
        
        if response.status_code == 200:
            commit_sha = response.json().get("original_commit_id")
            return repo, commit_sha, "OK"
        else:
            return repo, None, f"API error {response.status_code}"
            
    except requests.exceptions.RequestException as e:
        return repo, None, f"Network error: {e}"


# --- MAIN EXECUTION ---

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # Set GITHUB_TOKEN in your environment to avoid 403 rate limits

# List of PR comment URLs without embedded commit hashes
url_list = [
    "https://github.com/apache/flink/pull/24484/files/#r1525054790",
    "https://github.com/apache/flink/pull/24483/files/#r1523106513", 
    "https://github.com/apache/flink/pull/24429/files/#r1514835462", 
    "https://github.com/apache/flink/pull/24429/files/#r1514866138", 
    "https://github.com/apache/flink/pull/24402/files/#r1517422921", 
    "https://github.com/apache/flink/pull/24397/files/#r1507529592", 
    "https://github.com/apache/flink/pull/24387/files/#r1503776636", 
    "https://github.com/apache/flink/pull/24338/files/#r1498379848", 
    "https://github.com/apache/flink/pull/24325/files/#r1494349521", 
    "https://github.com/prestodb/presto/pull/22177/files/#r1522319112", 
    "https://github.com/prestodb/presto/pull/22109/files/#r1516015695", 
    "https://github.com/prestodb/presto/pull/22063/files/#r1511836171", 
    "https://github.com/prestodb/presto/pull/22063/files/#r1513749954", 
    "https://github.com/prestodb/presto/pull/22051/files/#r1511790604", 
    "https://github.com/prestodb/presto/pull/22051/files/#r1511803325", 
    "https://github.com/ytdl-org/youtube-dl/pull/32138/files/#r1183799718", 
    "https://github.com/ytdl-org/youtube-dl/pull/31305/files/#r1008775021"
]

print(f"Starting processing of {len(url_list)} URLs...\n")

for url in url_list:
    print(f"Processing: {url}")
    repo, commit_sha, status = get_commit_sha(url, GITHUB_TOKEN)

    if commit_sha:
        print(f"  [*] Hash: {commit_sha}")
        
        # 1. Fetch the specific commit directly
        fetch_cmd = f"git -C ../workspace_agente/{repo} fetch origin {commit_sha} --quiet"
        os.system(fetch_cmd)
        
        # 2. Checkout the commit
        checkout_cmd = f"git -C ../workspace_agente/{repo} checkout {commit_sha} --quiet"
        exit_code = os.system(checkout_cmd)
        
        if exit_code == 0:
            print("  [+] Checkout completed.")
        else:
            print("  [-] Error: Checkout failed.")
    else:
        print(f"  [-] Skipped: {status}")
    
    # Delay between requests to avoid hitting API rate limits
    time.sleep(0.5) 
    print("-" * 40)