# Automated Code Review Replication Package

This repository contains the scripts, dataset, and example artifacts required to replicate our experiments comparing an **autonomous code review agent** (built on [`mini-swe-agent`](https://mini-swe-agent.com/latest/)) against a **zero-shot LLM baseline** on real-world GitHub Pull Request review comments.

---

## Repository Structure

~~~text
code_review/
├── batch_runner/
│   └── run_agent_on_all_repo.py        # Batch orchestrator and output parser for both agent.py and zeroshot.py
├── data/
│   ├── final.csv                       # Dataset of GitHub PR review comment URLs and metadata
│   ├── example_of_comment_mined/       # Sample extracted review unit JSON from GitHub API
│   │   └── airbnb__lottie-android__pr2364__c1298731862.json
│   └── example_of_structured_review/   # Sample structured JSON outputs parsed from model reviews
│       ├── structured_review__3b1b__manim__pr1932__c1051540852.json
│       └── structured_review__airbnb__lottie-android__pr2323__c1257593152.json
├── mining/
│   ├── clone_all_repo.py               # Initializes local Git repositories and shallow-fetches target commits
│   ├── extract_review_units_batch.py   # Fetches PR metadata, file diffs at the exact review commit, and human comments
│   ├── extractpatch.py                 # Fallback utility to extract a commit diff directly from a local Git repo
│   └── getcommitandfetch.py            # Resolves and fetches commits for PR comment URLs without an embedded SHA
└── run_agent/
    ├── agent.py                        # Autonomous code review agent runner (mini-swe-agent)
    ├── mini_config.yaml                # Configuration file for mini-swe-agent
    └── zeroshot.py                     # Zero-shot static code review baseline (OpenAI API)
~~~

---

## Dataset & Example Artifacts (`data/`)

* **`data/final.csv`**: Contains the list of target GitHub Pull Request review comment URLs evaluated in the study.
* **`data/example_of_comment_mined/`**: Contains a sample JSON file (`airbnb__lottie-android__pr2364__c1298731862.json`) produced by the mining phase. Each file stores the PR metadata, the cumulative file `patch` at the exact commit when the review was left (`comment_commit_sha`), the `target_comment`, and all sibling human comments on the same file (`all_human_comments_on_file`).
* **`data/example_of_structured_review/`**: Contains sample structured JSON outputs (`structured_review__3b1b__manim__pr1932__c1051540852.json` and `structured_review__airbnb__lottie-android__pr2323__c1257593152.json`) generated after parsing the raw reviews produced by the agent or zero-shot baseline.

---

## Prerequisites & Setup

1. **Dependencies:**
   Ensure `git` and Python 3.10+ are installed, then install the required Python packages:
   ~~~bash
   pip install openai python-dotenv requests certifi mini-swe-agent
   ~~~

2. **Environment Variables:**
   Create a `.env` file inside the `run_agent/` directory with your OpenAI API key and target model name:
   ~~~ini
   OPENAI_API_KEY=sk-...
   MSWEA_MODEL_NAME=openai/gpt-5.4-mini
   ~~~
   For the GitHub data mining scripts, export a GitHub Personal Access Token to avoid API rate limits:
   ~~~bash
   export GITHUB_TOKEN=github_pat_...
   ~~~

---

## Replication Pipeline

### Step 1: Data Mining & Local Repository Preparation (`mining/`)

1. **Extract Review Units (JSON):**
   Given `data/final.csv`, `extract_review_units_batch.py` queries the GitHub API to retrieve PR metadata, human comments, and the exact cumulative file diff at the time the comment was made (`original_commit_id`):
   ~~~bash
   python3 mining/extract_review_units_batch.py data/final.csv data/review_units/
   ~~~

2. **Clone Repositories & Fetch Target Commits:**
   Initialize the local repositories and shallow-fetch (`--depth 1`) the target commits referenced in the CSV:
   ~~~bash
   python3 mining/clone_all_repo.py
   ~~~
   *Note:* For URLs in the dataset that only reference `#r<comment_id>` without an explicit commit hash in the URL path, run:
   ~~~bash
   python3 mining/getcommitandfetch.py
   ~~~

3. **Fallback Patch Extraction (Optional):**
   If a patch is truncated or unavailable via the GitHub Compare API, extract it directly from the local repository:
   ~~~bash
   python3 mining/extractpatch.py --repo <path_to_local_repo> --commit <commit_sha> --file <relative_file_path>
   ~~~

---

### Step 2: Running Code Reviews in Batch (`batch_runner/` & `run_agent/`)

`batch_runner/run_agent_on_all_repo.py` iterates over the CSV of GitHub URLs, checks out the corresponding commit in each local repository, executes either the autonomous agent or the zero-shot baseline, and parses the raw output (`final_output.txt`) into a structured JSON file (`structured_review__<owner>__<repo>__pr<N>__c<id>.json`).

* **Run the Autonomous Code Review Agent:**
  ~~~bash
  cd batch_runner
  python3 run_agent_on_all_repo.py \
      --csv ../data/final.csv \
      --jsons-dir ../data/review_units \
      --repos-base-dir ../workspace_agente \
      --agent-script ../run_agent/agent.py \
      --output-dir ../data/structured_reviews_agent
  ~~~

* **Run the Zero-Shot Baseline:**
  ~~~bash
  cd batch_runner
  python3 run_agent_on_all_repo.py \
      --csv ../data/final.csv \
      --jsons-dir ../data/review_units \
      --repos-base-dir ../workspace_agente \
      --agent-script ../run_agent/zeroshot.py \
      --output-dir ../data/structured_reviews_zeroshot
  ~~~

* **Additional Flags:**
  * `--dry-run`: Parses URLs and checks out the target commits without invoking the model (prints the exact command that would be executed).
  * `--parse-only`: Skips model execution and only parses existing `final_output.txt` files into structured JSONs.
  * Run `python3 run_agent_on_all_repo.py --help` to see all available options.

---

## Output Format

Each evaluated review unit produces a structured JSON file (like the examples in `data/example_of_structured_review/`) with the following schema:

~~~json
{
    "issue_found": true,
    "reviews": [
        {
            "review_title": "Review comment #1",
            "snippets_reviewed": [
                "<verbatim lines of code from the diff>"
            ],
            "comment": [
                "<description of the verified issue and concrete recommendation>"
            ]
        }
    ]
}
~~~