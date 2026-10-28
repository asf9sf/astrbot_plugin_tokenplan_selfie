"""AstrBot 插件：角色自拍 / 风景照生成（阿里云百炼 Token Plan）。

核心特性是「锁脸」：
1. 先用 /自拍 生成几张候选，挑一张满意的；
2. /定脸 <编号> 把这张锁成角色的脸；
3. 之后所有自拍都会带上这张参考图 + 固定外貌描述，保证是同一个人。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
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

        self._last_call: dict[str, float] = {}
        # 每轮 LLM 请求时缓存当前人格，供「人设 → 外貌描述」使用
        self._persona_cache: dict[str, str] = {}

        locked = self._locked_image()
        logger.info(
            f"[Selfie] 已加载：文生图={self.client.model_t2i} "
            f"参考图={self.client.model_i2i} 尺寸={self.client.size} "
            f"锁脸={'已锁定' if locked else '未锁定'} 工具={'开' if self.enable_tool else '关'}",
        )

    async def terminate(self) -> None:
        await self.client.close()

    # ---------------- 锁脸存储 ----------------

    def _locked_image(self) -> Path | None:
        p = self.face_dir / "locked.png"
        return p if p.exists() else None

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

    def _persona_text(self, event: AstrMessageEvent) -> str:
        return self._persona_cache.get(self._session_id(event), "") or self._persona_from_config()

    async def _ensure_appearance(self, event: AstrMessageEvent) -> str:
        """拿一段固定的外貌描述：已有缓存直接用，否则按人设总结一次并存下来。"""
        cached = self._load_appearance()
        if cached:
            return cached

        text = ""
        if self.face_from_persona:
            text = await self._summarize_appearance(self._persona_text(event))
        if not text:
            text = self.character_face
        self._save_appearance(text)
        logger.info(f"[Selfie] 已生成外貌描述：{text[:60]}…")
        return text

    def _load_reference_b64(self) -> str | None:
        p = self._locked_image()
        if not p:
            return None
        try:
            raw = p.read_bytes()
        except OSError as exc:
            logger.warning(f"[Selfie] 读取锁脸图失败: {exc}")
            return None
        return "data:image/png;base64," + base64.b64encode(raw).decode()

    def _save_lock(self, src: Path, session_id: str) -> None:
        target = self.face_dir / "locked.png"
        target.write_bytes(src.read_bytes())
        meta = {
            "locked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "session": session_id,
            "face": self.character_face,
            "source": src.name,
        }
        self._meta_path().write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
        logger.info(f"[Selfie] 已锁定角色长相：{src.name}")

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
        if scene:
            parts.append(f"身处{scene}")
        if mood:
            parts.append(f"神态{mood}")
        if self.selfie_style:
            parts.append(self.selfie_style)
        parts.append(angle or "手机前置摄像头自拍，近距离半身构图，自然光，真实照片质感，生活照")
        return "，".join(parts)

    def _photo_prompt(self, scene: str) -> str:
        parts = [scene or "一处风景"]
        if self.selfie_style:
            parts.append(self.selfie_style)
        parts.append("第一人称视角随手拍，真实照片质感，高清细节，无人物")
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

    def _cand_subdir(self, event: AstrMessageEvent) -> Path:
        key = hashlib.md5(self._session_id(event).encode()).hexdigest()[:8]
        d = self.cand_dir / key
        d.mkdir(parents=True, exist_ok=True)
        return d

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
        # 顺手把当前 system_prompt（内含人格设定）缓存下来，
        # 供「人设 → 外貌描述」使用；即使关闭提示注入也要缓存。
        try:
            sid = self._session_id(event)
            sp = getattr(req, "system_prompt", "") or ""
            if sp:
                self._persona_cache[sid] = sp[:2000]
        except Exception:  # noqa: BLE001
            pass

        if not (self.enable_tool and self.inject_prompt):
            return
        locked = "已锁定" if self._locked_image() else "未锁定"
        hint = (
            f"你具备拍照能力：调用 `{TOOL_SELFIE}` 拍自拍（画面里有你本人），"
            f"调用 `{TOOL_PHOTO}` 拍风景/环境（画面里没有人）。"
            "当用户想看你的样子、问你在做什么、想看你眼前的东西时，主动拍给他看。"
            "场景、穿着、神态要贴合人设和上下文，不要与剧情矛盾。"
            "工具的 scene/outfit/mood 参数直接影响画面，请写成具体可拍的描述，不要用抽象词。"
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
        """锁定角色长相：/定脸 2"""
        if index <= 0:
            yield event.plain_result("用法：/定脸 编号（先发 /自拍 生成候选）")
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
