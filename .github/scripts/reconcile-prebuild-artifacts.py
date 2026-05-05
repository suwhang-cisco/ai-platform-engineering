#!/usr/bin/env python3
"""Render the managed PR prebuild comment from status artifacts."""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Any


API_ROOT = "https://api.github.com"
STATUS_LABELS = {
    "building": "Build in process",
    "published": "Published",
    "failed": "Failed",
}


def env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


REPO = env("GITHUB_REPOSITORY")
TOKEN = env("GITHUB_TOKEN")
PR_NUMBER = env("PR_NUMBER")
HEAD_SHA = env("HEAD_SHA")
MARKER = f"<!-- prebuild-artifacts pr={PR_NUMBER} sha={HEAD_SHA} -->"


def api(
    method: str,
    path_or_url: str,
    payload: dict[str, Any] | None = None,
    binary: bool = False,
) -> Any:
    url = path_or_url if path_or_url.startswith("http") else f"{API_ROOT}{path_or_url}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "prebuild-artifact-comment-reconciler",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub API {method} {url} failed: {error.code} {details}")

    if binary:
        return body
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def list_artifacts() -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    page = 1
    while True:
        data = api("GET", f"/repos/{REPO}/actions/artifacts?per_page=100&page={page}")
        page_artifacts = data.get("artifacts", [])
        artifacts.extend(page_artifacts)
        if len(page_artifacts) < 100:
            return artifacts
        page += 1


def newest_matching_artifacts() -> list[dict[str, Any]]:
    prefix = f"prebuild-status-pr-{PR_NUMBER}-sha-{HEAD_SHA}-"
    by_name: dict[str, dict[str, Any]] = {}

    for artifact in list_artifacts():
        name = artifact.get("name", "")
        if artifact.get("expired") or not name.startswith(prefix):
            continue

        existing = by_name.get(name)
        if existing is None:
            by_name[name] = artifact
            continue

        artifact_time = parse_time(artifact.get("created_at") or artifact.get("updated_at"))
        existing_time = parse_time(existing.get("created_at") or existing.get("updated_at"))
        if artifact_time > existing_time:
            by_name[name] = artifact

    return list(by_name.values())


def download_status(artifact: dict[str, Any]) -> dict[str, Any] | None:
    artifact_id = artifact["id"]
    archive = api("GET", f"/repos/{REPO}/actions/artifacts/{artifact_id}/zip", binary=True)

    with zipfile.ZipFile(io.BytesIO(archive)) as zip_file:
        status_names = [name for name in zip_file.namelist() if name.endswith("status.json")]
        if not status_names:
            print(f"Skipping artifact {artifact.get('name')}: no status.json", file=sys.stderr)
            return None

        with zip_file.open(status_names[0]) as status_file:
            status = json.load(status_file)

    if str(status.get("pr_number")) != str(PR_NUMBER) or status.get("head_sha") != HEAD_SHA:
        print(f"Skipping artifact {artifact.get('name')}: PR/SHA mismatch", file=sys.stderr)
        return None

    status["_artifact_name"] = artifact.get("name", "")
    status["_artifact_created_at"] = artifact.get("created_at", "")
    return status


def load_rows() -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for artifact in newest_matching_artifacts():
        status = download_status(artifact)
        if not status:
            continue

        row_type = status.get("type", "")
        name = status.get("name", "")
        if not row_type or not name:
            print(f"Skipping artifact {artifact.get('name')}: missing type or name", file=sys.stderr)
            continue

        status["_sort_time"] = parse_time(
            status.get("created_at") or status.get("_artifact_created_at")
        )
        key = (row_type, name)
        current = rows_by_key.get(key)
        if current is None or status["_sort_time"] >= current["_sort_time"]:
            rows_by_key[key] = status

    return sorted(
        rows_by_key.values(),
        key=lambda row: (row.get("type", ""), row.get("name", "")),
    )


def clean(value: Any) -> str:
    return str(value or "").replace("\n", " ").replace("|", "\\|")


def code(value: Any) -> str:
    text = clean(value)
    return f"`{text}`" if text else ""


def ci_link(row: dict[str, Any]) -> str:
    run_url = clean(row.get("run_url"))
    if not run_url:
        return ""
    workflow = clean(row.get("workflow")) or "workflow"
    run_id = clean(row.get("run_id"))
    label = f"{workflow} #{run_id}" if run_id else workflow
    return f"[{label}]({run_url})"


def status_label(row: dict[str, Any]) -> str:
    return STATUS_LABELS.get(clean(row.get("status")), clean(row.get("status")) or "Unknown")


def render_comment(rows: list[dict[str, Any]]) -> str:
    docker_rows = [row for row in rows if row.get("type") == "docker"]
    helm_rows = [row for row in rows if row.get("type") == "helm"]

    lines = [
        MARKER,
        "",
        "## Prebuild Artifacts",
        "",
        f"Status artifacts for PR #{PR_NUMBER} at `{HEAD_SHA}`.",
        "",
    ]

    if not rows:
        lines.extend(
            [
                "No matching prebuild status artifacts were found yet.",
                "",
                "The next status dispatch will rebuild this comment from artifacts.",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    if docker_rows:
        lines.extend(
            [
                "### Docker",
                "",
                "| Image | Status | Tag | CI | Pull |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for row in docker_rows:
            pull = ""
            if row.get("status") == "published" and row.get("ref"):
                pull = code(f"docker pull {row['ref']}")
            lines.append(
                " | ".join(
                    [
                        "",
                        code(row.get("repository") or row.get("ref") or row.get("name")),
                        status_label(row),
                        code(row.get("tag")),
                        ci_link(row),
                        pull,
                        "",
                    ]
                )
            )
        lines.append("")

    if helm_rows:
        lines.extend(
            [
                "### Helm",
                "",
                "| Chart | Status | Version | CI | Install |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for row in helm_rows:
            install = ""
            if row.get("status") == "published" and row.get("ref") and row.get("version"):
                release_name = row.get("releaseName") or row.get("name")
                install = code(
                    f"helm upgrade --install {release_name} {row['ref']} --version {row['version']}"
                )
            lines.append(
                " | ".join(
                    [
                        "",
                        code(row.get("name")),
                        status_label(row),
                        code(row.get("version")),
                        ci_link(row),
                        install,
                        "",
                    ]
                )
            )
        lines.append("")

    lines.append("Failed rows remain visible until a newer status artifact replaces them.")
    return "\n".join(lines).rstrip() + "\n"


def list_issue_comments() -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    page = 1
    while True:
        data = api(
            "GET",
            f"/repos/{REPO}/issues/{PR_NUMBER}/comments?per_page=100&page={page}",
        )
        comments.extend(data)
        if len(data) < 100:
            return comments
        page += 1


def reconcile_comment(body: str) -> None:
    managed = [
        comment
        for comment in list_issue_comments()
        if MARKER in (comment.get("body") or "")
    ]
    managed.sort(key=lambda comment: parse_time(comment.get("created_at")), reverse=True)

    if managed:
        target = managed[0]
        if target.get("body") != body:
            api(
                "PATCH",
                f"/repos/{REPO}/issues/comments/{target['id']}",
                {"body": body},
            )
            print(f"Updated managed prebuild comment {target['id']}.")
        else:
            print(f"Managed prebuild comment {target['id']} is already current.")

        for duplicate in managed[1:]:
            api("DELETE", f"/repos/{REPO}/issues/comments/{duplicate['id']}")
            print(f"Deleted duplicate managed prebuild comment {duplicate['id']}.")
        return

    created = api(
        "POST",
        f"/repos/{REPO}/issues/{PR_NUMBER}/comments",
        {"body": body},
    )
    print(f"Created managed prebuild comment {created['id']}.")


def main() -> None:
    rows = load_rows()
    print(f"Reconciling {len(rows)} prebuild artifact row(s).")
    reconcile_comment(render_comment(rows))


if __name__ == "__main__":
    main()
