"""
B站视频随机推送 & 视频内容问答插件
适用: MaiBot 框架 (MaiCore v2 SDK)

功能:
  1. 随机间隔从B站热门池中抽取视频推送到群聊
  2. 支持关键词搜索B站视频并推送结果
  3. 群友提问视频内容时拉取简介/字幕/评论并回复
  4. 手动命令: /b站来一个 /b站热门 /b站排行榜 /b站搜索 /b站视频 /b站问答 /b站开关 /b站状态

所有配置可通过 WebUI 编辑, 也可直接修改 config.toml
"""

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx

from maibot_sdk import Command, EventHandler, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import EventType, ToolParameterInfo, ToolParamType

logger = logging.getLogger(__name__)


# ============================================================
# 配置模型 — 全量 WebUI 可编辑
# ============================================================

class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "基础"
    __ui_icon__ = "package"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="2.0.0", description="配置版本号")


# ---------- 模型配置 ----------

class ModelsConfig(PluginConfigBase):
    __ui_label__ = "回复模型"
    __ui_icon__ = "cpu"
    __ui_order__ = 10
    model_name: str = Field(default="replyer", description="回复生成模型名")
    temperature: float = Field(default=0.7, description="生成温度")
    context_time_gap: int = Field(default=600, description="上下文时间间隔(秒)")
    context_max_limit: int = Field(default=20, description="上下文最大条数")
    llm_timeout_seconds: int = Field(default=60, description="LLM 超时(秒)")


class SummarizerModelConfig(PluginConfigBase):
    __ui_label__ = "总结模型"
    __ui_icon__ = "cpu"
    __ui_order__ = 11
    model_name: str = Field(default="replyer", description="总结模型名")
    temperature: float = Field(default=0.5, description="生成温度")
    context_time_gap: int = Field(default=300, description="上下文时间间隔(秒)")
    context_max_limit: int = Field(default=5, description="上下文最大条数")
    llm_timeout_seconds: int = Field(default=90, description="LLM 超时(秒)")


class ModelsSectionConfig(PluginConfigBase):
    __ui_label__ = "模型"
    __ui_icon__ = "cpu"
    __ui_order__ = 10
    model_name: str = Field(default="replyer", description="回复生成模型名")
    temperature: float = Field(default=0.7, description="生成温度")
    context_time_gap: int = Field(default=600, description="上下文时间间隔(秒)")
    context_max_limit: int = Field(default=20, description="上下文最大条数")
    llm_timeout_seconds: int = Field(default=60, description="LLM 超时(秒)")
    summarizer: SummarizerModelConfig = Field(default_factory=SummarizerModelConfig)


# ---------- 随机推送 ----------

class PoolConfig(PluginConfigBase):
    __ui_label__ = "候选池"
    __ui_icon__ = "database"
    __ui_order__ = 1
    pool_size: int = Field(default=50, description="候选池大小")
    pick_algorithm: str = Field(default="roulette", description="选取算法: roulette/shuffle/weighted")
    dedup_window_hours: int = Field(default=24, description="去重窗口(小时)")
    use_popular: bool = Field(default=True, description="拉取综合热门")
    use_ranking: bool = Field(default=True, description="拉取全站排行榜")
    use_weekly: bool = Field(default=True, description="拉取每周必看")
    popular_weight: int = Field(default=50, description="热门权重")
    ranking_weight: int = Field(default=30, description="排行榜权重")
    weekly_weight: int = Field(default=20, description="每周必看权重")
    ranking_rid: int = Field(default=0, description="排行榜分区ID (0=全站)")


class RandomPushConfig(PluginConfigBase):
    __ui_label__ = "随机推送"
    __ui_icon__ = "send"
    __ui_order__ = 20
    enabled: bool = Field(default=True, description="启用随机推送")
    min_interval_minutes: int = Field(default=60, description="最短间隔(分钟)")
    max_interval_minutes: int = Field(default=240, description="最长间隔(分钟)")
    min_count: int = Field(default=1, description="每次最少推送数")
    max_count: int = Field(default=3, description="每次最多推送数")
    active_hours: list = Field(default_factory=lambda: ["08:00-23:00"], description="活跃时段(HH:MM-HH:MM)")
    quiet_hours: list = Field(default_factory=lambda: ["23:00-08:00"], description="静默时段(HH:MM-HH:MM)")
    pool: PoolConfig = Field(default_factory=PoolConfig)


# ---------- B站 API ----------

class BilibiliBackendConfig(PluginConfigBase):
    __ui_label__ = "B站 API"
    __ui_icon__ = "server"
    __ui_order__ = 30
    api_base: str = Field(default="https://api.bilibili.com", description="API 基础URL")
    timeout: int = Field(default=15, description="请求超时(秒)")
    max_retries: int = Field(default=3, description="最大重试次数")
    request_interval_ms: int = Field(default=500, description="请求间隔(毫秒)")
    popular_api: str = Field(default="/x/web-interface/popular", description="热门API")
    ranking_api: str = Field(default="/x/web-interface/ranking/v2", description="排行榜API")
    video_info_api: str = Field(default="/x/web-interface/view", description="视频详情API")
    subtitle_api: str = Field(default="/x/player/v2", description="字幕API")
    comment_api: str = Field(default="/x/v2/reply/main", description="评论API")
    search_api: str = Field(default="/x/web-interface/search/type", description="搜索API")
    weekly_api: str = Field(default="/x/web-interface/popular/series/one", description="每周必看API")
    user_videos_api: str = Field(default="/x/space/wbi/arc/search", description="UP主视频API")
    video_page: str = Field(default="https://www.bilibili.com/video/", description="视频页面URL")
    regions: dict = Field(
        default_factory=lambda: {
            "all": 0, "动画": 1, "音乐": 3, "游戏": 4, "娱乐": 5,
            "科技": 36, "鬼畜": 119, "舞蹈": 129, "影视": 181,
            "时尚": 155, "生活": 160, "美食": 211, "体育": 234, "汽车": 223, "动物": 217,
        },
        description="排行榜分区映射",
    )


# ---------- 网络 ----------

class NetworkConfig(PluginConfigBase):
    __ui_label__ = "网络"
    __ui_icon__ = "wifi"
    __ui_order__ = 31
    proxy: str = Field(default="", description="HTTP代理(留空=不使用)")
    cookie: str = Field(default="", description="B站Cookie(可填SESSDATA等)")
    use_mobile_api: bool = Field(default=False, description="使用移动端API")
    user_agents: list = Field(
        default_factory=lambda: [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        ],
        description="User-Agent 列表",
    )


# ---------- QQ 卡片 ----------

class MiniappConfig(PluginConfigBase):
    __ui_label__ = "小程序卡片"
    __ui_icon__ = "smartphone"
    appid: str = Field(default="1108338344", description="QQ小程序AppID")
    app_name: str = Field(default="哔哩哔哩", description="小程序名称")
    app_icon: str = Field(default="", description="小程序图标URL")
    scheme_template: str = Field(
        default="mqqapi://microapp/open?appid=1108338344&url=bilibili://video/{bvid}&version=1.0",
        description="跳转Scheme ({bvid}=BV号)",
    )
    web_url_template: str = Field(default="https://b23.tv/{bvid}", description="网页版URL ({bvid}=BV号)")


class CardContentConfig(PluginConfigBase):
    __ui_label__ = "卡片内容"
    __ui_icon__ = "edit"
    title_template: str = Field(default="{title}", description="标题模板")
    desc_template: str = Field(default="UP主: {author} | {play}播放 | {danmaku}弹幕", description="描述模板")
    source_text: str = Field(default="哔哩哔哩", description="来源标识")
    preview_template: str = Field(default="{cover}@480w_300h.jpg", description="预览图模板")


class ArkConfig(PluginConfigBase):
    __ui_label__ = "ARK卡片"
    __ui_icon__ = "layers"
    enabled: bool = Field(default=True, description="启用ARK卡片")
    ark_template: str = Field(
        default='{"app":"com.tencent.structmsg","meta":{"news":{"title":"{title}","desc":"{author}","jumpUrl":"https://b23.tv/{bvid}"}}}',
        description="ARK JSON模板",
    )


class FallbackConfig(PluginConfigBase):
    __ui_label__ = "文本回退"
    __ui_icon__ = "file-text"
    enabled: bool = Field(default=True, description="卡片失败时回退文本")
    fallback_template: str = Field(
        default="🎬 {title}\n👤 {author}\n▶️ {play}播放 | 💬 {danmaku}弹幕 | 👍 {like}点赞\n🔗 https://b23.tv/{bvid}",
        description="纯文本模板",
    )


class QQCardConfig(PluginConfigBase):
    __ui_label__ = "QQ 卡片"
    __ui_icon__ = "credit-card"
    __ui_order__ = 40
    enabled: bool = Field(default=True, description="启用卡片")
    card_type: str = Field(default="miniapp", description="卡片类型: miniapp/ark/share")
    miniapp: MiniappConfig = Field(default_factory=MiniappConfig)
    content: CardContentConfig = Field(default_factory=CardContentConfig)
    ark: ArkConfig = Field(default_factory=ArkConfig)
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)


# ---------- 内容抓取 ----------

class ContentFetchConfig(PluginConfigBase):
    __ui_label__ = "内容抓取"
    __ui_icon__ = "download"
    __ui_order__ = 50
    enabled: bool = Field(default=True, description="启用内容抓取")
    fetch_description: bool = Field(default=True, description="抓取视频简介")
    fetch_subtitle: bool = Field(default=True, description="抓取字幕")
    fetch_comments: bool = Field(default=True, description="抓取评论")
    max_comments: int = Field(default=20, description="最多抓取评论数")
    max_content_length: int = Field(default=5000, description="内容最大长度")
    content_timeout: int = Field(default=15, description="抓取超时(秒)")


# ---------- 问答 ----------

class QnAConfig(PluginConfigBase):
    __ui_label__ = "问答"
    __ui_icon__ = "help-circle"
    __ui_order__ = 60
    enabled: bool = Field(default=True, description="启用问答")
    auto_detect_bvid: bool = Field(default=True, description="自动检测BV号")
    auto_detect_keywords: list = Field(
        default_factory=lambda: ["这个视频", "讲了什么", "好看吗", "值得看吗", "总结一下", "概括", "评价", "内容", "介绍", "值得吗", "推荐吗", "怎么样", "什么内容"],
        description="自动触发关键词",
    )
    reply_template: str = Field(
        default="📺 关于《{title}》\n\n{summary}\n\n📊 ▶️ {play} | 👍 {like} | ⭐ {favorite} | 💬 {reply}\n🔗 https://b23.tv/{bvid}",
        description="问答回复模板",
    )


# ---------- 群组 ----------

class GroupsConfig(PluginConfigBase):
    __ui_label__ = "群组"
    __ui_icon__ = "users"
    __ui_order__ = 70
    bindings: list = Field(default_factory=list, description="绑定的stream_id")
    respond_all: bool = Field(default=True, description="响应所有stream")
    blacklist: list = Field(default_factory=list, description="黑名单stream_id")
    push_targets: list = Field(default_factory=list, description="推送目标stream_id")
    per_group_toggle: bool = Field(default=True, description="允许按群独立开关")


# ---------- 限流 ----------

class RateLimitConfig(PluginConfigBase):
    __ui_label__ = "限流"
    __ui_icon__ = "shield"
    __ui_order__ = 80
    per_group_per_minute: int = Field(default=5, description="每群每分钟上限")
    per_user_per_minute: int = Field(default=3, description="每用户每分钟上限")
    api_per_minute: int = Field(default=30, description="API每分钟上限")
    cache_ttl: int = Field(default=300, description="缓存TTL(秒)")


# ---------- 过滤器 ----------

class FiltersConfig(PluginConfigBase):
    __ui_label__ = "过滤器"
    __ui_icon__ = "filter"
    __ui_order__ = 90
    title_blacklist: list = Field(default_factory=list, description="标题黑名单关键词")
    min_play_count: int = Field(default=5000, description="最低播放量")
    region_whitelist: list = Field(default_factory=list, description="分区白名单(空=全部)")
    min_like_ratio: float = Field(default=0.005, description="最低点赞率(点赞/播放)")
    max_duration_seconds: int = Field(default=1800, description="最长视频时长(秒)")


# ---------- 提示词 ----------

class PromptsConfig(PluginConfigBase):
    __ui_label__ = "提示词"
    __ui_icon__ = "message-square"
    __ui_order__ = 100
    summarize_system: str = Field(
        default="你是一个B站视频内容总结助手。根据提供的视频信息（标题、简介、字幕、热门评论），生成一段简洁准确的视频内容概括。概括应包含：1.视频主题 2.核心内容/亮点 3.适合什么人群观看。控制在200字以内，语气轻松自然。",
        description="总结系统提示词",
    )
    qna_system: str = Field(
        default="你是一个B站视频内容问答助手。根据提供的视频信息回答用户关于视频的问题。如果信息不足以回答，诚实告知。回答简洁准确，不要编造没有的信息。",
        description="问答系统提示词",
    )
    search_system: str = Field(
        default="你是一个B站视频搜索助手。帮助用户找到他们感兴趣的视频。",
        description="搜索系统提示词",
    )
    summarize_user: str = Field(
        default="请概括以下B站视频的内容：\n\n标题: {title}\nUP主: {author}\n简介: {description}\n字幕片段: {subtitle}\n热门评论: {comments}\n\n请生成视频内容概括：",
        description="总结用户提示词模板",
    )
    qna_user: str = Field(
        default="关于以下B站视频，用户提出了问题：\n\n视频标题: {title}\nUP主: {author}\n简介: {description}\n字幕片段: {subtitle}\n热门评论: {comments}\n\n用户问题: {question}\n\n请根据以上信息回答用户的问题：",
        description="问答用户提示词模板",
    )


# ---------- 触发命令 ----------

class TriggersConfig(PluginConfigBase):
    __ui_label__ = "触发命令"
    __ui_icon__ = "terminal"
    __ui_order__ = 15
    commands: list = Field(default_factory=list, description="已注册的命令列表(仅供展示)")


# ---------- 高级 ----------

class AdvancedConfig(PluginConfigBase):
    __ui_label__ = "高级"
    __ui_icon__ = "settings"
    __ui_order__ = 200
    send_cover_image: bool = Field(default=True, description="发送封面图")
    cover_max_width: int = Field(default=480, description="封面最大宽度")
    cover_max_height: int = Field(default=360, description="封面最大高度")
    fallback_html_parse: bool = Field(default=True, description="回退HTML解析")
    log_level: str = Field(default="info", description="日志级别")
    stats_enabled: bool = Field(default=True, description="启用统计")
    prefetch_enabled: bool = Field(default=True, description="启用预缓存")
    prefetch_interval_minutes: int = Field(default=30, description="预缓存刷新间隔(分钟)")


# ---------- 顶级配置 ----------

class BilibiliTrendingConfig(PluginConfigBase):
    """B站视频随机推送 & 问答插件 — 全量配置"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    triggers: TriggersConfig = Field(default_factory=TriggersConfig)
    models: ModelsSectionConfig = Field(default_factory=ModelsSectionConfig)
    random_push: RandomPushConfig = Field(default_factory=RandomPushConfig)
    bilibili_backend: BilibiliBackendConfig = Field(default_factory=BilibiliBackendConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    qq_card: QQCardConfig = Field(default_factory=QQCardConfig)
    content_fetch: ContentFetchConfig = Field(default_factory=ContentFetchConfig)
    qna: QnAConfig = Field(default_factory=QnAConfig)
    groups: GroupsConfig = Field(default_factory=GroupsConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    advanced: AdvancedConfig = Field(default_factory=AdvancedConfig)


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
    stream_id: str = ""


# ============================================================
# 工具函数
# ============================================================

def _format_count(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _truncate(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    return s[:max_chars - 1] + "…"


# ============================================================
# B站 API 客户端
# ============================================================

class BilibiliAPI:
    """封装B站各类API请求"""

    def __init__(self, config: BilibiliTrendingConfig):
        backend = config.bilibili_backend
        network = config.network
        self.api_base = backend.api_base
        self.timeout = backend.timeout
        self.max_retries = backend.max_retries
        self.request_interval = backend.request_interval_ms / 1000.0
        self._backend = backend
        self._network = network
        self._last_request = 0.0
        self._client: Optional[httpx.AsyncClient] = None
        self._user_agents = network.user_agents or [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ]

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            ua = random.choice(self._user_agents)
            headers = {
                "User-Agent": ua,
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            cookie = self._network.cookie
            if cookie:
                headers["Cookie"] = cookie
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            )
        return self._client

    async def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < self.request_interval:
            await asyncio.sleep(self.request_interval - elapsed)
        self._last_request = time.time()

    async def _request(self, url: str, params: dict = None) -> dict | None:
        await self._rate_limit()
        client = await self._get_client()
        for attempt in range(self.max_retries):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 412:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
            except Exception:
                if attempt == self.max_retries - 1:
                    raise
                await asyncio.sleep(1 * (attempt + 1))
        return None

    # ----- 热门 & 排行榜 -----

    async def get_popular(self, pn: int = 1, ps: int = 50) -> list[VideoInfo]:
        url = f"{self.api_base}{self._backend.popular_api}"
        data = await self._request(url, {"pn": pn, "ps": ps})
        return self._parse_video_list(data)

    async def get_ranking(self, rid: int = 0, ps: int = 50) -> list[VideoInfo]:
        url = f"{self.api_base}{self._backend.ranking_api}"
        data = await self._request(url, {"rid": rid, "type": "all" if rid == 0 else "region"})
        return self._parse_video_list(data) if data else []

    async def get_weekly(self, number: int = 1) -> list[VideoInfo]:
        url = f"{self.api_base}{self._backend.weekly_api}"
        data = await self._request(url, {"number": number})
        return self._parse_video_list(data)

    # ----- 视频详情 -----

    async def get_video_info(self, bvid: str) -> Optional[VideoInfo]:
        url = f"{self.api_base}{self._backend.video_info_api}"
        data = await self._request(url, {"bvid": bvid})
        if data and data.get("code") == 0:
            v = data["data"]
            tags = [t["tag_name"] for t in v["tags"]] if v.get("tags") else []
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
                tags=tags,
                region=v.get("tname", ""),
            )
        return None

    # ----- 字幕 -----

    async def get_subtitle(self, bvid: str, aid: int = 0, cid: int = 0) -> str:
        url = f"{self.api_base}{self._backend.subtitle_api}"
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

    @staticmethod
    def _parse_subtitle(data: dict) -> str:
        body = data.get("body", [])
        lines = [item.get("content", "") for item in body]
        return "\n".join(lines[:200])

    # ----- 评论 -----

    async def get_comments(self, oid: int, pn: int = 1, ps: int = 20) -> list[str]:
        url = f"{self.api_base}{self._backend.comment_api}"
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
        url = f"{self.api_base}{self._backend.search_api}"
        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "order": "totalrank",
        }
        data = await self._request(url, params)
        return self._parse_video_list(data)

    # ----- 解析 -----

    def _parse_video_list(self, data: dict) -> list[VideoInfo]:
        if not data or data.get("code") != 0:
            return []
        videos = []
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
    """管理候选视频池"""

    def __init__(self, config: BilibiliTrendingConfig, api: BilibiliAPI):
        push_cfg = config.random_push
        pool_cfg = push_cfg.pool
        self._filters = config.filters
        self._pool_cfg = pool_cfg
        self._push_cfg = push_cfg
        self.api = api
        self._dedup_window = timedelta(hours=pool_cfg.dedup_window_hours)
        self._push_history: list[PushRecord] = []
        self._pool: list[VideoInfo] = []
        self._pool_updated_at: float = 0.0
        self._pool_ttl: float = 1800.0

    def _is_expired(self, record: PushRecord) -> bool:
        return (datetime.now() - datetime.fromtimestamp(record.pushed_at)) > self._dedup_window

    def _clean_history(self):
        self._push_history = [r for r in self._push_history if not self._is_expired(r)]

    def is_duplicate(self, bvid: str) -> bool:
        self._clean_history()
        return any(r.bvid == bvid for r in self._push_history)

    def record_push(self, bvid: str, stream_id: str = ""):
        self._push_history.append(PushRecord(bvid=bvid, pushed_at=time.time(), stream_id=stream_id))
        self._clean_history()

    def _build_sources(self) -> list[dict]:
        """从配置构建数据源列表"""
        sources = []
        pc = self._pool_cfg
        if pc.use_popular:
            sources.append({"type": "popular", "weight": pc.popular_weight})
        if pc.use_ranking:
            sources.append({"type": "ranking", "rid": pc.ranking_rid, "weight": pc.ranking_weight})
        if pc.use_weekly:
            sources.append({"type": "weekly", "weight": pc.weekly_weight})
        return sources

    async def refresh_pool(self):
        sources = self._build_sources()
        pool_size = self._pool_cfg.pool_size
        all_videos: list[VideoInfo] = []

        for src in sources:
            src_type = src.get("type", "")
            try:
                if src_type == "popular":
                    videos = await self.api.get_popular(ps=pool_size)
                elif src_type == "ranking":
                    videos = await self.api.get_ranking(rid=src.get("rid", 0), ps=pool_size)
                elif src_type == "weekly":
                    videos = await self.api.get_weekly()
                else:
                    continue
                for v in videos:
                    if self._pass_filter(v):
                        all_videos.append(v)
            except Exception:
                continue

        seen: set[str] = set()
        unique: list[VideoInfo] = []
        for v in all_videos:
            if v.bvid not in seen and not self.is_duplicate(v.bvid):
                seen.add(v.bvid)
                unique.append(v)

        self._pool = unique
        self._pool_updated_at = time.time()

    def _pass_filter(self, v: VideoInfo) -> bool:
        f = self._filters
        if v.play < f.min_play_count:
            return False
        if v.duration > f.max_duration_seconds:
            return False
        if v.play > 0 and v.like / v.play < f.min_like_ratio:
            return False
        for banned in f.title_blacklist:
            if banned.lower() in v.title.lower():
                return False
        if f.region_whitelist and v.region not in f.region_whitelist:
            return False
        return True

    def pick(self, count: int = 1) -> list[VideoInfo]:
        if not self._pool:
            return []
        algorithm = self._pool_cfg.pick_algorithm
        available = [v for v in self._pool if not self.is_duplicate(v.bvid)]
        if not available:
            return []
        if algorithm == "shuffle":
            return random.sample(available, min(count, len(available)))
        elif algorithm == "roulette":
            return self._roulette_pick(available, count)
        else:
            return random.sample(available, min(count, len(available)))

    def _roulette_pick(self, videos: list[VideoInfo], count: int) -> list[VideoInfo]:
        total_play = sum(max(v.play, 1) for v in videos)
        if total_play == 0:
            return random.sample(videos, min(count, len(videos)))
        picked: list[VideoInfo] = []
        remaining = list(videos)
        for _ in range(min(count, len(videos))):
            r = random.uniform(0, total_play)
            cumulative = 0
            chosen_idx = 0
            for i, v in enumerate(remaining):
                cumulative += max(v.play, 1)
                if cumulative >= r:
                    chosen_idx = i
                    break
            picked.append(remaining[chosen_idx])
            total_play -= max(remaining[chosen_idx].play, 1)
            remaining.pop(chosen_idx)
        return picked

    @property
    def size(self) -> int:
        return len(self._pool)

    @property
    def needs_refresh(self) -> bool:
        return (time.time() - self._pool_updated_at) > self._pool_ttl or not self._pool


# ============================================================
# 卡片构建器
# ============================================================

def _xml_escape(s: str) -> str:
    """转义 XML 特殊字符"""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")


class CardBuilder:
    def __init__(self, config: BilibiliTrendingConfig):
        self._card_cfg = config.qq_card

    def build_xml_card(self, video: VideoInfo) -> dict:
        """构建 QQ XML 卡片 — 主方案, 兼容性最好"""
        content = self._card_cfg.content
        mc = self._card_cfg.miniapp

        b23_url = f"https://b23.tv/{video.bvid}"
        cover_url = content.preview_template.format(cover=video.cover)
        title = _xml_escape(_truncate(video.title, 50))
        summary = _xml_escape(_truncate(
            content.desc_template.format(
                title=video.title, author=video.author,
                play=_format_count(video.play), danmaku=_format_count(video.danmaku),
            ), 60
        ))
        source_name = _xml_escape(content.source_text or "哔哩哔哩")
        source_icon = mc.app_icon or "https://www.bilibili.com/favicon.ico"

        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
            '<msg serviceID="1" templateID="1" action="web"'
            f' brief="{source_name}" sourceMsgId="0"'
            f' url="{_xml_escape(b23_url)}" flag="0" adverSign="0" multiMsgFlag="0">'
            '<item layout="2">'
            f'<picture cover="{_xml_escape(cover_url)}" w="0" h="0" />'
            f"<title>{title}</title>"
            f"<summary>{summary}</summary>"
            "</item>"
            f'<source name="{source_name}" icon="{_xml_escape(source_icon)}" action="" appid="0" />'
            "</msg>"
        )
        return {"data": xml}

    def build_miniapp_json(self, video: VideoInfo) -> dict:
        """构建 QQ 小程序 ARK JSON — 备选方案"""
        import json as _json
        mc = self._card_cfg.miniapp
        content = self._card_cfg.content

        scheme = mc.scheme_template.replace("{bvid}", video.bvid)
        web_url = mc.web_url_template.replace("{bvid}", video.bvid)
        title = _truncate(content.title_template.format(
            title=video.title, author=video.author,
            play=_format_count(video.play), danmaku=_format_count(video.danmaku),
        ), 24)
        desc = _truncate(content.desc_template.format(
            title=video.title, author=video.author,
            play=_format_count(video.play), danmaku=_format_count(video.danmaku),
        ), 40)
        preview = content.preview_template.format(cover=video.cover)

        share_data = {"type": "web", "url": web_url, "title": _truncate(video.title, 40)}
        ark = {
            "app": "com.tencent.miniapp_01",
            "config": {"ctime": int(time.time()), "token": video.bvid},
            "meta": {
                "detail_1": {
                    "appid": mc.appid,
                    "title": title,
                    "desc": desc,
                    "preview": preview,
                    "url": scheme,
                    "host": {"nick": mc.app_name, "icon": mc.app_icon} if mc.app_icon else {},
                    "shareData": share_data,
                }
            },
            "prompt": f"[{content.source_text}] {video.title}",
            "ver": "1.0.0",
            "view": "detail_1",
        }
        return {"data": _json.dumps(ark, ensure_ascii=False)}

    def build_text(self, video: VideoInfo) -> str:
        tpl = self._card_cfg.fallback.fallback_template
        if not tpl:
            tpl = "🎬 {title}\n👤 {author}\n▶️ {play}播放 | 💬 {danmaku}弹幕 | 👍 {like}点赞\n🔗 https://b23.tv/{bvid}"
        return tpl.format(
            title=video.title,
            author=video.author,
            play=_format_count(video.play),
            danmaku=_format_count(video.danmaku),
            like=_format_count(video.like),
            bvid=video.bvid,
        )

    def build_detail_text(self, video: VideoInfo) -> str:
        pubdate_str = ""
        if video.pubdate:
            pubdate_str = f"📅 发布: {datetime.fromtimestamp(video.pubdate).strftime('%Y-%m-%d')}\n"
        tags_str = f"🏷️ {' | '.join(video.tags[:8])}\n" if video.tags else ""
        region_str = f"📺 分区: {video.region}\n" if video.region else ""
        desc_str = f"\n📝 简介:\n{_truncate(video.description, 500)}\n" if video.description else ""

        return (
            f"🎬 {video.title}\n"
            f"👤 {video.author}\n"
            f"{region_str}{tags_str}{pubdate_str}"
            f"▶️ {_format_count(video.play)}播放 | 💬 {_format_count(video.danmaku)}弹幕\n"
            f"👍 {_format_count(video.like)}点赞 | ⭐ {_format_count(video.favorite)}收藏 | 💭 {_format_count(video.reply)}评论\n"
            f"⏱️ {video.duration // 60}分{video.duration % 60}秒\n"
            f"{desc_str}"
            f"🔗 https://b23.tv/{video.bvid}"
        )


# ============================================================
# 内容抓取器
# ============================================================

class ContentFetcher:
    def __init__(self, config: BilibiliTrendingConfig):
        self._cfg = config.content_fetch

    async def fetch(self, api: BilibiliAPI, video: VideoInfo) -> VideoInfo:
        if self._cfg.fetch_description and not video.description:
            info = await api.get_video_info(video.bvid)
            if info:
                video.description = info.description
        if self._cfg.fetch_subtitle:
            video.subtitle = await api.get_subtitle(video.bvid, video.aid)
        if self._cfg.fetch_comments:
            video.comments = await api.get_comments(video.aid, ps=self._cfg.max_comments)
        return video


# ============================================================
# 推送调度器
# ============================================================

class PushScheduler:
    def __init__(self, config: BilibiliTrendingConfig, pool: VideoPool, api: BilibiliAPI):
        self._cfg = config
        self.pool = pool
        self.api = api
        self._push_cfg = config.random_push
        self._tasks: dict[str, asyncio.Task] = {}
        self._stream_toggle: dict[str, bool] = {}
        self._send_callback = None

    def set_send_callback(self, callback):
        self._send_callback = callback

    def is_stream_enabled(self, stream_id: str) -> bool:
        return self._stream_toggle.get(stream_id, True)

    def toggle_stream(self, stream_id: str) -> bool:
        current = self._stream_toggle.get(stream_id, True)
        self._stream_toggle[stream_id] = not current
        return not current

    async def start(self, stream_ids: list[str]):
        for sid in stream_ids:
            if sid and sid not in self._tasks:
                self._tasks[sid] = asyncio.create_task(self._push_loop(sid))

    async def stop(self):
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def _push_loop(self, stream_id: str):
        while True:
            try:
                if not self._push_cfg.enabled or not self.is_stream_enabled(stream_id):
                    await asyncio.sleep(60)
                    continue
                if not self._in_active_hours():
                    await asyncio.sleep(300)
                    continue
                interval = random.randint(
                    self._push_cfg.min_interval_minutes * 60,
                    self._push_cfg.max_interval_minutes * 60,
                )
                await asyncio.sleep(interval)
                if not self.is_stream_enabled(stream_id):
                    continue
                await self._do_push(stream_id)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(60)

    async def _do_push(self, stream_id: str):
        count = random.randint(self._push_cfg.min_count, self._push_cfg.max_count)
        if self.pool.needs_refresh:
            await self.pool.refresh_pool()
        videos = self.pool.pick(count)
        if not videos:
            await self.pool.refresh_pool()
            videos = self.pool.pick(count)
        for video in videos:
            try:
                builder = CardBuilder(self._cfg)
                card = builder.build_xml_card(video)
                text_fb = builder.build_text(video)
                if self._send_callback:
                    await self._send_callback(stream_id, card, text_fb)
                self.pool.record_push(video.bvid, stream_id)
            except Exception:
                continue

    def _in_active_hours(self) -> bool:
        active = self._push_cfg.active_hours
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

class BilibiliTrendingPlugin(MaiBotPlugin):
    """B站视频随机推送 & 问答插件"""

    config_model = BilibiliTrendingConfig

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        self._api = BilibiliAPI(self.config)
        self._pool = VideoPool(self.config, self._api)
        self._fetcher = ContentFetcher(self.config)
        self._scheduler = PushScheduler(self.config, self._pool, self._api)
        self._scheduler.set_send_callback(self._send_to_stream)
        self._running = True
        self._rate_cache: dict[str, list[float]] = {}

        # 按配置的 push_targets + bindings 启动定时推送
        targets = list(set(
            list(self.config.groups.push_targets) + list(self.config.groups.bindings)
        ))
        if targets:
            await self._scheduler.start(targets)
            logger.info(f"[B站插件] 已启动定时推送, 目标: {targets}")
        else:
            logger.info("[B站插件] 未配置推送目标, 仅手动命令可用")

        logger.info("[B站插件] 已加载")

    async def on_unload(self) -> None:
        self._running = False
        if self._scheduler:
            await self._scheduler.stop()
        if self._api:
            await self._api.close()
        logger.info("[B站插件] 已卸载")

    # ===== 消息发送 =====

    async def _send_to_stream(self, stream_id: str, message, fallback: str = None):
        """发送消息: 优先 XML 卡片, 失败回退到文本"""
        try:
            if isinstance(message, dict) and "data" in message:
                # XML 或 JSON 卡片 → 走 send.custom
                card_type = "xml" if message.get("data", "").startswith("<?xml") else "json"
                send_custom = getattr(self.ctx.send, "custom", None)
                if send_custom:
                    await send_custom(message_type=card_type, content=message, stream_id=stream_id)
                elif fallback:
                    await self.ctx.send.text(fallback, stream_id)
            elif isinstance(message, dict):
                await self.ctx.send.forward([message], stream_id)
            else:
                await self.ctx.send.text(str(message), stream_id)
        except Exception as e:
            logger.warning(f"[B站插件] 发送卡片失败: {e}")
            if fallback:
                try:
                    await self.ctx.send.text(fallback, stream_id)
                except Exception:
                    pass

    async def _send_card(self, stream_id: str, video: VideoInfo):
        """发送 B站视频卡片: XML 优先 → JSON miniapp → 文本回退"""
        builder = CardBuilder(self.config)
        try:
            xml_card = builder.build_xml_card(video)
            await self._send_to_stream(stream_id, xml_card, builder.build_text(video))
        except Exception:
            # XML 失败就回退到文本
            await self._send_text(stream_id, builder.build_text(video))

    async def _send_text(self, stream_id: str, text: str):
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as e:
            logger.warning(f"[B站插件] 发送文本失败: {e}")

    # ===== 限流 =====

    def _check_rate_limit(self, stream_id: str, user_id: str) -> bool:
        now = time.time()
        rl = self.config.rate_limit
        per_stream = rl.per_group_per_minute
        per_user = rl.per_user_per_minute

        key_s = f"s:{stream_id}"
        self._rate_cache.setdefault(key_s, [])
        self._rate_cache[key_s] = [t for t in self._rate_cache[key_s] if now - t < 60]
        if len(self._rate_cache[key_s]) >= per_stream:
            return False
        self._rate_cache[key_s].append(now)

        if user_id:
            key_u = f"u:{user_id}"
            self._rate_cache.setdefault(key_u, [])
            self._rate_cache[key_u] = [t for t in self._rate_cache[key_u] if now - t < 60]
            if len(self._rate_cache[key_u]) >= per_user:
                return False
            self._rate_cache[key_u].append(now)
        return True

    async def _ensure_pool_fresh(self, stream_id: str, notify: bool = False):
        if self._pool.needs_refresh:
            if notify:
                await self._send_text(stream_id, "⏳ 正在拉取最新热门视频...")
            await self._pool.refresh_pool()

    # ================================================================
    # 命令
    # ================================================================

    @Command("bilibili_random", description="随机推送一个B站视频", pattern=r"^/b站来一个$")
    async def cmd_random_one(self, stream_id: str = "", **kwargs):
        del kwargs
        await self._ensure_pool_fresh(stream_id, notify=True)
        videos = self._pool.pick(1)
        if not videos:
            await self._pool.refresh_pool()
            videos = self._pool.pick(1)
        if not videos:
            await self._send_text(stream_id, "😢 拉取失败了，等会儿再试试")
            return True, "拉取失败", True
        await self._send_card(stream_id, videos[0])
        self._pool.record_push(videos[0].bvid, stream_id)
        return True, f"推送了 {videos[0].title}", True

    @Command("bilibili_hot", description="获取B站当前热门", pattern=r"^/b站热门$")
    async def cmd_hot(self, stream_id: str = "", **kwargs):
        del kwargs
        await self._ensure_pool_fresh(stream_id, notify=True)
        videos = self._pool.pick(3)
        if not videos:
            await self._send_text(stream_id, "😢 没拉到热门，等会儿再试")
            return True, "拉取失败", True
        for v in videos:
            await self._send_card(stream_id, v)
            self._pool.record_push(v.bvid, stream_id)
            await asyncio.sleep(0.5)
        return True, f"推送了 {len(videos)} 个热门视频", True

    @Command("bilibili_ranking", description="查看B站排行榜", pattern=r"^/b站排行榜(?:\s+(?P<region>\S+))?$")
    async def cmd_ranking(self, stream_id: str = "", **kwargs):
        matched = kwargs.get("matched_groups", {}) or {}
        region_name = str(matched.get("region", "")).strip() if isinstance(matched, dict) else ""
        rid = self.config.bilibili_backend.regions.get(region_name, 0) if region_name else 0

        await self._send_text(stream_id, f"⏳ 正在获取{'「' + region_name + '」' if region_name else '全站'}排行榜...")
        videos = await self._api.get_ranking(rid=rid, ps=10)
        if not videos:
            await self._send_text(stream_id, "😢 没拉到排行榜数据")
            return True, "拉取失败", True
        for v in videos[:5]:
            await self._send_card(stream_id, v)
            await asyncio.sleep(0.5)
        return True, f"推送了 {min(len(videos), 5)} 个排行视频", True

    @Command("bilibili_search", description="搜索B站视频", pattern=r"^/b站搜索\s+(?P<keyword>.+)$")
    async def cmd_search(self, stream_id: str = "", **kwargs):
        matched = kwargs.get("matched_groups", {}) or {}
        keyword = str(matched.get("keyword", "")).strip() if isinstance(matched, dict) else ""
        if not keyword:
            await self._send_text(stream_id, "格式: /b站搜索 <关键词>")
            return True, "缺少关键词", True
        await self._send_text(stream_id, f"🔍 正在搜索: {keyword} ...")
        videos = await self._api.search(keyword)
        if not videos:
            await self._send_text(stream_id, f"😢 没搜到「{keyword}」相关视频")
            return True, "无结果", True
        for v in videos[:3]:
            await self._send_card(stream_id, v)
            await asyncio.sleep(0.5)
        return True, f"搜索 {keyword} 返回 {min(len(videos), 3)} 个结果", True

    @Command("bilibili_video", description="查询B站视频详情", pattern=r"^/b站视频\s+(?P<bvid>BV[a-zA-Z0-9]{10})$")
    async def cmd_video_info(self, stream_id: str = "", **kwargs):
        matched = kwargs.get("matched_groups", {}) or {}
        bvid = str(matched.get("bvid", "")).strip() if isinstance(matched, dict) else ""
        if not bvid:
            await self._send_text(stream_id, "请提供有效的BV号，如: /b站视频 BV1xx411c7mD")
            return True, "缺少BV号", True
        await self._send_text(stream_id, f"⏳ 正在获取 {bvid} 的视频信息...")
        video = await self._api.get_video_info(bvid)
        if not video:
            await self._send_text(stream_id, f"没找到视频: {bvid}")
            return True, "视频未找到", True
        video = await self._fetcher.fetch(self._api, video)
        detail = CardBuilder(self.config).build_detail_text(video)
        await self._send_text(stream_id, detail)
        return True, f"查询了 {video.title}", True

    @Command("bilibili_qna", description="针对B站视频提问", pattern=r"^/b站问答\s+(?P<bvid>BV[a-zA-Z0-9]{10})\s+(?P<question>.+)$")
    async def cmd_qna(self, stream_id: str = "", **kwargs):
        matched = kwargs.get("matched_groups", {}) or {}
        if not isinstance(matched, dict):
            await self._send_text(stream_id, "格式: /b站问答 <BV号> <问题>")
            return True, "格式错误", True
        bvid = str(matched.get("bvid", "")).strip()
        question = str(matched.get("question", "")).strip()
        if not bvid or not bvid.upper().startswith("BV"):
            await self._send_text(stream_id, "BV号格式不对")
            return True, "BV号格式错误", True
        if not question:
            await self._send_text(stream_id, "格式: /b站问答 <BV号> <问题>")
            return True, "缺少问题", True

        await self._send_text(stream_id, f"⏳ 正在分析视频 {bvid} ...")
        video = await self._api.get_video_info(bvid)
        if not video:
            await self._send_text(stream_id, f"没找到视频: {bvid}")
            return True, "视频未找到", True
        video = await self._fetcher.fetch(self._api, video)

        parts = [
            f"📺 关于《{video.title}》",
            f"\n👤 UP主: {video.author}",
        ]
        if video.description:
            parts.append(f"\n📝 简介: {_truncate(video.description, 800)}")
        if video.subtitle:
            parts.append(f"\n💬 字幕摘要: {_truncate(video.subtitle, 500)}")
        if video.comments:
            top = "\n  - ".join(video.comments[:5])
            parts.append(f"\n🔥 热门评论:\n  - {top}")
        parts.append(f"\n\n📊 ▶️ {_format_count(video.play)} | 👍 {_format_count(video.like)} | ⭐ {_format_count(video.favorite)}")
        parts.append(f"\n🔗 https://b23.tv/{video.bvid}")
        parts.append(f"\n\n❓ 你的问题: {question}")
        parts.append(f"\n💡 提示: AI 助手可使用 /b站视频 {bvid} 查看详情后为你解答")

        await self._send_text(stream_id, "\n".join(parts))
        return True, f"查询了 {video.title}", True

    @Command("bilibili_toggle", description="切换本群随机推送开关", pattern=r"^/b站开关$")
    async def cmd_toggle(self, stream_id: str = "", **kwargs):
        del kwargs
        if self._scheduler:
            new_state = self._scheduler.toggle_stream(stream_id)
            await self._send_text(stream_id, f"随机推送 {'✅ 已开启' if new_state else '⏸️ 已关闭'}")
        return True, "切换推送开关", True

    @Command("bilibili_status", description="查看B站推送状态", pattern=r"^/b站状态$")
    async def cmd_status(self, stream_id: str = "", **kwargs):
        del kwargs
        enabled = self._scheduler.is_stream_enabled(stream_id) if self._scheduler else False
        pool_size = self._pool.size if self._pool else 0
        push_cfg = self.config.random_push
        push_enabled = push_cfg.enabled and enabled
        msg = (
            "⚙️ B站推送状态\n"
            f"随机推送: {'✅' if push_enabled else '❌'}\n"
            f"候选池: {pool_size} 个视频\n"
            f"活跃时段: {push_cfg.active_hours}\n"
            f"间隔: {push_cfg.min_interval_minutes}~{push_cfg.max_interval_minutes} 分钟\n"
            f"去重: {push_cfg.pool.dedup_window_hours} 小时"
        )
        await self._send_text(stream_id, msg)
        return True, "显示状态", True

    # ================================================================
    # 事件处理 — BV号自动检测
    # ================================================================

    @EventHandler("bilibili_auto_qna", description="检测消息中的BV号并回复视频信息", event_type=EventType.ON_MESSAGE)
    async def on_message(self, message: Any = None, stream_id: str = "", **kwargs):
        del kwargs
        if not message or not stream_id:
            return True, True, None, None, None

        raw = message.get("plain_text", "") if isinstance(message, dict) else str(message)
        if not raw:
            return True, True, None, None, None

        match = re.search(r'BV[a-zA-Z0-9]{10}', raw)
        if not match:
            return True, True, None, None, None
        bvid = match.group(0)

        keywords = self.config.qna.auto_detect_keywords or ["这个视频", "讲了什么", "好看吗", "值得看吗", "总结一下", "概括", "评价", "内容", "介绍"]
        has_keyword = any(kw in raw for kw in keywords)

        user_id = str(message.get("user_id", "")) if isinstance(message, dict) else ""
        if not self._check_rate_limit(stream_id, user_id):
            return True, True, None, None, None

        if has_keyword:
            logger.info(f"[B站插件] 自动检测到问答: {bvid} in {stream_id}")
            video = await self._api.get_video_info(bvid)
            if video:
                video = await self._fetcher.fetch(self._api, video)
                text = (
                    f"📺 检测到你提到了 {bvid}《{video.title}》\n"
                    f"👤 UP主: {video.author}\n"
                )
                if video.description:
                    text += f"📝 简介: {_truncate(video.description, 300)}\n"
                if video.subtitle:
                    text += f"💬 字幕: {_truncate(video.subtitle, 300)}\n"
                text += (
                    f"\n📊 ▶️ {_format_count(video.play)} | 👍 {_format_count(video.like)} | 💬 {_format_count(video.reply)}\n"
                    f"🔗 https://b23.tv/{video.bvid}"
                )
                await self._send_text(stream_id, text)
        return True, True, None, None, None

    # ================================================================
    # LLM Tool
    # ================================================================

    @Tool(
        "get_bilibili_video_info",
        description="获取B站视频的详细信息，包括标题、UP主、简介、播放量、字幕、评论等",
        parameters=[
            ToolParameterInfo(name="bvid", param_type=ToolParamType.STRING,
                              description="B站视频的BV号，例如 BV1xx411c7mD", required=True),
        ],
    )
    async def tool_get_video_info(self, bvid: str = "", **kwargs):
        del kwargs
        if not bvid or not bvid.upper().startswith("BV"):
            return {"name": "get_bilibili_video_info", "content": "错误: 请提供有效的BV号"}

        video = await self._api.get_video_info(bvid)
        if not video:
            return {"name": "get_bilibili_video_info", "content": f"未找到视频: {bvid}"}

        video = await self._fetcher.fetch(self._api, video)
        content = (
            f"B站视频信息\n"
            f"标题: {video.title}\nBV号: {video.bvid}\n"
            f"UP主: {video.author} (mid: {video.mid})\n"
            f"分区: {video.region}\n"
            f"时长: {video.duration // 60}分{video.duration % 60}秒\n"
            f"播放量: {_format_count(video.play)}\n"
            f"弹幕数: {_format_count(video.danmaku)}\n"
            f"点赞数: {_format_count(video.like)}\n"
            f"收藏数: {_format_count(video.favorite)}\n"
            f"评论数: {_format_count(video.reply)}\n"
            f"发布时间: {datetime.fromtimestamp(video.pubdate).strftime('%Y-%m-%d %H:%M') if video.pubdate else '未知'}\n"
            f"标签: {', '.join(video.tags) if video.tags else '无'}\n"
            f"\n简介: {video.description[:500] if video.description else '无'}\n"
            f"\n字幕片段: {video.subtitle[:800] if video.subtitle else '无'}\n"
            f"\n热门评论:\n" + "\n".join(f"  - {c}" for c in (video.comments[:5] or ["无"])) + "\n"
            f"\n链接: https://b23.tv/{video.bvid}"
        )
        return {"name": "get_bilibili_video_info", "content": content}

    # ================================================================
    # 配置热重载
    # ================================================================

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data, version
        if hasattr(self, '_api'):
            self._api = BilibiliAPI(self.config)
        if hasattr(self, '_pool'):
            self._pool = VideoPool(self.config, self._api)
        if hasattr(self, '_fetcher'):
            self._fetcher = ContentFetcher(self.config)
        logger.info("[B站插件] 配置已热重载")


# ============================================================
# v2 插件入口
# ============================================================

def create_plugin() -> BilibiliTrendingPlugin:
    """MaiCore v2 插件工厂函数"""
    return BilibiliTrendingPlugin()
