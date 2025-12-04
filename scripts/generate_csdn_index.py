#!/usr/bin/env python3
"""Generate README.md with a full index of CSDN columns and articles."""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests import Session
from requests.adapters import HTTPAdapter, Retry

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
PROFILE_URL = "https://blog.csdn.net/2301_80171004?type=blog"
PAGE_SIZE = 40
REQUEST_TIMEOUT = 30
EXCLUDED_TITLES = {"算法随记", "算法随机"}

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
COMMON_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": PROFILE_URL,
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class ColumnInfo:
    cid: int
    title: str
    url: str
    total: int
    sort: int

    @property
    def safe_title(self) -> str:
        cleaned = self.title.lstrip("# ").strip()
        return cleaned or self.title


def build_session() -> Session:
    session = requests.Session()
    session.headers.update(COMMON_HEADERS)
    retry = Retry(
        total=5,
        read=3,
        connect=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504, 521),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def extract_initial_state(html: str) -> dict:
    marker = "window.__INITIAL_STATE__="
    start = html.find(marker)
    if start == -1:
        raise RuntimeError("无法在主页中找到__INITIAL_STATE__数据")
    start += len(marker)
    end = html.find("</script>", start)
    if end == -1:
        raise RuntimeError("未找到__INITIAL_STATE__对应的</script>结尾")
    payload = html[start:end].strip().rstrip(";")
    return json.loads(payload)


def fetch_columns(session: Session) -> List[ColumnInfo]:
    resp = session.get(PROFILE_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    state = extract_initial_state(resp.text)
    raw_columns: Sequence[dict] = state["pageData"]["data"]["baseInfo"]["columnModule"]
    columns: List[ColumnInfo] = []
    for raw in raw_columns:
        columns.append(
            ColumnInfo(
                cid=int(raw["id"]),
                title=raw["title"],
                url=raw["url"],
                total=int(raw.get("sum", 0)),
                sort=int(raw.get("sort", 0)),
            )
        )
    columns.sort(key=lambda c: c.sort)
    return columns


def paginate_urls(base_url: str, max_total: int) -> List[str]:
    base = base_url.rsplit(".html", 1)[0]
    pages = max(1, math.ceil(max_total / PAGE_SIZE))
    urls = []
    for page in range(1, pages + 1):
        if page == 1:
            urls.append(base_url)
        else:
            urls.append(f"{base}_{page}.html")
    return urls


def to_personal_domain(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc != "blog.csdn.net":
        return url
    path = parsed.path
    prefix = "/2301_80171004"
    if path.startswith(prefix):
        path = path[len(prefix) :]
        if not path:
            path = "/"
    return f"https://lvynote.blog.csdn.net{path}"


def fetch_html_with_fallback(session: Session, url: str) -> str:
    attempts = 0
    last_error: Exception | None = None
    candidate_urls = [url]
    alt = to_personal_domain(url)
    if alt != url:
        candidate_urls.append(alt)

    while attempts < 3:
        for candidate in candidate_urls:
            try:
                resp = session.get(candidate, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 521 and candidate != candidate_urls[-1]:
                    continue
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(0.6 * (attempts + 1))
        attempts += 1
    if last_error is None:
        raise RuntimeError(f"无法获取页面：{url}")
    raise last_error


def fetch_column_articles(session: Session, column: ColumnInfo) -> List[dict]:
    collected: List[dict] = []
    for url in paginate_urls(column.url, column.total):
        html = fetch_html_with_fallback(session, url)
        soup = BeautifulSoup(html, "html.parser")
        items = soup.select("ul.column_article_list > li")
        for item in items:
            anchor = item.find("a", href=True)
            title_node = anchor.find("h2", class_="title") if anchor else None
            if not (anchor and title_node):
                continue
            title_text = " ".join(title_node.stripped_strings)
            collected.append({"title": title_text, "url": anchor["href"]})
            if len(collected) >= column.total:
                break
        if len(collected) >= column.total:
            break
        time.sleep(0.3)
    return collected


def build_markdown(columns: Sequence[ColumnInfo], articles: Dict[int, List[dict]]) -> str:
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    expanded_articles = sum(len(articles.get(col.cid, [])) for col in columns)

    lines: List[str] = []
    lines.append("# ThoughtMap")
    lines.append("blog-archive | catalog | sample code")
    lines.append("")
    lines.append(f"> 数据抓取时间：{generated_at} (本地时间)")
    lines.append(
        "> 数据来源：[lvy- · CSDN 专栏](https://blog.csdn.net/2301_80171004?type=blog)"
    )
    lines.append("")
    lines.append(
        f"## 专栏索引（共{len(columns)}个专栏，收录{expanded_articles}篇文章）"
    )
    lines.append("")

    for column in columns:
        lines.append(f"### {column.safe_title} · {column.total} 篇")
        if column.safe_title in EXCLUDED_TITLES:
            lines.append("> ※ 用户要求该专栏仅展示标题，暂不展开文章目录。")
            lines.append("")
            continue

        column_articles = articles.get(column.cid, [])
        if not column_articles:
            lines.append("> 暂无可用文章数据。")
        else:
            for article in column_articles:
                title = article["title"].strip()
                url = article["url"]
                lines.append(f"- [{title}]({url})")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    session = build_session()
    columns = fetch_columns(session)
    articles_by_column: Dict[int, List[dict]] = {}

    for idx, column in enumerate(columns, 1):
        print(f"[{idx}/{len(columns)}] 拉取 {column.safe_title} ...", file=sys.stderr)
        if column.safe_title in EXCLUDED_TITLES:
            continue
        if column.total == 0:
            articles_by_column[column.cid] = []
            continue
        try:
            time.sleep(0.5)
            articles_by_column[column.cid] = fetch_column_articles(session, column)
        except requests.RequestException as exc:
            print(
                f"    ! 获取专栏 {column.safe_title} 失败：{exc}",
                file=sys.stderr,
            )
            articles_by_column[column.cid] = []

    markdown = build_markdown(columns, articles_by_column)
    README_PATH.write_text(markdown, encoding="utf-8")
    print(f"README 已更新 -> {README_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

