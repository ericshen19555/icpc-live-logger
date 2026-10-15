import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
API = "https://codeforces.com/api"
LAST_TEAM_FILE = BASE_DIR / ".cf_last_team"


def load_last_team_name():
    try:
        team_name = LAST_TEAM_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return team_name or None


def api_result(method, **params):
    response = requests.get(f"{API}/{method}", params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "OK":
        raise RuntimeError(payload.get("comment", f"Codeforces API {method} failed"))
    return payload["result"]


def download_submissions(contest_id):
    return api_result("contest.status", contestId=contest_id, **{"from": 1, "count": 10000})


def get_team_start_time(submissions):
    """Read author.startTimeSeconds, a sibling of author.teamName in CF submissions."""
    starts = {
        item.get("author", {}).get("startTimeSeconds")
        for item in submissions
        if item.get("author", {}).get("startTimeSeconds") is not None
    }
    return min(starts) if starts else None


def write_team_submissions(contest_id, team_name):
    all_submissions = download_submissions(contest_id)
    team_submissions = [
        item for item in all_submissions
        if item.get("author", {}).get("teamName") == team_name
    ]
    start_time = get_team_start_time(team_submissions)

    # Preserve Codeforces submission objects and their standard camelCase field names.
    # The outer object carries the contest/team context needed by the visualizer.
    payload = {
        "status": "OK",
        "contestId": contest_id,
        "teamName": team_name,
        "startTimeSeconds": start_time,
        "result": team_submissions,
    }
    contest_dir = BASE_DIR / "logs" / f"{contest_id}"
    Path.mkdir(contest_dir, parents=True, exist_ok=True)
    output = contest_dir / f"gym_{contest_id}_{team_name.replace(' ', '_')}_submissions.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    LAST_TEAM_FILE.write_text(team_name + "\n", encoding="utf-8")
    print(f"成功抓取 {len(all_submissions)} 筆，篩選出 {len(team_submissions)} 筆：{output}")
    if start_time is None:
        print("符合隊名的 submission.author.startTimeSeconds 不存在；視覺化頁可由事件時間推定並手動調整起點。")
    else:
        dt = datetime.fromtimestamp(start_time, tz=timezone.utc)
        print(f"隊伍 startTimeSeconds: {start_time} ({dt.isoformat()})")


def main():
    parser = argparse.ArgumentParser(description="下載並篩選 Codeforces contest submissions")
    parser.add_argument("contest_id", nargs="?", type=int, help="Codeforces contest ID")
    parser.add_argument("team_name", nargs="?", help="Codeforces teamName")
    args = parser.parse_args()
    contest_id = args.contest_id or int(input("Contest ID: ").strip())
    last_team_name = load_last_team_name()
    supplied_team_name = args.team_name
    if supplied_team_name is None:
        prompt = f"Team name [{last_team_name}]: " if last_team_name else "Team name: "
        supplied_team_name = input(prompt)
    team_name = supplied_team_name.strip() or last_team_name
    if not team_name:
        parser.error("沒有上次抓取的隊名；請輸入隊名或以參數提供。")
    write_team_submissions(contest_id, team_name)


if __name__ == "__main__":
    main()
