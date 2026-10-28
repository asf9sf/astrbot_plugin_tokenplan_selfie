"""Token Plan（阿里云百炼）生图客户端。

Token 套餐域名只支持同步接口：
    POST {base}/api/v1/services/aigc/multimodal-generation/generation

请求体：
    {"model": "wan2.7-image",
     "input": {"messages": [{"role": "user", "content": [{"text": "提示词"}]}]},
     "parameters": {"size": "1024*1024"}}

图片在 output.choices[0].message.content[*].image（URL，24 小时过期，需及时下载）。
同步接口单次只出一张，n 张要并发调用。
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger

ENDPOINT_PATH = "/api/v1/services/aigc/multimodal-generation/generation"


class ImageError(Exception):
    """生图失败。"""


def _extract_images(body: dict[str, Any]) -> list[str]:
    """从响应里提出所有图片 URL。"""
    urls: list[str] = []
    try:
        content = body["output"]["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return urls
    for item in content if isinstance(content, list) else [content]:
        if isinstance(item, dict):
            img = item.get("image") or item.get("image_url")
            if img:
                urls.append(img)
    return urls


def _extract_openai_images(body: dict[str, Any]) -> list[str]:
    """OpenAI 兼容协议的响应：{"data": [{"url": ...} 或 {"b64_json": ...}]}"""
    out: list[str] = []
    if not isinstance(body, dict):
        return out
    for item in body.get("data") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if url:
            out.append(str(url))
        b64 = item.get("b64_json")
        if b64:
            out.append("data:image/png;base64," + str(b64))
    return out


def _b64_to_bytes(value: str) -> bytes | None:
    """兼容 data URI 形式的 base64 图片。"""
    if not value:
        return None
    if value.startswith("data:"):
        try:
            return base64.b64decode(value.split(",", 1)[1])
        except Exception:  # noqa: BLE001
            return None
    return None


class TokenPlanImageClient:
    def __init__(
        self,
        api_key: str,
        api_base: str,
        model_t2i: str = "wan2.7-image",
        model_i2i: str = "qwen-image-3.0-pro",
        size: str = "1024*1024",
        timeout: int = 180,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.api_base = (api_base or "").strip().rstrip("/")
        self.model_t2i = model_t2i
        self.model_i2i = model_i2i
        self.size = size or "1024*1024"
        self.timeout = int(timeout or 180)
        self._session: aiohttp.ClientSession | None = None

    @property
    def endpoint(self) -> str:
        if self.api_base.endswith(ENDPOINT_PATH):
            return self.api_base
        return self.api_base + ENDPOINT_PATH

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------

    @property
    def is_openai_compat(self) -> bool:
        """base_url 里带 compatible-mode 就按 OpenAI 协议走，否则走 DashScope 原生接口。"""
        return "/compatible-mode" in self.api_base.lower()

    @property
    def openai_endpoint(self) -> str:
        return self.api_base.rstrip("/") + "/images/generations"

    @property
    def native_base(self) -> str:
        """从 OpenAI 兼容地址倒推业务空间根地址，用于失败回退。"""
        base = self.api_base.rstrip("/")
        idx = base.lower().find("/compatible-mode")
        return base[:idx] if idx > 0 else base

    @property
    def openai_size(self) -> str:
        # 原生接口用 1024*1024，OpenAI 兼容协议用 1024x1024
        return (self.size or "1024*1024").replace("*", "x")

    def _build_payload(self, prompt: str, reference_b64: str | None, model: str) -> dict:
        content: list[dict[str, Any]] = []
        if reference_b64:
            # Token 套餐理论上不支持本地上传，但 DashScope 多模态接口接受 data URI，
            # 实测能用就用，不能用会在上层自动降级为纯文本描述。
            content.append({"image": reference_b64})
        content.append({"text": prompt})
        return {
            "model": model,
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": {"size": self.size},
        }

    def _build_openai_payload(
        self,
        prompt: str,
        reference_b64: str | None,
        model: str,
    ) -> dict:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "size": self.openai_size,
            "n": 1,
        }
        if reference_b64:
            # OpenAI 兼容协议的图生图走 image 字段，可传 URL 或 base64
            payload["image"] = [reference_b64]
        return payload

    async def _post(self, url: str, payload: dict) -> dict[str, Any]:
        session = self._ensure_session()
        async with session.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        ) as resp:
            raw = await resp.read()
            text = raw.decode("utf-8", errors="ignore")
            if resp.status != 200:
                raise ImageError(f"生图接口返回 {resp.status}: {text[:300]}")
            import json as _json

            try:
                body = _json.loads(text)
            except Exception as exc:  # noqa: BLE001
                raise ImageError(f"生图响应不是 JSON: {text[:300]}") from exc
            if isinstance(body, dict) and body.get("code"):
                raise ImageError(f"{body.get('code')}: {body.get('message', '')}"[:300])
            return body

    async def _call_native(
        self,
        prompt: str,
        reference_b64: str | None,
        model: str,
    ) -> str:
        url = self.native_base.rstrip("/") + ENDPOINT_PATH
        body = await self._post(url, self._build_payload(prompt, reference_b64, model))
        urls = _extract_images(body)
        if not urls:
            raise ImageError(f"响应里没有图片: {str(body)[:300]}")
        return urls[0]

    async def _call_openai(
        self,
        prompt: str,
        reference_b64: str | None,
        model: str,
    ) -> str:
        body = await self._post(
            self.openai_endpoint,
            self._build_openai_payload(prompt, reference_b64, model),
        )
        urls = _extract_openai_images(body)
        if not urls:
            raise ImageError(f"响应里没有图片: {str(body)[:300]}")
        return urls[0]

    async def generate_one(
        self,
        prompt: str,
        reference_b64: str | None = None,
        model: str | None = None,
        use_reference: bool = True,
    ) -> tuple[str, bool]:
        """生成一张图。

        Returns:
            (图片 URL 或 data URI, 是否实际使用了参考图)

        Raises:
            ImageError
        """
        if not self.api_key:
            raise ImageError("未配置 Token Plan API Key")

        ref = reference_b64 if use_reference else None
        chosen = model or (self.model_i2i if ref else self.model_t2i)

        async def _call(p: str, r: str | None, m: str) -> str:
            # base_url 指向业务空间的 /compatible-mode/v1 时先走 OpenAI 协议
            if self.is_openai_compat:
                try:
                    return await self._call_openai(p, r, m)
                except ImageError as exc:
                    logger.warning(f"[Selfie] OpenAI 兼容端点失败，回退原生接口: {exc}")
            return await self._call_native(p, r, m)

        try:
            url = await _call(prompt, ref, chosen)
            return url, bool(ref)
        except ImageError as exc:
            # 带了参考图却失败，降级为纯文本生成，至少能出图
            if ref:
                logger.warning(f"[Selfie] 参考图模式失败，降级为纯文本：{exc}")
                url = await _call(prompt, None, self.model_t2i)
                return url, False
            raise

    async def generate_many(
        self,
        prompt: str,
        count: int,
        reference_b64: str | None = None,
        prompts: list[str] | None = None,
    ) -> list[tuple[str, bool]]:
        """并发生成多张（同步接口一次只出一张）。

        最多 2 路并发，避免一次打满 RPM 触发限流。
        prompts 不为空时按序号逐张使用不同提示词（用于生成有差异的候选）。
        """
        sem = asyncio.Semaphore(2)

        async def _one(idx: int):
            async with sem:
                p = prompts[idx] if prompts and idx < len(prompts) else prompt
                return await self.generate_one(p, reference_b64=reference_b64)

        tasks = [asyncio.create_task(_one(i)) for i in range(max(1, count))]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[tuple[str, bool]] = []
        errors: list[str] = []
        for item in results:
            if isinstance(item, BaseException):
                errors.append(str(item))
            else:
                out.append(item)
        if not out:
            raise ImageError(errors[0] if errors else "全部生成失败")
        if errors:
            logger.warning(f"[Selfie] {len(errors)} 张生成失败：{errors[0]}")
        return out

    async def save_image(self, url_or_b64: str, save_dir: Path, prefix: str = "img") -> Path:
        """把 URL / data URI 存成本地文件，返回路径。"""
        save_dir.mkdir(parents=True, exist_ok=True)
        file_path = save_dir / f"{prefix}_{uuid.uuid4().hex[:10]}.png"

        data = _b64_to_bytes(url_or_b64)
        if data:
            file_path.write_bytes(data)
            return file_path

        session = self._ensure_session()
        async with session.get(url_or_b64, timeout=aiohttp.ClientTimeout(total=90)) as resp:
            if resp.status != 200:
                raise ImageError(f"下载图片失败: HTTP {resp.status}")
            file_path.write_bytes(await resp.read())
        return file_path
