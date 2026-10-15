"""
NBA 即時比分 / 勝負結果 / 簽約交易 / 年度獎項 / 每週每月最佳 / 重大新聞 / 紀錄 / Shams 爆料 → Discord 機器人
資料來源：ESPN NBA API（比分、球員異動、新聞、獎項）、維基百科（每週、每月最佳），皆免費，不需要金鑰
Shams Charania 的新聞另外以「Shams 最新消息」發送，一般新聞會排除它們，避免重複發文
球隊、獎項清單都從 API 動態取得，球隊搬遷改名、新增獎項都不用改程式
"""

import os
import re
import json
import time
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

# ===== 設定 =====
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
_channel_id = (os.getenv("CHANNEL_ID") or "").strip()
CHANNEL_ID = int(_channel_id) if _channel_id.isdigit() else 0
CHECK_SECONDS = float(os.getenv("CHECK_SECONDS", "60"))
# 只追蹤這些球隊的比分和異動（逗號分隔的 ESPN 縮寫，例如 LAL,GS,NY）；留空代表全部球隊
FOLLOW_TEAMS = {t.strip().upper() for t in os.getenv("FOLLOW_TEAMS", "").split(",") if t.strip()}
# 要推送的異動類型：trade 交易、sign 簽約/續約、claim 讓渡認領、waive 釋出、
# option 合約選項、coach 教練/球團人事、gleague 下放/召回發展聯盟
TX_TYPES = {t.strip().lower() for t in (os.getenv("TX_TYPES") or "trade,sign,claim").split(",")
            if t.strip()}
SKIP_MINOR_DEALS = os.getenv("SKIP_MINOR_DEALS", "true").strip().lower() == "true"
# 要推送的 ESPN 新聞類型：HeadlineNews 是突發/重大新聞；可加 Story、Recap、Preview、Media
NEWS_TYPES = {t.strip() for t in os.getenv("NEWS_TYPES", "HeadlineNews").split(",") if t.strip()}
# 每幾分鐘檢查一次年度獎項
AWARD_CHECK_MINUTES = float(os.getenv("AWARD_CHECK_MINUTES", "30"))

ESPN_API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"  # Google 免費翻譯
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_HEADERS = {"User-Agent": "NBA-Discord-bot/1.0 (https://github.com/Jackkuo1104/NBA-bot)"}
SHAMS_KEYWORD = "charania"  # 作者、標題或摘要含這個字的新聞當作 Shams 的消息
# 標題或摘要符合這些字的新聞當作「紀錄」消息發送（不限 NEWS_TYPES）
RECORD_NEWS = re.compile(r"\brecords?\b|record-breaking|all-time|first player|"
                         r"NBA history|franchise history", re.I)
STATE_FILE = "state.json"
SEEN_LIMIT = 500  # 只保留最近的 ID，避免檔案無限變大
TW = timezone(timedelta(hours=8))
ET = ZoneInfo("America/New_York")  # 自動處理夏令/冬令時間
REFRESH_SECONDS = 12 * 3600  # 球隊清單多久重新抓一次
DRAFT_URL = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/draft"
LOTTERY_PICKS = 14  # 樂透籤（沒進季後賽的 14 隊）
# 抽籤前排第 1~14 的隊伍抽中狀元的機率（%）
LOTTERY_ODDS = [14.0, 14.0, 14.0, 12.5, 10.5, 9.0, 7.5, 6.0, 4.5, 3.0, 2.0, 1.5, 1.0, 0.5]
DRAFT_BUSY_MONTHS = (4, 5, 6, 7)  # 樂透抽籤（5 月）到選秀（6 月）前後每次都檢查，其他月份每小時一次
DRAFT_LIVE_SECONDS = 15 * 60  # 最後一次有人被選中後 15 分鐘內算「選秀進行中」，GitHub 版會每 20 秒檢查一次

TEAMS = {  # 預設的 ESPN 球隊 ID → (縮寫, 中文隊名)；執行時會用 API 的最新球隊清單更新
    "1": ("ATL", "老鷹"), "2": ("BOS", "塞爾提克"), "17": ("BKN", "籃網"), "30": ("CHA", "黃蜂"),
    "4": ("CHI", "公牛"), "5": ("CLE", "騎士"), "6": ("DAL", "獨行俠"), "7": ("DEN", "金塊"),
    "8": ("DET", "活塞"), "9": ("GS", "勇士"), "10": ("HOU", "火箭"), "11": ("IND", "溜馬"),
    "12": ("LAC", "快艇"), "13": ("LAL", "湖人"), "29": ("MEM", "灰熊"), "14": ("MIA", "熱火"),
    "15": ("MIL", "公鹿"), "16": ("MIN", "灰狼"), "3": ("NO", "鵜鶘"), "18": ("NY", "尼克"),
    "25": ("OKC", "雷霆"), "19": ("ORL", "魔術"), "20": ("PHI", "76人"), "21": ("PHX", "太陽"),
    "22": ("POR", "拓荒者"), "23": ("SAC", "國王"), "24": ("SA", "馬刺"), "28": ("TOR", "暴龍"),
    "26": ("UTAH", "爵士"), "27": ("WSH", "巫師"),
}
# 目前的 NBA 球隊 ID → (縮寫, 顯示名稱)；新球隊沒有中文名時顯示英文隊名
nba_teams: dict[str, tuple[str, str]] = dict(TEAMS)
team_by_fullname: dict[str, str] = {}  # 英文全名（例如 Milwaukee Bucks）→ 顯示名稱
team_terms: set[str] = set()  # 英文隊名（全名、暱稱），翻譯時保留不翻
_teams_loaded_at = 0.0

AWARD_ZH = {  # ESPN 獎項 ID → 中文名稱；不在這裡的獎項顯示英文名稱
    "33": "年度最有價值球員 MVP", "39": "年度最佳防守球員", "35": "年度最佳新秀",
    "40": "年度最佳第六人", "36": "年度進步最多球員", "297": "年度關鍵球員",
    "43": "總冠軍賽 MVP", "218": "東區冠軍賽 MVP", "217": "西區冠軍賽 MVP",
    "44": "年度最佳陣容（第一隊）", "45": "年度最佳陣容（第二隊）", "46": "年度最佳陣容（第三隊）",
    "47": "年度最佳新秀陣容（第一隊）", "48": "年度最佳新秀陣容（第二隊）",
    "49": "年度最佳防守陣容（第一隊）", "50": "年度最佳防守陣容（第二隊）",
    "53": "明星賽 MVP", "77": "年度最佳隊友獎", "377": "NBA 盃 MVP", "378": "NBA 盃明星隊",
}

HONOR_ZH = {  # 維基百科球季頁面的小節標題 → 中文名稱（每週、每月獎項，ESPN 沒有這些資料）
    "players of the week": "本週最佳球員",
    "players of the month": "本月最佳球員",
    "defensive players of the month": "本月最佳防守球員",
    "rookies of the month": "本月最佳新秀",
    "coaches of the month": "本月最佳教練",
}
YEARLY_HEADING = "yearly awards"
YEARLY_ZH = {  # 維基百科年度獎項表格裡、ESPN 沒有的獎項（其他年度獎項由 ESPN 發送，避免重複）
    "coach of the year": "年度最佳教練",
    "executive of the year": "年度最佳經理",
    "sportsmanship": "年度運動家精神獎",
    "community assist": "年度社區貢獻獎",
    "social justice": "年度社會正義冠軍獎",
    "hustle": "年度拼勁獎",
}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")

TX_KINDS = {  # 異動類型 → (標題, 顏色, 敘述開頭的英文動詞)
    "trade": ("🔄 交易", 0x1E88E5, ("Acquired", "Traded")),
    "sign": ("✍️ 簽約", 0x43A047, ("Signed", "Re-signed", "Agreed", "Extended", "Converted")),
    "claim": ("📋 讓渡認領", 0x8E24AA, ("Claimed",)),
    "waive": ("👋 釋出", 0x757575, ("Waived", "Released", "Bought")),
    "option": ("📝 合約選項", 0x00897B, ("Exercised", "Declined", "Picked")),
    "coach": ("👔 人事異動", 0xF9A825, ("Hired", "Fired", "Named", "Relieved", "Promoted")),
    "gleague": ("🔁 發展聯盟", 0xFB8C00, ("Assigned", "Recalled")),
}
MINOR_DEAL = re.compile(r"two-way|exhibit 10", re.I)  # 雙向合約、Exhibit 10 這類邊緣合約

# 季後賽（type 3）、附加賽（type 5）、明星賽、NBA 盃淘汰賽才發即時比分；其他比賽只在全部結束後發一則比分
LIVE_SEASON_TYPES = {3, 5}
LIVE_NOTES = re.compile(r"All-Star|Cup.*(Quarterfinal|Semifinal|Championship|Final)", re.I)

# 這些狀態（延賽、暫停、取消）出現時另外通知
NOTABLE_STATUS = ("DELAYED", "SUSPENDED", "POSTPONED", "CANCELED", "FORFEIT")
CALLED_OFF = ("SUSPENDED", "POSTPONED", "CANCELED")
LEADER_UNITS = {"points": "分", "rebounds": "籃板", "assists": "助攻"}


# ===== 狀態紀錄 =====
def load_state() -> dict | None:
    """回傳上次的狀態；檔案不存在代表第一次執行，回傳 None"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        print("⚠️ state.json 格式錯誤，當作第一次執行處理")
        return None


def save_state(state: dict) -> None:
    for key in ("tx", "news", "shams", "honors"):
        if key in state:
            state[key] = state[key][-SEEN_LIMIT:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ===== API =====
async def get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None) -> dict:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_games(session: aiohttp.ClientSession) -> list[dict]:
    """美東時間昨天和今天的所有比賽，涵蓋跨午夜才結束的晚場"""
    today = datetime.now(ET).date()
    days = [today - timedelta(days=1), today]
    results = await asyncio.gather(*(
        get_json(session, f"{ESPN_API}/scoreboard", {"dates": d.strftime("%Y%m%d")}) for d in days))
    games = []
    for day, data in zip(days, results):
        for game in data.get("events", []):
            game["_date"] = str(day)  # 美東時間的比賽日期
            games.append(game)
    return games


async def fetch_transactions(session: aiohttp.ClientSession, days: int = 3) -> list[dict]:
    """最近幾天 NBA 球隊的異動，舊到新"""
    data = await get_json(session, f"{ESPN_API}/transactions", {"limit": "100"})
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    txs = [t for t in data.get("transactions", []) if parse_time(t.get("date")) >= cutoff]
    return list(reversed(txs))


async def refresh_teams(session: aiohttp.ClientSession) -> None:
    """用 API 的最新 NBA 球隊清單更新 nba_teams；失敗時沿用舊的"""
    global _teams_loaded_at
    if time.time() - _teams_loaded_at < REFRESH_SECONDS:
        return
    try:
        data = await get_json(session, f"{ESPN_API}/teams")
        entries = data["sports"][0]["leagues"][0]["teams"]
    except Exception as e:
        print(f"⚠️ 抓取球隊清單失敗，沿用舊資料：{e}")
        return
    teams, fullnames = {}, {}
    for entry in entries:
        t = entry.get("team") or {}
        if t.get("id"):
            fallback = (None, t.get("shortDisplayName") or t.get("displayName", "?"))
            teams[str(t["id"])] = (t.get("abbreviation") or "?", TEAMS.get(str(t["id"]), fallback)[1])
            if t.get("displayName"):
                fullnames[t["displayName"]] = teams[str(t["id"])][1]
            team_terms.update(n for n in (t.get("displayName"), t.get("name"), t.get("shortDisplayName")) if n)
    if teams:
        nba_teams.clear()
        nba_teams.update(teams)
        team_by_fullname.clear()
        team_by_fullname.update(fullnames)
        _teams_loaded_at = time.time()


async def fetch_awards(session: aiohttp.ClientSession, season: int,
                       limit: asyncio.Semaphore) -> list[dict]:
    """某個球季的所有獎項（含得獎者）；該球季還沒有資料時回傳空清單"""
    async with limit:
        async with session.get(f"{ESPN_CORE}/seasons/{season}/awards", params={"limit": "100"},
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 404:
                return []
            resp.raise_for_status()
            refs = [i["$ref"] for i in (await resp.json()).get("items", []) if i.get("$ref")]

    async def fetch(ref: str) -> dict:
        async with limit:
            return await get_json(session, https(ref))

    return list(await asyncio.gather(*(fetch(r) for r in refs)))


def award_seasons() -> list[int]:
    """ESPN 的球季以結束的年份命名（2025-26 球季 = 2026）；同時檢查上一季，接住季後才公布的獎項"""
    now = datetime.now(ET)
    season = now.year + 1 if now.month >= 8 else now.year
    return [season - 1, season]


async def fetch_news(session: aiohttp.ClientSession, limit: int = 50) -> list[dict]:
    data = await get_json(session, f"{ESPN_API}/news", {"limit": str(limit)})
    return data.get("articles", [])  # 新到舊


async def wiki_parse(session: aiohttp.ClientSession, page: str, **params) -> dict:
    async with session.get(WIKI_API, params={"action": "parse", "page": page, "format": "json",
                                             "formatversion": "2", **params},
                           headers=WIKI_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_honors(session: aiohttp.ClientSession, season: int) -> list[tuple[str, str, str, str]]:
    """從維基百科的球季頁面抓每週、每月獎項和 ESPN 沒有的年度獎項，
    回傳 [(小節標題, 期間或獎項名稱, 東區或得獎者, 西區)]；頁面還沒建立時回傳空清單"""
    page = f"{season - 1}–{str(season)[2:]} NBA season"
    data = await wiki_parse(session, page, prop="sections")
    if "error" in data:  # 頁面不存在
        return []
    honors = []
    for section in data["parse"]["sections"]:
        heading = re.sub(r"<[^>]+>", "", section.get("line", "")).strip().lower()
        if heading not in HONOR_ZH and heading != YEARLY_HEADING:
            continue
        text = (await wiki_parse(session, page, prop="wikitext",
                                 section=section["index"]))["parse"]["wikitext"]
        if heading == YEARLY_HEADING:
            honors += [(heading, award, winners, "") for award, winners in parse_yearly_table(text)]
        else:
            honors += [(heading, period, east, west) for period, east, west in parse_honor_table(text)]
    return honors


# ===== 翻譯 =====
_zh_cache: dict[str, str] = {}
_translate_limit = asyncio.Semaphore(5)  # 同時最多 5 個翻譯請求，避免被 Google 擋
# 異動敘述裡「位置 + 人名」的人名，例如 Waived G Trey Jemison III → Trey Jemison III
TX_NAME = re.compile(r"\b(?:PG|SG|SF|PF|G|F|C|G-F|F-G|F-C|C-F|[Cc]oach|[Gg]uard|[Ff]orward|[Cc]enter)\s+"
                     r"((?:[A-Z][\w.'’-]*|Jr\.?|Sr\.?|II|III|IV)(?:\s+(?:[A-Z][\w.'’-]*|Jr\.?|Sr\.?|II|III|IV|de|van|da))+)")


FIXED_ZH = {  # 短詞直接對照，不送去翻譯（Google 常翻錯，例如 Postponed → 延遲）
    "postponed": "延期", "delayed": "延遲", "suspended": "暫停", "canceled": "取消", "cancelled": "取消",
    "forfeit": "沒收比賽", "final": "終場", "rain": "下雨", "weather": "天候因素", "inclement weather": "天候不佳",
}
PRE_ZH = [  # 送去翻譯前先換掉的固定說法
    (re.compile(r"\bGame Highlights\b"), "比賽精華"),
    (re.compile(r"\bGame (\d+)\b"), r"第 \1 戰"),
]
# 文章沒標記到的人名：連續 2~4 個大寫開頭的字；含下列常見字的不算人名
NAME_LIKE = re.compile(r"(?<![\w'’])[A-Z][a-z'’.-]+(?: +(?:[A-Z][a-z'’.-]+|Jr\.?|Sr\.?|II|III|IV)){1,3}(?![\w'’])")
CJK_LATIN = (re.compile(r"([一-鿿])([A-Za-z0-9])"), re.compile(r"([A-Za-z0-9.])([一-鿿])"))
NOT_NAME = set("""a an the and or of in on at to for with from by vs after before as is are was
game games highlights east west eastern western semifinals finals final conference most valuable
player players week month year rookie defensive all star league cup draft trade trades free agent
summer preseason season playoffs playoff day night monday tuesday wednesday thursday friday saturday
sunday january february march april may june july august september october november december award
awards team teams first second third world series american national division wild card opening spring
training hall fame gold glove silver slugger fantasy basketball baseball mock points power rankings now
later new era takeaways updates update latest news what why how who which when where takeaway sources
source report reports breaking live recap preview analysis grades grade best worst top big early late
manager coach coaches general president owner front office mvp""".split())


def guess_names(text: str) -> set[str]:
    names = set()
    for m in NAME_LIKE.finditer(text or ""):
        words = m.group(0).split()
        if not any(w.lower().strip(".'’") in NOT_NAME for w in words):
            names |= person_terms(m.group(0))
    return names


def person_terms(*names: str) -> set[str]:
    """人名和姓氏（文章常只寫姓），翻譯時保留不翻"""
    terms = set()
    for name in names:
        name = (name or "").strip(" .,")
        if name:
            terms.add(name)
            last = re.sub(r"\s+(Jr\.?|Sr\.?|II|III|IV)$", "", name).split()[-1]
            if len(last) >= 3:
                terms.add(last)
    return terms


async def translate(session: aiohttp.ClientSession, text: str, keep=()) -> str:
    """英文翻成繁體中文；keep 和所有隊名先換成代號保護起來，翻完再換回英文。失敗時回傳原文"""
    if not text or not re.search(r"[A-Za-z]{2}", text):
        return text
    if text in _zh_cache:
        return _zh_cache[text]
    fixed = [FIXED_ZH.get(p.strip().lower()) for p in text.split(":")]  # 例如 Delayed: Rain
    if all(fixed):
        _zh_cache[text] = "：".join(fixed)
        return _zh_cache[text]
    terms = sorted({t for t in (*keep, *team_terms) if t and t in text}, key=len, reverse=True)
    protected = text
    for pattern, repl in PRE_ZH:
        protected = pattern.sub(repl, protected)
    if terms:
        pattern = re.compile("|".join(rf"(?<!\w){re.escape(t)}(?!\w)" for t in terms))
        protected = pattern.sub(lambda m: f"__N{terms.index(m.group(0))}__", protected)
    try:
        async with _translate_limit, session.get(
                TRANSLATE_URL, params={"client": "gtx", "sl": "en", "tl": "zh-TW", "dt": "t", "q": protected},
                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        out = "".join(seg[0] for seg in data[0] if seg and seg[0])
        out = re.sub(r"_+\s*[Nn]\s*(\d+)\s*_+",
                     lambda m: terms[int(m.group(1))] if int(m.group(1)) < len(terms) else m.group(0), out)
        if not out.strip() or "__N" in out:
            raise ValueError(f"翻譯結果異常：{out}")
        for pattern in CJK_LATIN:  # 中文和英文、數字之間加空格比較好讀
            out = pattern.sub(r"\1 \2", out)
    except Exception as e:
        print(f"⚠️ 翻譯失敗，顯示英文原文：{e}")
        return text
    _zh_cache[text] = out
    return out


def zh(text: str) -> str:
    """已經翻譯過的文字回傳中文，沒翻過的回傳原文（翻譯要先用 prefetch 做好）"""
    return _zh_cache.get(text, text)


async def prefetch(session: aiohttp.ClientSession, texts, keep=()) -> None:
    await asyncio.gather(*(translate(session, t, keep) for t in set(texts) if t))


async def zh_article(session: aiohttp.ClientSession, article: dict) -> dict:
    """新聞標題和摘要翻成中文；文章標記的球員、教練、球隊名稱保留英文"""
    keep = set()
    for line in f"{article.get('headline') or ''}\n{article.get('description') or ''}".splitlines():
        keep |= guess_names(line)
    for c in article.get("categories") or []:
        if c.get("type") in ("athlete", "coach", "person"):
            keep |= person_terms(c.get("description", ""))
        elif c.get("type") == "team" and c.get("description"):
            keep.add(c["description"])
    out = dict(article)
    for key in ("headline", "description"):
        out[key] = await translate(session, article.get(key) or "", keep)
    return out


async def zh_articles(session: aiohttp.ClientSession, articles: list[dict]) -> list[dict]:
    return list(await asyncio.gather(*(zh_article(session, a) for a in articles)))


def tx_terms(tx: dict) -> set[str]:
    return person_terms(*TX_NAME.findall(tx.get("description", "")))


TX_VERB_ZH = {  # 異動敘述開頭的動詞
    "Waived": "釋出", "Released": "釋出", "Signed": "簽下", "Re-signed": "續約", "Agreed": "談妥",
    "Extended": "延長合約", "Converted": "轉換合約", "Traded": "交易送走", "Acquired": "得到",
    "Claimed": "認領", "Exercised": "執行", "Declined": "拒絕執行", "Picked": "執行",
    "Assigned": "下放", "Recalled": "召回", "Hired": "聘請", "Fired": "解雇", "Named": "任命",
    "Promoted": "升任", "Relieved": "解除職務", "Bought": "買斷",
}
POS_ZH = {"PG": "控球後衛", "SG": "得分後衛", "SF": "小前鋒", "PF": "大前鋒", "G": "後衛", "F": "前鋒",
          "C": "中鋒", "G-F": "後衛/前鋒", "F-G": "前鋒/後衛", "F-C": "前鋒/中鋒", "C-F": "中鋒/前鋒"}
POS_BEFORE_NAME = re.compile(r"\b(PG|SG|SF|PF|G-F|F-G|F-C|C-F|G|F|C)\s+(?=[A-Z])")


async def translate_tx(session: aiohttp.ClientSession, tx: dict) -> None:
    """異動敘述翻成中文：動詞、位置先自己翻，其他交給 Google；結果存在 zh() 用的快取"""
    text = tx.get("description", "")
    if not text or text in _zh_cache:
        return
    first, _, rest = text.partition(" ")
    verb = TX_VERB_ZH.get(first)
    body = POS_BEFORE_NAME.sub(lambda m: f"{POS_ZH[m.group(1)]} ", rest if verb else text)
    body = re.sub(r"\s+off waivers from (?:the )?(.+?)\.?$", r"（原屬 \1）", body)
    body = await translate(session, body.rstrip(". "), tx_terms(tx))  # 動詞自己翻，不讓 Google 改掉
    _zh_cache[text] = f"{verb} {body}" if verb else body


# ===== 小工具 =====
def https(url: str) -> str:
    return url.replace("http://", "https://", 1)


def parse_time(value: str | None) -> datetime:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def team_label(team: dict) -> str:
    known = nba_teams.get(str(team.get("id")))
    return f"{known[1]} {known[0]}" if known else team.get("displayName") or team.get("name", "?")


def team_abbr(team: dict) -> str:
    known = nba_teams.get(str(team.get("id")))
    return known[0] if known else team.get("abbreviation") or team.get("name", "?")


def team_name(team_id) -> str:
    known = nba_teams.get(str(team_id)) or TEAMS.get(str(team_id))
    return known[1] if known else ""


def follows(*abbrs: str) -> bool:
    return not FOLLOW_TEAMS or any(a in FOLLOW_TEAMS for a in abbrs)


def competition(game: dict) -> dict:
    return (game.get("competitions") or [{}])[0]


def side(game: dict, home_away: str) -> dict:
    return next((c for c in competition(game).get("competitors", [])
                 if c.get("homeAway") == home_away), {"team": {}})


def score(competitor: dict) -> int:
    try:
        return int(competitor.get("score") or 0)
    except ValueError:
        return 0


def game_status(game: dict) -> tuple[str, str]:
    """回傳 (pre / in / post, 狀態代碼例如 STATUS_HALFTIME)"""
    t = (game.get("status") or {}).get("type") or {}
    return t.get("state", ""), t.get("name", "")


def is_called_off(game: dict) -> bool:
    return any(w in game_status(game)[1] for w in CALLED_OFF)


def is_special(game: dict) -> bool:
    """明星賽這類非 NBA 球隊對戰的比賽（熱身賽對國外球隊除外）"""
    if (game.get("season") or {}).get("type") == 1:
        return False
    return not all(str(c.get("team", {}).get("id")) in nba_teams
                   for c in competition(game).get("competitors", []))


def notes_text(game: dict) -> str:
    return "｜".join(n.get("headline", "") for n in competition(game).get("notes") or []
                    if n.get("headline"))


def is_live_game(game: dict) -> bool:
    if (game.get("season") or {}).get("type") in LIVE_SEASON_TYPES:
        return True
    return is_special(game) or bool(LIVE_NOTES.search(notes_text(game)))


def period_name(n: int) -> str:
    if n <= 4:
        return f"第 {n} 節"
    return "延長賽" if n == 5 else f"第 {n - 4} 次延長賽"


def completed_periods(game: dict) -> int:
    """已經打完幾節（含延長賽）"""
    state, name = game_status(game)
    period = (game.get("status") or {}).get("period") or 0
    if state == "post" or name in ("STATUS_END_PERIOD", "STATUS_HALFTIME"):
        return period
    return max(period - 1, 0) if state == "in" else 0


def clock_text(game: dict) -> str:
    status = game.get("status") or {}
    state, name = game_status(game)
    period = status.get("period") or 0
    if state != "in" or not period:
        return ""
    if name == "STATUS_HALFTIME":
        return "中場休息"
    if name == "STATUS_END_PERIOD":
        return f"{period_name(period)}結束"
    return f"{period_name(period)} 剩 {status.get('displayClock', '')}"


def game_url(game: dict) -> str:
    return f"https://www.espn.com/nba/game/_/gameId/{game['id']}"


def score_title(game: dict) -> str:
    away, home = side(game, "away"), side(game, "home")
    return f"{team_label(away['team'])} {score(away)} : {score(home)} {team_label(home['team'])}"


def recap_text(game: dict) -> str:
    return (competition(game).get("headlines") or [{}])[0].get("shortLinkText") or ""


def status_detail(game: dict) -> str:
    t = (game.get("status") or {}).get("type") or {}
    return t.get("detail") or t.get("description", "")


def game_texts(game: dict) -> list[str]:
    """這場比賽的訊息裡會出現的英文（系列賽說明、比賽狀態、賽後標題）"""
    return [series_text(game), status_detail(game), recap_text(game)]


def game_keep(game: dict) -> set[str]:
    """賽後標題常提到得分王，人名保留不翻"""
    names = [((cat.get("leaders") or [{}])[0].get("athlete") or {}).get("displayName", "")
             for c in competition(game).get("competitors", []) for cat in c.get("leaders") or []]
    return person_terms(*names)


def series_text(game: dict) -> str:
    summary = (competition(game).get("series") or {}).get("summary", "")
    return "｜".join(p for p in (notes_text(game), summary) if p)


def linescore_table(game: dict) -> str:
    away, home = side(game, "away"), side(game, "home")
    n = max(len(away.get("linescores") or []), len(home.get("linescores") or []))
    if not n:
        return ""
    labels = [str(i) if i <= 4 else ("OT" if i == 5 else f"{i - 4}OT") for i in range(1, n + 1)]

    def row(c: dict) -> str:
        vals = [str(x.get("displayValue", "")) for x in c.get("linescores") or []]
        vals += [""] * (n - len(vals))
        cells = " ".join(f"{v:>3}" for v in vals)
        return f"{team_abbr(c['team']):<5}{cells} |{score(c):>4}"

    header = "     " + " ".join(f"{l:>3}" for l in labels) + " |   T"
    return "```\n" + "\n".join([header, row(away), row(home)]) + "\n```"


def leaders_line(competitor: dict) -> str:
    parts = []
    for cat in competitor.get("leaders") or []:
        unit = LEADER_UNITS.get(cat.get("name"))
        top = (cat.get("leaders") or [{}])[0]
        name = (top.get("athlete") or {}).get("shortName")
        if unit and name:
            parts.append(f"{name} {top.get('displayValue', '')} {unit}")
    if not parts:
        return ""
    return f"**{team_label(competitor['team'])}**：" + "｜".join(parts)


def game_embed(game: dict, title: str, lines: list[str], color: int) -> discord.Embed:
    embed = discord.Embed(title=title[:256], url=game_url(game),
                          description="\n".join(l for l in lines if l)[:4000], color=color)
    series = zh(series_text(game))
    embed.set_footer(text=f"NBA｜{series}"[:2048] if series else "NBA")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


# ===== 訊息內容 =====
def start_message(game: dict) -> tuple[str, discord.Embed]:
    away, home = side(game, "away"), side(game, "home")
    venue = (competition(game).get("venue") or {}).get("fullName", "")
    title = f"{team_label(away['team'])} @ {team_label(home['team'])}"
    return "🏀 **比賽開始**", game_embed(game, title, [f"📍 {venue}" if venue else ""], 0x1D428A)


def period_message(game: dict) -> tuple[str, discord.Embed]:
    n = completed_periods(game)
    label = "中場" if n == 2 else f"{period_name(n)}結束"
    return f"⏱️ **{label}**", game_embed(game, score_title(game), [linescore_table(game)], 0xC8102E)


def final_message(game: dict) -> tuple[str, discord.Embed]:
    away, home = side(game, "away"), side(game, "home")
    winner = away if away.get("winner") else home if home.get("winner") else None
    periods = (game.get("status") or {}).get("period") or 0
    overtime = f"（{period_name(periods)}）" if periods > 4 else ""
    lines = [f"🏆 **{team_label(winner['team'])}** 獲勝{overtime}" if winner else "🤝 平手",
             leaders_line(away), leaders_line(home), linescore_table(game)]
    recap = recap_text(game)
    if recap:
        lines.append(f"📝 {zh(recap)}")
    return "🏁 **終場**", game_embed(game, score_title(game), lines, 0xFFB81C)


def status_message(game: dict) -> tuple[str, discord.Embed]:
    lines = [f"狀態：{zh(status_detail(game))}", clock_text(game)]
    return "⏸️ **比賽狀態更新**", game_embed(game, score_title(game), lines, 0x757575)


def tx_kind(tx: dict) -> str:
    first = (tx.get("description") or "").split(" ", 1)[0]
    return next((k for k, (_, _, verbs) in TX_KINDS.items() if first in verbs), "other")


def tx_id(tx: dict) -> str:
    return f"{(tx.get('date') or '')[:10]}|{tx.get('team', {}).get('id')}|{tx.get('description', '')}"


def team_logo(team: dict) -> str | None:
    logos = team.get("logos") or []
    return logos[0].get("href") if logos else None


def tx_mentions(a: dict, b: dict) -> bool:
    """a 的敘述有沒有提到 b 的球隊（用來把交易雙方合併成一則）"""
    team = b.get("team") or {}
    words = [w for w in (team.get("name"), team.get("location"), team.get("displayName"))
             if w and len(w) > 2]
    return any(re.search(rf"\b{re.escape(w)}\b", a.get("description", "")) for w in words)


def group_trades(txs: list[dict]) -> list[list[dict]]:
    """同一天、互相提到對方球隊的交易紀錄合併成一組，其他異動各自一組"""
    groups: list[list[dict]] = []
    for tx in txs:
        if tx_kind(tx) == "trade":
            match = next((g for g in groups if tx_kind(g[0]) == "trade" and g[0].get("date") == tx.get("date")
                          and any(tx_mentions(tx, x) or tx_mentions(x, tx) for x in g)), None)
            if match:
                match.append(tx)
                continue
        groups.append([tx])
    return groups


def tx_message(group: list[dict]) -> tuple[str, discord.Embed]:
    label, color, _ = TX_KINDS.get(tx_kind(group[0]), ("📰 異動", 0x757575, ()))
    if len(group) == 1:
        description = zh(group[0].get("description", ""))
    else:
        description = "\n".join(f"**{team_name(t.get('team', {}).get('id')) or t.get('team', {}).get('displayName', '')}**："
                                f"{zh(t.get('description', ''))}" for t in group)
    embed = discord.Embed(title=label, description=description[:4000], color=color)
    team = group[0].get("team") or {}
    if len(group) == 1 and team.get("id"):
        embed.set_author(name=team_name(team["id"]) or team.get("displayName", ""), icon_url=team_logo(team))
    embed.set_footer(text=f"NBA 異動｜{(group[0].get('date') or '')[:10]}")
    return "📰 **NBA 球員異動**", embed


def wanted_tx(tx: dict) -> bool:
    if tx_kind(tx) not in TX_TYPES or not follows(tx.get("team", {}).get("abbreviation", "")):
        return False
    return not (SKIP_MINOR_DEALS and MINOR_DEAL.search(tx.get("description", "")))


def season_label(season: int) -> str:
    return f"{season - 1}-{str(season)[2:]}"


def ref_id(obj: dict | None, kind: str) -> str:
    m = re.search(rf"/{kind}/(\d+)", (obj or {}).get("$ref", ""))
    return m.group(1) if m else ""


def winner_ref(w: dict) -> str | None:
    return next((w[k]["$ref"] for k in ("athlete", "coach") if (w.get(k) or {}).get("$ref")), None)


def winner_key(season: int, award: dict, w: dict) -> str:
    person = ref_id(w.get("athlete"), "athletes") or ref_id(w.get("coach"), "coaches")
    return f"{season}:{award.get('id')}:{person}:{ref_id(w.get('team'), 'teams')}"


def person_text(person: dict, team_id: str) -> str:
    name = person.get("displayName") or " ".join(
        p for p in (person.get("firstName"), person.get("lastName")) if p) or "?"
    pos = (person.get("position") or {}).get("abbreviation", "")
    text = " ".join(p for p in (pos, name) if p)
    team = team_name(team_id)
    return f"{text}（{team}）" if team else text


def award_message(award: dict, season: int,
                  winners: list[tuple[dict, str]]) -> tuple[str, discord.Embed]:
    """winners：[(球員/教練資料, 球隊 ID)]"""
    name = AWARD_ZH.get(str(award.get("id"))) or zh(award.get("name", ""))
    embed = discord.Embed(title=f"{season_label(season)} {name}"[:256],
                          description="\n".join(f"• {person_text(p, t)}" for p, t in winners)[:4000],
                          color=0xFFD700)
    links = award.get("links") or []
    if links and links[0].get("href"):
        embed.url = links[0]["href"]
    headshot = (winners[0][0].get("headshot") or {}).get("href") if len(winners) == 1 else None
    if headshot:
        embed.set_thumbnail(url=headshot)
    embed.set_footer(text="NBA 獎項")
    embed.timestamp = datetime.now(timezone.utc)
    return "🏆 **NBA 獎項揭曉**", embed


def wiki_text(cell: str) -> str:
    """把維基語法轉成純文字：[[連結|文字]] → 文字，移除 {{small|(1/2)}}、<ref> 這類東西"""
    cell = re.sub(r"<ref[^>]*/>|<ref.*?</ref>", "", cell, flags=re.S)
    cell = re.sub(r"\{\{(?:small|Hs|sort)\|[^{}]*\}\}", "", cell, flags=re.I)
    cell = re.sub(r"\{\{nowrap\|([^{}]*)\}\}", r"\1", cell, flags=re.I)
    cell = re.sub(r"\{\{[^{}]*\}\}", "", cell)
    cell = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", cell)
    cell = re.sub(r"'{2,}|<[^>]+>", "", cell)
    return " ".join(cell.split())


def parse_honor_table(text: str) -> list[tuple[str, str, str]]:
    """解析「期間｜東區｜西區｜出處」的表格；只取有附出處、兩區都填好的列，避免抓到還沒寫完或亂改的內容"""
    rows = []
    for raw in re.split(r"\n\|-", text):
        cells = []
        for line in raw.split("\n"):
            if line.startswith("|") and not line.startswith(("|}", "|-", "|+")):
                cells += line[1:].split("||")
        if len(cells) < 4 or "<ref" not in cells[3]:
            continue
        # 有樣式的格子長這樣：style="..."|內容
        cells = [c.split("|", 1)[1] if re.match(r'\s*(style|class|rowspan|colspan)=[^|]*\|', c) else c
                 for c in cells]
        period, east, west = (wiki_text(c) for c in cells[:3])
        if period and east and west:
            rows.append((period, east, west))
    return rows


def zh_period(period: str) -> str:
    """October 27 – November 2 → 10/27 – 11/2；October/November → 10月/11月"""
    for i, month in enumerate(MONTHS, 1):
        period = re.sub(rf"\b{month}\s+(\d+)", rf"{i}/\1", period)
        period = re.sub(rf"\b{month}\b", f"{i}月", period)
    return period


def zh_winner(text: str) -> str:
    """Giannis Antetokounmpo (Milwaukee Bucks) → Giannis Antetokounmpo（公鹿）"""
    m = re.match(r"(.*?)\s*\(([^()]*)\)\s*$", text)
    if not m:
        return text
    return f"{m.group(1)}（{team_by_fullname.get(m.group(2), m.group(2))}）"


def yearly_zh(award: str) -> str | None:
    return next((zh for key, zh in YEARLY_ZH.items() if key in award.lower()), None)


def parse_yearly_table(text: str) -> list[tuple[str, str]]:
    """解析年度獎項表格「獎項 || 得獎者 || 入圍者」，只取 ESPN 沒有、而且附了出處的獎項；
    回傳 [(英文獎項名稱, 得獎者（多位用換行分隔）)]"""
    rows = []
    for raw in re.split(r"\n\|-", text):
        cells = []
        for line in raw.split("\n"):
            if line.startswith("|") and not line.startswith(("|}", "|-", "|+")):
                cells += line[1:].split("||")
        if len(cells) < 2 or "<ref" not in cells[1]:
            continue
        award = wiki_text(cells[0])
        if not yearly_zh(award):
            continue
        # 這張表的球隊寫在 {{small|(Boston Celtics)}} 裡，先拿出來，不然會被 wiki_text 刪掉
        winners = re.sub(r"\{\{small\|(\([^{}]*\))\}\}", r" \1", cells[1], flags=re.I)
        names = [wiki_text(w) for w in re.split(r"<br\s*/?>", winners)]
        names = [n for n in names if n]
        if names:
            rows.append((award, "\n".join(names)))
    return rows


def honor_key(season: int, heading: str, period: str) -> str:
    return f"{season}:{heading}:{period}"


def honor_message(season: int, heading: str, period: str, east: str,
                  west: str) -> tuple[str, discord.Embed]:
    if heading == YEARLY_HEADING:  # period 是獎項名稱，east 是得獎者
        embed = discord.Embed(title=f"{season_label(season)} {yearly_zh(period)}"[:256],
                              description="\n".join(f"• {zh_winner(w)}" for w in east.split("\n"))[:4000],
                              color=0xFFD700)
        embed.set_footer(text="NBA 獎項")
        embed.timestamp = datetime.now(timezone.utc)
        return "🏆 **NBA 獎項揭曉**", embed
    name = HONOR_ZH[heading]
    embed = discord.Embed(title=f"{name}｜{zh_period(period)}"[:256],
                          description=f"**東區**：{zh_winner(east)}\n**西區**：{zh_winner(west)}"[:4000],
                          color=0xFFD700)
    embed.set_footer(text=f"NBA {season_label(season)} 球季")
    embed.timestamp = datetime.now(timezone.utc)
    return f"🏅 **NBA {name}**", embed


def is_shams(article: dict) -> bool:
    text = " ".join(str(article.get(k) or "") for k in ("byline", "headline", "description"))
    return SHAMS_KEYWORD in text.lower()


def is_record(article: dict) -> bool:
    return bool(RECORD_NEWS.search(f"{article.get('headline') or ''} {article.get('description') or ''}"))


def wanted_news(article: dict) -> bool:
    return (article.get("type") in NEWS_TYPES or is_record(article)) and not is_shams(article)


def news_label(article: dict) -> str:
    return "📊 **NBA 紀錄**" if is_record(article) else "🚨 **NBA 重大消息**"


def news_embed(article: dict) -> discord.Embed:
    embed = discord.Embed(
        title=(article.get("headline") or "（無標題）")[:256],
        url=article.get("links", {}).get("web", {}).get("href"),
        description=(article.get("description") or "")[:4000],
        color=0xC8102E,
    )
    if article.get("byline"):
        embed.set_author(name=article["byline"][:256])
    images = article.get("images") or []
    if images and images[0].get("url"):
        embed.set_image(url=images[0]["url"])
    if article.get("published"):
        published = parse_time(article["published"])
        if published.year > 1:
            embed.timestamp = published
    embed.set_footer(text="ESPN NBA")
    return embed


def article_id(article: dict) -> str:
    return str(article.get("id") or article.get("links", {}).get("web", {}).get("href", ""))


def final_line(game: dict) -> str:
    away, home = side(game, "away"), side(game, "home")
    periods = (game.get("status") or {}).get("period") or 0
    overtime = f"（{period_name(periods)}）" if periods > 4 else ""
    return (f"{team_label(away['team'])} **{score(away)} : {score(home)}** "
            f"{team_label(home['team'])}{overtime}")


def day_done_message(date: str, games: list[dict]) -> tuple[str, discord.Embed]:
    finished = [g for g in games if not is_called_off(g)]
    lines = [final_line(g) for g in finished]
    if len(finished) < len(games):
        lines.append(f"另有 {len(games) - len(finished)} 場延賽或取消")
    lines.append("👉 點上方標題查看完整比分")
    embed = discord.Embed(title=f"NBA 全部比分｜{date}",
                          url=f"https://www.espn.com/nba/scoreboard/_/date/{date.replace('-', '')}",
                          description="\n".join(lines)[:4000], color=0x1D428A)
    embed.set_footer(text="NBA")
    embed.timestamp = datetime.now(timezone.utc)
    return "📋 **今日 NBA 比賽全部結束**", embed


def day_done_updates(games: list[dict], state: dict, initial: bool) -> list:
    """例行賽、熱身賽的日子，當天所有比賽都結束（或延賽）後，發一則整天的比分"""
    by_date: dict[str, list[dict]] = {}
    for game in games:
        by_date.setdefault(game["_date"], []).append(game)
    first_time = "days_done" not in state
    done = state.setdefault("days_done", [])
    messages = []
    for date, day_games in sorted(by_date.items()):
        regular = [g for g in day_games if not is_live_game(g)]
        if not regular or date in done or all(is_called_off(g) for g in regular):
            continue
        if not all(game_status(g)[0] == "post" or is_called_off(g) for g in regular):
            continue
        done.append(date)
        if not (initial or first_time):
            messages.append(day_done_message(date, regular))
    state["days_done"] = done[-10:]
    return messages


# ===== 比對新舊資料，產生要發送的訊息 =====
async def game_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    games = await fetch_games(session)
    messages = day_done_updates(games, state, initial)
    new_games = {}
    for game in games:
        status, name = game_status(game)
        prev = state["games"].get(game["id"])
        cur = {"state": status, "status": name, "periods": completed_periods(game)}
        new_games[game["id"]] = cur
        # 新出現的比賽只記錄不發文（第一次執行，或機器人關掉一段時間後）
        if prev is None or initial:
            continue
        # 例行賽、熱身賽不發即時比分，改成全部結束後發一次整天比分（見 day_done_updates）
        if not is_live_game(game):
            continue
        if not is_special(game) and not follows(  # 明星賽不受 FOLLOW_TEAMS 限制
                team_abbr(side(game, "away")["team"]), team_abbr(side(game, "home")["team"])):
            continue

        # 這場比賽有變化（可能要發訊息）時，先翻譯訊息裡會用到的英文
        if (status, name, cur["periods"]) != (prev["state"], prev["status"], prev["periods"]):
            await prefetch(session, game_texts(game), game_keep(game))
        notable = any(w in name for w in NOTABLE_STATUS)
        if status == "post":
            if prev["state"] != "post":
                messages.append(status_message(game) if notable else final_message(game))
            continue
        if name != prev["status"] and notable:
            messages.append(status_message(game))
        if status != "in":
            continue
        if prev["state"] == "pre":
            messages.append(start_message(game))
        if cur["periods"] > prev["periods"]:  # 漏掉好幾節時只發最新一節
            messages.append(period_message(game))
    state["games"] = new_games
    return messages


async def tx_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    seen = set(state["tx"])
    new_txs = [t for t in await fetch_transactions(session)
               if wanted_tx(t) and tx_id(t) not in seen]
    state["tx"] += [tx_id(t) for t in new_txs]
    if initial:
        return []
    await asyncio.gather(*(translate_tx(session, t) for t in new_txs))
    return [tx_message(group) for group in group_trades(new_txs)]


async def news_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    seen = set(state["news"])
    new_articles = [a for a in await fetch_news(session)
                    if wanted_news(a) and article_id(a) not in seen]
    new_articles.reverse()  # 舊的先發
    state["news"] += [article_id(a) for a in new_articles]
    if initial:
        return []
    # 用英文原文判斷是不是紀錄類新聞，再發翻譯後的內容
    return [(news_label(a), news_embed(z)) for a, z in zip(new_articles, await zh_articles(session, new_articles))]


async def shams_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    """Shams 的新聞不限類型都發（和原本的 shams-bot 相同）"""
    seen = set(state["shams"])
    new_articles = [a for a in await fetch_news(session)
                    if is_shams(a) and article_id(a) not in seen]
    new_articles.reverse()  # 舊的先發
    state["shams"] += [article_id(a) for a in new_articles]
    if initial:
        return []
    return [("🚨 **Shams 最新消息**", news_embed(a)) for a in await zh_articles(session, new_articles)]


async def award_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    if not initial and time.time() - state.get("awards_checked", 0) < AWARD_CHECK_MINUTES * 60:
        return []
    limit = asyncio.Semaphore(8)  # 同時最多 8 個請求，避免對 ESPN 太兇
    seasons = award_seasons()
    results = await asyncio.gather(*(fetch_awards(session, s, limit) for s in seasons))
    seen = set(state["awards"])
    keys, new = [], []
    for season, awards in zip(seasons, results):
        for award in awards:
            winners = [w for w in award.get("winners") or [] if winner_ref(w)]
            keys += [winner_key(season, award, w) for w in winners]
            fresh = [w for w in winners if winner_key(season, award, w) not in seen]
            if fresh:
                new.append((season, award, fresh))
    state["awards"] = keys  # 只保留目前檢查的球季，檔案不會一直變大
    state["awards_checked"] = time.time()
    if initial or not new:
        return []

    async def person(w: dict) -> tuple[dict, str]:
        async with limit:
            try:
                data = await get_json(session, https(winner_ref(w)))
            except Exception as e:
                print(f"⚠️ 抓取得獎者資料失敗：{e}")
                data = {}
        return data, ref_id(w.get("team"), "teams")

    await prefetch(session, [a.get("name", "") for _, a, _ in new if str(a.get("id")) not in AWARD_ZH])
    messages = []
    for season, award, fresh in new:
        winners = list(await asyncio.gather(*(person(w) for w in fresh)))
        messages.append(award_message(award, season, winners))
    return messages


async def honor_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    """每週、每月最佳球員 / 新秀 / 防守 / 教練（資料來自維基百科，NBA 公布後通常幾小時內更新）"""
    if not initial and time.time() - state.get("honors_checked", 0) < AWARD_CHECK_MINUTES * 60:
        return []
    season = award_seasons()[-1]
    honors = await fetch_honors(session, season)
    seen = set(state["honors"])
    new = [h for h in honors if honor_key(season, h[0], h[1]) not in seen]
    state["honors"] += [honor_key(season, h[0], h[1]) for h in new]
    state["honors_checked"] = time.time()
    if initial:
        return []
    return [honor_message(season, *h) for h in new]


# ===== 選秀、樂透抽籤 =====
def draft_year() -> int:
    """選秀在 6 月，8 月以後就看明年的選秀"""
    now = datetime.now(ET)
    return now.year if now.month <= 7 else now.year + 1


async def fetch_draft(session: aiohttp.ClientSession, year: int) -> dict | None:
    """某年的選秀資料；順位還沒決定（找不到）時回傳 None"""
    async with session.get(DRAFT_URL, params={"season": str(year)},
                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status == 404:
            return None
        resp.raise_for_status()
        data = await resp.json()
    return data if data.get("picks") else None


def pick_made(p: dict) -> bool:
    return p.get("status") == "SELECTION_MADE" and bool((p.get("athlete") or {}).get("displayName"))


def draft_team(team_id) -> str:
    known = nba_teams.get(str(team_id))
    return f"{known[1]} {known[0]}" if known else str(team_id)


def trade_note(p: dict) -> str:
    """via IND → 原屬 IND；from MEM via ATL and UTA → 原屬 MEM，經 ATL、UTA；Traded to MIN → 選後交易至 MIN"""
    note = (p.get("tradeNote") or "").strip()
    if not note:
        return ""
    m = re.match(r"traded to\s+(.+)", note, re.I)
    if m:
        return f"（選後交易至 {m.group(1)}）"
    frm = re.search(r"\bfrom\s+(\w+)", note, re.I)
    via = re.search(r"\bvia\s+(.+)$", note, re.I)
    if not frm and not via:
        return f"（{note}）"
    if not frm:  # 只有 via：就是原本的球隊
        return f"（原屬 {via.group(1).replace(' and ', '、')}）"
    return f"（原屬 {frm.group(1)}" + (f"，經 {via.group(1).replace(' and ', '、')}" if via else "") + "）"


def pick_line(p: dict, positions: dict) -> str:
    a = p.get("athlete") or {}
    pos = positions.get(str((a.get("position") or {}).get("id")), "")
    school = (a.get("team") or {}).get("shortDisplayName") or (a.get("team") or {}).get("location", "")
    extra = "｜".join(x for x in (POS_ZH.get(pos, pos), school) if x)
    return (f"**#{p.get('overall')}** {draft_team(p.get('teamId'))}{trade_note(p)}："
            f"{a.get('displayName', '')}" + (f"｜{extra}" if extra else ""))


def draft_embed(year: int, title: str, lines: list[str], color: int = 0x1D428A) -> discord.Embed:
    embed = discord.Embed(title=title[:256], description="\n".join(lines)[:4000], color=color,
                          url="https://www.espn.com/nba/draft/rounds")
    embed.set_footer(text=f"{year} NBA 選秀")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def order_message(year: int, picks: list[dict]) -> tuple[str, discord.Embed]:
    """抽籤前的選秀順位：樂透區 14 隊（含狀元機率）＋ 第 15~30 順位"""
    lines = ["**樂透區（5 月抽籤決定前 4 順位）**"]
    for i, p in enumerate(picks[:LOTTERY_PICKS], 1):
        lines.append(f"第 {i} 順位：{draft_team(p.get('teamId'))}{trade_note(p)}　狀元機率 {LOTTERY_ODDS[i - 1]}%")
    lines.append("\n**第 15～30 順位（依戰績）**")
    lines += [f"第 {p.get('overall')} 順位：{draft_team(p.get('teamId'))}{trade_note(p)}"
              for p in picks[LOTTERY_PICKS:30]]
    return "📋 **NBA 選秀順位公布（樂透抽籤前）**", draft_embed(year, f"{year} NBA 選秀順位", lines)


def move_text(team: str, slot: int, before: list[str] | None) -> str:
    if not before or team not in before:
        return ""
    was = before.index(team) + 1
    odds = f"（抽籤前排第 {was}，狀元機率 {LOTTERY_ODDS[was - 1]}%）" if was <= len(LOTTERY_ODDS) else ""
    if was > slot:
        return f"⬆️ 上升 {was - slot} 名{odds}"
    if was < slot:
        return f"⬇️ 下滑 {slot - was} 名{odds}"
    return f"➖ 維持原順位{odds}"


def lottery_reveal(year: int, order: list[str], before: list[str] | None) -> list:
    """樂透抽籤揭曉：從第 14 順位一隊一隊發到狀元，最後發一則總整理"""
    messages = []
    for slot in range(len(order), 0, -1):
        team = order[slot - 1]
        if slot == 1:
            content, title = "🏆 **NBA 狀元籤揭曉！**", f"{year} 狀元籤：{draft_team(team)}"
        else:
            content, title = "🎰 **NBA 樂透抽籤**", f"第 {slot} 順位：{draft_team(team)}"
        messages.append((content, draft_embed(year, title, [move_text(team, slot, before)], 0x7B1FA2)))
    lines = []
    for slot in range(1, len(order) + 1):
        team = order[slot - 1]
        move = ""
        if before and team in before:
            was = before.index(team) + 1
            move = f"（⬆️ 原第 {was}）" if was > slot else f"（⬇️ 原第 {was}）" if was < slot else ""
        lines.append(f"{'🏆 狀元' if slot == 1 else f'第 {slot} 順位'}：{draft_team(team)}{move}")
    messages.append(("📋 **NBA 樂透抽籤結果**", draft_embed(year, f"{year} NBA 選秀樂透抽籤結果", lines, 0x7B1FA2)))
    return messages


def pick_message(year: int, p: dict, positions: dict) -> tuple[str, discord.Embed]:
    """選秀：每選一位發一則"""
    a = p.get("athlete") or {}
    pos = positions.get(str((a.get("position") or {}).get("id")), "")
    school = a.get("team") or {}
    lines = [f"第 {p.get('round')} 輪第 {p.get('pick')} 順位"]
    if pos:
        lines.append(f"位置：{POS_ZH.get(pos, pos)}")
    if school.get("location") or school.get("shortDisplayName"):
        lines.append(f"來自：{school.get('location') or school.get('shortDisplayName')}")
    if a.get("displayHeight"):
        lines.append(f"身高 {a['displayHeight']}" + (f"｜體重 {a['displayWeight']}" if a.get("displayWeight") else ""))
    note = trade_note(p)
    if note:
        lines.append(f"選秀權：{note.strip('（）')}")
    embed = draft_embed(year, f"第 {p.get('overall')} 順位｜{draft_team(p.get('teamId'))} 選中 {a.get('displayName', '')}",
                        lines)
    if a.get("link"):
        embed.url = a["link"]
    if (a.get("headshot") or {}).get("href"):
        embed.set_thumbnail(url=a["headshot"]["href"])
    return "🏀 **NBA 選秀**", embed


def draft_results(year: int, picks: list[dict], positions: dict) -> list:
    """選秀結束：每一輪一則完整名單（含選秀當天的交易）"""
    messages = []
    for rnd in sorted({p.get("round") for p in picks}):
        lines = [pick_line(p, positions) for p in picks if p.get("round") == rnd]
        messages.append((f"📋 **{year} NBA 選秀結果**" if not messages else "📋 **（續）**",
                         draft_embed(year, f"{year} NBA 選秀結果｜第 {rnd} 輪", lines)))
    return messages


def trade_updates(year: int, picks: list[dict], d: dict) -> list:
    """選秀權換隊（還沒選）或選完馬上被交易（ESPN 註記 Traded to XXX）"""
    owners, notes = d.setdefault("owners", {}), d.setdefault("notes", {})
    messages = []
    for p in picks:
        key, team, note = str(p.get("overall")), str(p.get("teamId")), p.get("tradeNote") or ""
        old_team, old_note = owners.get(key), notes.get(key)
        # 抽籤前，前 14 順位的球隊會因抽籤換位置，不算交易
        watching = d.get("announced") and (d.get("lottery_done") or p.get("overall", 0) > LOTTERY_PICKS)
        if watching and old_team and old_team != team and not pick_made(p):
            messages.append(("🔄 **NBA 選秀權交易**", draft_embed(
                year, f"第 {key} 順位籤：{draft_team(old_team)} → {draft_team(team)}",
                [f"第 {p.get('round')} 輪第 {p.get('pick')} 順位"], 0xF57C00)))
        if pick_made(p) and old_note is not None and old_note != note and re.match(r"traded to", note, re.I):
            name = (p.get("athlete") or {}).get("displayName", "")
            messages.append(("🔄 **NBA 選秀後交易**", draft_embed(
                year, f"#{key} {name}：{draft_team(team)} 選中後交易至 {note[9:].strip()}",
                [f"第 {p.get('round')} 輪第 {p.get('pick')} 順位"], 0xF57C00)))
        owners[key] = team
        if pick_made(p):
            notes[key] = note
    return messages


async def draft_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    """選秀順位公布 → 樂透抽籤一隊一隊揭曉 → 抽籤總整理 → 選秀每選一位發一則 → 交易 → 選秀結果"""
    year = draft_year()
    d = state["draft"]
    if d.get("year") != year:  # 換年度：重新開始記錄
        d.clear()
        d["year"] = year
    now = datetime.now(ET)
    if now.month not in DRAFT_BUSY_MONTHS and time.time() - d.get("checked", 0) < 3600:
        return []
    d["checked"] = time.time()
    data = await fetch_draft(session, year)
    if data is None:
        return []
    picks = sorted(data["picks"], key=lambda p: p.get("overall") or 0)
    positions = {str(p.get("id")): p.get("abbreviation", "") for p in data.get("positions") or []}
    order = [str(p.get("teamId")) for p in picks[:LOTTERY_PICKS]]
    started = any(pick_made(p) for p in picks)
    # 例行賽 4 月中結束 → 4/15~5/9 公布抽籤前順位；5/10 起是抽籤（通常 5 月中）
    pre_window = (now.month == 4 and now.day >= 15) or (now.month == 5 and now.day < 10)
    lottery_window = (now.month == 5 and now.day >= 10) or now.month == 6
    messages = []

    if started:  # 選秀開始後就不再發順位和抽籤
        d["announced"] = d["lottery_done"] = True
    if not d.get("announced") and pre_window and all(order):
        messages.append(order_message(year, picks))
        d["announced"] = True
        d["order"] = order  # 抽籤前的順位，之後拿來比對升降
    if not d.get("lottery_done") and lottery_window and all(order):
        before = d.get("order") if d.get("announced") else None
        reordered = bool(before) and sorted(before) == sorted(order) and before != order
        unchanged_late = bool(before) and before == order and now.month == 5 and now.day >= 20  # 抽完都沒換位置
        if reordered or unchanged_late or not before:
            messages += lottery_reveal(year, order, before)
            d["announced"] = d["lottery_done"] = True
            for p in picks[:LOTTERY_PICKS]:  # 抽籤造成的換位置不算交易
                d.setdefault("owners", {})[str(p.get("overall"))] = str(p.get("teamId"))
    if not d.get("announced"):
        d["order"] = order  # 還沒公布前，順位會隨戰績變動，持續更新

    messages += trade_updates(year, picks, d)

    posted = set(d.get("picks", []))
    new = [p for p in picks if pick_made(p) and p.get("overall") not in posted]
    if new:
        d["live_until"] = time.time() + DRAFT_LIVE_SECONDS
    d["picks"] = sorted(posted | {p.get("overall") for p in new})
    messages += [pick_message(year, p, positions) for p in new]

    if not d.get("finished") and picks and all(pick_made(p) for p in picks):
        d["finished"] = True
        messages += draft_results(year, picks, positions)
    return [] if initial else messages


async def collect_updates(session: aiohttp.ClientSession) -> tuple[list, dict]:
    """回傳 (要發送的 [(文字, embed)], 更新後的狀態)；呼叫端發完訊息後要 save_state"""
    state = load_state() or {}
    await refresh_teams(session)
    messages = []
    for key, name, check, empty in (
            ("games", "比分", game_updates, {}),
            ("tx", "異動", tx_updates, []),
            ("news", "新聞", news_updates, []),
            ("shams", "Shams 新聞", shams_updates, []),
            ("awards", "獎項", award_updates, []),
            ("honors", "每週/每月獎項", honor_updates, []),
            ("draft", "選秀", draft_updates, {})):
        # 第一次抓到某個來源時只記錄現有資料，不發文，避免洗版
        initial = key not in state
        state.setdefault(key, empty)
        try:
            messages += await check(session, state, initial)
        except Exception as e:
            print(f"⚠️ 抓取{name}失敗：{e}")
            if initial:
                del state[key]  # 下次成功時再初始化
            continue
        if initial:
            print(f"初始化完成，已記錄 {len(state[key])} 筆{name}")
    return messages, state


# ===== Discord 機器人 =====
class NBABot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.session = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.tree.sync()
        self.check_updates.change_interval(seconds=CHECK_SECONDS)
        self.check_updates.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        print(f"✅ 已登入：{self.user}，每 {CHECK_SECONDS:g} 秒檢查一次 NBA 比分、異動和新聞")

    @tasks.loop(seconds=60)
    async def check_updates(self):
        messages, state = await collect_updates(self.session)
        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            print("⚠️ 找不到頻道，請確認 CHANNEL_ID 和機器人權限")
            return  # 不存狀態，下次再試
        for content, embed in messages:
            try:
                await channel.send(content=content, embed=embed)
                print(f"已發送：{content} {embed.title}")
            except discord.HTTPException as e:
                print(f"⚠️ 發送失敗：{e}")
        save_state(state)

    @check_updates.before_loop
    async def before_check(self):
        await self.wait_until_ready()


bot = NBABot()


def scoreboard_line(game: dict) -> str:
    away, home = side(game, "away"), side(game, "home")
    status, _ = game_status(game)
    if status == "pre":
        start = parse_time(game.get("date")).astimezone(TW)
        return (f"🕒 {team_label(away['team'])} @ {team_label(home['team'])}"
                f"｜台灣時間 {start:%m/%d %H:%M}")
    if status == "post":
        return f"🏁 {final_line(game)}" + (f"｜{zh(status_detail(game))}" if is_called_off(game) else "")
    return f"🔴 {final_line(game)}｜{clock_text(game)}"


@bot.tree.command(name="score", description="查看今天的 NBA 比分")
async def score_command(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        games = await fetch_games(bot.session)
    except Exception as e:
        await interaction.followup.send(f"⚠️ 抓取比分失敗：{e}")
        return
    today = str(datetime.now(ET).date())
    games = [g for g in games if g["_date"] == today]
    if not games:
        await interaction.followup.send("今天（美國時間）沒有 NBA 比賽。")
        return
    await prefetch(bot.session, [status_detail(g) for g in games if is_called_off(g)])
    embed = discord.Embed(title=f"NBA 比分｜{today}",
                          description="\n".join(scoreboard_line(g) for g in games)[:4000],
                          color=0x1D428A)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="news", description="查看最近的 NBA 重大新聞（不含 Shams）")
@app_commands.describe(count="要顯示幾則（1-5）")
async def news_command(interaction: discord.Interaction,
                       count: app_commands.Range[int, 1, 5] = 3):
    await interaction.response.defer()
    try:
        articles = [a for a in await fetch_news(bot.session) if wanted_news(a)]
    except Exception as e:
        await interaction.followup.send(f"⚠️ 抓取 ESPN 新聞失敗：{e}")
        return
    if not articles:
        await interaction.followup.send("ESPN 最近 50 則新聞裡沒有重大消息。")
        return
    articles = await zh_articles(bot.session, articles[:count])
    await interaction.followup.send(embeds=[news_embed(a) for a in articles])


@bot.tree.command(name="shams", description="查看最近的 Shams 新聞")
@app_commands.describe(count="要顯示幾則（1-5）")
async def shams_command(interaction: discord.Interaction,
                        count: app_commands.Range[int, 1, 5] = 3):
    await interaction.response.defer()
    try:
        articles = [a for a in await fetch_news(bot.session) if is_shams(a)]
    except Exception as e:
        await interaction.followup.send(f"⚠️ 抓取 ESPN 新聞失敗：{e}")
        return
    if not articles:
        await interaction.followup.send("ESPN 最近 50 則新聞裡沒有 Shams 的消息。")
        return
    articles = await zh_articles(bot.session, articles[:count])
    await interaction.followup.send(embeds=[news_embed(a) for a in articles])


@bot.tree.command(name="moves", description="查看最近的 NBA 簽約、交易")
@app_commands.describe(days="往回查幾天（1-14）")
async def moves_command(interaction: discord.Interaction,
                        days: app_commands.Range[int, 1, 14] = 3):
    await interaction.response.defer()
    try:
        txs = [t for t in await fetch_transactions(bot.session, days) if wanted_tx(t)]
    except Exception as e:
        await interaction.followup.send(f"⚠️ 抓取 NBA 異動失敗：{e}")
        return
    if not txs:
        await interaction.followup.send(f"最近 {days} 天沒有簽約或交易。")
        return
    txs = txs[-40:]  # 最多翻譯最近 40 筆，embed 也放不下更多
    await asyncio.gather(*(translate_tx(bot.session, t) for t in txs))
    lines = [f"{TX_KINDS.get(tx_kind(t), ('•',))[0]}｜{(t.get('date') or '')[:10]}｜"
             f"{team_name(t.get('team', {}).get('id')) or team_abbr(t.get('team', {}))}："
             f"{zh(t.get('description', ''))}" for t in reversed(txs)]
    text = ""
    for line in lines:  # 新的在上面，塞到 embed 上限為止
        if len(text) + len(line) > 3900:
            break
        text += line + "\n"
    embed = discord.Embed(title=f"NBA 簽約 / 交易（最近 {days} 天）", description=text, color=0x1E88E5)
    await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    missing = [k for k, v in {"DISCORD_TOKEN": DISCORD_TOKEN, "CHANNEL_ID": CHANNEL_ID}.items()
               if not v]
    if missing:
        raise SystemExit(f"請先在 .env 設定：{', '.join(missing)}（CHANNEL_ID 必須是數字）")
    bot.run(DISCORD_TOKEN)
