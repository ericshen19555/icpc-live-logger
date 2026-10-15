import argparse
import hashlib
import getpass
import json
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE_DIR = Path(__file__).resolve().parent
BASE_URL = "https://qoj.ac"
CREDENTIALS_FILE = BASE_DIR / ".qoj_credentials.json"


class PageParser(HTMLParser):
    """Collect forms, table rows, and pagination links without extra dependencies."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.tables = []
        self._form = None
        self._table = None
        self._row = None
        self._cell = None
        self._anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._form = {"action": attrs.get("action", ""), "method": attrs.get("method", "get").lower(), "inputs": []}
        elif tag == "input" and self._form is not None:
            self._form["inputs"].append(attrs)
        elif tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            # QOJ's profile table currently omits the closing </tr> after
            # its <thead>; treat the next <tr> as an implicit row boundary.
            if self._row is not None:
                self._table.append(self._row)
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": "", "links": [], "datetime": None}
        elif tag == "time" and self._cell is not None:
            self._cell["datetime"] = attrs.get("datetime")
        elif tag == "a":
            self._anchor = {"href": attrs.get("href", ""), "text": ""}

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data
        if self._anchor is not None:
            self._anchor["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            if self._cell is not None:
                self._cell["links"].append(self._anchor)
            self._anchor = None
        elif tag in ("td", "th") and self._cell is not None:
            self._cell["text"] = " ".join(self._cell["text"].split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None


def load_credentials():
    if CREDENTIALS_FILE.exists():
        try:
            credentials = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
            username, password = credentials["username"], credentials["password"]
            if username and password:
                return {"username": username, "password": password}
        except (OSError, ValueError, KeyError, TypeError):
            pass

    print("找不到可用的 QOJ 帳號密碼設定，請輸入一次以儲存到本機設定檔。")
    username = input("QOJ username: ").strip()
    password = getpass.getpass("QOJ password: ")
    if not username or not password:
        raise ValueError("QOJ username 和 password 都不能空白")
    credentials = {"username": username, "password": password}
    CREDENTIALS_FILE.write_text(json.dumps(credentials, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(CREDENTIALS_FILE, 0o600)
    except OSError:
        pass
    return credentials


def login(session, credentials):
    login_url = urljoin(BASE_URL, "/login")
    response = session.get(login_url, timeout=30)
    response.raise_for_status()
    if "Just a moment" in response.text and "challenge-platform" in response.text:
        raise RuntimeError("QOJ 回傳 Cloudflare 驗證頁，無法以 requests 登入。")
    # QOJ's login form is intercepted by submitLoginPost() and sent via
    # jQuery $.post. The browser adds the page's CSRF token and sends an MD5
    # digest of the password, along with empty `login` and `trust` fields.
    token_match = re.search(r"_token\s*:\s*['\"]([^'\"]+)['\"]", response.text)
    if not token_match:
        raise RuntimeError("QOJ 登入頁找不到 AJAX 使用的 CSRF token；頁面格式可能已變更。")

    password_md5 = hashlib.md5(credentials["password"].encode("utf-8")).hexdigest()
    response = session.post(
        login_url,
        data={
            "_token": token_match.group(1),
            "login": "",
            "username": credentials["username"],
            "password": password_md5,
            "trust": "",
        },
        headers={
            "Origin": BASE_URL,
            "Referer": login_url,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "*/*",
        },
        timeout=30,
    )
    response.raise_for_status()
    if "Just a moment" in response.text and "challenge-platform" in response.text:
        raise RuntimeError("QOJ 回傳 Cloudflare 驗證頁，無法確認登入狀態。")
    if response.text.strip() != "ok":
        if response.text.strip() == "failed":
            raise RuntimeError("QOJ 登入失敗，請確認設定檔中的帳號密碼。")
        raise RuntimeError(
            f"QOJ AJAX 登入未成功：HTTP {response.status_code}，"
            f"回應 {response.text.strip()[:120]!r}。"
        )

    # The AJAX endpoint returns plain-text `ok`; confirm the session cookie
    # was accepted by requesting an authenticated page.
    home = session.get(BASE_URL + "/", headers={"Referer": login_url}, timeout=30)
    home.raise_for_status()
    if "Just a moment" in home.text and "challenge-platform" in home.text:
        raise RuntimeError("QOJ 回傳 Cloudflare 驗證頁，無法確認登入狀態。")
    if not re.search(r"href=['\"][^'\"]*/logout(?:['\"/?#])", home.text, re.IGNORECASE):
        raise RuntimeError(
            f"QOJ AJAX 登入回應為 HTTP {response.status_code}，但首頁未顯示已登入狀態。"
        )


def extract_submission_rows(html):
    parser = PageParser()
    parser.feed(html)
    rows = []
    for table in parser.tables:
        for row in table:
            submission_link = next((link for cell in row for link in cell["links"]
                                    if re.search(r"/submission(?:s)?/\d+", link["href"])), None)
            if submission_link is None:
                continue
            submission_id = re.search(r"/submission(?:s)?/(\d+)", submission_link["href"]).group(1)
            cells = [cell["text"] for cell in row]
            problem_link = next((link for cell in row for link in cell["links"]
                                 if re.search(r"/contest/\d+/problem/\d+", link["href"])), None)
            rows.append({
                "id": int(submission_id),
                "cells": cells,
                "url": urljoin(BASE_URL, submission_link["href"]),
                "problemUrl": urljoin(BASE_URL, problem_link["href"]) if problem_link else None,
            })
    return rows


def parse_elapsed_seconds(value):
    match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d)", value.strip())
    if not match:
        return None
    hours, minutes, seconds = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds


def parse_quantity(value, unit):
    match = re.fullmatch(r"\s*([\d.]+)\s*" + re.escape(unit) + r"\s*", value, re.IGNORECASE)
    return float(match.group(1)) if match else None


def normalize_verdict(value):
    verdict = value.strip().upper()
    mappings = (
        (r"^AC(?:CEPTED)?\b", "OK"),
        (r"^WA\b|WRONG ANSWER", "WRONG_ANSWER"),
        (r"^TL\b|^TLE\b|TIME LIMIT", "TIME_LIMIT_EXCEEDED"),
        (r"^ML\b|^MLE\b|MEMORY LIMIT", "MEMORY_LIMIT_EXCEEDED"),
        (r"^RE\b|RUNTIME ERROR", "RUNTIME_ERROR"),
        (r"^PE\b|PRESENTATION ERROR", "PRESENTATION_ERROR"),
        (r"^CE\b|COMPILE ERROR", "COMPILATION_ERROR"),
        (r"^OLE\b|OUTPUT LIMIT|SYSTEM ERROR", "FAILED"),
    )
    for pattern, normalized in mappings:
        if re.search(pattern, verdict):
            return normalized
    return verdict or "TESTING"


def normalize_submission(row, contest_id, team_name, start_time):
    """Convert a QOJ listing row into the shared Codeforces submission shape."""
    cells = row["cells"]
    problem_text = cells[1] if len(cells) > 1 else ""
    problem_match = re.match(r"^([A-Z][A-Z0-9]*)[.．:：]\s*(.*)$", problem_text)
    problem_index = problem_match.group(1) if problem_match else problem_text
    problem_name = problem_match.group(2) if problem_match else problem_text
    elapsed = parse_elapsed_seconds(cells[7]) if len(cells) > 7 else None
    time_ms = parse_quantity(cells[4], "ms") if len(cells) > 4 else None
    memory_kb = parse_quantity(cells[5], "kb") if len(cells) > 5 else None
    problem_id_match = re.search(r"/problem/(\d+)(?:$|[/?#])", row.get("problemUrl") or "")

    return {
        "id": row["id"],
        "contestId": contest_id,
        "creationTimeSeconds": start_time + elapsed if start_time is not None and elapsed is not None else None,
        "relativeTimeSeconds": elapsed,
        "problem": {
            "contestId": contest_id,
            "index": problem_index,
            "name": problem_name,
            "type": "PROGRAMMING",
            "tags": [],
            "qojProblemId": int(problem_id_match.group(1)) if problem_id_match else None,
        },
        "author": {
            "contestId": contest_id,
            "members": [{"handle": team_name}],
            "participantType": "VIRTUAL",
            "teamName": team_name,
            "startTimeSeconds": start_time,
        },
        "programmingLanguage": None,
        "verdict": normalize_verdict(cells[3] if len(cells) > 3 else ""),
        "testset": None,
        "passedTestCount": None,
        "timeConsumedMillis": round(time_ms) if time_ms is not None else None,
        "memoryConsumedBytes": round(memory_kb * 1024) if memory_kb is not None else None,
        "url": row["url"],
        "qojFileSizeBytes": round(parse_quantity(cells[6], "kb") * 1024)
        if len(cells) > 6 and parse_quantity(cells[6], "kb") is not None else None,
    }


def download_virtual_start_times(username, session):
    """Read virtual start times from the account profile's rendered table."""
    profile_url = urljoin(BASE_URL, f"/user/profile/{username}")
    response = session.get(profile_url, timeout=30)
    response.raise_for_status()
    if "Just a moment" in response.text and "challenge-platform" in response.text:
        raise RuntimeError("QOJ 回傳 Cloudflare 驗證頁，無法讀取個人頁的 virtual 紀錄。")
    if "/login" in response.url or "Virtual Participations" not in response.text:
        raise RuntimeError("無法讀取 QOJ 個人頁的 Virtual Participations 表格；請確認登入狀態或頁面格式。")

    parser = PageParser()
    parser.feed(response.text)
    starts = {}
    for table in parser.tables:
        has_expected_headers = any(
            [cell["text"].casefold() for cell in row][:2] == ["name", "start time"]
            for row in table
        )
        if not has_expected_headers:
            continue
        for row in table:
            contest_id = None
            start_time = None
            for cell in row:
                for link in cell["links"]:
                    match = re.search(r"/contest/(\d+)(?:$|[/?#])", link["href"])
                    if match:
                        contest_id = int(match.group(1))
                if cell["datetime"]:
                    try:
                        parsed = datetime.fromisoformat(cell["datetime"])
                    except ValueError as error:
                        raise RuntimeError(
                            f"QOJ 個人頁的 virtual start time 格式無法解析：{cell['datetime']!r}"
                        ) from error
                    if parsed.tzinfo is None:
                        raise RuntimeError("QOJ 個人頁的 virtual start time 缺少時區資訊。")
                    start_time = int(parsed.astimezone(timezone.utc).timestamp())
            if contest_id is not None and start_time is not None:
                starts[contest_id] = start_time
    return starts


def download_submissions(contest_id, session):
    submissions = []
    seen_ids = set()
    # QOJ uses the standard page query on contest submission listings. Continue
    # until a page has no new submission rows, with links used to honor a
    # customized page size if the site provides one.
    page = 1
    while True:
        url = f"{BASE_URL}/contest/{contest_id}/submissions"
        response = session.get(url, params={"page": page}, timeout=30)
        response.raise_for_status()
        if "Just a moment" in response.text and "challenge-platform" in response.text:
            raise RuntimeError("QOJ 回傳 Cloudflare 驗證頁；請稍後重試或改用已登入的瀏覽器。")
        rows = extract_submission_rows(response.text)
        new_rows = []
        for row in rows:
            if row["id"] not in seen_ids:
                seen_ids.add(row["id"])
                new_rows.append(row)
        if not new_rows:
            break

        # UOJ/QOJ's contest page defaults to the authenticated user's entries.
        # Its separate "show all submissions" option sets a cookie; this
        # session never sets that cookie, so these are already account-scoped.
        submissions.extend(new_rows)
        page += 1
        if page > 10000:
            raise RuntimeError("分頁數量異常，停止抓取以避免無限迴圈。")
    return submissions


def write_team_submissions(contest_id, team_name=None):
    credentials = load_credentials()
    team_name = team_name or credentials["username"]
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    login(session, credentials)
    if credentials["username"].casefold() != team_name.casefold():
        raise ValueError(
            f"輸出隊伍帳號 {team_name!r} 與登入帳號 {credentials['username']!r} 不同；"
            "QOJ contest submissions 頁只列出目前登入帳號的提交。"
        )
    virtual_start_times = download_virtual_start_times(team_name, session)
    start_time = virtual_start_times.get(contest_id)
    rows = download_submissions(contest_id, session)
    submissions = [normalize_submission(row, contest_id, team_name, start_time) for row in rows]
    payload = {
        "status": "OK",
        "contestId": contest_id,
        "teamName": team_name,
        "startTimeSeconds": start_time,
        "result": submissions,
    }
    contest_dir = BASE_DIR / "logs" / f"{contest_id}"
    contest_dir.mkdir(parents=True, exist_ok=True)
    output = contest_dir / f"qoj_{contest_id}_{team_name.replace(' ', '_')}_submissions.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"成功抓取並篩選出 {len(submissions)} 筆：{output}")
    if start_time is None:
        print(f"QOJ 個人頁的 Virtual Participations 表格沒有 contest {contest_id} 的 start time。")
    else:
        start_dt = datetime.fromtimestamp(start_time, tz=timezone.utc)
        print(f"隊伍 virtual startTimeSeconds: {start_time} ({start_dt.isoformat()})")


def main():
    parser = argparse.ArgumentParser(description="下載 QOJ contest submissions")
    parser.add_argument("contest_id", nargs="?", type=int, help="QOJ contest ID")
    parser.add_argument("team_name", nargs="?", help="QOJ 隊伍帳號（預設使用登入帳號）")
    args = parser.parse_args()
    contest_id = args.contest_id or int(input("Contest ID: ").strip())
    write_team_submissions(contest_id, args.team_name)


if __name__ == "__main__":
    main()
