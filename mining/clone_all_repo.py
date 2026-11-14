"""
Repository setup script: reads a CSV file containing GitHub PR commit URLs
(format: https://github.com/<owner>/<repo>/pull/<pr>/files/<commit_sha>),
initializes local Git repositories inside the target workspace directory,
fetches each required commit with --depth 1, and checks out the last fetched commit.
"""

import csv
import os
import re
import subprocess


def extract_data(url):
  pattern = r'(https://github\.com/[^/]+/[^/]+)/pull/\d+/files/([a-f0-9]{40})'
  match = re.search(pattern, url)
  if match:
    repo_url = match.group(1) + '.git'
    commit_hash = match.group(2)
    project_name = repo_url.split('/')[-1].replace('.git', '')
    return repo_url, commit_hash, project_name
  return None, None, None


def prepare_local_repositories(csv_path, base_dir='agent_workspace'):
  os.makedirs(base_dir, exist_ok=True)
  projects = {}

  print(f"Reading file '{csv_path}'...")

  try:
    with open(csv_path, mode='r', encoding='utf-8') as file:
      reader = csv.reader(file)
      for row in reader:
        for cell in row:
          url = cell.strip()
          if 'github.com' in url and '/pull/' in url and '/files/' in url:
            repo_url, commit_hash, project_name = extract_data(url)
            if repo_url and commit_hash:
              if project_name not in projects:
                projects[project_name] = {'url': repo_url, 'commits': set()}
              projects[project_name]['commits'].add(commit_hash)

  except FileNotFoundError:
    print(f"File '{csv_path}' does not exist.")
    return

  for project_name, data in projects.items():
    repo_path = os.path.join(base_dir, project_name)
    repo_url = data['url']
    commits = list(data['commits'])

    if not os.path.exists(repo_path):
      os.makedirs(repo_path)
      subprocess.run(
          ['git', 'init'], cwd=repo_path, check=True, capture_output=True
      )
      subprocess.run(
          ['git', 'remote', 'add', 'origin', repo_url],
          cwd=repo_path,
          check=True,
          capture_output=True,
      )

    last_fetched_hash = None

    for i, commit_hash in enumerate(commits, 1):
      print(f'  [{i}/{len(commits)}] Fetching commit {commit_hash[:7]}...')
      try:
        subprocess.run(
            ['git', 'fetch', '--depth', '1', 'origin', commit_hash],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        last_fetched_hash = commit_hash
      except subprocess.CalledProcessError as e:
        print(f"   Git error: {e.stderr.decode('utf-8').strip()}")

    if last_fetched_hash:
      subprocess.run(
          ['git', 'checkout', '-f', last_fetched_hash],
          cwd=repo_path,
          check=True,
          capture_output=True,
      )


if __name__ == '__main__':
  csv_file = 'a.csv'
  prepare_local_repositories(csv_file)