"""
B站视频随机推送 & 视频内容问答插件
适用: maibot 框架
功能:
  1. 随机间隔从B站热门池中随机抽取视频，以QQ小程序卡片格式推送到群聊
  2. 点击卡片直接跳转QQ内的B站小程序观看
  3. 群友提问视频内容时拉取简介/字幕/评论，由LLM概括回答
  4. 支持关键词搜索B站视频并推送结果
"""

import asyncio
import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote, urlencode

import httpx
import tomllib


# ============================================================
# 数据模型
# ============================================================

@dataclass
class VideoInfo:
    """B站视频信息"""
    bvid: str = ""
    aid: int = 0
    title: str = ""
    author: str = ""
    mid: int = 0
    cover: str = ""
    description: str = ""
    duration: int = 0
    play: int = 0
    danmaku: int = 0
    like: int = 0
    favorite: int = 0
    reply: int = 0
    share: int = 0
    pubdate: int = 0
    tags: list[str] = field(default_factory=list)
    region: str = ""
    subtitle: str = ""
    comments: list[str] = field(default_factory=list)


@dataclass
class PushRecord:
    """推送记录 (去重用)"""
    bvid: str
    pushed_at: float
    group_id: str = ""


# ============================================================
# B站API客户端
# ============================================================

class BilibiliAPI:
    """封装B站各类API请求"""

    def __init__(self, config: dict):
        self.config = config
        self.backend = config.get("bilibili_backend", {})
        self.network = config.get("network", {})
        self.api_base = self.backend.get("api_base", "https://api.bilibili.com")
        self.timeout = self.backend.get("timeout", 15)
        self.max_retries = self.backend.get("max_retries", 3)
        self.request_interval = self.backend.get("request_interval_ms", 500) / 1000.0
        self._last_request = 0.0
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            ua = random.choice(self.network.get("user_agents", [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            ]))
            headers = {
                "User-Agent": ua,
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            cookie = self.network.get("cookie", "")
            if cookie:
                headers["Cookie"] = cookie
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            )
        return self._client

    async def _rate_limit(self):
        """请求间隔控制"""
        elapsed = time.time() - self._last_request
        if elapsed < self.request_interval:
            await asyncio.sleep(self.request_interval - elapsed)
        self._last_request = time.time()

    async def _request(self, url: str, params: dict = None) -> dict | None:
        """带重试的GET请求"""
        await self._rate_limit()
        client = await self._get_client()
        for attempt in range(self.max_retries):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 412:
                    # 被拦截，等久一点重试
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise
                await asyncio.sleep(1 * (attempt + 1))
        return None

    # ----- 热门 & 排行榜 -----

    async def get_popular(self, pn: int = 1, ps: int = 50) -> list[VideoInfo]:
        """获取综合热门"""
        url = f"{self.api_base}{self.backend.get('popular_api', '/x/web-interface/popular')}"
        data = await self._request(url, {"pn": pn, "ps": ps})
        return self._parse_video_list(data)

    async def get_ranking(self, rid: int = 0, pn: int = 1, ps: int = 50) -> list[VideoInfo]:
        """获取排行榜"""
        url = f"{self.api_base}{self.backend.get('ranking_api', '/x/web-interface/ranking/v2')}"
        data = await self._request(url, {"rid": rid, "type": "all" if rid == 0 else "region"})
        return self._parse_video_list(data)

    async def get_weekly(self, number: int = 1) -> list[VideoInfo]:
        """获取每周必看"""
        # 先获取期数列表，再取最新
        url = f"{self.api_base}{self.backend.get('weekly_api', '/x/web-interface/popular/series/one')}"
        data = await self._request(url, {"number": number})
        return self._parse_video_list(data)

    # ----- 视频详情 -----

    async def get_video_info(self, bvid: str) -> Optional[VideoInfo]:
        """获取单个视频详情"""
        url = f"{self.api_base}{self.backend.get('video_info_api', '/x/web-interface/view')}"
        data = await self._request(url, {"bvid": bvid})
        if data and data.get("code") == 0:
            v = data["data"]
            return VideoInfo(
                bvid=v.get("bvid", bvid),
                aid=v.get("aid", 0),
                title=v.get("title", ""),
                author=v["owner"]["name"] if "owner" in v else "",
                mid=v["owner"]["mid"] if "owner" in v else 0,
                cover=v.get("pic", ""),
                description=v.get("desc", ""),
                duration=v.get("duration", 0),
                play=v["stat"].get("view", 0) if "stat" in v else 0,
                danmaku=v["stat"].get("danmaku", 0) if "stat" in v else 0,
                like=v["stat"].get("like", 0) if "stat" in v else 0,
                favorite=v["stat"].get("favorite", 0) if "stat" in v else 0,
                reply=v["stat"].get("reply", 0) if "stat" in v else 0,
                share=v["stat"].get("share", 0) if "stat" in v else 0,
                pubdate=v.get("pubdate", 0),
                tags=[t["tag_name"] for t in v.get("tags", [])] if v.get("tags") else [],
                region=v.get("tname", ""),
            )
        return None

    # ----- 字幕 -----

    async def get_subtitle(self, bvid: str, aid: int = 0, cid: int = 0) -> str:
        """获取视频字幕 (CC字幕或AI字幕)"""
        url = f"{self.api_base}{self.backend.get('subtitle_api', '/x/player/v2')}"
        params = {"bvid": bvid}
        if cid:
            params["cid"] = cid
        data = await self._request(url, params)
        if data and data.get("code") == 0:
            subtitle_data = data["data"].get("subtitle", {})
            subtitles = subtitle_data.get("subtitles", [])
            if subtitles:
                sub_url = subtitles[0].get("subtitle_url", "")
                if sub_url:
                    if sub_url.startswith("//"):
                        sub_url = "https:" + sub_url
                    client = await self._get_client()
                    resp = await client.get(sub_url)
                    if resp.status_code == 200:
                        return self._parse_subtitle(resp.json())
        return ""

    def _parse_subtitle(self, data: dict) -> str:
        """解析字幕JSON为纯文本"""
        body = data.get("body", [])
        lines = [item.get("content", "") for item in body]
        return "\n".join(lines[:200])  # 限制长度

    # ----- 评论 -----

    async def get_comments(self, oid: int, pn: int = 1, ps: int = 20) -> list[str]:
        """获取视频评论"""
        url = f"{self.api_base}{self.backend.get('comment_api', '/x/v2/reply/main')}"
        data = await self._request(url, {"oid": oid, "type": 1, "pn": pn, "ps": ps, "sort": 1})
        comments = []
        if data and data.get("code") == 0:
            for reply in data["data"].get("replies", []):
                content = reply["content"].get("message", "")
                if content:
                    comments.append(content)
        return comments

    # ----- 搜索 -----

    async def search(self, keyword: str, page: int = 1, ps: int = 10) -> list[VideoInfo]:
        """搜索视频"""
        url = f"{self.api_base}{self.backend.get('search_api', '/x/web-interface/search/type')}"
        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "order": "totalrank",
        }
        data = await self._request(url, params)
        return self._parse_video_list(data)

    # ----- 用户视频 -----

    async def get_user_videos(self, mid: int, ps: int = 10) -> list[VideoInfo]:
        """获取UP主投稿视频"""
        url = f"{self.api_base}{self.backend.get('user_videos_api', '/x/space/wbi/arc/search')}"
        data = await self._request(url, {"mid": mid, "ps": ps, "order": "pubdate"})
        return self._parse_video_list(data)

    # ----- 解析 -----

    def _parse_video_list(self, data: dict) -> list[VideoInfo]:
        """统一解析视频列表响应"""
        if not data or data.get("code") != 0:
            return []
        videos = []
        # 兼容多种数据结构
        raw_list = (
            data.get("data", {}).get("list", [])
            or data.get("data", {}).get("archives", [])
            or data.get("data", {}).get("result", [])
            or data.get("data", [])
        )
        if isinstance(raw_list, dict):
            raw_list = raw_list.get("vlist", []) or []
        for item in raw_list:
            try:
                v = VideoInfo(
                    bvid=item.get("bvid", ""),
                    aid=item.get("aid", 0),
                    title=item.get("title", ""),
                    author=item["owner"]["name"] if "owner" in item else item.get("author", ""),
                    mid=item["owner"]["mid"] if "owner" in item else item.get("mid", 0),
                    cover=item.get("pic", ""),
                    description=item.get("desc", ""),
                    duration=item.get("duration", 0),
                    play=item.get("stat", {}).get("view", item.get("play", 0)),
                    danmaku=item.get("stat", {}).get("danmaku", item.get("danmaku", 0)),
                    like=item.get("stat", {}).get("like", item.get("like", 0)),
                    favorite=item.get("stat", {}).get("favorite", 0),
                    reply=item.get("stat", {}).get("reply", 0),
                    share=item.get("stat", {}).get("share", 0),
                    pubdate=item.get("pubdate", 0),
                )
                videos.append(v)
            except Exception:
                continue
        return videos

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None


# ============================================================
# 视频池 & 去重
# ============================================================

class VideoPool:
    """管理候选视频池，支持多来源混合 + 去重"""

    def __init__(self, config: dict, api: BilibiliAPI):
        self.config = config
        self.api = api
        self.push_config = config.get("random_push", {})
        self.pool_config = self.push_config.get("pool", {})
        self.filters = config.get("filters", {})
        self.dedup_window = timedelta(
            hours=self.pool_config.get("dedup_window_hours", 24)
        )
        self._push_history: list[PushRecord] = []
        self._pool: list[VideoInfo] = []
        self._pool_updated_at: float = 0.0
        self._pool_ttl: float = 1800.0  # 池子30分钟刷新

    def _is_expired(self, record: PushRecord) -> bool:
        return (datetime.now() - datetime.fromtimestamp(record.pushed_at)) > self.dedup_window

    def _clean_history(self):
        self._push_history = [r for r in self._push_history if not self._is_expired(r)]

    def is_duplicate(self, bvid: str) -> bool:
        self._clean_history()
        return any(r.bvid == bvid for r in self._push_history)

    def record_push(self, bvid: str, group_id: str = ""):
        self._push_history.append(PushRecord(bvid=bvid, pushed_at=time.time(), group_id=group_id))
        self._clean_history()

    async def refresh_pool(self):
        """从多个来源拉取视频混入候选池"""
        sources = self.pool_config.get("sources", [
            {"type": "popular", "weight": 50},
            {"type": "ranking", "rid": 0, "weight": 30},
            {"type": "weekly", "weight": 20},
        ])
        pool_size = self.pool_config.get("pool_size", 50)
        all_videos: list[tuple[VideoInfo, int]] = []

        for src in sources:
            src_type = src.get("type", "")
            weight = src.get("weight", 10)
            try:
                if src_type == "popular":
                    videos = await self.api.get_popular(ps=pool_size)
                elif src_type == "ranking":
                    rid = src.get("rid", 0)
                    videos = await self.api.get_ranking(rid=rid, ps=pool_size)
                elif src_type == "weekly":
                    videos = await self.api.get_weekly()
                else:
                    continue
                for v in videos:
                    if self._pass_filter(v):
                        all_videos.append((v, weight))
            except Exception:
                continue

        if not all_videos:
            return

        # 去重
        seen: set[str] = set()
        unique: list[tuple[VideoInfo, int]] = []
        for v, w in all_videos:
            if v.bvid not in seen and not self.is_duplicate(v.bvid):
                seen.add(v.bvid)
                unique.append((v, w))

        self._pool = [v for v, _ in unique]
        self._pool_updated_at = time.time()

    def _pass_filter(self, v: VideoInfo) -> bool:
        """检查视频是否通过过滤器"""
        min_play = self.filters.get("min_play_count", 5000)
        max_duration = self.filters.get("max_duration_seconds", 1800)
        min_like_ratio = self.filters.get("min_like_ratio", 0.005)
        title_blacklist = self.filters.get("title_blacklist", [])
        region_whitelist = self.filters.get("region_whitelist", [])

        if v.play < min_play:
            return False
        if v.duration > max_duration:
            return False
        if v.play > 0 and v.like / v.play < min_like_ratio:
            return False
        for banned in title_blacklist:
            if banned.lower() in v.title.lower():
                return False
        if region_whitelist and v.region not in region_whitelist:
            return False
        return True

    def pick(self, count: int = 1) -> list[VideoInfo]:
        """按算法从池中选取视频"""
        if not self._pool:
            return []
        algorithm = self.pool_config.get("pick_algorithm", "roulette")
        available = [v for v in self._pool if not self.is_duplicate(v.bvid)]
        if not available:
            return []

        if algorithm == "shuffle":
            picked = random.sample(available, min(count, len(available)))
        elif algorithm == "roulette":
            # 按播放量加权轮盘赌
            picked = self._roulette_pick(available, count)
        else:
            # weighted: 完全随机
            picked = random.sample(available, min(count, len(available)))
        return picked

    def _roulette_pick(self, videos: list[VideoInfo], count: int) -> list[VideoInfo]:
        total_play = sum(max(v.play, 1) for v in videos)
        if total_play == 0:
            return random.sample(videos, min(count, len(videos)))
        picked: list[VideoInfo] = []
        remaining = list(videos)
        for _ in range(min(count, len(videos))):
            r = random.uniform(0, total_play)
            cumulative = 0
            chosen = remaining[0]
            chosen_idx = 0
            for i, v in enumerate(remaining):
                cumulative += max(v.play, 1)
                if cumulative >= r:
                    chosen = v
                    chosen_idx = i
                    break
            picked.append(chosen)
            total_play -= max(chosen.play, 1)
            remaining.pop(chosen_idx)
        return picked

    @property
    def pool_size(self) -> int:
        return len(self._pool)

    @property
    def needs_refresh(self) -> bool:
        return (time.time() - self._pool_updated_at) > self._pool_ttl or not self._pool


# ============================================================
# QQ卡片构建器
# ============================================================

class CardBuilder:
    """构建QQ小程序卡片 / ARK卡片 / 文本回退"""

    def __init__(self, config: dict):
        self.config = config
        self.card_config = config.get("qq_card", {})

    def build_miniapp(self, video: VideoInfo) -> dict:
        """构建QQ小程序卡片"""
        mc = self.card_config.get("miniapp", {})
        content = self.card_config.get("content", {})
        scheme = mc.get("scheme_template", "").replace("{bvid}", video.bvid)
        web_url = mc.get("web_url_template", "").replace("{bvid}", video.bvid)
        title_tpl = content.get("title_template", "{title}")
        desc_tpl = content.get("desc_template", "UP主: {author} | {play}播放 | {danmaku}弹幕")

        title = self._truncate(title_tpl.format(
            title=video.title, author=video.author,
            play=self._format_count(video.play),
            danmaku=self._format_count(video.danmaku),
        ), 24)

        desc = self._truncate(desc_tpl.format(
            title=video.title, author=video.author,
            play=self._format_count(video.play),
            danmaku=self._format_count(video.danmaku),
        ), 40)

        preview = content.get("preview_template", "{cover}@480w_300h.jpg").format(cover=video.cover)

        return {
            "type": "miniapp",
            "appid": mc.get("appid", "1108338344"),
            "app_name": mc.get("app_name", "哔哩哔哩"),
            "app_icon": mc.get("app_icon", ""),
            "title": title,
            "desc": desc,
            "preview": preview,
            "scheme": scheme,
            "web_url": web_url,
            "source_text": content.get("source_text", "哔哩哔哩"),
        }

    def build_ark(self, video: VideoInfo) -> dict:
        """构建ARK卡片 (备选方案)"""
        if not self.card_config.get("ark", {}).get("enabled", True):
            return {}
        tpl = self.card_config["ark"]["ark_template"]
        raw = tpl.replace("{bvid}", video.bvid)\
                  .replace("{title}", self._escape_json(video.title))\
                  .replace("{author}", self._escape_json(video.author))\
                  .replace("{play}", self._format_count(video.play))\
                  .replace("{danmaku}", self._format_count(video.danmaku))\
                  .replace("{cover}", video.cover)\
                  .replace("{timestamp}", str(int(time.time())))
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def build_fallback_text(self, video: VideoInfo) -> str:
        """构建文本回退消息"""
        tpl = self.card_config.get("fallback", {}).get("fallback_template", "")
        if not tpl:
            tpl = "🎬 {title}\n👤 {author}\n▶️ {play}播放 | 💬 {danmaku}弹幕 | 👍 {like}点赞\n🔗 https://b23.tv/{bvid}"
        return tpl.format(
            title=video.title,
            author=video.author,
            play=self._format_count(video.play),
            danmaku=self._format_count(video.danmaku),
            like=self._format_count(video.like),
            bvid=video.bvid,
        )

    @staticmethod
    def _format_count(n: int) -> str:
        if n >= 10000:
            return f"{n/10000:.1f}万"
        return str(n)

    @staticmethod
    def _truncate(s: str, max_chars: int) -> str:
        if len(s) <= max_chars:
            return s
        return s[:max_chars - 1] + "…"

    @staticmethod
    def _escape_json(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')


# ============================================================
# 内容抓取器
# ============================================================

class ContentFetcher:
    """抓取视频内容: 简介、字幕、评论"""

    def __init__(self, config: dict, api: BilibiliAPI):
        self.config = config
        self.api = api
        self.cf = config.get("content_fetch", {})

    async def fetch(self, video: VideoInfo) -> VideoInfo:
        """填充视频的详细内容"""
        if self.cf.get("fetch_description", True) and not video.description:
            info = await self.api.get_video_info(video.bvid)
            if info:
                video.description = info.description

        if self.cf.get("fetch_subtitle", True):
            video.subtitle = await self.api.get_subtitle(video.bvid, video.aid)

        if self.cf.get("fetch_comments", True):
            max_comments = self.cf.get("max_comments", 20)
            video.comments = await self.api.get_comments(video.aid, ps=max_comments)

        return video


# ============================================================
# Q&A处理器
# ============================================================

class QnAHandler:
    """处理视频内容问答"""

    def __init__(self, config: dict, api: BilibiliAPI, fetcher: ContentFetcher):
        self.config = config
        self.api = api
        self.fetcher = fetcher
        self.qna_config = config.get("qna", {})
        self.prompts = config.get("prompts", {})

    async def answer(self, bvid: str, question: str, llm_call) -> str:
        """针对视频回答用户问题"""
        video = await self.api.get_video_info(bvid)
        if not video:
            return f"没找到这个视频 BV号: {bvid}"

        video = await self.fetcher.fetch(video)

        system_prompt = self.prompts.get("qna_system",
            "你是一个B站视频内容问答助手。根据提供的视频信息回答用户关于视频的问题。"
        )
        user_prompt = self.prompts.get("qna_user", "")
        if not user_prompt:
            user_prompt = (
                "关于以下B站视频，用户提出了问题：\n\n"
                "视频标题: {title}\nUP主: {author}\n简介: {description}\n"
                "字幕片段: {subtitle}\n热门评论: {comments}\n\n"
                "用户问题: {question}\n\n请根据以上信息回答用户的问题："
            )

        user_prompt = user_prompt.format(
            title=video.title,
            author=video.author,
            description=video.description[:500] if video.description else "",
            subtitle=video.subtitle[:1000] if video.subtitle else "",
            comments="\n".join(video.comments[:10]) if video.comments else "",
            question=question,
        )

        reply = await llm_call(system_prompt, user_prompt)

        reply_tpl = self.qna_config.get("reply_template", "")
        if reply_tpl:
            return reply_tpl.format(
                title=video.title, summary=reply,
                play=self._fmt(video.play), like=self._fmt(video.like),
                favorite=self._fmt(video.favorite), reply=self._fmt(video.reply),
                bvid=video.bvid,
            )
        return reply

    async def summarize(self, video: VideoInfo, llm_call) -> str:
        """概括视频内容"""
        video = await self.fetcher.fetch(video)

        system_prompt = self.prompts.get("summarize_system",
            "你是一个B站视频内容总结助手。请根据视频信息生成简洁准确的概括。"
        )
        user_prompt = self.prompts.get("summarize_user", "")
        if not user_prompt:
            user_prompt = (
                "请概括以下B站视频的内容：\n\n"
                "标题: {title}\nUP主: {author}\n简介: {description}\n"
                "字幕片段: {subtitle}\n热门评论: {comments}\n\n"
                "请生成视频内容概括："
            )

        user_prompt = user_prompt.format(
            title=video.title, author=video.author,
            description=video.description[:500] if video.description else "",
            subtitle=video.subtitle[:1000] if video.subtitle else "",
            comments="\n".join(video.comments[:10]) if video.comments else "",
        )

        return await llm_call(system_prompt, user_prompt)

    @staticmethod
    def _fmt(n: int) -> str:
        if n >= 10000:
            return f"{n/10000:.1f}万"
        return str(n)


# ============================================================
# 推送调度器
# ============================================================

class PushScheduler:
    """管理随机推送的定时调度"""

    def __init__(self, config: dict, pool: VideoPool, api: BilibiliAPI, card_builder: CardBuilder):
        self.config = config
        self.pool = pool
        self.api = api
        self.card_builder = card_builder
        self.push_config = config.get("random_push", {})
        self.advanced = config.get("advanced", {})
        self._tasks: dict[str, asyncio.Task] = {}
        self._group_toggle: dict[str, bool] = {}  # 按群开关
        self.enabled = self.push_config.get("enabled", True)

    def is_group_enabled(self, group_id: str) -> bool:
        if self.config.get("groups", {}).get("per_group_toggle", True):
            return self._group_toggle.get(group_id, True)
        return True

    def set_group_enabled(self, group_id: str, enabled: bool):
        self._group_toggle[group_id] = enabled

    async def start(self, group_ids: list[str], send_callback):
        """为每个群启动推送任务"""
        for gid in group_ids:
            if gid not in self._tasks:
                self._tasks[gid] = asyncio.create_task(
                    self._push_loop(gid, send_callback)
                )

    async def stop(self):
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def _push_loop(self, group_id: str, send_callback):
        """单个群的推送循环"""
        while True:
            try:
                if not self.enabled or not self.is_group_enabled(group_id):
                    await asyncio.sleep(60)
                    continue

                if not self._in_active_hours():
                    await asyncio.sleep(300)
                    continue

                # 随机间隔
                min_int = self.push_config.get("min_interval_minutes", 60)
                max_int = self.push_config.get("max_interval_minutes", 240)
                interval = random.randint(min_int * 60, max_int * 60)
                await asyncio.sleep(interval)

                if not self.is_group_enabled(group_id):
                    continue

                await self._do_push(group_id, send_callback)

            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(60)

    async def _do_push(self, group_id: str, send_callback):
        """执行一次推送"""
        min_count = self.push_config.get("min_count", 1)
        max_count = self.push_config.get("max_count", 3)
        count = random.randint(min_count, max_count)

        if self.pool.needs_refresh:
            await self.pool.refresh_pool()

        videos = self.pool.pick(count)
        if not videos:
            # 空池，重试刷新
            await self.pool.refresh_pool()
            videos = self.pool.pick(count)

        for video in videos:
            try:
                card = self.card_builder.build_miniapp(video)
                await send_callback(group_id, card, self.card_builder.build_fallback_text(video))
                self.pool.record_push(video.bvid, group_id)
            except Exception:
                continue

    def _in_active_hours(self) -> bool:
        """检查当前是否在活跃时间段内"""
        active = self.push_config.get("active_hours", [])
        if not active:
            return True
        now = datetime.now().strftime("%H:%M")
        for period in active:
            parts = period.split("-")
            if len(parts) == 2 and parts[0] <= now <= parts[1]:
                return True
        return False


# ============================================================
# 主插件类
# ============================================================

class Plugin:
    """B站视频随机推送 & 问答插件"""

    def __init__(self, framework):
        self.fw = framework
        self.config_path = os.path.join(os.path.dirname(__file__), "config.toml")
        self.config: dict = {}
        self.api: Optional[BilibiliAPI] = None
        self.pool: Optional[VideoPool] = None
        self.card_builder: Optional[CardBuilder] = None
        self.fetcher: Optional[ContentFetcher] = None
        self.qna: Optional[QnAHandler] = None
        self.scheduler: Optional[PushScheduler] = None
        self._running = False

    # ===== 生命周期 =====

    async def on_load(self):
        """插件加载"""
        self._load_config()
        self.api = BilibiliAPI(self.config)
        self.pool = VideoPool(self.config, self.api)
        self.card_builder = CardBuilder(self.config)
        self.fetcher = ContentFetcher(self.config, self.api)
        self.qna = QnAHandler(self.config, self.api, self.fetcher)
        self.scheduler = PushScheduler(self.config, self.pool, self.api, self.card_builder)
        self.fw.logger.info("[B站插件] 已加载")

    async def on_enable(self, group_id: str = ""):
        """插件启用"""
        self._load_config()
        self._running = True
        # 启动推送调度
        groups_config = self.config.get("groups", {})
        push_targets = groups_config.get("push_targets", [])
        bindings = groups_config.get("bindings", [])
        all_targets = list(set(push_targets + bindings))
        if group_id:
            all_targets = [group_id]

        # 检查预缓存
        if self.config.get("advanced", {}).get("prefetch_enabled", True):
            asyncio.create_task(self._prefetch_loop())

        await self.scheduler.start(all_targets, self._send_to_group)
        self.fw.logger.info(f"[B站插件] 已启用, 目标群: {all_targets}")

    async def on_disable(self):
        """插件禁用"""
        self._running = False
        if self.scheduler:
            await self.scheduler.stop()
        if self.api:
            await self.api.close()
        self.fw.logger.info("[B站插件] 已禁用")

    async def on_unload(self):
        """插件卸载"""
        await self.on_disable()

    def _load_config(self):
        if os.path.exists(self.config_path):
            with open(self.config_path, "rb") as f:
                self.config = tomllib.load(f)

    # ===== 消息处理 =====

    async def on_group_message(self, event: dict):
        """处理群消息 - 命令路由"""
        raw = event.get("message", "").strip()
        group_id = str(event.get("group_id", ""))
        user_id = str(event.get("user_id", ""))

        # 限流检查
        if not self._check_rate_limit(group_id, user_id):
            return

        # 命令分发
        if raw.startswith("/b站"):
            await self._handle_command(raw, group_id, user_id, event)
            return

        # 自动检测BV号 + 问答关键词
        if self.config.get("qna", {}).get("auto_detect_bvid", True):
            bvid = self._extract_bvid(raw)
            if bvid:
                keywords = self.config.get("qna", {}).get("auto_detect_keywords", [])
                if any(kw in raw for kw in keywords):
                    await self._handle_qna(bvid, raw, group_id)

    async def on_private_message(self, event: dict):
        """处理私聊消息"""
        raw = event.get("message", "").strip()
        user_id = str(event.get("user_id", ""))
        if raw.startswith("/b站"):
            await self._handle_command(raw, user_id, user_id, event, is_group=False)

    # ===== 命令处理 =====

    async def _handle_command(self, raw: str, group_id: str, user_id: str, event: dict, is_group: bool = True):
        """命令路由"""
        send = lambda msg: self._send_to_group(group_id, msg) if is_group else self._send_to_user(user_id, msg)

        try:
            # /b站来一个
            if raw in ("/b站来一个", "/b站来一个 "):
                await self._cmd_random_one(send)

            # /b站热门
            elif raw.startswith("/b站热门"):
                await self._cmd_hot(send)

            # /b站排行榜 [分区]
            elif raw.startswith("/b站排行榜"):
                region = raw.replace("/b站排行榜", "").strip()
                await self._cmd_ranking(send, region)

            # /b站搜索 <关键词>
            elif raw.startswith("/b站搜索"):
                keyword = raw.replace("/b站搜索", "").strip()
                await self._cmd_search(send, keyword)

            # /b站视频 <BV号>
            elif raw.startswith("/b站视频"):
                bvid = raw.replace("/b站视频", "").strip()
                await self._cmd_video_info(send, bvid)

            # /b站问答 <BV号> <问题>
            elif raw.startswith("/b站问答"):
                parts = raw.replace("/b站问答", "").strip().split(maxsplit=1)
                if len(parts) >= 2:
                    await self._cmd_qna(send, parts[0], parts[1])
                elif len(parts) == 1:
                    await send("格式: /b站问答 <BV号> <问题>")

            # /b站开关
            elif raw.startswith("/b站开关"):
                await self._cmd_toggle(send, group_id)

            # /b站状态
            elif raw.startswith("/b站状态"):
                await self._cmd_status(send, group_id)

            else:
                await send("未知命令，试试 /b站来一个")

        except Exception as e:
            self.fw.logger.error(f"[B站插件] 命令错误: {e}")
            await send(f"出错了: {e}")

    # ----- 具体命令实现 -----

    async def _cmd_random_one(self, send):
        """随机推送一个视频"""
        if self.pool.needs_refresh:
            await send("稍等，正在拉取最新热门...")
            await self.pool.refresh_pool()
        videos = self.pool.pick(1)
        if not videos:
            await self.pool.refresh_pool()
            videos = self.pool.pick(1)
        if not videos:
            await send("拉取失败了，等会儿再试试")
            return
        video = videos[0]
        card = self.card_builder.build_miniapp(video)
        await send(card)
        self.pool.record_push(video.bvid)

    async def _cmd_hot(self, send):
        """获取热门列表 (推1-3个)"""
        await self.pool.refresh_pool()
        videos = self.pool.pick(3)
        if not videos:
            await send("没拉到热门，一会儿再试")
            return
        for v in videos:
            card = self.card_builder.build_miniapp(v)
            await send(card)
            self.pool.record_push(v.bvid)
            await asyncio.sleep(0.5)

    async def _cmd_ranking(self, send, region_name: str = ""):
        """查看排行榜"""
        regions = self.config.get("bilibili_backend", {}).get("regions", {})
        rid = regions.get(region_name, 0) if region_name else 0
        videos = await self.api.get_ranking(rid=rid, ps=10)
        if not videos:
            await send("没拉到排行榜")
            return
        videos = videos[:5]
        for v in videos:
            card = self.card_builder.build_miniapp(v)
            await send(card)
            await asyncio.sleep(0.5)

    async def _cmd_search(self, send, keyword: str):
        """搜索视频"""
        if not keyword:
            await send("格式: /b站搜索 <关键词>")
            return
        await send(f"正在搜索: {keyword} ...")
        videos = await self.api.search(keyword)
        if not videos:
            await send("没搜到")
            return
        for v in videos[:3]:
            card = self.card_builder.build_miniapp(v)
            await send(card)
            await asyncio.sleep(0.5)

    async def _cmd_video_info(self, send, bvid: str):
        """查看视频详情"""
        if not bvid or not bvid.upper().startswith("BV"):
            await send("请提供有效的BV号")
            return
        video = await self.api.get_video_info(bvid)
        if not video:
            await send(f"没找到视频: {bvid}")
            return
        video = await self.fetcher.fetch(video)
        text = self.card_builder.build_fallback_text(video)
        await send(text)

    async def _cmd_qna(self, send, bvid: str, question: str):
        """视频问答"""
        if not bvid.upper().startswith("BV"):
            await send("BV号格式不对")
            return
        await send("正在分析视频内容...")
        answer = await self.qna.answer(bvid, question, self._llm_call)
        await send(answer)

    async def _cmd_toggle(self, send, group_id: str):
        """切换本群推送开关"""
        if self.scheduler:
            current = self.scheduler.is_group_enabled(group_id)
            self.scheduler.set_group_enabled(group_id, not current)
            state = "已开启" if not current else "已关闭"
            await send(f"随机推送 {state}")

    async def _cmd_status(self, send, group_id: str):
        """查看推送状态"""
        enabled = self.scheduler.is_group_enabled(group_id) if self.scheduler else False
        pool_size = self.pool.pool_size if self.pool else 0
        push_enabled = self.config.get("random_push", {}).get("enabled", True)
        msg = (
            f"⚙️ B站推送状态\n"
            f"随机推送: {'✅' if push_enabled and enabled else '❌'}\n"
            f"候选池: {pool_size} 个视频\n"
            f"去重窗口: {self.config.get('random_push', {}).get('pool', {}).get('dedup_window_hours', 24)} 小时"
        )
        await send(msg)

    # ===== 视频问答 =====

    async def _handle_qna(self, bvid: str, question: str, group_id: str):
        """自动检测到的问答"""
        answer = await self.qna.answer(bvid, question, self._llm_call)
        await self._send_to_group(group_id, answer)

    # ===== 工具方法 =====

    async def _llm_call(self, system_prompt: str, user_prompt: str) -> str:
        """调用框架LLM接口"""
        try:
            models_config = self.config.get("models", {})
            model_name = models_config.get("model_name", "replyer")
            temperature = models_config.get("temperature", 0.7)
            timeout = models_config.get("llm_timeout_seconds", 60)
            return await self.fw.llm.chat(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                timeout=timeout,
            )
        except Exception as e:
            self.fw.logger.error(f"[B站插件] LLM调用失败: {e}")
            return "抱歉，AI分析暂时不可用。"

    async def _send_to_group(self, group_id: str, message, fallback: str = None):
        """发送消息到群 (支持卡片)"""
        try:
            if isinstance(message, dict) and message.get("type") == "miniapp":
                # 发送小程序卡片
                await self.fw.send_group_miniapp(group_id, message)
            elif isinstance(message, dict):
                # 发送ARK卡片
                await self.fw.send_group_ark(group_id, message)
            else:
                # 文本消息
                await self.fw.send_group_message(group_id, str(message))
        except Exception as e:
            # 卡片失败，发送文本回退
            if fallback:
                try:
                    await self.fw.send_group_message(group_id, fallback)
                except Exception:
                    pass
            self.fw.logger.warning(f"[B站插件] 发送失败: {e}")

    async def _send_to_user(self, user_id: str, message):
        """发送私聊消息"""
        try:
            await self.fw.send_private_message(user_id, str(message))
        except Exception as e:
            self.fw.logger.warning(f"[B站插件] 私聊发送失败: {e}")

    def _extract_bvid(self, text: str) -> Optional[str]:
        """从文本中提取BV号"""
        match = re.search(r'BV[a-zA-Z0-9]{10}', text)
        return match.group(0) if match else None

    def _check_rate_limit(self, group_id: str, user_id: str) -> bool:
        """检查限流"""
        # 简化实现：用内存字典记录
        now = time.time()
        rl = self.config.get("rate_limit", {})
        per_group = rl.get("per_group_per_minute", 5)
        per_user = rl.get("per_user_per_minute", 3)

        if not hasattr(self, "_rate_cache"):
            self._rate_cache: dict[str, list[float]] = {}

        # 群限流
        key_g = f"g:{group_id}"
        self._rate_cache.setdefault(key_g, [])
        self._rate_cache[key_g] = [t for t in self._rate_cache[key_g] if now - t < 60]
        if len(self._rate_cache[key_g]) >= per_group:
            return False
        self._rate_cache[key_g].append(now)

        # 用户限流
        key_u = f"u:{user_id}"
        self._rate_cache.setdefault(key_u, [])
        self._rate_cache[key_u] = [t for t in self._rate_cache[key_u] if now - t < 60]
        if len(self._rate_cache[key_u]) >= per_user:
            return False
        self._rate_cache[key_u].append(now)

        return True

    # ===== 预缓存 =====

    async def _prefetch_loop(self):
        """定期预缓存视频数据"""
        interval = self.config.get("advanced", {}).get("prefetch_interval_minutes", 30) * 60
        while self._running:
            try:
                await self.pool.refresh_pool()
                # 预拉取池中前几个视频的详情
                for v in self.pool._pool[:10]:
                    if not v.description or not v.subtitle:
                        await self.fetcher.fetch(v)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(interval)


# ============================================================
# v2 插件入口
# ============================================================

def create_plugin():
    """MaiCore v2 插件工厂函数 — 返回Plugin类，框架负责实例化"""
    return Plugin
