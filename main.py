import asyncio
import re

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig, astrbot_config


@register(
    "astrbot_plugin_ImgTag",
    "Xc_Star",
    "将群聊图片上传到 ImgTag 图库",
    "1.0.0",
    "https://github.com/Xc-Star/astrbot_plugin_ImgTag",
)
class ImgTagPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @filter.command("偷图")
    async def toutu(self, event: AstrMessageEvent):
        """Upload images from a quoted message to the configured ImgTag gallery."""
        images = [
            reply_component
            for component in event.get_messages()
            if isinstance(component, Reply) and component.chain
            for reply_component in component.chain
            if isinstance(reply_component, Image)
        ]

        if not images:
            yield event.plain_result("请引用一条包含图片的消息后发送 /偷图。")
            return

        command_args = re.sub(
            r"^\s*/?偷图(?=$|[\s,，])", "", event.message_str, count=1
        )
        tags = [tag for tag in re.split(r"[\s,，]+", command_args.strip()) if tag]

        api_base_url = str(self.config.get("api_base_url", "")).strip().rstrip("/")
        api_key = str(self.config.get("api_key", "")).strip()
        if not api_base_url or not api_key:
            yield event.plain_result("请先在插件配置中填写 ImgTag API 地址和 API Key。")
            return

        uploaded_images: dict[int, str] = {}
        analysis_results: dict[int, dict] = {}
        action = "上传引用图片"
        try:
            async with httpx.AsyncClient(
                base_url=api_base_url,
                headers={"X-API-Key": api_key},
                timeout=30.0,
                follow_redirects=True,
            ) as client:
                for image in images:
                    image_url = image.url or image.file or ""
                    if not image_url.startswith(("http://", "https://")):
                        if not astrbot_config.get("callback_api_base"):
                            yield event.plain_result(
                                "当前图片没有可直接访问的链接，请先配置 "
                                "AstrBot 的 callback_api_base。"
                            )
                            return
                        image_url = await image.register_to_file_service()

                    response = await client.post(
                        "/api/v1/external/images",
                        json={
                            "category_id": 4,
                            "tags": tags,
                            "image_url": image_url,
                            "auto_analyze": True,
                        },
                    )
                    response.raise_for_status()
                    try:
                        result = response.json()
                    except ValueError:
                        yield event.plain_result("ImgTag 返回了无法解析的上传结果。")
                        return

                    if not isinstance(result, dict) or not isinstance(
                        result.get("id"), int
                    ):
                        yield event.plain_result(
                            "ImgTag 上传成功，但没有返回有效的图片 ID。"
                        )
                        return
                    image_id = result["id"]
                    stored_url = result.get("image_url")
                    uploaded_images[image_id] = (
                        stored_url
                        if isinstance(stored_url, str) and stored_url
                        else image_url
                    )

                yield event.plain_result(
                    f"已提交 {len(uploaded_images)} 张图片，正在解析中..."
                )

                pending_image_ids = set(uploaded_images)
                for attempt in range(60):
                    for image_id in tuple(pending_image_ids):
                        action = f"查询图片 {image_id} 的 AI 分析结果"
                        response = await client.get(
                            f"/api/v1/external/images/{image_id}"
                        )
                        response.raise_for_status()
                        try:
                            detail = response.json()
                        except ValueError:
                            yield event.plain_result(
                                "ImgTag 返回了无法解析的图片详情。"
                            )
                            return
                        if not isinstance(detail, dict):
                            yield event.plain_result(
                                "ImgTag 返回了格式异常的图片详情。"
                            )
                            return

                        description = detail.get("description")
                        # User-provided tags are stored before asynchronous analysis finishes.
                        if isinstance(description, str) and description.strip():
                            analysis_results[image_id] = detail
                            pending_image_ids.remove(image_id)

                    if not pending_image_ids or attempt == 59:
                        break
                    await asyncio.sleep(2)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip()[:300]
            logger.error(
                "ImgTag request failed with HTTP %s: %s",
                exc.response.status_code,
                detail,
            )
            yield event.plain_result(
                f"{action}失败（HTTP {exc.response.status_code}）："
                f"{detail or '服务返回错误'}"
            )
            return
        except httpx.RequestError as exc:
            logger.error("ImgTag request failed: %s", exc)
            yield event.plain_result(f"{action}时无法连接 ImgTag，请检查网络。")
            return

        for image_id, uploaded_url in uploaded_images.items():
            detail = analysis_results.get(image_id)
            if detail is None:
                yield event.plain_result(
                    f"图片 ID {image_id} 已上传，但未获取到 AI 分析结果。"
                )
                continue

            message_chain: list[Image | Plain] = []
            image_url = detail.get("url") or uploaded_url
            if isinstance(image_url, str) and image_url.startswith(
                ("http://", "https://")
            ):
                message_chain.append(Image.fromURL(image_url))

            tags = detail.get("tags")
            tag_names = (
                [str(tag).strip() for tag in tags if str(tag).strip()]
                if isinstance(tags, list)
                else []
            )
            tag_line = "🏷️ "
            if tag_names:
                tag_line += f" · {' · '.join(tag_names)}"
            message_chain.append(Plain(f"{tag_line}\n"))

            description = detail.get("description")
            description = (
                description.strip()
                if isinstance(description, str) and description.strip()
                else "暂无描述"
            )
            message_chain.append(Plain(f"📝 {description}"))
            yield event.chain_result(message_chain)
