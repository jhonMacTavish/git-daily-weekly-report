#!/usr/bin/env python3
"""Collect local Git commits as JSON for Chinese daily and weekly reports."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
RECORD_SEPARATOR = "\x1e"
FIELD_SEPARATOR = "\x1f"
LOG_FORMAT = (
    "--format=%H%x1f%h%x1f%an%x1f%ae%x1f%cI%x1f%s%x1f%b%x1f%D%x1e"
)
PACKAGE_EXCLUDES = {
    ".git",
    ".cache",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "vendor",
}
FRAMEWORK_MARKERS = (
    ("next", "Next.js"),
    ("nuxt", "Nuxt"),
    ("@angular/core", "Angular"),
    ("vue", "Vue"),
    ("react", "React"),
    ("svelte", "Svelte"),
)


def run_git(
    repo: Path, args: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def parse_iso_date(value: str, option: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{option} must use YYYY-MM-DD: {value}") from exc


def build_period(
    period: str,
    selected_date: Optional[str],
    since: Optional[str],
    until: Optional[str],
    now: datetime,
) -> Dict[str, str]:
    today = now.date()

    if selected_date and (since or until):
        raise ValueError("--date cannot be combined with --since or --until")

    if bool(since) != bool(until):
        raise ValueError("--since and --until must be provided together")

    if since and until:
        since_date = parse_iso_date(since, "--since")
        until_date = parse_iso_date(until, "--until")
        if since_date > until_date:
            raise ValueError("--since cannot be later than --until")
        start = datetime.combine(since_date, time.min, tzinfo=CHINA_TZ)
        end = datetime.combine(
            until_date + timedelta(days=1), time.min, tzinfo=CHINA_TZ
        )
    elif period == "daily":
        anchor = parse_iso_date(selected_date, "--date") if selected_date else today
        if anchor > today:
            raise ValueError("--date cannot be in the future")
        start = datetime.combine(anchor, time.min, tzinfo=CHINA_TZ)
        next_day = start + timedelta(days=1)
        end = now if anchor == today else next_day
        since_date = anchor
    else:
        anchor = parse_iso_date(selected_date, "--date") if selected_date else today
        if anchor > today:
            raise ValueError("--date cannot be in the future")
        since_date = anchor - timedelta(days=anchor.weekday())
        week_end = since_date + timedelta(days=7)
        start = datetime.combine(since_date, time.min, tzinfo=CHINA_TZ)
        end = now if since_date <= today < week_end else datetime.combine(
            week_end, time.min, tzinfo=CHINA_TZ
        )

    if end <= start:
        raise ValueError("the selected period has no elapsed time")

    actual_until_date = (end - timedelta(microseconds=1)).astimezone(CHINA_TZ).date()
    return {
        "since": start.isoformat(),
        "until": end.isoformat(),
        "since_date": since_date.isoformat(),
        "until_date": actual_until_date.isoformat(),
        "boundary": "[since, until)",
        "timezone": "Asia/Shanghai",
    }


def resolve_repo(path_value: str) -> Tuple[Optional[Path], Optional[str]]:
    requested = Path(path_value).expanduser()
    if not requested.exists():
        return None, f"{requested}: path does not exist"

    try:
        resolved = requested.resolve()
    except OSError as exc:
        return None, f"{requested}: cannot resolve path: {exc}"

    inside_work_tree = run_git(resolved, ["rev-parse", "--is-inside-work-tree"])
    if inside_work_tree.returncode != 0:
        return None, f"{resolved}: not a Git working tree"

    root_result = run_git(resolved, ["rev-parse", "--show-toplevel"])
    if root_result.returncode != 0 or not root_result.stdout.strip():
        return None, f"{resolved}: cannot determine Git worktree root"

    return Path(root_result.stdout.strip()).resolve(), None


def has_head(repo: Path) -> bool:
    result = run_git(repo, ["rev-parse", "--verify", "--quiet", "HEAD"])
    return result.returncode == 0


def current_ref(repo: Path) -> str:
    branch = run_git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if branch.returncode == 0 and branch.stdout.strip():
        return branch.stdout.strip()

    head = run_git(repo, ["rev-parse", "--short", "HEAD"])
    if head.returncode == 0 and head.stdout.strip():
        return f"detached:{head.stdout.strip()}"
    return "(empty)"


def resolve_current_user_email(repo: Path) -> Optional[str]:
    configured = run_git(repo, ["config", "--get", "user.email"])
    if configured.returncode == 0 and configured.stdout.strip():
        return configured.stdout.strip()

    if not has_head(repo):
        return None

    latest = run_git(repo, ["log", "-1", "--format=%ae", "HEAD"])
    if latest.returncode == 0 and latest.stdout.strip():
        return latest.stdout.strip()
    return None


def author_matches(
    commit: Dict[str, Any],
    author_mode: str,
    filter_values: Sequence[str],
) -> bool:
    if author_mode == "all_authors":
        return True
    if not filter_values:
        return False

    author_name = commit["author_name"].casefold()
    author_email = commit["author_email"].casefold()
    return any(
        author_name == wanted or author_email == wanted
        for wanted in (value.casefold() for value in filter_values)
    )


def author_values_for_commits(
    commits: Sequence[Dict[str, Any]],
    base_filter: Optional[str],
    aliases: Sequence[str],
) -> List[str]:
    values = {value.strip() for value in aliases if value.strip()}
    if base_filter:
        values.add(base_filter.strip())
        wanted_email = base_filter.strip().casefold()
        values.update(
            commit["author_name"]
            for commit in commits
            if commit["author_email"].casefold() == wanted_email
        )
    return sorted(value for value in values if value)


def collect_commit_stats(
    repo: Path,
    commits: Sequence[Dict[str, Any]],
    warnings: List[str],
) -> Dict[str, Any]:
    all_files = set()
    additions = 0
    deletions = 0

    for commit in commits:
        result = run_git(
            repo,
            [
                "show",
                "--numstat",
                "--format=",
                "--no-renames",
                commit["hash"],
            ],
        )
        commit_files = set()
        commit_additions = 0
        commit_deletions = 0

        if result.returncode != 0:
            warnings.append(
                f"{repo}: cannot read change stats for {commit['short_hash']}: "
                f"{result.stderr.strip() or 'git show failed'}"
            )
        else:
            for line in result.stdout.splitlines():
                parts = line.split("\t", 2)
                if len(parts) != 3:
                    continue
                added, removed, path = parts
                commit_files.add(path)
                if added.isdigit():
                    commit_additions += int(added)
                if removed.isdigit():
                    commit_deletions += int(removed)

        all_files.update(commit_files)
        additions += commit_additions
        deletions += commit_deletions
        commit["files"] = sorted(commit_files)
        commit["additions"] = commit_additions
        commit["deletions"] = commit_deletions

    timestamps = sorted(
        datetime.fromisoformat(commit["committed_at"]) for commit in commits
    )
    if timestamps:
        started_at = timestamps[0].astimezone(CHINA_TZ)
        ended_at = timestamps[-1].astimezone(CHINA_TZ)
        if started_at.date() == ended_at.date():
            time_range = f"{started_at:%H:%M}–{ended_at:%H:%M}"
        else:
            time_range = f"{started_at:%m-%d %H:%M}–{ended_at:%m-%d %H:%M}"
        started_value: Optional[str] = started_at.isoformat()
        ended_value: Optional[str] = ended_at.isoformat()
    else:
        time_range = "—"
        started_value = None
        ended_value = None

    return {
        "files_changed": len(all_files),
        "additions": additions,
        "deletions": deletions,
        "time_range": time_range,
        "started_at": started_value,
        "ended_at": ended_value,
        "files": sorted(all_files),
    }


def find_package_files(repo: Path, max_depth: int = 3) -> List[Path]:
    package_files: List[Path] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError:
            return
        for child in children:
            if child.is_file() and child.name == "package.json":
                package_files.append(child)
            elif (
                child.is_dir()
                and child.name not in PACKAGE_EXCLUDES
                and not child.name.startswith(".")
            ):
                walk(child, depth + 1)

    walk(repo, 1)
    return package_files


def detect_framework(package_file: Path) -> Optional[str]:
    try:
        package_data = json.loads(package_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    dependencies = {
        **(package_data.get("dependencies") or {}),
        **(package_data.get("devDependencies") or {}),
    }
    for marker, label in FRAMEWORK_MARKERS:
        if marker in dependencies:
            return label
    return None


def detect_project(
    repo: Path,
    project_name: Optional[str],
    changed_files: Sequence[str],
) -> Dict[str, Optional[str]]:
    package_files = find_package_files(repo)
    best_package: Optional[Path] = None
    best_score = -1

    for package_file in package_files:
        package_dir = package_file.parent
        relative = package_dir.relative_to(repo)
        prefix = "" if str(relative) == "." else f"{relative.as_posix().rstrip('/')}/"
        score = sum(
            1 for path in changed_files if not prefix or path.startswith(prefix)
        )
        if score > best_score:
            best_package = package_file
            best_score = score

    technology: Optional[str] = None
    package_path: Optional[str] = None
    if best_package is not None:
        package_path = str(best_package.parent.relative_to(repo))
        if package_path == ".":
            package_path = ""
        technology = detect_framework(best_package)

    if technology is None:
        for package_file in package_files:
            detected = detect_framework(package_file)
            if detected:
                technology = detected
                package_path = str(package_file.parent.relative_to(repo))
                if package_path == ".":
                    package_path = ""
                break

    if technology is None:
        lowered = [path.casefold() for path in changed_files]
        if any(path.endswith(".vue") for path in lowered):
            technology = "Vue"
        elif any(path.endswith((".jsx", ".tsx")) for path in lowered):
            technology = "React"

    return {
        "name": project_name or repo.name,
        "repository_name": repo.name,
        "technology": technology,
        "package_path": package_path,
    }


def parse_commits(
    raw_output: str, max_body_chars: int, warnings: List[str], repo: Path
) -> List[Dict[str, Any]]:
    commits: List[Dict[str, Any]] = []
    for raw_record in raw_output.split(RECORD_SEPARATOR):
        record = raw_record.strip("\r\n")
        if not record:
            continue

        fields = record.split(FIELD_SEPARATOR)
        if len(fields) != 8:
            warnings.append(
                f"{repo}: skipped malformed Git log record with {len(fields)} fields"
            )
            continue

        body = fields[6].strip()
        if max_body_chars >= 0 and len(body) > max_body_chars:
            body = body[:max_body_chars].rstrip() + "..."

        commits.append(
            {
                "hash": fields[0],
                "short_hash": fields[1],
                "author_name": fields[2],
                "author_email": fields[3],
                "committed_at": fields[4],
                "subject": fields[5],
                "body": body,
                "refs": fields[7],
            }
        )
    return commits


def collect_repo_commits(
    repo: Path,
    start: str,
    end: str,
    author_mode: str,
    explicit_author: Optional[str],
    author_aliases: Sequence[str],
    project_name: Optional[str],
    all_refs: bool,
    include_merges: bool,
    max_body_chars: int,
    warnings: List[str],
) -> Dict[str, Any]:
    branch = current_ref(repo)
    base_author = (
        resolve_current_user_email(repo)
        if author_mode == "current_user"
        else explicit_author
    )

    entry: Dict[str, Any] = {
        "path": str(repo),
        "name": repo.name,
        "branch": branch,
        "project": {
            "name": project_name or repo.name,
            "repository_name": repo.name,
            "technology": None,
            "package_path": None,
        },
        "resolved_author_email": base_author,
        "resolved_author_values": [],
        "matched_authors": [],
        "stats": {
            "files_changed": 0,
            "additions": 0,
            "deletions": 0,
            "time_range": "—",
            "started_at": None,
            "ended_at": None,
            "files": [],
        },
        "commit_count": 0,
        "commits": [],
    }

    if not has_head(repo) and not all_refs:
        warnings.append(f"{repo}: repository has no commits yet")
        return entry

    if author_mode == "current_user" and not base_author:
        warnings.append(
            f"{repo}: cannot determine current user email; no commits were selected"
        )
        return entry

    end_inclusive = datetime.fromisoformat(end) - timedelta(seconds=1)
    cmd = ["log"]
    if not include_merges:
        cmd.append("--no-merges")
    cmd.extend(
        [
            f"--since={start}",
            f"--until={end_inclusive.isoformat()}",
            LOG_FORMAT,
        ]
    )
    if all_refs:
        cmd.append("--all")
    else:
        cmd.append("HEAD")

    result = run_git(repo, cmd)
    if result.returncode != 0:
        message = result.stderr.strip() or "git log failed"
        warnings.append(f"{repo}: {message}")
        return entry

    parsed = parse_commits(result.stdout, max_body_chars, warnings, repo)
    author_values = (
        []
        if author_mode == "all_authors"
        else author_values_for_commits(parsed, base_author, author_aliases)
    )
    commits = [
        commit
        for commit in parsed
        if author_matches(commit, author_mode, author_values)
    ]
    stats = collect_commit_stats(repo, commits, warnings)
    project = detect_project(repo, project_name, stats["files"])
    matched_authors = []
    author_counts: Dict[Tuple[str, str], int] = {}
    for commit in commits:
        key = (commit["author_name"], commit["author_email"])
        author_counts[key] = author_counts.get(key, 0) + 1
    for (name, email), count in sorted(author_counts.items()):
        matched_authors.append(
            {"name": name, "email": email, "commit_count": count}
        )

    entry["project"] = project
    entry["resolved_author_values"] = author_values
    entry["matched_authors"] = matched_authors
    entry["stats"] = stats
    entry["commit_count"] = len(commits)
    entry["commits"] = commits
    return entry


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect local Git commits as structured JSON for Chinese daily "
            "and weekly reports."
        )
    )
    parser.add_argument(
        "--period",
        choices=("daily", "weekly"),
        default="daily",
        help="Report period type (default: daily).",
    )
    parser.add_argument(
        "--date",
        help="Anchor date in YYYY-MM-DD; defaults to today.",
    )
    parser.add_argument(
        "--since",
        help="Inclusive start date in YYYY-MM-DD; requires --until.",
    )
    parser.add_argument(
        "--until",
        help="Inclusive end date in YYYY-MM-DD; requires --since.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        nargs="+",
        metavar="PATH",
        help="Git worktree path; repeat the option for multiple repositories.",
    )
    author_group = parser.add_mutually_exclusive_group()
    author_group.add_argument(
        "--author",
        help="Case-insensitive exact match on author name or email.",
    )
    author_group.add_argument(
        "--all-authors",
        action="store_true",
        help="Include all authors instead of the current Git user.",
    )
    parser.add_argument(
        "--author-alias",
        action="append",
        default=[],
        metavar="NAME_OR_EMAIL",
        help=(
            "Additional exact author name or email to merge into the current "
            "or specified author; repeat for multiple aliases."
        ),
    )
    parser.add_argument(
        "--project-name",
        help="Display project name to include in the JSON metadata.",
    )
    parser.add_argument(
        "--all-refs",
        action="store_true",
        help="Scan all refs instead of the current branch.",
    )
    parser.add_argument(
        "--include-merges",
        action="store_true",
        help="Include merge commits.",
    )
    parser.add_argument(
        "--max-body-chars",
        type=int,
        default=2000,
        help="Maximum commit-body characters; use -1 for no limit.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    now = datetime.now(CHINA_TZ)

    try:
        period = build_period(args.period, args.date, args.since, args.until, now)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.max_body_chars < -1:
        print("error: --max-body-chars must be -1 or greater", file=sys.stderr)
        return 2

    author_aliases = [
        alias.strip()
        for value in args.author_alias
        for alias in value.split(",")
        if alias.strip()
    ]
    requested_repos = (
        [path for group in args.repo for path in group] if args.repo else [os.getcwd()]
    )
    author_mode = (
        "all_authors"
        if args.all_authors
        else "specified_author"
        if args.author
        else "current_user"
    )
    warnings: List[str] = []
    repositories: List[Dict[str, Any]] = []
    seen_paths = set()

    for requested in requested_repos:
        repo, error = resolve_repo(requested)
        if error:
            warnings.append(error)
            continue
        if repo is None or repo in seen_paths:
            continue
        seen_paths.add(repo)
        repositories.append(
            collect_repo_commits(
                repo=repo,
                start=period["since"],
                end=period["until"],
                author_mode=author_mode,
                explicit_author=args.author,
                author_aliases=author_aliases,
                project_name=args.project_name,
                all_refs=args.all_refs,
                include_merges=args.include_merges,
                max_body_chars=args.max_body_chars,
                warnings=warnings,
            )
        )

    output = {
        "report_type": args.period,
        "period": period,
        "author_filter": {
            "mode": author_mode,
            "value": args.author,
            "aliases": author_aliases,
        },
        "ref_scope": "all_refs" if args.all_refs else "current_branch",
        "include_merges": args.include_merges,
        "generated_at": now.isoformat(),
        "repositories": repositories,
        "total_commits": sum(
            int(repository["commit_count"]) for repository in repositories
        ),
        "warnings": warnings,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

    if not repositories:
        print("error: no valid Git repositories were found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
