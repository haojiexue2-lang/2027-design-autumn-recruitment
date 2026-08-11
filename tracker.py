"""2027 届设计秋招官方来源追踪器。

程序只把通过资格校验的岗位写入飞书，并且永远不写入个人投递进度字段。
默认在没有飞书密钥时以 dry-run 运行，便于安全验收。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, urlunparse

TZ_NAME = "Asia/Shanghai"
CANONICAL_FIELDS = (
    "公司",
    "是否已投递",
    "公司级别",
    "优先级",
    "岗位名称",
    "方向标签",
    "招聘批次",
    "工作城市",
    "毕业届别",
    "学历要求",
    "专业要求",
    "招聘状态",
    "开放日期",
    "截止日期",
    "投递链接",
    "官方来源",
    "作品集要求",
    "资格摘要",
    "首次发现时间",
    "最后核验时间",
    "变更说明",
    "我的投递状态",
)
PROTECTED_FIELDS = frozenset({"是否已投递", "我的投递状态"})
MATERIAL_FIELDS = frozenset(
    {
        "公司级别",
        "优先级",
        "岗位名称",
        "方向标签",
        "招聘批次",
        "工作城市",
        "毕业届别",
        "学历要求",
        "专业要求",
        "招聘状态",
        "开放日期",
        "截止日期",
        "投递链接",
        "官方来源",
        "作品集要求",
        "资格摘要",
    }
)

ALLOWED_CITIES = (
    "北京",
    "上海",
    "广州",
    "深圳",
    "杭州",
    "成都",
    "重庆",
    "武汉",
    "苏州",
    "西安",
    "南京",
    "长沙",
    "郑州",
    "天津",
    "合肥",
    "青岛",
    "东莞",
    "宁波",
    "佛山",
)

ROLE_KEYWORDS = (
    "ui设计",
    "ux设计",
    "ui/ux",
    "ux/ui",
    "用户体验",
    "交互设计",
    "产品设计",
    "体验设计",
    "服务设计",
    "人机交互",
    "ai体验",
    "ai 体验",
    "智能座舱",
    "数字座舱",
    "视觉设计师",
    "界面设计",
)
INTERNSHIP_KEYWORDS = ("实习", "intern", "internship", "暑期项目")
CLOSED_KEYWORDS = (
    "职位已关闭",
    "岗位已关闭",
    "停止招聘",
    "已停止招聘",
    "招聘已结束",
    "职位不存在",
    "已下线",
)
BACHELOR_PATTERNS = (
    r"本科(?:及以上|或以上)?",
    r"本科生",
    r"学士(?:及以上)?",
    r"bachelor",
)
PORTFOLIO_KEYWORDS = ("作品集", "portfolio")
YEAR_2027_PATTERNS = (r"2027\s*届", r"2027\s*年", r"毕业时间[^\n]{0,40}2027")


def now_cn() -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(TZ_NAME))


def compact(value: Any) -> str:
    """把飞书可能返回的富文本、数组或标量统一成可比较字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                parts.append(compact(item.get("text") or item.get("name") or item.get("link") or item))
            else:
                parts.append(compact(item))
        return "".join(part for part in parts if part)
    if isinstance(value, dict):
        return compact(value.get("text") or value.get("name") or value.get("link") or json.dumps(value, ensure_ascii=False, sort_keys=True))
    return str(value).strip()


def normalize_field_name(name: str) -> str:
    return re.sub(r"[\s,，、。；;：:]+", "", name or "").lower()


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def canonical_job_id(url: str) -> str:
    url = normalize_url(url)
    for pattern in (
        r"/position/(\d+)",
        r"/job/(\d+)",
        r"/jobs/(\d+)",
        r"(?:job|position|post|id)[_-]?id=([A-Za-z0-9_-]+)",
    ):
        match = re.search(pattern, url, re.I)
        if match:
            return match.group(1)
    return url


def contains_pattern(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def infer_city(text: str) -> str:
    found = [city for city in ALLOWED_CITIES if city in text]
    return "、".join(dict.fromkeys(found[:4]))


def infer_direction(title: str, text: str) -> str:
    content = f"{title}\n{text}".lower()
    tags: list[str] = []
    mapping = (
        ("智能座舱体验", ("智能座舱", "数字座舱", "车机", "hmi")),
        ("AI体验设计", ("ai体验", "ai 体验", "生成式", "generative ui", "aigc", "vibe coding")),
        ("交互设计", ("交互", "interaction", "人机交互")),
        ("UX/体验设计", ("ux", "用户体验", "体验设计", "服务设计")),
        ("UI/视觉设计", ("ui", "视觉设计", "界面设计")),
        ("产品设计", ("产品设计", "product design")),
    )
    for label, words in mapping:
        if any(word in content for word in words):
            tags.append(label)
    return "、".join(tags[:4]) or "设计岗位"


def extract_deadline(text: str) -> str:
    patterns = (
        r"(?:截止|结束|投递至|申请至)[^\d]{0,12}(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})",
        r"(?:截止|结束|投递至|申请至)[^\d]{0,12}(\d{1,2})[月./-](\d{1,2})",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text)
        if not match:
            continue
        if index == 0:
            year, month, day = map(int, match.groups())
        else:
            month, day = map(int, match.groups())
            year = now_cn().year
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return ""


def is_within_7_days(deadline: str, today: date | None = None) -> bool:
    if not deadline:
        return False
    try:
        end = date.fromisoformat(deadline[:10])
    except ValueError:
        return False
    start = today or now_cn().date()
    return start <= end <= start + timedelta(days=7)


def title_matches_design(title: str) -> bool:
    normalized = re.sub(r"\s+", "", title).lower()
    if "产品经理" in normalized and "产品设计" not in normalized:
        return False
    return any(keyword.replace(" ", "") in normalized for keyword in ROLE_KEYWORDS)


def eligibility_reasons(title: str, text: str, source: dict[str, Any]) -> list[str]:
    """返回不通过原因；空列表表示通过硬性资格校验。"""
    reasons: list[str] = []
    content = f"{title}\n{text}".lower()
    if not title_matches_design(title):
        reasons.append("岗位名称不属于设计主线")
    # 只用岗位标题判定实习。正文中常出现“有相关实习经历优先”，不能据此误杀正式岗位。
    if any(keyword in title.lower() for keyword in INTERNSHIP_KEYWORDS):
        reasons.append("属于实习岗位")
    if not (source.get("campaign_2027") or contains_pattern(content, YEAR_2027_PATTERNS)):
        reasons.append("未核验为2027届")
    if not contains_pattern(content, BACHELOR_PATTERNS):
        reasons.append("未明确本科可投")
    # 工作地点通常位于详情页开头；限制取样可避免页脚“热门城市”造成假阳性。
    if not infer_city(f"{title}\n{text[:2500]}"):
        reasons.append("工作城市不在范围或未明确")
    if any(keyword in content for keyword in CLOSED_KEYWORDS):
        reasons.append("官方页面明确关闭")
    return reasons


@dataclass(slots=True)
class PageEvidence:
    url: str
    title: str
    text: str
    fetched_at: str
    available: bool = True
    error: str = ""


@dataclass(slots=True)
class JobRecord:
    company: str
    company_level: str
    priority: str
    title: str
    direction: str
    batch: str
    city: str
    cohort: str
    education: str
    major: str
    status: str
    open_date: str
    deadline: str
    apply_url: str
    official_source: str
    portfolio: str
    qualification: str
    first_seen: str
    last_verified: str
    change_note: str

    def fields(self) -> dict[str, str]:
        return {
            "公司": self.company,
            "公司级别": self.company_level,
            "优先级": self.priority,
            "岗位名称": self.title,
            "方向标签": self.direction,
            "招聘批次": self.batch,
            "工作城市": self.city,
            "毕业届别": self.cohort,
            "学历要求": self.education,
            "专业要求": self.major,
            "招聘状态": self.status,
            "开放日期": self.open_date,
            "截止日期": self.deadline,
            "投递链接": self.apply_url,
            "官方来源": self.official_source,
            "作品集要求": self.portfolio,
            "资格摘要": self.qualification,
            "首次发现时间": self.first_seen,
            "最后核验时间": self.last_verified,
            "变更说明": self.change_note,
        }


def build_record(source: dict[str, Any], evidence: PageEvidence, detected_at: datetime | None = None) -> JobRecord | None:
    detected_at = detected_at or now_cn()
    reasons = eligibility_reasons(evidence.title, evidence.text, source)
    if reasons:
        return None
    text = evidence.text
    deadline = extract_deadline(text)
    markers = ["今日新增"]
    if is_within_7_days(deadline, detected_at.date()):
        markers.append("7天内截止")
    note = "｜".join(markers + ["官方页面已核验"])
    education = "本科及以上"
    portfolio = "需要" if any(key.lower() in text.lower() for key in PORTFOLIO_KEYWORDS) else "官方未明确"
    major = source.get("default_major", "设计、艺术、人机交互等相关专业，以官方岗位说明为准")
    qualification = source.get("qualification_note", "2027届本科可投；方向与设计主线匹配；城市符合范围")
    return JobRecord(
        company=source["name"],
        company_level=source["company_level"],
        priority=source["priority"],
        title=evidence.title.strip(),
        direction=infer_direction(evidence.title, evidence.text),
        batch=source.get("batch", "2027届校园招聘"),
        city=infer_city(f"{evidence.title}\n{text[:2500]}"),
        cohort="2027届",
        education=education,
        major=major,
        status="可投",
        open_date=source.get("open_date", ""),
        deadline=deadline,
        apply_url=normalize_url(evidence.url),
        official_source=normalize_url(evidence.url),
        portfolio=portfolio,
        qualification=qualification,
        first_seen=detected_at.strftime("%Y-%m-%d %H:%M"),
        last_verified=detected_at.strftime("%Y-%m-%d %H:%M"),
        change_note=note,
    )


def build_placeholder(source: dict[str, Any], campaign: PageEvidence, detected_at: datetime | None = None) -> JobRecord:
    """官方项目已确认、但尚无合格设计岗位时保留监控占位行。"""
    detected_at = detected_at or now_cn()
    status = "待开启" if campaign.available else "待复核"
    note = "今日更新｜官方校招页已核验，设计岗位待发布" if campaign.available else "今日更新｜官方页面暂时不可访问，待下次复核"
    return JobRecord(
        company=source["name"],
        company_level=source["company_level"],
        priority=source["priority"],
        title=source.get("placeholder_title", "2027届设计岗位（待官方发布）"),
        direction="UI/UX、交互、产品设计、AI体验设计",
        batch=source.get("batch", "2027届校园招聘"),
        city="目标城市待官方岗位发布",
        cohort="2027届",
        education="本科（具体岗位待官方发布）",
        major="设计相关专业（具体岗位待官方发布）",
        status=status,
        open_date=source.get("open_date", ""),
        deadline="",
        apply_url=normalize_url(source["entry_url"]),
        official_source=normalize_url(source["entry_url"]),
        portfolio="待官方岗位发布",
        qualification="官方校招项目监控中；尚未发现同时满足届别、学历、方向和城市的正式设计岗位",
        first_seen=detected_at.strftime("%Y-%m-%d %H:%M"),
        last_verified=detected_at.strftime("%Y-%m-%d %H:%M"),
        change_note=note,
    )


def deduplicate(records: Iterable[JobRecord]) -> list[JobRecord]:
    output: dict[str, JobRecord] = {}
    for record in records:
        job_id = canonical_job_id(record.apply_url)
        key = job_id or "|".join((record.company, record.title, record.city, record.batch))
        current = output.get(key)
        if current is None or len(record.qualification) > len(current.qualification):
            output[key] = record
    return list(output.values())


class OfficialBrowserScanner:
    def __init__(self, *, headless: bool = True) -> None:
        self.headless = headless
        self._playwright = None
        self._browser = None

    async def __aenter__(self) -> "OfficialBrowserScanner":
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def fetch(self, url: str, timeout_ms: int = 45_000) -> PageEvidence:
        assert self._browser is not None
        page = await self._browser.new_page(locale="zh-CN")
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(1800)
            title = (await page.title()).strip()
            text = (await page.locator("body").inner_text(timeout=10_000)).strip()
            available = response is None or response.status < 500
            return PageEvidence(url=page.url, title=title, text=text, fetched_at=now_cn().isoformat(), available=available)
        except Exception as exc:  # 页面临时异常必须待复核，不能误判关闭
            return PageEvidence(url=url, title="", text="", fetched_at=now_cn().isoformat(), available=False, error=str(exc))
        finally:
            await page.close()

    async def discover_links(self, source: dict[str, Any]) -> tuple[list[str], PageEvidence]:
        assert self._browser is not None
        entry_url = source["entry_url"]
        page = await self._browser.new_page(locale="zh-CN")
        try:
            response = await page.goto(entry_url, wait_until="domcontentloaded", timeout=60_000)
            for _ in range(source.get("scroll_rounds", 4)):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(900)
            text = (await page.locator("body").inner_text(timeout=15_000)).strip()
            title = (await page.title()).strip()
            raw_links = await page.locator("a[href]").evaluate_all("els => els.map(e => e.href)")
            allowed = set(source["allowed_domains"])
            link_pattern = re.compile(source.get("job_link_regex", r"/(?:job|position|post|detail)/"), re.I)
            links: list[str] = []
            for link in [*source.get("seed_jobs", []), *raw_links]:
                parsed = urlparse(urljoin(entry_url, link))
                if parsed.netloc not in allowed or not link_pattern.search(parsed.path + "?" + parsed.query):
                    continue
                links.append(normalize_url(parsed.geturl()))
            evidence = PageEvidence(
                url=page.url,
                title=title,
                text=text,
                fetched_at=now_cn().isoformat(),
                available=response is None or response.status < 500,
            )
            return list(dict.fromkeys(links))[: int(source.get("max_details", 80))], evidence
        except Exception as exc:
            evidence = PageEvidence(url=entry_url, title="", text="", fetched_at=now_cn().isoformat(), available=False, error=str(exc))
            return list(dict.fromkeys(source.get("seed_jobs", []))), evidence
        finally:
            await page.close()

    async def scan_source(self, source: dict[str, Any]) -> tuple[list[JobRecord], dict[str, Any]]:
        links, campaign = await self.discover_links(source)
        records: list[JobRecord] = []
        failures: list[str] = []
        semaphore = asyncio.Semaphore(int(source.get("concurrency", 3)))

        async def inspect(link: str) -> None:
            async with semaphore:
                evidence = await self.fetch(link)
            if not evidence.available:
                failures.append(link)
                return
            record = build_record(source, evidence)
            if record:
                records.append(record)

        await asyncio.gather(*(inspect(link) for link in links))
        qualified_count = len(records)
        if not records:
            records.append(build_placeholder(source, campaign))
        report = {
            "company": source["name"],
            "entry_url": source["entry_url"],
            "campaign_available": campaign.available,
            "discovered_links": len(links),
            "qualified_jobs": qualified_count,
            "failures": failures,
        }
        return records, report


class FeishuBitable:
    def __init__(self, app_id: str, app_secret: str, app_token: str, table_id: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self.table_id = table_id
        self.base_url = "https://open.feishu.cn/open-apis"
        import httpx

        self.client = httpx.AsyncClient(timeout=httpx.Timeout(45.0), follow_redirects=True)
        self._token = ""
        self.field_map: dict[str, str] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def authenticate(self) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.client.post(
                    f"{self.base_url}/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") != 0:
                    raise RuntimeError(f"飞书鉴权失败：{payload.get('msg')} ({payload.get('code')})")
                self._token = payload["tenant_access_token"]
                return
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        assert last_error is not None
        raise last_error

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json; charset=utf-8"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self._token:
            await self.authenticate()
        response = await self.client.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs)
        if response.status_code == 401:
            await self.authenticate()
            response = await self.client.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"飞书 API 失败：{payload.get('msg')} ({payload.get('code')})")
        return payload

    async def load_fields(self) -> dict[str, str]:
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields?page_size=100"
        payload = await self._request("GET", path)
        actual = [item["field_name"] for item in payload.get("data", {}).get("items", [])]
        normalized = {normalize_field_name(name): name for name in actual}
        mapping: dict[str, str] = {}
        for canonical in CANONICAL_FIELDS:
            key = normalize_field_name(canonical)
            if key in normalized:
                mapping[canonical] = normalized[key]
                continue
            # 容错 UI 输入法遗留的标点，例如“方向标签，，，”
            starts = [name for norm, name in normalized.items() if norm.startswith(key)]
            if starts:
                mapping[canonical] = starts[0]
        missing = [name for name in CANONICAL_FIELDS if name not in mapping]
        if missing:
            raise RuntimeError(f"飞书表格缺少字段：{', '.join(missing)}")
        self.field_map = mapping
        return mapping

    async def list_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"
            payload = await self._request("GET", path, params=params)
            data = payload.get("data", {})
            records.extend(data.get("items", []))
            if not data.get("has_more"):
                return records
            page_token = data["page_token"]

    def to_actual_fields(self, canonical_fields: dict[str, Any]) -> dict[str, Any]:
        if not self.field_map:
            raise RuntimeError("尚未读取飞书字段")
        safe = {key: value for key, value in canonical_fields.items() if key not in PROTECTED_FIELDS}
        return {self.field_map[key]: value for key, value in safe.items() if key in self.field_map}

    async def batch_create(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/batch_create"
        for start in range(0, len(records), 500):
            payload = {"records": [{"fields": self.to_actual_fields(item)} for item in records[start : start + 500]]}
            await self._request("POST", path, json=payload)

    async def batch_update(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/batch_update"
        for start in range(0, len(records), 500):
            payload = {
                "records": [
                    {"record_id": item["record_id"], "fields": self.to_actual_fields(item["fields"])}
                    for item in records[start : start + 500]
                ]
            }
            await self._request("POST", path, json=payload)


def canonicalize_existing_fields(fields: dict[str, Any], field_map: dict[str, str]) -> dict[str, str]:
    return {canonical: compact(fields.get(actual)) for canonical, actual in field_map.items()}


def composite_key(fields: dict[str, str]) -> str:
    return "|".join(compact(fields.get(name)) for name in ("公司", "岗位名称", "工作城市", "招聘批次"))


def build_write_plan(
    discovered: list[JobRecord],
    existing: list[dict[str, Any]],
    field_map: dict[str, str],
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    checked_at = checked_at or now_cn()
    by_url: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    by_composite: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    for item in existing:
        canonical = canonicalize_existing_fields(item.get("fields", {}), field_map)
        if canonical.get("投递链接"):
            by_url[canonical_job_id(canonical["投递链接"])] = (item, canonical)
        by_composite[composite_key(canonical)] = (item, canonical)

    creates: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    unchanged = 0
    changed_companies: list[str] = []
    today = checked_at.date()

    for record in deduplicate(discovered):
        incoming = record.fields()
        match = by_url.get(canonical_job_id(record.apply_url)) or by_composite.get(composite_key(incoming))
        if not match:
            creates.append(incoming)
            changed_companies.append(record.company)
            continue
        item, current = match
        incoming["首次发现时间"] = current.get("首次发现时间") or incoming["首次发现时间"]
        material_changes = [field for field in MATERIAL_FIELDS if compact(incoming.get(field)) != compact(current.get(field))]
        note_parts: list[str] = []
        if material_changes:
            note_parts.append("今日更新")
            note_parts.append("变更：" + "、".join(sorted(material_changes)))
            changed_companies.append(record.company)
        else:
            old_note = re.sub(r"(?:今日新增|今日更新)(?:｜)?", "", current.get("变更说明", "")).strip("｜")
            if old_note:
                note_parts.append(old_note)
        if is_within_7_days(incoming.get("截止日期", ""), today):
            note_parts.insert(0, "7天内截止")
        incoming["变更说明"] = "｜".join(dict.fromkeys(part for part in note_parts if part)) or "官方页面已核验"
        incoming["最后核验时间"] = checked_at.strftime("%Y-%m-%d %H:%M")

        changed = {
            key: value
            for key, value in incoming.items()
            if key not in PROTECTED_FIELDS and compact(value) != compact(current.get(key))
        }
        if changed:
            updates.append({"record_id": item["record_id"], "fields": changed})
        else:
            unchanged += 1

    return {
        "creates": creates,
        "updates": updates,
        "unchanged": unchanged,
        "changed_companies": sorted(set(changed_companies)),
    }


def load_config(path: str | Path) -> dict[str, Any]:
    import yaml

    with Path(path).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict) or not isinstance(config.get("sources"), list):
        raise ValueError("sources.yaml 缺少 sources 列表")
    return config


async def run(config_path: Path, dry_run: bool) -> dict[str, Any]:
    config = load_config(config_path)
    all_records: list[JobRecord] = []
    source_reports: list[dict[str, Any]] = []
    async with OfficialBrowserScanner(headless=True) as scanner:
        for source in config["sources"]:
            records, report = await scanner.scan_source(source)
            all_records.extend(records)
            source_reports.append(report)

    discovered = deduplicate(all_records)
    summary: dict[str, Any] = {
        "run_at": now_cn().isoformat(),
        "sources": source_reports,
        "qualified_jobs": sum(record.status == "可投" for record in discovered),
        "tracked_rows": len(discovered),
        "jobs": [asdict(record) for record in discovered],
        "dry_run": dry_run,
    }

    required_env = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_APP_TOKEN", "FEISHU_TABLE_ID")
    missing_env = [name for name in required_env if not os.getenv(name)]
    if dry_run or missing_env:
        summary["write_skipped"] = "dry-run" if dry_run else "缺少云端密钥：" + ", ".join(missing_env)
        return summary

    client = FeishuBitable(*(os.environ[name] for name in required_env))
    try:
        field_map = await client.load_fields()
        existing = await client.list_records()
        plan = build_write_plan(discovered, existing, field_map)
        await client.batch_create(plan["creates"])
        await client.batch_update(plan["updates"])
        summary.update(
            {
                "new_records": len(plan["creates"]),
                "updated_records": len(plan["updates"]),
                "unchanged_records": plan["unchanged"],
                "changed_companies": plan["changed_companies"],
            }
        )
    finally:
        await client.close()
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="2027 届设计秋招官方来源追踪器")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("sources.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="只扫描与输出，不写入飞书")
    parser.add_argument("--output", type=Path, default=Path("run-summary.json"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = asyncio.run(run(args.config, args.dry_run))
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in summary.items() if key != "jobs"}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"追踪任务失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
