"""
دانلود مستقیم از توییتر/ایکس بدون yt-dlp.

چرا این ماژول لازم بود؟ اکسترکتور توییتر تو yt-dlp این مدت‌ها به‌خاطر
تغییرات مکرر توییتر/X ناپایدار شده و خیلی وقتا fail می‌کنه. این ماژول به‌جاش
مستقیم از همون API داخلی GraphQL توییتر (با guest token عمومی) استفاده می‌کنه -
دقیقاً همون روشی که خیلی از ابزارهای دانلودر توییتر ازش استفاده می‌کنن.

نکته: BEARER_TOKEN زیر یه توکن *عمومیه* که خودِ وب‌کلاینت twitter.com/x.com
همیشه برای درخواست‌های "مهمان" (guest, بدون لاگین) استفاده می‌کنه - همون چیزی
که تو باندل جاوااسکریپت خودِ سایت هم قابل مشاهده‌ست، نه یه secret اختصاصی.
"""
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from loguru import logger

BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
API_BASE_URL = "https://api.x.com"
GRAPHQL_BASE_URL = "https://x.com/i/api/graphql"
GRAPHQL_TWEET_RESULT_PATH = "/2ICDjqPd81tulZcYrtpTuQ/TweetResultByRestId"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# توکن مهمون معمولاً چند ساعت معتبره؛ برای اینکه هر دانلود یه درخواست اضافه
# نزنه، تو حافظه نگهش می‌داریم و فقط هر ۲۵ دقیقه (یا موقع خطای ۴۰۳) رفرشش می‌کنیم.
_guest_token_cache: dict = {"token": None, "fetched_at": 0.0}
_GUEST_TOKEN_TTL = 25 * 60

_FEATURES = {
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "payments_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

_FIELD_TOGGLES = {
    "withArticleRichContentState": True,
    "withArticlePlainText": False,
    "withGrokAnalyze": False,
    "withDisallowedReplyControls": False,
}


@dataclass
class TwitterMedia:
    type: str  # "photo" یا "video"
    url: str
    thumbnail: Optional[str] = None


@dataclass
class TwitterTweet:
    text: str
    author_name: str
    author_username: str
    medias: list = field(default_factory=list)


def extract_tweet_id(url: str) -> Optional[str]:
    m = re.search(r"status/(\d+)", url)
    return m.group(1) if m else None


async def _refresh_guest_token(client: httpx.AsyncClient) -> str:
    resp = await client.post(f"{API_BASE_URL}/1.1/guest/activate.json")
    resp.raise_for_status()
    token = resp.json().get("guest_token")
    if not token:
        raise RuntimeError("توکن مهمان توییتر برنگشت")
    _guest_token_cache["token"] = token
    _guest_token_cache["fetched_at"] = time.monotonic()
    return token


async def _get_guest_token(client: httpx.AsyncClient) -> str:
    cached = _guest_token_cache["token"]
    if cached and (time.monotonic() - _guest_token_cache["fetched_at"]) < _GUEST_TOKEN_TTL:
        return cached
    return await _refresh_guest_token(client)


def _parse_tweet_result(data: dict) -> Optional[TwitterTweet]:
    tweet = ((data.get("data") or {}).get("tweetResult") or {}).get("result")
    if not tweet:
        return None

    # بعضی توییت‌ها (مثلاً محدود به سن) یه لایه‌ی اضافه‌ی "tweet" دارن
    if tweet.get("__typename") == "TweetWithVisibilityResults":
        tweet = tweet.get("tweet") or tweet

    legacy = tweet.get("legacy") or {}
    user = ((tweet.get("core") or {}).get("user_results") or {}).get("result") or {}
    user_legacy = user.get("legacy") or {}
    user_core = user.get("core") or {}

    text = legacy.get("full_text", "")
    for u in (legacy.get("entities", {}).get("urls") or []):
        if u.get("url"):
            text = text.replace(u["url"], "")
    for m in (legacy.get("entities", {}).get("media") or []):
        if m.get("url"):
            text = text.replace(m["url"], "")
    text = text.strip()

    medias = []
    for m in (legacy.get("extended_entities", {}).get("media") or []):
        if m.get("type") == "photo":
            if m.get("media_url_https"):
                medias.append(TwitterMedia(type="photo", url=m["media_url_https"]))
        else:
            variants = ((m.get("video_info") or {}).get("variants")) or []
            best = None
            for v in variants:
                if "bitrate" in v and (best is None or v["bitrate"] > best["bitrate"]):
                    best = v
            if best and best.get("url"):
                medias.append(TwitterMedia(type="video", url=best["url"], thumbnail=m.get("media_url_https")))

    return TwitterTweet(
        text=text,
        author_name=user_core.get("name") or user_legacy.get("name") or "",
        author_username=user_core.get("screen_name") or user_legacy.get("screen_name") or "",
        medias=medias,
    )


async def fetch_tweet(url: str) -> Optional[TwitterTweet]:
    """
    اطلاعات و مدیای یه توییت رو مستقیم از GraphQL توییتر می‌گیره.
    اگه توییت پیدا نشه، حذف شده باشه، یا مدیایی نداشته باشه، None برمی‌گردونه.
    """
    tweet_id = extract_tweet_id(url)
    if not tweet_id:
        return None

    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {BEARER_TOKEN}",
    }
    variables = {
        "tweetId": tweet_id,
        "withCommunity": False,
        "includePromotedContent": False,
        "withVoice": False,
    }
    params = {
        "variables": json.dumps(variables),
        "features": json.dumps(_FEATURES),
        "fieldToggles": json.dumps(_FIELD_TOGGLES),
    }

    async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
        guest_token = await _get_guest_token(client)
        resp = await client.get(
            f"{GRAPHQL_BASE_URL}{GRAPHQL_TWEET_RESULT_PATH}",
            params=params,
            headers={"x-guest-token": guest_token},
        )
        if resp.status_code == 403:
            # توکن منقضی شده - یه بار با توکن تازه دوباره امتحان کن
            logger.info("توکن مهمان توییتر منقضی شده بود، رفرش شد")
            guest_token = await _refresh_guest_token(client)
            resp = await client.get(
                f"{GRAPHQL_BASE_URL}{GRAPHQL_TWEET_RESULT_PATH}",
                params=params,
                headers={"x-guest-token": guest_token},
            )
        if resp.status_code == 404:
            # این یعنی خودِ doc_id (شناسه‌ی عملیات GraphQL) دیگه معتبر نیست -
            # توییتر/X این شناسه‌ها رو هر ۲ تا ۴ هفته یه بار عوض می‌کنه تا
            # جلوی اسکرپرها رو بگیره. این با انقضای guest token فرق داره (که
            # ۴۰۳ می‌ده و بالا هندل شده) - این خطا یعنی باید GRAPHQL_TWEET_RESULT_PATH
            # رو با یه doc_id تازه آپدیت کرد (مثلاً از سورس فعلیِ yt-dlp:
            # yt_dlp/extractor/twitter.py، کلاس TwitterIE._build_graphql_query).
            logger.error(
                "درخواست GraphQL توییتر ۴۰۴ داد - احتمالاً doc_id توی "
                "GRAPHQL_TWEET_RESULT_PATH دیگه منقضی/عوض‌شده و نیاز به آپدیت داره"
            )
        resp.raise_for_status()
        data = resp.json()

    return _parse_tweet_result(data)
