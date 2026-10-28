"""AstrBot 插件：角色自拍 / 风景照生成（阿里云百炼 Token Plan）。

核心特性是「锁脸」：
1. 先用 /自拍 生成几张候选，挑一张满意的；
2. /定脸 <编号> 把这张锁成角色的脸；
3. 之后所有自拍都会带上这张参考图 + 固定外貌描述，保证是同一个人。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

try:
    from astrbot.api.event import MessageChain
except ImportError:  # 兼容旧版导出路径
    from astrbot.core.message.message_chain import MessageChain

from .image_client import ImageError, TokenPlanImageClient

TOOL_SELFIE = "take_selfie"
TOOL_PHOTO = "take_photo"

DEFAULT_FACE = "一位年轻女性，黑色长发，琥珀色眼睛，皮肤白皙，五官清秀"

# 候选自拍轮流使用不同的拍摄角度，让几张候选有明显差异，方便挑选
SELFIE_ANGLES = (
    "手机前置摄像头自拍，近距离半身，自然光，真实照片质感",
    "对着镜子自拍，手机挡住下半张脸，室内暖光",
    "略微俯拍的近景自拍，微微仰头，柔和顶光",
    "侧脸自拍，逆光，发丝边缘泛光，浅景深",
    "户外随手自拍，背景轻微虚化，自然日光",
    "低头看镜头的特写自拍，顶光，情绪感强",
)

# ---- 真实感：手机前置的物理特征 ----
# AI 图之所以一眼假，是因为太完美、太居中、光影太均匀。
# 这几组片段负责把「手机随手拍」的缺陷和物理特性塞回去。
REAL_PHONE_SHOTS = (
    "手机前置摄像头近距自拍，24mm 广角，鼻子和额头因广角略被拉大，画面边缘有轻微形变",
    "手机前置自拍，微微仰角，下巴和脖子有自然阴影，边缘轻微拉伸",
    "手持自拍，一只手入镜拿着手机，构图有点歪，不在正中",
    "对着镜子自拍，手机挡住下半张脸，镜面有淡淡反光和一点指纹",
    "前置自拍，镜头离脸很近，一只眼睛略微失焦，另一只清晰",
    "稍微低角度的前置自拍，下巴线条显圆，肩膀有点耸",
    "轻微侧脸角度自拍，脸型看起来不太对称，一边脸颊更明显",
)

REAL_IMPERFECTIONS = (
    "皮肤有毛孔和细小瑕疵，鼻翼微微泛油光，不是磨皮效果",
    "头发有几缕不听话的碎发翘着，没有全部服帖",
    "脸颊有一点点不均匀的泛红，左右不对称",
    "眼睛下方有轻微的黑眼圈和眼袋",
    "嘴唇略干，能看到一点唇纹",
    "衣领有点皱，肩线不完全对称",
    "背景里有生活杂物：充电线、水杯、纸巾盒之类",
    "画面有轻微噪点，白平衡偏暖，不是影棚色温",
    "背光处有一点点过曝，高光溢出但没修",
    "右下角有一点镜头雾感，像镜头上沾了指纹",
    "背景虚化不彻底，能看出远处物体的轮廓",
    "构图不在正中，人物偏左或偏右，有一点歪",
)

REAL_SUFFIX = (
    "真实手机随手拍，非精修、非 AI 绘画，允许不完美："
    "不要塑料感皮肤、不要过度磨皮、不要完美对称的构图、不要影棚打光"
)

# ---- 真实感：环境 ----
# AI 生成的背景是「被设计过的」：白墙、一盆绿植、大面积柔焦。
# 真实背景是「被生活堆出来的」：插座、外卖盒、没叠的衣服、卷边的贴纸。
REAL_ENVIRONMENTS = (
    "出租屋的小卧室，床边堆着没叠的衣服，床头柜上有纸巾盒和半杯水",
    "老小区的厨房门口，灶台上放着刚洗的碗，墙上那张贴纸已经卷边了",
    "客厅沙发一角，茶几上摊着外卖盒、充电线和遥控器",
    "书桌前，桌上摊着笔记本、几支笔和一个吃了一半的橘子",
    "阳台门口，身后晾着衣服，地上有塑料盆和一双拖鞋",
    "便利店的货架前，身后是摆得整整齐齐的零食和饮料冰柜",
    "地铁车厢角落，背景是扶手杆和邻座乘客的半个背影",
    "奶茶店靠窗的座位，桌上有没喝完的杯子、揉皱的纸巾和手机",
    "宿舍上床下桌，身后挂着毛巾，书架上贴满便签",
    "卧室床边，衣柜门半开着，能看到里面塞得有点乱的衣架",
)

REAL_TRACES = (
    "背景里有没收拾的生活用品，摆放得很随意",
    "墙上有插座、开关或者贴着的东西，不是一面白墙",
    "桌面和台面上堆着杂物，不是干净摆拍",
    "角落里能看到垃圾桶、纸箱或者塑料袋",
    "有一两件东西明显没摆正，歪着",
)

REAL_LIGHTING = (
    "只有窗外一侧的光，脸的一侧亮一侧暗，背景靠里的角落是暗的",
    "头顶日光灯偏白，脸上光很平，下巴有硬阴影",
    "台灯的暖光和窗外进来的冷光混着，色温不统一",
    "手机屏幕的光打在脸上，背景明显更暗",
    "傍晚的光，整体偏橙，影子被拉得很长",
    "阴天的散射光，没有明显影子，但整体偏灰",
)

# 手机前置摄像头传感器小、又是广角，景深很大，背景其实不会糊。
# AI 图经常把人像模式的强虚化塞进来，这是「一眼 AI」的重要来源之一。
NO_PORTRAIT_BLUR = "背景清晰可辨，能看到具体的物件和空间关系，不要人像模式虚化、不要整片糊掉的散景"

# 风景 / 环境照用主摄视角
REAL_PHOTO_SHOTS = (
    "手机主摄随手拍，构图不刻意，地平线略微倾斜",
    "手持手机拍摄，有轻微的抖动模糊",
    "手机广角拍摄，画面边缘轻微畸变",
    "随手一拍，主体不在正中央，边上有杂物入镜",
    "隔着玻璃拍的，有一点反光和眩光",
)


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str):
    """从可能带解释或代码块的回复里抠出 JSON。"""
    if not text:
        return None
    m = _JSON_BLOCK.search(text)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:  # noqa: BLE001
                continue
    return None


def sniff_mime(raw: bytes) -> str:
    """按文件头判断图片 MIME，data URI 里用错了模型会拒绝收。"""
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG"):
        return "image/png"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"GIF8"):
        return "image/gif"
    if raw.startswith(b"BM"):
        return "image/bmp"
    return "image/png"


def unwrap_event(arg):
    """兼容新旧版本：工具入参可能是 AstrMessageEvent 或 ContextWrapper。"""
    if arg is None:
        return None
    if hasattr(arg, "get_session_id") or hasattr(arg, "unified_msg_origin"):
        return arg
    ctx = getattr(arg, "context", None)
    if ctx is not None:
        ev = getattr(ctx, "event", None)
        if ev is not None and hasattr(ev, "get_session_id"):
            return ev
        if hasattr(ctx, "get_session_id"):
            return ctx
    ev = getattr(arg, "event", None)
    if ev is not None and hasattr(ev, "get_session_id"):
        return ev
    return arg


class SelfiePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

        def cfg(key: str, default):
            value = self.config.get(key, default)
            return default if value is None else value

        self.api_key: str = str(cfg("api_key", "") or "")
        self.character_face: str = str(cfg("character_face", "") or "").strip() or DEFAULT_FACE
        self.selfie_style: str = str(cfg("selfie_style", "") or "").strip()
        self.candidate_count: int = int(cfg("candidate_count", 4) or 4)
        self.max_prompt_chars: int = int(cfg("max_prompt_chars", 120) or 120)
        self.cooldown: int = int(cfg("cooldown", 10) or 0)
        self.enable_tool: bool = bool(cfg("enable_tool", True))
        self.inject_prompt: bool = bool(cfg("inject_prompt", True))
        self.keep_candidates: int = int(cfg("keep_candidates", 60) or 60)
        self.face_from_persona: bool = bool(cfg("face_from_persona", True))
        self.appearance_hint: str = str(cfg("appearance_hint", "") or "").strip()
        # 真实感档位：0 关闭，1-4 表示每张图塞几条「不完美」细节
        self.realism: int = int(cfg("realism", 3) or 0)
        # 主动发图
        self.proactive_photo: bool = bool(cfg("proactive_photo", True))
        self.daily_limit: int = int(cfg("daily_limit", 1) or 1)
        self.quiet_hours: str = str(cfg("quiet_hours", "0-8") or "")
        self.active_hours: float = float(cfg("active_hours", 24) or 24)
        self.check_interval: int = int(cfg("check_interval_minutes", 20) or 20)

        self.client = TokenPlanImageClient(
            api_key=self.api_key,
            api_base=str(cfg("api_base", "https://token-plan.cn-beijing.maas.aliyuncs.com")),
            model_t2i=str(cfg("model_text2image", "wan2.7-image")),
            model_i2i=str(cfg("model_with_reference", "qwen-image-3.0-pro")),
            size=str(cfg("image_size", "1024*1024")),
            timeout=int(cfg("timeout", 180) or 180),
        )

        try:
            data_dir = Path(StarTools.get_data_dir())
        except Exception:  # noqa: BLE001
            data_dir = Path("data/plugin_data/astrbot_plugin_tokenplan_selfie")
        self.data_dir = data_dir
        self.face_dir = data_dir / "face"
        self.cand_dir = data_dir / "candidates"
        self.face_dir.mkdir(parents=True, exist_ok=True)
        self.cand_dir.mkdir(parents=True, exist_ok=True)

        self.state_path = data_dir / "state.json"
        self.state: dict = self._load_state()

        self._last_call: dict[str, float] = {}
        # 每轮 LLM 请求时缓存当前人格，供「人设 → 外貌描述」使用
        self._persona_cache: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self._busy = False

        locked = self._locked_image()
        logger.info(
            f"[Selfie] 已加载：文生图={self.client.model_t2i} "
            f"参考图={self.client.model_i2i} 尺寸={self.client.size} "
            f"真实感={self.realism} 锁脸={'已锁定' if locked else '未锁定'} "
            f"主动发图={'开' if self.proactive_photo else '关'} "
            f"工具={'开' if self.enable_tool else '关'}",
        )

    async def initialize(self) -> None:
        if self.proactive_photo:
            self._task = asyncio.create_task(self._proactive_loop())

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        await self.client.close()

    # ---------------- 会话状态 ----------------

    def _load_state(self) -> dict:
        default = {"sessions": {}, "photo_date": "", "photo_count": 0}
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k, v in default.items():
                        data.setdefault(k, v)
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Selfie] 读取状态失败: {exc}")
        return default

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Selfie] 保存状态失败: {exc}")

    def _session(self, umo: str) -> dict:
        return self.state["sessions"].setdefault(
            umo, {"enabled": True, "last_active": 0, "next_photo_at": 0}
        )

    def _touch(self, umo: str) -> None:
        try:
            sess = self._session(umo)
            now = time.time()
            if now - float(sess.get("last_active", 0)) < 60:
                return
            sess["last_active"] = now
            self._save_state()
        except Exception:  # noqa: BLE001
            pass

    def _in_quiet_hours(self) -> bool:
        raw = (self.quiet_hours or "").strip()
        if not raw or "-" not in raw:
            return False
        try:
            a, b = raw.split("-", 1)
            start, end = int(a), int(b)
        except ValueError:
            return False
        if start == end:
            return False
        hour = datetime.now().hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _count_today(self) -> int:
        return self.state.get("photo_count", 0) if self.state.get("photo_date") == self._today() else 0

    def _plan_next_photo(self) -> float:
        """在今天允许的时段里随机挑一个时刻，让主动发图不显得像定时任务。"""
        now = datetime.now()
        start_h = 8
        end_h = 23
        raw = (self.quiet_hours or "").strip()
        if raw and "-" in raw:
            try:
                a, b = raw.split("-", 1)
                start_h = max(int(b), 8) if int(a) == 0 else 8
                end_h = 23
            except ValueError:
                pass
        if now.hour >= end_h:
            # 今天窗口已过，安排到明天开场
            return (
                datetime(now.year, now.month, now.day) + timedelta(days=1)
            ).replace(hour=start_h, minute=random.randint(0, 40)).timestamp()

        lo = max(now.hour, start_h)
        target_h = random.randint(lo, end_h - 1)
        target_m = random.randint(0, 59)
        if target_h == now.hour and target_m <= now.minute:
            target_m = min(59, now.minute + random.randint(5, 40))
        return now.replace(hour=target_h, minute=target_m, second=0, microsecond=0).timestamp()

    # ---------------- 锁脸存储 ----------------

    def _locked_image(self) -> Path | None:
        for ext in ("png", "jpg", "jpeg", "webp", "bmp"):
            p = self.face_dir / f"locked.{ext}"
            if p.exists():
                return p
        return None

    def _meta_path(self) -> Path:
        return self.face_dir / "face.json"

    # ---------------- 外貌描述（人设 → 提示词） ----------------

    def _appearance_path(self) -> Path:
        return self.face_dir / "appearance.txt"

    def _load_appearance(self) -> str:
        p = self._appearance_path()
        try:
            return p.read_text(encoding="utf-8").strip() if p.exists() else ""
        except OSError:
            return ""

    def _save_appearance(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        try:
            self._appearance_path().write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            logger.warning(f"[Selfie] 保存外貌描述失败: {exc}")

    async def _summarize_appearance(self, persona: str) -> str:
        """让人设所在的大模型把角色人设总结成一段可用的生图外貌描述。"""
        try:
            provider = self.context.get_using_provider()
        except TypeError:
            try:
                provider = self.context.get_using_provider(None)
            except Exception:  # noqa: BLE001
                provider = None
        except Exception:  # noqa: BLE001
            provider = None

        if not provider:
            logger.warning("[Selfie] 没有可用的 LLM Provider，无法从人设生成外貌")
            return ""

        task = (
            "请只输出一段外貌描述，用于 AI 绘画提示词，描述这个人长什么样。\n"
            "必须包含：性别与年龄感、发型与发色、眼睛颜色与形状、脸型、肤色、体型，"
            "以及 1-2 个辨识度高的特征（例如泪痣、耳钉、眉形）。\n"
            "不要写性格、职业、身份、剧情；不要写衣服和场景；"
            "不要任何前缀、引号、解释或换行；只输出 40-80 字的连续中文描述。"
        )
        prompt = f"以下是这个角色的人设设定：\n{persona[:1500]}\n\n{task}" if persona else task
        if self.appearance_hint:
            prompt += f"\n补充要求：{self.appearance_hint}"

        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt=(
                    "你是一名专精人物设定的美术指导，擅长把文字人设转写成精准、"
                    "可直接用于 AI 绘画的视觉外貌描述。只输出描述本身。"
                ),
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Selfie] 人设转外貌失败: {exc}")
            return ""

        text = text.replace("\n", " ").strip().strip('"').strip("“”").strip("「」")
        return text[:200]

    def _persona_from_config(self) -> str:
        """兜底：从 AstrBot 配置里尝试读取默认人格文本。"""
        try:
            conf = self.context.get_config()
        except Exception:  # noqa: BLE001
            return ""
        if not conf:
            return ""
        for key in ("persona", "personas"):
            try:
                items = conf.get(key) or []
            except Exception:  # noqa: BLE001
                continue
            if isinstance(items, dict):
                items = [items]
            if isinstance(items, str):
                return items.strip()
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    for field in ("system_prompt", "prompt", "content", "text"):
                        val = item.get(field)
                        if isinstance(val, str) and val.strip():
                            return val.strip()
                elif isinstance(item, str) and item.strip():
                    return item.strip()
        return ""

    def _persona_text_umo(self, umo: str) -> str:
        return self._persona_cache.get(umo, "") or self._persona_from_config()

    def _persona_text(self, event: AstrMessageEvent) -> str:
        return self._persona_text_umo(self._session_id(event))

    async def _ensure_appearance_umo(self, umo: str) -> str:
        """拿一段固定的外貌描述：已有缓存直接用，否则按人设总结一次并存下来。"""
        cached = self._load_appearance()
        if cached:
            return cached

        text = ""
        if self.face_from_persona:
            text = await self._summarize_appearance(self._persona_text_umo(umo))
        if not text:
            text = self.character_face
        self._save_appearance(text)
        logger.info(f"[Selfie] 已生成外貌描述：{text[:60]}…")
        return text

    async def _ensure_appearance(self, event: AstrMessageEvent) -> str:
        return await self._ensure_appearance_umo(self._session_id(event))

    def _load_reference_b64(self) -> str | None:
        p = self._locked_image()
        if not p:
            return None
        try:
            raw = p.read_bytes()
        except OSError as exc:
            logger.warning(f"[Selfie] 读取锁脸图失败: {exc}")
            return None
        mime = sniff_mime(raw)
        return f"data:{mime};base64," + base64.b64encode(raw).decode()

    def _apply_lock(self, raw: bytes, source: str, session_id: str) -> str:
        """把图片字节写成锁定的脸，返回实际使用的后缀。"""
        suffix = {"image/jpeg": "jpg", "image/webp": "webp", "image/bmp": "bmp"}.get(
            sniff_mime(raw), "png"
        )
        target = self.face_dir / f"locked.{suffix}"
        for old in self.face_dir.glob("locked.*"):
            if old != target:
                try:
                    old.unlink()
                except OSError:
                    pass
        target.write_bytes(raw)
        meta = {
            "locked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "session": session_id,
            "face": self._load_appearance() or self.character_face,
            "source": source,
        }
        self._meta_path().write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
        logger.info(f"[Selfie] 已锁定角色长相（{source}，{suffix}，{len(raw)} 字节）")
        return suffix

    def _save_lock(self, src: Path, session_id: str) -> None:
        self._apply_lock(src.read_bytes(), src.name, session_id)

    # ---------------- 待锁图缓存（用户直接发图时） ----------------

    def _pending_path(self) -> Path:
        return self.face_dir / "pending.img"

    async def _cache_pending_image(self, event: AstrMessageEvent) -> None:
        """用户发图时先把图存下来，等 AI 判断是否要锁定。"""
        imgs = self._extract_images(event)
        if not imgs:
            return
        try:
            raw = base64.b64decode(await imgs[0].convert_to_base64())
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Selfie] 缓存待锁图失败: {exc}")
            return
        if len(raw) < 1024:
            return
        try:
            self._pending_path().write_bytes(raw)
            logger.info(f"[Selfie] 已缓存一张待锁定的图（{len(raw)} 字节）")
        except OSError as exc:
            logger.warning(f"[Selfie] 写入待锁图失败: {exc}")

    def _take_pending_image(self, max_age: int = 3600) -> bytes | None:
        p = self._pending_path()
        try:
            if not p.exists():
                return None
            if time.time() - p.stat().st_mtime > max_age:
                return None
            return p.read_bytes()
        except OSError:
            return None

    # ---------------- 提示词 ----------------

    def _base_face(self) -> str:
        # 优先用按人设总结出来的外貌描述，其次用配置里的固定描述
        face = self._load_appearance() or self.character_face
        if self._locked_image():
            # 有参考图时，文字侧再强调一次"与参考图同一个人"
            face += "，五官和脸型必须与参考图中的是同一个人"
        return face

    def _selfie_prompt(self, scene: str, outfit: str, mood: str, angle: str = "") -> str:
        parts = [self._base_face()]
        if outfit:
            parts.append(f"穿着{outfit}")
        # 环境：AI 会把背景画成样板间（白墙+绿植+大面积柔焦），
        # 所以要么给一整个「被生活堆出来的」具体空间，要么在给定场景上补一条生活痕迹。
        if scene:
            parts.append(f"在{scene}")
            if self.realism > 0:
                parts.append(random.choice(REAL_TRACES))
        elif self.realism > 0:
            parts.append(f"在{random.choice(REAL_ENVIRONMENTS)}")
        if self.realism > 0:
            parts.append(random.choice(REAL_LIGHTING))
        if mood:
            parts.append(f"神态{mood}")
        if self.selfie_style:
            parts.append(self.selfie_style)
        # 拍摄方式：优先用调用方指定的角度，否则随机一个真实手机自拍姿势
        parts.append(angle or random.choice(REAL_PHONE_SHOTS))
        # 注入「不完美」细节——这是让画面摆脱 AI 感的关键
        if self.realism > 0:
            k = min(self.realism, len(REAL_IMPERFECTIONS))
            parts.extend(random.sample(REAL_IMPERFECTIONS, k))
            parts.append(NO_PORTRAIT_BLUR)
            parts.append(REAL_SUFFIX)
        return "，".join(parts)

    def _photo_prompt(self, scene: str) -> str:
        parts = [scene or "一处风景"]
        if self.selfie_style:
            parts.append(self.selfie_style)
        parts.append(random.choice(REAL_PHOTO_SHOTS))
        if self.realism > 0:
            k = min(self.realism, len(REAL_IMPERFECTIONS))
            parts.extend(random.sample(REAL_IMPERFECTIONS, k))
            parts.append("自然光，有明确的明暗方向，不是均匀打光")
            parts.append(REAL_SUFFIX)
        parts.append("画面里没有人物")
        return "，".join(parts)

    # ---------------- 通用 ----------------

    def _session_id(self, event: AstrMessageEvent) -> str:
        event = unwrap_event(event)
        try:
            umo = getattr(event, "unified_msg_origin", "")
            if umo:
                return str(umo)
        except Exception:  # noqa: BLE001
            pass
        try:
            return str(event.get_session_id())
        except Exception:  # noqa: BLE001
            return "unknown"

    def _extract_images(self, event: AstrMessageEvent) -> list:
        """取出本条消息里用户上传的图片组件。"""
        try:
            chain = getattr(event.message_obj, "message", None)
        except Exception:  # noqa: BLE001
            return []
        if not chain:
            return []
        try:
            return [seg for seg in chain if isinstance(seg, Image)]
        except Exception:  # noqa: BLE001
            return []

    def _cand_dir_umo(self, umo: str) -> Path:
        key = hashlib.md5(umo.encode()).hexdigest()[:8]
        d = self.cand_dir / key
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _cand_subdir(self, event: AstrMessageEvent) -> Path:
        return self._cand_dir_umo(self._session_id(event))

    def _in_cooldown(self, event: AstrMessageEvent) -> bool:
        if self.cooldown <= 0:
            return False
        return time.time() - self._last_call.get(self._session_id(event), 0.0) < self.cooldown

    def _mark_call(self, event: AstrMessageEvent) -> None:
        self._last_call[self._session_id(event)] = time.time()

    def _cleanup_candidates(self) -> None:
        try:
            files = sorted(
                self.cand_dir.rglob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True
            )
        except OSError:
            return
        for f in files[self.keep_candidates:]:
            try:
                f.unlink()
            except OSError:
                pass

    # ---------------- 主动发图 ----------------

    async def _proactive_loop(self) -> None:
        await asyncio.sleep(120)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[Selfie] 主动发图检查出错: {exc}")
            await asyncio.sleep(max(5, self.check_interval) * 60)

    async def _tick(self) -> None:
        if not self.proactive_photo or self._busy:
            return
        if self._in_quiet_hours():
            return
        if self._count_today() >= self.daily_limit:
            return
        # 没锁脸时主动发图没意义，脸会飘，等用户先定脸
        if not self._locked_image():
            return

        now_ts = time.time()
        targets = []
        planned = False
        for umo, sess in list(self.state.get("sessions", {}).items()):
            if not sess.get("enabled", True):
                continue
            if now_ts - float(sess.get("last_active", 0)) > self.active_hours * 3600:
                continue
            nxt = float(sess.get("next_photo_at") or 0)
            if not nxt:
                # 新会话先排一个当天的随机时刻，别一装完就立刻发
                sess["next_photo_at"] = self._plan_next_photo()
                planned = True
                continue
            if nxt <= now_ts:
                targets.append(umo)

        if planned:
            self._save_state()
        if not targets:
            return

        umo = random.choice(targets)
        self._busy = True
        try:
            await self._proactive_send(umo)
        finally:
            self._busy = False
            today = self._today()
            if self.state.get("photo_date") != today:
                self.state["photo_date"] = today
                self.state["photo_count"] = 0
            self.state["photo_count"] = int(self.state.get("photo_count", 0)) + 1
            self._session(umo)["next_photo_at"] = self._plan_next_photo()
            self._save_state()

    async def _imagine_scene(self, umo: str) -> tuple[str, str, str]:
        """让角色自己想一个「此刻发给你看」的画面。"""
        try:
            provider = self.context.get_using_provider()
        except Exception:  # noqa: BLE001
            provider = None

        fallback = ("房间的窗边", "居家的衣服", "放松")
        if not provider:
            return fallback

        persona = self._persona_text_umo(umo)[:800]
        hour = datetime.now().hour
        if hour < 11:
            phase = "早上"
        elif hour < 14:
            phase = "中午"
        elif hour < 18:
            phase = "下午"
        elif hour < 23:
            phase = "晚上"
        else:
            phase = "深夜"

        prompt = (
            f"你的人设：{persona or '（普通年轻人）'}\n"
            f"现在是{phase}。你想给 ta 随手发一张自拍。\n"
            "想一个此刻真实可信的画面：你在哪、穿着什么、什么表情。"
            "要贴合人设和时间，不要每次都是咖啡厅，也可以在家、在路上、在便利店、在教室。\n"
            '只输出 JSON：{"scene":"场景，15字内","outfit":"穿着，15字内","mood":"神态，10字内"}'
        )
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt="只输出 JSON，不要任何解释。",
            )
            data = extract_json(getattr(resp, "completion_text", "") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Selfie] 想象场景失败: {exc}")
            data = None

        if isinstance(data, dict):
            return (
                str(data.get("scene") or "")[:30],
                str(data.get("outfit") or "")[:30],
                str(data.get("mood") or "")[:20],
            )
        return fallback

    async def _generate_images(self, prompts: list[str], umo: str) -> tuple[list[Path], bool]:
        ref = self._load_reference_b64()
        results = await self.client.generate_many(
            prompts[0], len(prompts), reference_b64=ref, prompts=prompts
        )
        subdir = self._cand_dir_umo(umo)
        paths: list[Path] = []
        used_ref = False
        for url, ur in results:
            try:
                paths.append(await self.client.save_image(url, subdir, "selfie"))
                used_ref = used_ref or ur
            except ImageError as exc:
                logger.warning(f"[Selfie] 下载失败: {exc}")
        return paths, used_ref

    async def _proactive_send(self, umo: str) -> None:
        await self._ensure_appearance_umo(umo)
        scene, outfit, mood = await self._imagine_scene(umo)
        prompt = self._selfie_prompt(scene, outfit, mood)
        try:
            paths, _ = await self._generate_images([prompt], umo)
        except ImageError as exc:
            logger.warning(f"[Selfie] 主动发图失败: {exc}")
            return
        if not paths:
            return
        try:
            ok = await self.context.send_message(
                umo, MessageChain([Image.fromFileSystem(str(p)) for p in paths])
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[Selfie] 主动发图发送失败: {exc}")
            return
        if ok:
            logger.info(f"[Selfie] 主动发图成功 → {umo}（{scene}）")
        self._cleanup_candidates()

    # ---------------- LLM 工具 ----------------

    @filter.llm_tool(TOOL_SELFIE)
    async def take_selfie(
        self,
        event: AstrMessageEvent,
        scene: str = "",
        outfit: str = "",
        mood: str = "",
    ):
        """以你当前扮演角色的身份，拍一张自拍并发给用户。

        当用户想看你的样子、要自拍、问「你在干嘛/穿什么/在哪」，或者当前情境适合晒一张照片时调用。
        场景、穿着、神态要结合对话上下文和你的人设来写，不要凭空乱编。

        Args:
            scene(string): 拍摄场景，如「午后的咖啡馆窗边」「雨夜的街头」「家里的沙发」
            outfit(string): 穿着，如「米色针织衫」「黑色吊带裙」「宽松白衬衫」
            mood(string): 神态表情，如「微微一笑」「害羞低头」「眯眼笑」
        """
        event = unwrap_event(event)
        yield await self._do_selfie(event, scene, outfit, mood, count=1)
        return

    @filter.llm_tool(TOOL_PHOTO)
    async def take_photo(self, event: AstrMessageEvent, scene: str = ""):
        """以第一人称视角拍一张风景 / 环境照片发给用户（画面里没有人物）。

        当用户想看某个地方、想让你拍风景、分享眼前的景色时调用。

        Args:
            scene(string): 要拍的画面，如「傍晚的海边落日」「雪后的老街」「樱花树下的小路」
        """
        event = unwrap_event(event)
        yield await self._do_photo(event, scene)
        return

    @filter.llm_tool("lock_face")
    async def lock_face(self, event: AstrMessageEvent):
        """把用户发来的照片锁定为角色本人的长相，之后所有自拍都以这张脸为准。

        仅在用户明确表达确认意图时调用，例如「就用这张脸」「记住我长这样」
        「以后都按这张」「就长这样吧」「这个就是我」。
        用户只是发图聊天、问问题、让你评价照片、或发的是风景/物品时，不要调用。
        """
        event = unwrap_event(event)

        raw: bytes | None = None
        imgs = self._extract_images(event)
        if imgs:
            try:
                raw = base64.b64decode(await imgs[0].convert_to_base64())
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[Selfie] 读取当前消息图片失败: {exc}")
                raw = None

        # 当前消息没有图（用户先发图、后说话）就取最近缓存的那张
        if not raw or len(raw) < 1024:
            raw = self._take_pending_image()

        if not raw or len(raw) < 1024:
            yield (
                "没有拿到可用的照片。请让用户先发一张清晰的正脸照，再执行锁定。"
            )
            return

        self._apply_lock(raw, "chat_upload", self._session_id(event))
        yield (
            "已把这张照片锁定为角色的长相。之后换衣服、换场景的自拍都会以这张脸为准。"
            "不要再用文字重复描述这张照片。"
        )

    async def _do_selfie(
        self,
        event: AstrMessageEvent,
        scene: str,
        outfit: str,
        mood: str,
        count: int = 1,
    ) -> str:
        if not self.api_key:
            return "生成失败：插件未配置 Token Plan API Key。"
        if self._in_cooldown(event):
            return "生成失败：太频繁了，稍等一下再拍。"

        for label, val in (("场景", scene), ("穿着", outfit), ("神态", mood)):
            if len(val) > self.max_prompt_chars:
                return f"生成失败：{label}描述过长（{len(val)} 字），请压缩到 {self.max_prompt_chars} 字以内。"

        self._mark_call(event)
        await self._ensure_appearance(event)
        prompt = self._selfie_prompt(scene, outfit, mood)
        ref = self._load_reference_b64()

        try:
            results = await self.client.generate_many(prompt, count, reference_b64=ref)
        except ImageError as exc:
            logger.warning(f"[Selfie] 自拍失败: {exc}")
            return f"生成失败：{exc}"

        subdir = self._cand_subdir(event)
        images = []
        used_ref = False
        for url, ur in results:
            try:
                images.append(await self.client.save_image(url, subdir, "selfie"))
                used_ref = used_ref or ur
            except ImageError as exc:
                logger.warning(f"[Selfie] 下载失败: {exc}")

        if not images:
            return "生成失败：图片下载失败。"

        try:
            await event.send(MessageChain([Image.fromFileSystem(str(p)) for p in images]))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[Selfie] 发送失败: {exc}")
            return f"图片已经生成但发送失败：{exc}"

        self._cleanup_candidates()

        if not self._locked_image():
            return (
                "自拍已发送。注意：角色长相还没锁定，目前每次生成的脸可能不一样。"
                "建议让用户先挑一张满意的，用 /自拍 然后 /定脸 编号 锁定。"
            )
        return f"自拍已发送（{'参考图锁定' if used_ref else '文字描述'}）。不要再用文字重复描述这张照片。"

    async def _do_photo(self, event: AstrMessageEvent, scene: str) -> str:
        if not self.api_key:
            return "生成失败：插件未配置 Token Plan API Key。"
        if self._in_cooldown(event):
            return "生成失败：太频繁了，稍等一下再拍。"
        if len(scene) > self.max_prompt_chars:
            return f"生成失败：场景描述过长（{len(scene)} 字）。"

        self._mark_call(event)
        prompt = self._photo_prompt(scene)
        try:
            url, _ = await self.client.generate_one(prompt, reference_b64=None)
            path = await self.client.save_image(url, self._cand_subdir(event), "photo")
        except ImageError as exc:
            logger.warning(f"[Selfie] 风景照失败: {exc}")
            return f"生成失败：{exc}"

        try:
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[Selfie] 发送失败: {exc}")
            return f"图片已经生成但发送失败：{exc}"
        self._cleanup_candidates()
        return "照片已发送。不要再用文字重复描述这张照片。"

    # ---------------- 提示注入 ----------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        # 记录活跃，主动发图只发给最近说过话的会话
        self._touch(self._session_id(event))

        # 顺手把当前 system_prompt（内含人格设定）缓存下来，
        # 供「人设 → 外貌描述」使用；即使关闭提示注入也要缓存。
        try:
            sid = self._session_id(event)
            sp = getattr(req, "system_prompt", "") or ""
            if sp:
                self._persona_cache[sid] = sp[:2000]
        except Exception:  # noqa: BLE001
            pass

        # 用户这一轮如果发了图，先存下来，等 AI 决定要不要锁成角色的脸
        try:
            await self._cache_pending_image(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Selfie] 缓存待锁图异常: {exc}")

        if not (self.enable_tool and self.inject_prompt):
            return
        locked = "已锁定" if self._locked_image() else "未锁定"
        hint = (
            f"你具备拍照能力：调用 `{TOOL_SELFIE}` 拍自拍（画面里有你本人），"
            f"调用 `{TOOL_PHOTO}` 拍风景/环境（画面里没有人）。"
            "当用户想看你的样子、问你在做什么、想看你眼前的东西时，主动拍给他看。"
            "场景、穿着、神态要贴合人设和上下文，不要与剧情矛盾。"
            "工具的 scene/outfit/mood 参数直接影响画面，请写成具体可拍的描述，不要用抽象词。"
            "另外你具备「锁定长相」的能力：当用户发来一张照片，并明确表示"
            "「就用这张脸」「记住我长这样」「以后按这张」「就长这样吧」，"
            "调用 `lock_face` 把这张照片定为角色本人的长相。"
            "用户只是发图聊天、问问题、或让你评价照片时，绝对不要调用 lock_face。"
            f"当前角色长相：{locked}。"
        )
        try:
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=hint)
            if hasattr(part, "mark_as_temp"):
                part = part.mark_as_temp()
            req.extra_user_content_parts.append(part)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            req.system_prompt = f"{req.system_prompt or ''}\n{hint}"
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Selfie] 注入提示失败: {exc}")

    # ---------------- 命令 ----------------

    @filter.command("自拍")
    async def cmd_selfie(self, event: AstrMessageEvent, count: int = 0):
        """生成候选自拍：/自拍 或 /自拍 6"""
        if not self.api_key:
            yield event.plain_result("未配置 Token Plan API Key，请先在插件配置中填写。")
            return

        n = count if 1 <= count <= 8 else self.candidate_count
        self._mark_call(event)
        ref = self._load_reference_b64()

        # 先按人设总结出固定外貌，再据此出图（只总结一次，之后复用）
        first_time = not self._load_appearance()
        if first_time and self.face_from_persona and not self._persona_text(event):
            yield event.plain_result(
                "还没拿到你的人格设定，这次先用兜底描述生成。"
                "想更贴合角色，先随便跟我聊一句话再发 /自拍。",
            )
        elif first_time:
            yield event.plain_result("先根据人设总结一下角色的长相，稍等…")
        appearance = await self._ensure_appearance(event)
        if first_time:
            yield event.plain_result(f"外貌已定：{appearance}")

        prompts = [
            self._selfie_prompt("", "", "", SELFIE_ANGLES[i % len(SELFIE_ANGLES)])
            for i in range(n)
        ]
        yield event.plain_result(f"正在拍 {n} 张，稍等一下…")

        try:
            results = await self.client.generate_many(
                prompts[0], n, reference_b64=ref, prompts=prompts
            )
        except ImageError as exc:
            yield event.plain_result(f"生成失败：{exc}")
            return

        subdir = self._cand_subdir(event)
        paths: list[Path] = []
        for url, _ in results:
            try:
                paths.append(await self.client.save_image(url, subdir, "cand"))
            except ImageError as exc:
                logger.warning(f"[Selfie] 下载失败: {exc}")

        if not paths:
            yield event.plain_result("生成失败：图片下载失败。")
            return

        # 清掉旧的候选，保证编号对应本次
        for old in subdir.glob("cand_*.png"):
            if old not in paths:
                try:
                    old.unlink()
                except OSError:
                    pass

        yield event.chain_result([Image.fromFileSystem(str(p)) for p in paths])

        tip = f"共 {len(paths)} 张，按顺序编号 1~{len(paths)}。"
        if self._locked_image():
            tip += "当前已锁定角色长相，不满意可 /定脸 重新锁定。"
        else:
            tip += "挑一张满意的，用 /定脸 编号 锁定成你的脸，之后所有自拍都会保持一致。"
        yield event.plain_result(tip)

    @filter.command("定脸")
    async def cmd_lock(self, event: AstrMessageEvent, index: int = 0):
        """锁定角色长相：/定脸 2（从候选里选），或直接发一张图 + /定脸"""
        # 情况一：消息里带图，直接用这张图锁脸
        imgs = self._extract_images(event)
        if imgs:
            try:
                b64 = await imgs[0].convert_to_base64()
                raw = base64.b64decode(b64)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[Selfie] 读取上传图片失败: {exc}")
                yield event.plain_result(f"读取图片失败：{exc}")
                return
            if len(raw) < 1024:
                yield event.plain_result("这张图好像没读完整，换一张试试。")
                return

            self._apply_lock(raw, "upload", self._session_id(event))
            yield event.plain_result(
                "已用你上传的这张图锁定角色长相。之后换衣服、换场景都会以这张脸为准。",
            )
            return

        # 情况二：从候选里按编号选
        if index <= 0:
            yield event.plain_result(
                "两种用法：\n"
                "· 直接发一张图 + 附上 /定脸 —— 用这张图当角色的脸\n"
                "· /定脸 编号 —— 从 /自拍 生成的候选里挑一张",
            )
            return

        subdir = self._cand_subdir(event)
        files = sorted(subdir.glob("cand_*.png"), key=lambda p: p.stat().st_mtime)
        if not files:
            yield event.plain_result("没有找到候选自拍，先发 /自拍 生成几张。")
            return
        if index > len(files):
            yield event.plain_result(f"编号超出范围，本次只有 {len(files)} 张候选。")
            return

        target = files[index - 1]
        self._save_lock(target, self._session_id(event))
        yield event.plain_result(
            f"已锁定第 {index} 张作为角色的脸。之后的自拍都会以这张为准，"
            "换衣服、换场景也还是这张脸。",
        )

    @filter.command("定脸重置")
    async def cmd_unlock(self, event: AstrMessageEvent):
        """解除长相锁定"""
        p = self._locked_image()
        if p:
            try:
                p.unlink()
            except OSError:
                pass
        m = self._meta_path()
        if m.exists():
            try:
                m.unlink()
            except OSError:
                pass
        yield event.plain_result("已解除长相锁定，下次可以重新挑脸。")

    @filter.command("定脸状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看锁定状态"""
        p = self._locked_image()
        if not p:
            yield event.plain_result("当前未锁定角色长相。")
            return
        meta = {}
        try:
            meta = json.loads(self._meta_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        lines = [
            "已锁定角色长相。",
            f"锁定时间：{meta.get('locked_at', '未知')}",
            f"外貌描述：{meta.get('face', self.character_face)}",
            f"文生图模型：{self.client.model_t2i}",
            f"参考图模型：{self.client.model_i2i}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("外貌")
    async def cmd_appearance(self, event: AstrMessageEvent):
        """查看当前用来出图的外貌描述"""
        text = self._load_appearance()
        if not text:
            yield event.plain_result(
                "还没有外貌描述。发 /自拍 会自动根据人设总结一份，"
                "或用 /外貌重写 立刻重新总结。",
            )
            return
        yield event.plain_result(f"当前角色外貌描述：\n{text}")

    @filter.command("外貌重写")
    async def cmd_rewrite_appearance(self, event: AstrMessageEvent):
        """丢弃旧的外貌描述，按当前人设重新总结一份"""
        p = self._appearance_path()
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
        yield event.plain_result("正在根据人设重新总结外貌…")
        text = await self._ensure_appearance(event)
        yield event.plain_result(f"新的外貌描述：\n{text}")

    @filter.command("主动图开")
    async def cmd_proactive_on(self, event: AstrMessageEvent):
        """允许角色偶尔主动发照片"""
        umo = self._session_id(event)
        self._touch(umo)
        sess = self._session(umo)
        sess["enabled"] = True
        sess["next_photo_at"] = self._plan_next_photo()
        self._save_state()
        yield event.plain_result(
            f"好，我偶尔会随手拍一张发给你（每天最多 {self.daily_limit} 张，不会太频繁）。",
        )

    @filter.command("主动图关")
    async def cmd_proactive_off(self, event: AstrMessageEvent):
        """关闭主动发照片"""
        umo = self._session_id(event)
        self._touch(umo)
        self._session(umo)["enabled"] = False
        self._save_state()
        yield event.plain_result("好，我不主动发了。想看就跟我说。")

    @filter.command("主动图状态")
    async def cmd_proactive_status(self, event: AstrMessageEvent):
        """查看主动发图的状态"""
        umo = self._session_id(event)
        sess = self._session(umo)
        nxt = float(sess.get("next_photo_at") or 0)
        now = time.time()
        if nxt and nxt > now:
            eta = int((nxt - now) / 60)
            nxt_txt = f"约 {eta} 分钟后"
        else:
            nxt_txt = "待安排"
        yield event.plain_result(
            f"主动发图：{'开' if sess.get('enabled', True) else '关'}\n"
            f"今天已发：{self._count_today()}/{self.daily_limit}\n"
            f"下一次：{nxt_txt}\n"
            f"锁脸：{'已锁定' if self._locked_image() else '未锁定（未锁脸不会主动发）'}",
        )
