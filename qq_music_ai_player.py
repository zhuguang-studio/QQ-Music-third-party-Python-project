#!/usr/bin/env python3
"""
QQ音乐AI播放器 - 基于 DeepSeek AI 的自然语言音乐控制wdf-1.3.1
======================================================
通过自然语言指令控制 QQ 音乐播放，AI 自动理解用户意图并执行相应操作。

依赖安装：
    pip install openai qqmusic-api-python python-mpv

运行方式：
    python qq_music_ai_player.py

环境变量：
    DEEPSEEK_API_KEY    DeepSeek API 密钥（必须）
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ============================================================
# 第三方库导入检查
# ============================================================

MISSING_DEPS: List[str] = []

try:
    from openai import AsyncOpenAI
except ImportError:
    MISSING_DEPS.append("openai")

try:
    # qqmusic-api-python 库：主类名为 Client
    from qqmusic_api import Client as QQMusicClientLib, Credential as QQCredential
    from qqmusic_api.modules.song import SongFileInfo, SongFileType
except ImportError:
    MISSING_DEPS.append("qqmusic-api-python")

# 尝试导入 python-mpv，如果失败则使用 subprocess 方案
# 注意：python-mpv 即使 pip 安装成功，也可能因为找不到 mpv 原生 DLL 而抛 OSError
try:
    import mpv as _mpv_lib  # type: ignore
    HAS_MPV_LIB = True
except (ImportError, OSError):
    HAS_MPV_LIB = False

if MISSING_DEPS:
    print(f"缺少依赖库: {', '.join(MISSING_DEPS)}")
    print("请运行: pip install openai qqmusic-api-python python-mpv")
    sys.exit(1)


# ============================================================
# 配置常量
# ============================================================

# 凭证文件路径
CREDENTIAL_FILE = Path(__file__).parent / "credential.json"

# DeepSeek API 配置
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"  # DeepSeek 对话模型

# 系统提示词
SYSTEM_PROMPT = """你是QQ音乐播放器控制器。必须通过函数调用执行操作，禁止纯文本回复！

用户说"播放/放/听/来一首/任意"→ search_and_play
用户说"下载XXX"（单曲）→ search_and_download
用户说"下载XX的全部音乐/所有歌/批量下载XX"→ download_all_by_artist
用户说"下一首/换歌/切歌/第二首/播下一首"→ play_next
用户说"暂停"→ pause_playback
用户说"继续/开始播放"→ resume_playback
用户说"停止"→ stop_playback
用户说"推荐"→ recommend_songs

回复最多5个字。"""


# ============================================================
# 工具类：凭证管理
# ============================================================

class CredentialManager:
    """管理 QQ 音乐登录凭证的持久化存储。使用 qqmusic_api 的 Credential 模型。"""
    def __init__(self, filepath: Path = CREDENTIAL_FILE) -> None:
        self._filepath = filepath

    def save(self, credential: Any) -> None:
        """
        保存登录凭证到文件。
        credential: qqmusic_api.Credential 实例
        """
        data = credential.model_dump()
        # 将非序列化字段移除
        data["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._filepath.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"[凭证] 已保存到 {self._filepath}")

    def load(self) -> Optional[Any]:
        """从文件加载登录凭证，返回 Credential 对象或 None。"""
        if not self._filepath.exists():
            return None
        try:
            data = json.loads(self._filepath.read_text(encoding="utf-8"))
            if "musicid" in data and "musickey" in data:
                return QQCredential.model_validate(data)
        except Exception as e:
            print(f"[凭证] 加载失败: {e}")
        return None

    def clear(self) -> None:
        """清除凭证文件。"""
        if self._filepath.exists():
            self._filepath.unlink()
            print("[凭证] 已清除")


# ============================================================
# 核心类：QQMusicClient
# ============================================================

class QQMusicClient:
    """
    封装 qqmusic-api-python 库（Client），提供搜索、获取播放链接、歌单管理等功能。
    支持二维码扫码登录与会话复用。
    """

    def __init__(self) -> None:
        self._api: Any = None           # qqmusic_api.Client 实例
        self._cred_mgr = CredentialManager()
        self._logged_in = False

    # ---------- 登录相关 ----------

    async def login(self) -> bool:
        """
        登录 QQ 音乐。优先尝试从 credential.json 恢复会话，
        如果失败则启动手机验证码登录流程。
        """
        # 1. 创建 Client 实例
        self._api = QQMusicClientLib()

        # 2. 尝试恢复已有凭证
        cred = self._cred_mgr.load()
        if cred is not None:
            print("[登录] 发现已保存的凭证，尝试恢复会话...")
            try:
                # 直接设置 credential 到客户端
                self._api.credential = cred
                # 检查是否过期
                if hasattr(self._api.login, "check_expired"):
                    is_expired = await self._api.login.check_expired()
                    if is_expired:
                        print("[登录] 凭证已过期，尝试刷新...")
                        new_cred = await self._api.login.refresh_credential()
                        self._api.credential = new_cred
                        self._cred_mgr.save(new_cred)
                self._logged_in = True
                print("[登录] 会话恢复成功！")
                return True
            except Exception as e:
                print(f"[登录] 会话恢复失败: {e}")
                self._cred_mgr.clear()
                self._api = QQMusicClientLib()

        # 3. 手机验证码登录
        return await self._phone_login()

    async def _phone_login(self) -> bool:
        """通过手机号 + 短信验证码登录 QQ 音乐。"""
        import re
        from qqmusic_api.models.login import PhoneLoginEvents

        loop = asyncio.get_event_loop()

        # ---------- 输入手机号 ----------
        for attempt in range(3):
            phone_str = (await loop.run_in_executor(
                None, lambda: input("[登录] 请输入手机号: ").strip()
            )).strip()

            if re.match(r"^1[3-9]\d{9}$", phone_str):
                phone = int(phone_str)  # ⚠️ 必须传 int，str 会被当作加密号
                break
            print("[登录] ❌ 手机号格式不正确，请输入 11 位中国大陆手机号。")
        else:
            print("[登录] 手机号输入次数过多，登录取消。")
            return False

        # ---------- 发送验证码 ----------
        for send_attempt in range(3):
            print(f"[登录] 正在向 {phone_str} 发送验证码...")
            try:
                result = await self._api.login.send_authcode(phone, country_code=86)

                if result.event == PhoneLoginEvents.CAPTCHA:
                    captcha_url = result.info
                    print(f"[登录] ⚠️ 需要完成图形验证码，正在用浏览器打开...")
                    print(f"       如需手动打开: {captcha_url}")
                    self._open_url(captcha_url)
                    input("[登录] 在浏览器中完成验证后，按 Enter 重试发送...")
                    continue  # 重试发送
                elif result.event == PhoneLoginEvents.FREQUENCY:
                    print("[登录] ❌ 操作过于频繁，请等 60 秒后再试。")
                    await asyncio.sleep(10)
                    continue
                elif result.event == PhoneLoginEvents.SEND:
                    print(f"[登录] 📩 验证码已发送，请注意查收短信。")
                    break  # 成功，跳出循环
                else:
                    print(f"[登录] ❌ 发送验证码失败 (event={result.event})")
                    return False
            except Exception as e:
                print(f"[登录] ❌ 发送验证码失败: {e}")
                return False
        else:
            print("[登录] 验证码发送失败，请稍后重试。")
            return False

        # ---------- 输入验证码 ----------
        for attempt in range(5):
            auth_code = (await loop.run_in_executor(
                None, lambda: input("[登录] 请输入短信验证码: ").strip()
            )).strip()

            if not auth_code.isdigit() or len(auth_code) < 4:
                print("[登录] ❌ 验证码格式不正确，请输入纯数字验证码。")
                continue

            print("[登录] 正在验证...")
            try:
                credential = await self._api.login.phone_authorize(phone, auth_code)
                if credential is not None and credential.musicid != 0:
                    self._api.credential = credential
                    self._cred_mgr.save(credential)
                    self._logged_in = True
                    print("[登录] 🎉 登录成功！")
                    return True
                else:
                    print("[登录] ❌ 验证失败，请检查验证码是否正确。")
            except Exception as e:
                print(f"[登录] ❌ 验证失败: {e}")
                if attempt < 4:
                    print(f"[登录] 还剩 {4 - attempt} 次尝试机会。")
        else:
            print("[登录] 验证码输入次数过多，登录取消。")
            return False

        return False

    @staticmethod
    def _open_url(url: str) -> None:
        """尝试用系统默认浏览器打开 URL。"""
        try:
            if sys.platform == "win32":
                os.startfile(url)
            elif sys.platform == "darwin":
                subprocess.run(["open", url], check=False)
            else:
                subprocess.run(["xdg-open", url], check=False)
        except Exception:
            pass

    # ---------- 搜索 ----------

    async def search_song(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        搜索歌曲（无需登录也可搜索）。
        返回: [{"id": "song_mid", "name": "歌曲名", "artist": "歌手名", "album": "专辑名"}, ...]
        """
        try:
            result = await self._api.search.general_search(keyword, num=limit)
            songs = result.song.items  # 歌曲列表

            parsed: List[Dict[str, Any]] = []
            for s in songs[:limit]:
                # 提取歌手名
                artist = ""
                if s.singer:
                    artist = " / ".join(sg.name for sg in s.singer if sg.name)

                parsed.append({
                    "id": s.mid or str(s.id),  # mid 用于获取播放链接
                    "mid": s.mid,
                    "name": s.name or s.title or "未知",
                    "artist": artist or "未知",
                    "album": getattr(s.album, "name", "") if s.album else "",
                })
            return parsed
        except Exception as e:
            print(f"[搜索] 搜索失败: {e}")
            return []

    # ---------- 获取播放链接 ----------

    async def get_song_url(self, song_mid: str) -> Optional[str]:
        """
        根据歌曲 mid 获取可播放的 URL。
        返回播放链接字符串，失败返回 None。
        """
        try:
            fi = SongFileInfo(mid=song_mid, file_type=SongFileType.MP3_128)
            # ⚠️ 必须显式传 credential，否则服务端不返回真实播放链接
            result = await self._api.song.get_song_urls(
                [fi], credential=self._api.credential
            )

            if result.data and len(result.data) > 0:
                item = result.data[0]
                if item.purl and item.purl.strip():
                    # purl 格式如 "M500xxxx.mp3?guid=...&vkey=...&uin=..."
                    full_url = f"https://isure6.stream.qqmusic.qq.com/{item.purl}"
                    return full_url
                else:
                    print(f"[播放链接] 无法获取 '{song_mid}' 的播放链接 (result={item.result})")
                    return None
            return None
        except Exception as e:
            print(f"[播放链接] 获取失败: {e}")
            return None

    # ---------- 下载 ----------

    @staticmethod
    def _safe_filename(name: str, fallback: str = "unknown") -> str:
        """把歌名转成合法文件名（去掉 Windows 非法字符）。"""
        safe = "".join(c for c in (name or "") if c not in r'\/:*?"<>|').strip()
        # 去掉结尾的点/空格（Windows 不允许）
        safe = safe.rstrip(". ")
        return safe[:120] or fallback  # 限制长度，避免路径过长

    @staticmethod
    def _existing_file(save_dir: Path, safe_name: str) -> Optional[Path]:
        """检查该歌曲是否已下载过（任意音质后缀）。"""
        for p in save_dir.glob(f"{safe_name}.*"):
            if p.is_file() and p.stat().st_size > 1024:  # >1KB 才算有效文件
                return p
        return None

    async def download_song(self, song_mid: str, song_name: str = "",
                            save_dir: Optional[Path] = None,
                            quality: Any = None,
                            skip_existing: bool = False) -> Optional[Path]:
        """
        下载歌曲到本地，自动尝试从高到低音质。
        quality: None=自动最高, SongFileType.xxx 指定音质
        skip_existing: True 时若已存在同名文件则跳过
        """
        if save_dir is None:
            save_dir = Path(__file__).parent / "downloads"
        save_dir.mkdir(parents=True, exist_ok=True)

        safe_name = self._safe_filename(song_name, song_mid)

        # 已下载过则跳过
        if skip_existing:
            existed = self._existing_file(save_dir, safe_name)
            if existed:
                print(f"[跳过] 已存在: {existed.name}")
                return existed

        # 音质优先级：用户指定 > MASTER > ATMOS_2 > FLAC > MP3_320 > MP3_128
        quality_order = [
            (SongFileType.MASTER, "臻品母带"),
            (SongFileType.ATMOS_2, "臻品全景声"),
            (SongFileType.FLAC, "无损FLAC"),
            (SongFileType.MP3_320, "MP3 320k"),
            (SongFileType.MP3_128, "MP3 128k"),
        ]

        if quality is not None:
            quality_order = [(quality, "指定音质")] + quality_order

        last_error = ""
        for q, q_name in quality_order:
            try:
                fi = SongFileInfo(mid=song_mid, file_type=q)
                result = await self._api.song.get_song_urls(
                    [fi], credential=self._api.credential
                )
                if not result.data or not result.data[0].purl:
                    continue  # 该音质不可用，试下一个

                purl = result.data[0].purl.strip()
                url = f"https://isure6.stream.qqmusic.qq.com/{purl}"

                ext = Path(purl.split("?")[0]).suffix or ".mp3"
                filepath = save_dir / f"{safe_name}{ext}"

                print(f"[下载] {q_name} | {safe_name}{ext} ...")
                await self._download_file(url, filepath)

                size_mb = filepath.stat().st_size / (1024 * 1024)
                print(f"[下载] ✅ {q_name} | {filepath} ({size_mb:.1f} MB)")
                return filepath

            except Exception as e:
                last_error = str(e)
                continue

        print(f"[下载] 所有音质均失败: {last_error}" if last_error else "[下载] 无可用音质")
        return None

    @staticmethod
    async def _download_file(url: str, filepath: Path) -> None:
        """异步下载文件，带进度条。"""
        import httpx
        from tqdm import tqdm

        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length", 0))

                with tqdm(
                    total=total, unit="B", unit_scale=True, unit_divisor=1024,
                    desc=f"  📥 {filepath.name}", ncols=80, leave=False,
                ) as pbar:
                    with open(filepath, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            f.write(chunk)
                            pbar.update(len(chunk))

    # ---------- 歌手 ----------

    async def find_singer_mid(self, keyword: str) -> Optional[Dict[str, str]]:
        """
        按关键词查找歌手的 mid。
        返回 {"mid": ..., "name": ...}，找不到返回 None。
        """
        try:
            result = await self._api.search.general_search(keyword, num=30)
            kw = keyword.lower().strip()
            fallback: Optional[Dict[str, str]] = None

            for s in result.song.items:
                for sg in (s.singer or []):
                    if not sg.name or not sg.mid:
                        continue
                    name_l = sg.name.lower()
                    if name_l == kw:                 # 完全匹配，直接返回
                        return {"mid": sg.mid, "name": sg.name}
                    if kw in name_l and fallback is None:
                        fallback = {"mid": sg.mid, "name": sg.name}
            return fallback
        except Exception as e:
            print(f"[歌手] 查找失败: {e}")
            return None

    async def get_singer_songs(self, singer_mid: str,
                               max_songs: int = 100) -> List[Dict[str, Any]]:
        """
        获取某歌手的歌曲列表（自动翻页，按 mid 去重）。
        max_songs: 最多获取多少首
        """
        songs: List[Dict[str, Any]] = []
        seen = set()
        page = 1
        page_size = 30

        while len(songs) < max_songs:
            try:
                res = await self._api.singer.get_songs_list(
                    singer_mid, num=page_size, page=page
                )
            except Exception as e:
                print(f"[歌手] 第 {page} 页获取失败: {e}")
                break

            items = getattr(res, "song_list", None) or []
            if not items:
                break

            added = 0
            for s in items:
                mid = getattr(s, "mid", None)
                if not mid or mid in seen:
                    continue
                seen.add(mid)

                artist = ""
                for sg in (getattr(s, "singer", None) or []):
                    if getattr(sg, "name", None):
                        artist = f"{artist} / {sg.name}" if artist else sg.name

                songs.append({
                    "id": mid,
                    "mid": mid,
                    "name": getattr(s, "name", "") or getattr(s, "title", "未知"),
                    "artist": artist or "未知",
                    "album": getattr(getattr(s, "album", None), "name", "") or "",
                })
                added += 1

            total_num = getattr(res, "total_num", 0) or 0
            print(f"[歌手] 第 {page} 页: +{added} 首（累计 {len(songs)}/{total_num}）")

            if added == 0 or len(songs) >= total_num:
                break
            page += 1
            await asyncio.sleep(0.5)  # 翻页间隔，避免触发风控

        return songs[:max_songs]

    # ---------- 批量下载 ----------

    async def download_batch(self, songs: List[Dict[str, Any]],
                             save_dir: Optional[Path] = None,
                             skip_existing: bool = True,
                             delay: float = 1.2) -> Dict[str, Any]:
        """
        批量下载歌曲。
        songs: [{"id": mid, "name": ..., "artist": ...}, ...]
        skip_existing: 已存在同名文件则跳过
        delay: 每首之间的间隔秒数（避免请求过快被限流）
        返回统计: {"total","success","skipped","failed","failed_songs":[...]}
        """
        if save_dir is None:
            save_dir = Path(__file__).parent / "downloads"
        save_dir.mkdir(parents=True, exist_ok=True)

        # 按 mid 去重
        seen = set()
        unique: List[Dict[str, Any]] = []
        for s in songs:
            mid = s.get("id") or s.get("mid")
            if mid and mid not in seen:
                seen.add(mid)
                unique.append(s)

        stats: Dict[str, Any] = {
            "total": len(unique), "success": 0, "skipped": 0,
            "failed": 0, "failed_songs": [],
        }

        for i, song in enumerate(unique, 1):
            name = song.get("name", "未知")
            artist = song.get("artist", "")
            mid = song.get("id") or song.get("mid")

            print(f"\n──── [{i}/{stats['total']}] {name}"
                  + (f" - {artist}" if artist else ""))

            # 已下载过则跳过
            if skip_existing:
                safe = self._safe_filename(name, mid)
                existed = self._existing_file(save_dir, safe)
                if existed:
                    print(f"[跳过] 已下载: {existed.name}")
                    stats["skipped"] += 1
                    if i < stats["total"] and delay > 0:
                        await asyncio.sleep(0.05)
                    continue

            try:
                path = await self.download_song(
                    mid, name, save_dir=save_dir, skip_existing=skip_existing
                )
                if path:
                    stats["success"] += 1
                else:
                    stats["failed"] += 1
                    stats["failed_songs"].append(f"{name} - {artist}")
            except Exception as e:
                stats["failed"] += 1
                stats["failed_songs"].append(f"{name} - {artist}")
                print(f"[失败] {name}: {e}")

            # 间隔，避免请求过快
            if i < stats["total"] and delay > 0:
                await asyncio.sleep(delay)

        return stats

    # ---------- 歌单 ----------

    async def get_playlists(self) -> List[Dict[str, Any]]:
        """获取用户歌单列表（需要登录）。"""
        if not self._logged_in:
            print("[歌单] 需要登录才能获取歌单")
            return []

        try:
            # 获取用户创建的歌单
            uin = self._api.credential.encrypt_uin or self._api.credential.str_musicid
            if not uin:
                return []
            created = await self._api.user.get_created_songlist(int(uin))

            playlists: List[Dict[str, Any]] = []
            # 解析歌单列表
            if hasattr(created, 'items'):
                items = created.items
            elif hasattr(created, 'songlist'):
                items = created.songlist
            elif isinstance(created, list):
                items = created
            else:
                items = []

            for pl in (items or []):
                if isinstance(pl, dict):
                    playlists.append({
                        "id": pl.get("tid") or pl.get("id", ""),
                        "name": pl.get("name") or pl.get("title", "未命名"),
                        "count": pl.get("song_count") or pl.get("total", 0),
                    })
                elif hasattr(pl, 'tid'):
                    playlists.append({
                        "id": str(getattr(pl, 'tid', '')),
                        "name": getattr(pl, 'name', '未命名'),
                        "count": getattr(pl, 'song_count', 0),
                    })
            return playlists
        except Exception as e:
            print(f"[歌单] 获取失败: {e}")
            return []

    async def get_playlist_songs(self, playlist_id: str) -> List[Dict[str, Any]]:
        """获取指定歌单中的歌曲列表。"""
        try:
            detail = await self._api.songlist.get_detail(int(playlist_id))
            # 歌单详情中包含歌曲列表
            if hasattr(detail, 'songlist'):
                items = detail.songlist
            elif hasattr(detail, 'items'):
                items = detail.items
            else:
                return []

            songs: List[Dict[str, Any]] = []
            for s in (items or []):
                if hasattr(s, 'mid'):
                    songs.append({
                        "id": s.mid or str(getattr(s, 'id', '')),
                        "mid": s.mid,
                        "name": getattr(s, 'name', '') or getattr(s, 'title', '未知'),
                        "artist": "",
                        "album": "",
                    })
            return songs
        except Exception as e:
            print(f"[歌单歌曲] 获取失败: {e}")
            return []

    # ---------- 推荐 ----------

    async def get_recommendations(self) -> List[Dict[str, Any]]:
        """获取推荐歌曲。需要登录则尝试登录接口，否则用搜索热歌替代。"""
        # 方式 1：登录后的个性化推荐
        if self._logged_in:
            try:
                try:
                    result = await self._api.recommend.get_guess_recommend()
                except Exception:
                    result = await self._api.recommend.get_recommend_newsong()
                songs = self._parse_recommend_result(result)
                if songs:
                    return songs
            except Exception as e:
                print(f"[推荐] 个性化推荐失败: {e}，回退到热歌搜索...")

        # 方式 2：搜索热门歌曲作为推荐
        print("[推荐] 正在为你搜索热门歌曲...")
        return await self.search_song("热门歌曲", limit=15)

    def _parse_recommend_result(self, raw: Any) -> List[Dict[str, Any]]:
        """解析推荐结果。"""
        songs: List[Dict[str, Any]] = []
        items = []
        if hasattr(raw, 'items'):
            items = raw.items
        elif hasattr(raw, 'songlist'):
            items = raw.songlist
        elif isinstance(raw, list):
            items = raw

        for s in (items or [])[:20]:
            if hasattr(s, 'mid'):
                songs.append({
                    "id": s.mid or str(getattr(s, 'id', '')),
                    "mid": s.mid,
                    "name": getattr(s, 'name', '') or getattr(s, 'title', '未知'),
                    "artist": "",
                    "album": "",
                })
        return songs


# ============================================================
# 核心类：MPVPlayer
# ============================================================

class MPVPlayer:
    """
    封装 MPV 播放器控制逻辑。
    优先使用 python-mpv 库，若不可用则回退到 subprocess + JSON IPC。
    """

    def __init__(self) -> None:
        self._player: Any = None         # python-mpv 实例 或 subprocess.Popen
        self._use_lib = HAS_MPV_LIB      # 是否使用 python-mpv 库
        self._current_url: str = ""
        self._is_playing = False
        self._is_paused = False
        self._available = False          # 播放器是否可用

    # ---------- python-mpv 模式 ----------

    def _init_mpv_lib(self) -> None:
        """使用 python-mpv 库初始化播放器。"""
        # 在 Windows 上可能需要指定 mpv.dll 路径
        self._player = _mpv_lib.MPV(
            input_default_bindings=True,
            input_vo_keyboard=True,
            # 可选：指定 mpv 可执行文件路径
            # mpv_location="mpv",
        )
        # 注册事件回调
        @self._player.event_callback("end-file")
        def on_end_file(event: Any) -> None:
            self._is_playing = False
            print("[播放器] 播放结束")

    # ---------- subprocess 模式 ----------

    @staticmethod
    def _find_mpv() -> Optional[str]:
        """自动搜索 mpv 可执行文件路径。"""
        # 先检查 PATH
        import shutil
        path_mpv = shutil.which("mpv")
        if path_mpv:
            return path_mpv

        # 常见安装路径
        common_paths = [
            r"C:\Program Files\mpv\mpv.exe",
            r"C:\Program Files\MPV Player\mpv.exe",
            r"C:\Program Files (x86)\mpv\mpv.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\mpv\mpv.exe"),
            os.path.expandvars(r"%APPDATA%\mpv\mpv.exe"),
            r"D:\mpv\mpv.exe",
            # scoop 安装
            os.path.expandvars(r"%USERPROFILE%\scoop\apps\mpv\current\mpv.exe"),
            os.path.expandvars(r"%USERPROFILE%\scoop\shims\mpv.exe"),
        ]
        for p in common_paths:
            if Path(p).exists():
                return p
        return None

    def _init_mpv_subprocess(self) -> None:
        """使用 subprocess 启动 mpv，通过 JSON IPC 控制。"""
        mpv_path = self._find_mpv()
        if not mpv_path:
            raise FileNotFoundError(
                "未找到 mpv。请安装: winget install --id=shinchiro.mpv -e\n"
                "或从 https://mpv.io/installation/ 下载"
            )

        # 创建 IPC 管道
        if sys.platform == "win32":
            self._ipc_pipe = r"\\.\pipe\qqmusic-mpv-" + str(os.getpid())
        else:
            self._ipc_socket = tempfile.mktemp(suffix=".sock", prefix="mpv-")

        cmd = [mpv_path, "--idle=yes", "--no-terminal", f"--input-ipc-server={self._ipc_pipe}"]
        self._player = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)
        self._available = True
        print(f"[播放器] mpv 已启动 ({mpv_path})")

    # ---------- 公共接口 ----------

    async def start(self) -> bool:
        """
        初始化播放器。
        返回 True 表示播放器可用，False 表示不可用（未安装 mpv）。
        """
        self._available = False

        if self._use_lib:
            try:
                self._init_mpv_lib()
                self._available = True
                print("[播放器] 使用 python-mpv 库模式")
                return True
            except Exception as e:
                print(f"[播放器] python-mpv 初始化失败: {e}，尝试 subprocess 模式...")
                self._use_lib = False

        # subprocess 模式
        try:
            self._init_mpv_subprocess()
            return True
        except FileNotFoundError:
            print("[播放器] ⚠️ 未找到 mpv，无法播放音乐。")
            print("[播放器] 请安装 mpv 播放器: https://mpv.io/installation/")
            print("[播放器] 或使用 winget: winget install mpv")
            print("[播放器] 程序将在「仅搜索」模式运行，播放功能不可用。")
            self._available = False
            return False
        except Exception as e:
            print(f"[播放器] ⚠️ mpv 初始化失败: {e}")
            self._available = False
            return False

    async def play(self, url: str, title: str = "") -> None:
        """
        播放指定的 URL。
        url: 音频文件 URL
        title: 歌曲名称（用于显示）
        """
        if not self._available:
            print(f"[播放器] ⚠️ 播放器不可用，无法播放: {title or url}")
            return
        if not url:
            print("[播放器] URL 为空，无法播放")
            return

        self._current_url = url
        if title:
            print(f"[播放器] 正在播放: {title}")

        if self._use_lib:
            self._player.play(url)
            self._is_playing = True
            self._is_paused = False
        else:
            await self._ipc_command("loadfile", [url, "replace"])
            self._is_playing = True
            self._is_paused = False

    async def pause(self) -> None:
        """暂停播放。"""
        if not self._available or not self._is_playing or self._is_paused:
            return
        if self._use_lib:
            self._player.pause = True
        else:
            await self._ipc_command("set_property", ["pause", True])
        self._is_paused = True
        print("[播放器] 已暂停")

    async def resume(self) -> None:
        """继续播放。"""
        if not self._available or not self._is_paused:
            return
        if self._use_lib:
            self._player.pause = False
        else:
            await self._ipc_command("set_property", ["pause", False])
        self._is_paused = False
        self._is_playing = True
        print("[播放器] 继续播放")

    async def stop(self) -> None:
        """停止播放。"""
        if self._use_lib:
            self._player.stop()
        else:
            await self._ipc_command("stop")
        self._is_playing = False
        self._is_paused = False
        self._current_url = ""
        print("[播放器] 已停止")

    async def set_volume(self, volume: int) -> None:
        """
        设置音量。
        volume: 0-100 的整数
        """
        vol = max(0, min(100, volume))
        if self._use_lib:
            self._player.volume = vol
        else:
            await self._ipc_command("set_property", ["volume", vol])

    async def get_position(self) -> float:
        """获取当前播放位置（秒）。"""
        if not self._available:
            return 0.0
        try:
            if self._use_lib:
                return self._player.time_pos or 0.0
            else:
                result = await self._ipc_command("get_property", ["time-pos"])
                return float(result.get("data", 0) or 0)
        except Exception:
            return 0.0

    async def get_duration(self) -> float:
        """获取当前歌曲总时长（秒）。"""
        if not self._available:
            return 0.0
        try:
            if self._use_lib:
                return self._player.duration or 0.0
            else:
                result = await self._ipc_command("get_property", ["duration"])
                return float(result.get("data", 0) or 0)
        except Exception:
            return 0.0

    @property
    def available(self) -> bool:
        return self._available

    async def quit(self) -> None:
        """退出播放器。"""
        try:
            if self._use_lib:
                self._player.terminate()
            else:
                await self._ipc_command("quit")
                self._player.terminate()
                self._player.wait(timeout=5)
        except Exception:
            pass

    # ---------- IPC 辅助 ----------

    async def _ipc_command(self, command: str, args: Optional[List[Any]] = None) -> Any:
        """通过 JSON IPC 向 mpv 发送命令。"""
        if args is None:
            args = []
        payload = json.dumps({"command": [command] + args}) + "\n"

        try:
            if sys.platform == "win32":
                return await self._ipc_windows(payload)
            else:
                return await self._ipc_unix(payload)
        except Exception as e:
            print(f"[IPC] 命令发送失败: {e}")
        return None

    async def _ipc_windows(self, payload: str) -> Any:
        """Windows 命名管道 IPC。"""
        try:
            import win32pipe  # type: ignore
            import win32file  # type: ignore

            handle = win32file.CreateFile(
                self._ipc_pipe,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
            try:
                win32file.WriteFile(handle, payload.encode())
                # ReadFile 返回 (error_code, data)
                _hr, data = win32file.ReadFile(handle, 4096)
                if isinstance(data, bytes) and data.strip():
                    return json.loads(data.decode("utf-8"))
                return {}
            finally:
                win32file.CloseHandle(handle)
        except ImportError:
            return await self._ipc_fallback(payload)

    async def _ipc_unix(self, payload: str) -> Any:
        """Unix socket IPC。"""
        import socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(5)
            sock.connect(self._ipc_socket)
            sock.sendall(payload.encode())
            data = sock.recv(4096)
            return json.loads(data.decode("utf-8"))
        finally:
            sock.close()

    async def _ipc_fallback(self, payload: str) -> Any:
        """备用方式：通过 echo 向 mpv 命名管道发送命令。"""
        # 使用 PowerShell 或 cmd 的 echo 写入命名管道
        try:
            escaped = payload.replace('"', '\\"').strip()
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command",
                f'$pipe = new-object System.IO.Pipes.NamedPipeClientStream(".", "{self._ipc_pipe.lstrip(chr(92))}");'
                f'$pipe.Connect(2000); $writer = new-object System.IO.StreamWriter($pipe);'
                f'$writer.Write("{escaped}"); $writer.Flush(); $pipe.Close()',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception:
            pass
        return {}


# ============================================================
# 核心类：DeepSeekAgent
# ============================================================

class DeepSeekAgent:
    """
    与 DeepSeek API 通信，解析用户意图，映射到具体的函数调用。
    """

    def __init__(self, qq_client: QQMusicClient, player: MPVPlayer) -> None:
        if not DEEPSEEK_API_KEY:
            raise RuntimeError(
                "未设置 DEEPSEEK_API_KEY 环境变量。\n"
                "请在终端中执行: set DEEPSEEK_API_KEY=你的密钥  (Windows)\n"
                "或在终端中执行: export DEEPSEEK_API_KEY=你的密钥  (Linux/Mac)"
            )

        self._client = AsyncOpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
        )
        self._qq = qq_client
        self._player = player
        self._chat_history: List[Dict[str, Any]] = []
        self._search_cache: List[Dict[str, Any]] = []  # 缓存最近搜索结果
        self._playlist_cache: List[Dict[str, Any]] = []  # 缓存歌单列表
        self._play_index = 0  # 当前播放索引
        self._progress_task: Optional[asyncio.Task] = None  # 进度条后台任务

        # 定义可用工具（OpenAI 兼容格式）
        self._tools = self._build_tools()

    def _build_tools(self) -> List[Dict[str, Any]]:
        """构建 DeepSeek 函数调用工具定义。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_and_play",
                    "description": "搜索歌曲并立即播放第一首结果。用户说'播放/放/听'时用这个。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keyword": {
                                "type": "string",
                                "description": "搜索关键词，如'晴天 周杰伦'",
                            },
                        },
                        "required": ["keyword"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_and_download",
                    "description": "搜索歌曲并立即下载第一首结果（最高音质）。用户说'下载/下'时用这个。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keyword": {
                                "type": "string",
                                "description": "搜索关键词，如'稻香 周杰伦'",
                            },
                        },
                        "required": ["keyword"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "download_all_by_artist",
                    "description": (
                        "批量下载某歌手的全部歌曲。用户说'下载XX的全部音乐'、"
                        "'下载XX所有歌'、'批量下载XX'时用这个。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keyword": {
                                "type": "string",
                                "description": "歌手名或关键词，如'm3mo'、'周杰伦'",
                            },
                            "max_songs": {
                                "type": "integer",
                                "description": "最多下载多少首，默认 50",
                            },
                        },
                        "required": ["keyword"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "play_next",
                    "description": "播放下一首（从上次搜索结果中切换）。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "pause_playback",
                    "description": "暂停播放。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resume_playback",
                    "description": "继续播放。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "stop_playback",
                    "description": "停止播放。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "recommend_songs",
                    "description": "获取推荐歌曲并自动播放第一首。用户说'推荐'时用。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    # ---------- 对话 ----------

    @staticmethod
    def _extract_batch_keyword(text: str) -> Optional[str]:
        """
        从「下载m3mo的全部音乐」这类输入中提取歌手关键词。
        只有同时包含『下载』和『全部/所有/批量/all』时才认为是批量下载。
        返回 None 表示不是批量下载指令。
        """
        if "下载" not in text:
            return None
        if not any(w in text.lower() for w in ("全部", "所有", "批量", "都下", "all")):
            return None

        kw = text
        # 注意顺序：先去掉较长的短语，再去掉短的
        noise = [
            "批量下载", "下载", "帮我", "请",
            "的全部音乐", "的全部歌曲", "的所有音乐", "的所有歌曲",
            "全部音乐", "全部歌曲", "所有音乐", "所有歌曲",
            "的音乐", "的歌曲", "的歌", "音乐", "歌曲",
            "所有歌", "全部歌", "所有曲", "全部曲", "全部的歌",
            "的全部", "的所有", "全部", "所有", "批量", "都下",
            "all", "music", "songs", "song",
        ]
        for w in noise:
            kw = kw.replace(w, "")
        kw = kw.strip().strip("的").strip("，,。.！!？? ").strip()

        # 去掉残留的结尾量词（如「m3mo歌」→「m3mo」）
        for suffix in ("歌", "曲", "音乐", "歌曲"):
            if kw.endswith(suffix) and len(kw) > len(suffix):
                kw = kw[: -len(suffix)]
        kw = kw.strip().strip("的").strip()

        return kw or None

    async def chat(self, user_input: str) -> str:
        """
        处理用户输入，调用 DeepSeek API，执行相应操作。
        返回给用户展示的回复文本。
        """
        # ── 本地快速匹配：简单命令不经过 AI，避免 AI 偷懒 ──
        cmd = user_input.strip().lower()

        # 批量下载：「下载XX的全部/所有音乐」→ 本地直接触发，保证生效
        batch_kw = self._extract_batch_keyword(user_input.strip())
        if batch_kw:
            return await self._do_batch_download(batch_kw, 100)

        if cmd in ("next", "下一首", "下一曲", "换歌", "切歌", "换一首", "切一首") or ("下一首") in cmd:
            return await self._do_play_next()
        if cmd in ("pause", "暂停", "暂停播放") or ("暂停") in cmd:
            return await self._do_pause()
        if cmd in ("resume", "继续", "继续播放", "开始", "开始播放") or ("继续") in cmd:
            return await self._do_resume()
        if cmd in ("stop", "停止", "停止播放") or ("停止") in cmd:
            return await self._do_stop()

        # 添加用户消息
        self._chat_history.append({"role": "user", "content": user_input})

        # 保持对话历史在合理长度（更短以减少 AI 幻觉）
        if len(self._chat_history) > 6:
            self._chat_history = self._chat_history[-6:]

        try:
            # 调用 DeepSeek API（带函数调用）
            response = await self._client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    *self._chat_history,
                ],
                tools=self._tools,
                temperature=0.3,       # 降低温度，减少随机性
                max_tokens=512,
                tool_choice="auto",     # 让模型自动决定是否调用函数
            )

            message = response.choices[0].message

            # 检查是否有函数调用
            if message.tool_calls:
                reply = await self._handle_tool_calls(message.tool_calls, message.content)
                self._chat_history.append({"role": "assistant", "content": reply})
                return reply
            else:
                # 纯文本回复——可能 AI 又偷懒了，追加强制提示重试一次
                reply = message.content or ""
                if not reply or len(reply) < 3:
                    # 空回复，强制重试
                    return await self._force_function_call(user_input)
                self._chat_history.append({"role": "assistant", "content": reply})
                return reply

        except Exception as e:
            error_msg = f"与 AI 通信时出错: {e}"
            print(f"[Agent] {error_msg}")
            return f"抱歉，{error_msg}"

    async def _force_function_call(self, user_input: str) -> str:
        """当 AI 不调函数时，强制要求它调用。"""
        try:
            response = await self._client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": "你必须调用函数！禁止文字回复！"},
                    {"role": "user", "content": user_input},
                ],
                tools=self._tools,
                temperature=0.1,
                max_tokens=128,
                tool_choice="required",  # 强制调用函数
            )
            message = response.choices[0].message
            if message.tool_calls:
                reply = await self._handle_tool_calls(message.tool_calls, message.content)
                self._chat_history.append({"role": "assistant", "content": reply})
                return reply
        except Exception:
            pass
        return "请重新输入指令。"

    async def _handle_tool_calls(
        self, tool_calls: List[Any], text_content: Optional[str]
    ) -> str:
        """
        执行 DeepSeek 返回的函数调用，并将结果反馈给 AI。
        """
        results: List[str] = []

        for tc in tool_calls:
            func_name = tc.function.name
            try:
                func_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                func_args = {}

            print(f"[Agent] 调用函数: {func_name}({func_args})")

            # 执行对应函数
            if func_name == "search_and_play":
                result_text = await self._do_search_and_play(func_args.get("keyword", ""))
            elif func_name == "search_and_download":
                result_text = await self._do_search_and_download(func_args.get("keyword", ""))
            elif func_name == "download_all_by_artist":
                result_text = await self._do_batch_download(
                    func_args.get("keyword", ""),
                    int(func_args.get("max_songs") or 50),
                )
            elif func_name == "pause_playback":
                result_text = await self._do_pause()
            elif func_name == "resume_playback":
                result_text = await self._do_resume()
            elif func_name == "stop_playback":
                result_text = await self._do_stop()
            elif func_name == "get_playlists":
                result_text = await self._do_get_playlists()
            elif func_name == "play_playlist":
                result_text = await self._do_play_playlist(
                    func_args.get("playlist_id", ""),
                    func_args.get("playlist_name", ""),
                )
            elif func_name == "recommend_songs":
                result_text = await self._do_recommend()
            elif func_name == "play_next":
                result_text = await self._do_play_next()
            else:
                result_text = f"未知操作: {func_name}"

            results.append(result_text)

        # 合并结果直接返回，不再让 AI 总结（AI 容易只聊天不执行）
        return "\n".join(results) if results else "操作已完成。"

    # ---------- 具体操作实现 ----------

    async def _do_search_and_play(self, keyword: str) -> str:
        """搜索并立即播放第一首结果。"""
        if not keyword:
            return "请提供搜索关键词。"

        songs = await self._qq.search_song(keyword, limit=8)
        self._search_cache = songs
        self._play_index = 0

        if not songs:
            return f"没有找到与「{keyword}」相关的歌曲。"

        first = songs[0]
        url = await self._qq.get_song_url(first["id"])
        if url:
            await self._player.play(url, title=f"{first['name']} - {first['artist']}")
            self._start_progress()
            return f"正在播放: {first['name']} - {first['artist']}"
        else:
            return f"找到《{first['name']}》但暂时无法播放。"

    async def _do_search_and_download(self, keyword: str) -> str:
        """搜索并立即下载第一首结果。"""
        if not keyword:
            return "请提供搜索关键词。"

        songs = await self._qq.search_song(keyword, limit=8)
        self._search_cache = songs
        self._play_index = 0

        if not songs:
            return f"没有找到与「{keyword}」相关的歌曲。"

        first = songs[0]
        filepath = await self._qq.download_song(first["id"], first["name"])
        if filepath:
            return f"下载完成: {first['name']} - {first['artist']} → {filepath}"
        else:
            return f"《{first['name']}》下载失败，可能因版权限制。"

    async def _do_batch_download(self, keyword: str, max_songs: int = 50,
                                 save_dir: Optional[Path] = None) -> str:
        """
        批量下载某歌手的全部歌曲。
        优先按歌手精确获取；找不到歌手则回退到关键词搜索。
        """
        if not keyword:
            return "请提供歌手名或关键词。"

        keyword = keyword.strip()
        print(f"\n[批量下载] 正在查找「{keyword}」...")

        # 1. 尝试按歌手获取
        singer = await self._qq.find_singer_mid(keyword)
        artist_name = keyword
        songs: List[Dict[str, Any]] = []

        if singer:
            artist_name = singer["name"]
            print(f"[批量下载] 找到歌手: {artist_name} (mid={singer['mid']})")
            songs = await self._qq.get_singer_songs(singer["mid"], max_songs=max_songs)
        else:
            print(f"[批量下载] 未找到歌手「{keyword}」，改用搜索模式...")
            songs = await self._qq.search_song(keyword, limit=max_songs)

        if not songs:
            return f"没有找到「{keyword}」的歌曲。"

        # 2. 保存到 downloads/<歌手名>/ 子目录
        if save_dir is None:
            safe_artist = self._qq._safe_filename(artist_name, keyword)
            save_dir = Path(__file__).parent / "downloads" / safe_artist

        print(f"[批量下载] 共 {len(songs)} 首，保存到: {save_dir}")
        print("=" * 50)

        # 3. 执行批量下载
        stats = await self._qq.download_batch(songs, save_dir=save_dir)

        # 4. 汇总结果
        lines = [
            "=" * 50,
            f"🎉 批量下载完成「{artist_name}」",
            f"  总计    : {stats['total']} 首",
            f"  ✅ 成功  : {stats['success']}",
            f"  ⏭ 跳过  : {stats['skipped']}（已下载过）",
            f"  ❌ 失败  : {stats['failed']}",
            f"  📁 目录  : {save_dir}",
        ]
        if stats["failed_songs"]:
            preview = "、".join(stats["failed_songs"][:8])
            more = f" 等 {len(stats['failed_songs'])} 首" if len(stats["failed_songs"]) > 8 else ""
            lines.append(f"  失败列表: {preview}{more}")

        return "\n".join(lines)

    async def _do_play(self, song_id: str, song_name: str) -> str:
        """
        获取播放链接并播放歌曲。
        智能处理：如果 song_id 不是有效的 mid（含中文等），自动搜索。
        """
        # ── 智能修正：song_id 含中文则先搜索 ──
        if song_id and not song_id.isascii():
            # AI 把歌名当 ID 传了，用歌名搜索
            keyword = song_id
            print(f"[Agent] 检测到 ID 无效（'{song_id}'），自动搜索...")
            search_result = await self._do_search(keyword)
            if self._search_cache:
                song_id = self._search_cache[0]["id"]
                song_name = self._search_cache[0]["name"]
            else:
                return f"没有搜到「{keyword}」，无法播放。"

        # ── 空 ID：从缓存取第一首 ──
        if not song_id:
            if self._search_cache:
                song_id = self._search_cache[0]["id"]
                song_name = self._search_cache[0]["name"]
            elif song_name:
                # 有歌名没 ID，搜索
                await self._do_search(song_name)
                if self._search_cache:
                    song_id = self._search_cache[0]["id"]
                    song_name = self._search_cache[0]["name"]

        if not song_id:
            return "没有可播放的歌曲。请先搜索。"

        # ── 获取播放链接 ──
        url = await self._qq.get_song_url(song_id)
        if not url:
            return f"无法获取「{song_name}」的播放链接，可能因版权限制。"

        await self._player.play(url, title=song_name)
        self._start_progress()
        artist = ""
        for s in self._search_cache:
            if s["id"] == song_id:
                artist = s.get("artist", "")
                break
        info = f"{song_name}" + (f" - {artist}" if artist else "")
        return f"正在播放: {info}"

    async def _do_pause(self) -> str:
        """暂停播放。"""
        self._stop_progress()
        await self._player.pause()
        return "播放已暂停。"

    async def _do_resume(self) -> str:
        """继续播放。如果已停止则从头播放缓存第一首。"""
        if self._player._is_paused:
            await self._player.resume()
            self._start_progress()
            return "继续播放。"
        elif not self._player._is_playing and self._search_cache:
            # 已停止，重新播放缓存第一首
            song = self._search_cache[0]
            url = await self._qq.get_song_url(song["id"])
            if url:
                await self._player.play(url, title=f"{song['name']} - {song['artist']}")
                self._play_index = 0
                self._start_progress()
                return f"重新播放: {song['name']} - {song['artist']}"
        return "没有可播放的歌曲，请先搜索。"

    async def _do_stop(self) -> str:
        """停止播放。"""
        self._stop_progress()
        await self._player.stop()
        return "播放已停止。"

    async def _do_download(self, song_id: str, song_name: str) -> str:
        """下载歌曲到本地。智能处理无效 ID。"""
        # ── 智能修正 ──
        if song_id and not song_id.isascii():
            print(f"[Agent] 下载检测到无效 ID，自动搜索...")
            await self._do_search(song_id)
            if self._search_cache:
                song_id = self._search_cache[0]["id"]
                song_name = self._search_cache[0]["name"]

        if not song_id:
            if self._search_cache:
                song_id = self._search_cache[0]["id"]
                song_name = self._search_cache[0]["name"]
            elif song_name:
                await self._do_search(song_name)
                if self._search_cache:
                    song_id = self._search_cache[0]["id"]
                    song_name = self._search_cache[0]["name"]

        if not song_id:
            return "没有可下载的歌曲。请先搜索。"

        filepath = await self._qq.download_song(song_id, song_name)
        if filepath:
            return f"下载完成！已保存到: {filepath}"
        else:
            return f"下载「{song_name}」失败，可能因版权限制。"

    async def _do_get_playlists(self) -> str:
        """获取歌单列表。"""
        playlists = await self._qq.get_playlists()
        self._playlist_cache = playlists

        if not playlists:
            return "没有找到歌单。请确认已登录且有创建歌单。"

        lines = ["你的歌单："]
        for i, pl in enumerate(playlists, 1):
            lines.append(f"  {i}. {pl['name']} ({pl['count']}首) (ID: {pl['id']})")
        return "\n".join(lines)

    async def _do_play_playlist(self, playlist_id: str, playlist_name: str = "") -> str:
        """播放歌单中的歌曲。"""
        if not playlist_id:
            return "请指定要播放的歌单。"

        songs = await self._qq.get_playlist_songs(playlist_id)
        if not songs:
            return f"无法获取歌单歌曲列表。"

        # 缓存歌曲列表用于后续播放
        self._search_cache = songs

        # 播放第一首
        first_song = songs[0]
        url = await self._qq.get_song_url(first_song["id"])
        if url:
            await self._player.play(url, title=f"{first_song['name']} - {first_song['artist']}")
            self._start_progress()
            return f"正在播放歌单「{playlist_name or '未知'}」(共{len(songs)}首): {first_song['name']}"
        return "无法获取播放链接。"

    async def _do_recommend(self) -> str:
        """获取推荐歌曲，自动播放第一首。"""
        songs = await self._qq.get_recommendations()
        self._search_cache = songs
        self._play_index = 0

        if not songs:
            return "暂时无法获取推荐，请稍后再试。"

        # 自动播放第一首
        first = songs[0]
        url = await self._qq.get_song_url(first["id"])
        if url:
            await self._player.play(url, title=f"{first['name']} - {first['artist']}")
            self._start_progress()
            return f"正在播放推荐: {first['name']} - {first['artist']}（共 {len(songs)} 首，说'下一首'切歌）"
        else:
            return f"推荐列表已就绪（共 {len(songs)} 首），但第一首暂不可播放。"

    async def _do_play_next(self) -> str:
        """播放缓存中的下一首歌曲。"""
        if not self._search_cache:
            return "没有播放列表。请先搜索歌曲。"

        total = len(self._search_cache)
        self._play_index += 1
        if self._play_index >= total:
            self._play_index = 0  # 循环

        song = self._search_cache[self._play_index]
        url = await self._qq.get_song_url(song["id"])
        if url:
            await self._player.play(url, title=f"{song['name']} - {song['artist']}")
            self._start_progress()
            return f"第{self._play_index + 1}/{total}首: {song['name']} - {song['artist']}"
        else:
            # 当前首不可用，自动跳到下一首
            print(f"[Agent] 第{self._play_index + 1}首不可用，自动跳过...")
            return await self._do_play_next()

    # ---------- 进度条 ----------

    @staticmethod
    def _bar(percent: float, width: int = 30) -> str:
        """绘制文本进度条。"""
        filled = int(width * percent)
        bar_chars = "█" * filled + "░" * (width - filled)
        return f"│{bar_chars}│"

    @staticmethod
    def _fmt_time(sec: float) -> str:
        """格式化秒数为 m:ss。"""
        m, s = divmod(int(sec), 60)
        return f"{m}:{s:02d}"

    async def _show_playback_progress(self) -> None:
        """后台任务：每秒刷新播放进度条，歌曲播完自动切下一首。"""
        near_end_count = 0
        try:
            while self._player.available and self._player._is_playing:
                pos = await self._player.get_position()
                dur = await self._player.get_duration()

                if dur > 1 and pos > 0:  # 有效数据
                    pct = pos / dur
                    bar = self._bar(pct)
                    print(f"\r  🎵 {bar} {self._fmt_time(pos)}/{self._fmt_time(dur)}", end="", flush=True)

                    # 播放到 97% 以上，连续 2 秒 = 播完了
                    if pct > 0.97:
                        near_end_count += 1
                        if near_end_count >= 2:
                            print()
                            self._player._is_playing = False
                            await self._play_next_internal()
                            return
                    else:
                        near_end_count = 0
                else:
                    # 刚开始加载，位置/时长还没就绪
                    print(f"\r  🎵 加载中...", end="", flush=True)
                    near_end_count = 0

                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            print()

    async def _play_next_internal(self) -> None:
        """内部自动切歌（不经过 AI）。"""
        if not self._search_cache:
            return

        total = len(self._search_cache)
        self._play_index += 1
        if self._play_index >= total:
            self._play_index = 0

        song = self._search_cache[self._play_index]
        url = await self._qq.get_song_url(song["id"])
        if url:
            print(f"  ⏭ 自动切歌: {song['name']} - {song['artist']}")
            await self._player.play(url, title=f"{song['name']} - {song['artist']}")
            self._start_progress()
        else:
            # 跳过不可用的
            self._player._is_playing = True  # 临时标记以继续循环
            await self._play_next_internal()

    def _start_progress(self) -> None:
        """启动播放进度条后台任务。"""
        self._stop_progress()
        if self._player.available:
            self._progress_task = asyncio.create_task(self._show_playback_progress())

    def _stop_progress(self) -> None:
        """停止播放进度条后台任务。"""
        if self._progress_task and not self._progress_task.done():
            self._progress_task.cancel()
            self._progress_task = None

    # ---------- 重置对话 ----------

    def reset_conversation(self) -> None:
        """重置对话历史。"""
        self._chat_history.clear()
        self._search_cache.clear()
        print("[Agent] 对话历史已清空")


# ============================================================
# 主程序入口
# ============================================================

# 用于优雅退出的标志
_shutdown_flag = False


def _signal_handler(signum: int, frame: Any) -> None:
    """处理 Ctrl+C 信号。"""
    global _shutdown_flag
    _shutdown_flag = True
    print("\n正在退出...")


async def _print_banner() -> None:
    """打印欢迎横幅。"""
    banner = r"""
  ____   ____    __  __           _         
 / __ \ / __ \  |  \/  |         (_)        
| |  | | |  | | | \  / |_   _ ___ _ _______ 
| |  | | |  | | | |\/| | | | / __| |_  / _ \
| |__| | |__| | | |  | | |_| \__ \ |/ /  __/
 \___\_\\___\_\ |_|  |_|\__,_|___/_/___\___|
      AI-Powered Music Player
==============================================
"""
    print(banner)


async def _run_repl(agent: DeepSeekAgent) -> None:
    """运行交互式 REPL 主循环。"""
    print("输入你的音乐指令（如「播放周杰伦的晴天」「推荐轻音乐」「暂停」）")
    print("输入 /help 查看帮助, /quit 退出, /clear 清空对话")
    print("-" * 50)

    while not _shutdown_flag:
        try:
            # 使用 asyncio 等待用户输入
            user_input = await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("\n🎵 你: ").strip()
            )
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input:
            continue

        # 处理内置命令
        if user_input.startswith("/"):
            cmd = user_input[1:].lower()
            if cmd in ("quit", "exit", "q"):
                break
            elif cmd in ("help", "h", "?"):
                _show_help()
                continue
            elif cmd in ("clear", "c"):
                agent.reset_conversation()
                continue
            elif cmd == "login":
                print("正在重新登录...")
                continue
            else:
                print(f"未知命令: {user_input}，输入 /help 查看帮助")
                continue

        # 调用 Agent 处理
        print("🤖 AI: ", end="", flush=True)
        reply = await agent.chat(user_input)
        print(reply)


def _show_help() -> None:
    """显示帮助信息。"""
    help_text = """
可用命令:
  /help, /h, /?   显示此帮助
  /quit, /exit, /q 退出程序
  /clear, /c       清空对话历史
  /login           重新登录

自然语言示例:
  播放周杰伦的晴天
  下载晴天                          # 下载单曲
  下载m3mo的全部音乐                 # 批量下载歌手全部歌曲
  下载周杰伦的所有歌曲                # 同上
  推荐一些轻音乐
  暂停 / 继续 / 停止
  下一首 / 换歌 / 切歌
  我的歌单
  播放我的「最爱」歌单

批量下载说明:
  · 自动识别「下载XX的全部/所有音乐」并下载该歌手的全部歌曲
  · 文件保存到 downloads/<歌手名>/ 目录
  · 优先下载臻品母带音质，失败自动降级
  · 已下载过的歌曲会自动跳过，可重复运行续传
"""
    print(help_text)


async def main() -> None:
    """主函数：初始化各模块并启动交互循环。"""
    # 打印横幅
    await _print_banner()

    # 检查 API Key
    if not DEEPSEEK_API_KEY:
        print("[错误] 未设置 DEEPSEEK_API_KEY 环境变量！")
        print("  Windows: set DEEPSEEK_API_KEY=你的密钥")
        print("  Linux/Mac: export DEEPSEEK_API_KEY=你的密钥")
        return

    # 注册信号处理
    signal.signal(signal.SIGINT, _signal_handler)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _signal_handler)

    # 1. 初始化 QQ 音乐客户端并登录
    print("[初始化] 正在连接 QQ 音乐...")
    qq_client = QQMusicClient()
    login_ok = await qq_client.login()
    if not login_ok:
        print("[错误] QQ 音乐登录失败，程序退出。")
        return

    # 2. 初始化播放器
    print("[初始化] 正在启动播放器...")
    player = MPVPlayer()
    player_ok = await player.start()
    if not player_ok:
        print("[初始化] 播放器不可用，程序将在「仅搜索/推荐」模式运行。")

    # 3. 初始化 AI Agent
    print("[初始化] 正在连接 DeepSeek AI...")
    try:
        agent = DeepSeekAgent(qq_client, player)
    except RuntimeError as e:
        print(f"[错误] {e}")
        return

    print("[就绪] 所有模块初始化完成！")
    print()

    # 4. 进入交互循环
    try:
        await _run_repl(agent)
    finally:
        # 清理资源
        print("\n[退出] 正在清理资源...")
        await player.stop()
        await player.quit()
        print("再见！👋")


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="asyncio")

    asyncio.run(main())
