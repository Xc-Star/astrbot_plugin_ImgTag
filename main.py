import asyncio
import re
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core import AstrBotConfig, astrbot_config

PLUGIN_NAME = "astrbot_plugin_ImgTag"


@register(
    "astrbot_plugin_ImgTag",
    "Xc_Star",
    "将群聊图片上传到 ImgTag 图库",
    "1.1.0",
    "https://github.com/Xc-Star/astrbot_plugin_ImgTag",
)
class ImgTagPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._callback_lock = asyncio.Lock()
        context.register_web_api(
            f"/{PLUGIN_NAME}/callback_notice",
            self.callback_notice,
            ["POST"],
            "AI分析完成回调API",
        )

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

        callback_api_key = str(self.config.get("callback_api_key", "")).strip()
        callback_api_base = (
            str(astrbot_config.get("callback_api_base", "")).strip().rstrip("/")
        )
        if callback_api_key and not callback_api_base:
            yield event.plain_result(
                "已配置回调 API Key，请先配置 AstrBot 的 callback_api_base。"
            )
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

                    request_data = {
                        "category_id": 4,
                        "tags": tags,
                        "image_url": image_url,
                        "auto_analyze": True,
                    }
                    if callback_api_key:
                        request_id = uuid4().hex
                        # Persist before uploading: analysis may finish before POST returns.
                        await self.put_kv_data(
                            f"pending_callback:{request_id}",
                            {"umo": event.unified_msg_origin},
                        )
                        request_data.update(
                            {
                                "callback_url": (
                                    f"{callback_api_base}/api/v1/plugins/extensions/"
                                    f"{PLUGIN_NAME}/callback_notice?request_id={request_id}"
                                ),
                                "callback_headers": {
                                    "x-api-key": callback_api_key,
                                },
                            }
                        )

                    response = await client.post(
                        "/api/v1/external/images",
                        json=request_data,
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
                if callback_api_key:
                    return

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

            message_chain = self._build_analysis_message(detail, uploaded_url)
            yield event.chain_result(message_chain.chain)

    def _build_analysis_message(self, detail: dict, uploaded_url: str) -> MessageChain:
        """Build the shared polling and callback result message.

        Args:
            detail: ImgTag analysis result.
            uploaded_url: Image URL to use when the result has no URL.

        Returns:
            A message chain containing the image, tags, and description.
        """
        message_chain = MessageChain()
        image_url = detail.get("url") or detail.get("image_url") or uploaded_url
        if isinstance(image_url, str) and image_url.strip():
            api_base_url = str(self.config.get("api_base_url", "")).strip().rstrip("/")
            try:
                image_url = urljoin(f"{api_base_url}/", image_url.strip())
                parsed_url = urlsplit(image_url)
                if (
                    parsed_url.scheme not in ("http", "https")
                    or not parsed_url.hostname
                ):
                    raise ValueError("Image URL has no HTTP(S) host.")
            except ValueError:
                logger.warning("ImgTag analysis result has an invalid image URL.")
            else:
                message_chain.url_image(image_url)

        tags = detail.get("tags")
        tag_names = (
            [str(tag).strip() for tag in tags if str(tag).strip()]
            if isinstance(tags, list)
            else []
        )
        tag_line = "🏷️ 表情包"
        if tag_names:
            tag_line += f" · {' · '.join(tag_names)}"
        message_chain.message(f"{tag_line}\n")

        description = detail.get("description")
        description = (
            description.strip()
            if isinstance(description, str) and description.strip()
            else "暂无描述"
        )
        return message_chain.message(f"📝 {description}")

    async def callback_notice(self):
        """Send an analysis notification to the persisted originating session.

        Returns:
            A JSON acknowledgment or an explicit validation/delivery error.
        """
        logger.info("回调API")
        callback_req = await request.json(default=None)
        logger.info(f"callback_req: {callback_req}")
        if not isinstance(callback_req, dict):
            return error_response("Request body must be a JSON object.")

        request_id = request.query.get("request_id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            return error_response("request_id must be a valid upload request ID.")

        image_id = callback_req.get("image_id")
        if type(image_id) is not int or image_id <= 0:
            return error_response("image_id must be a positive integer.")
        task_id = callback_req.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return error_response("task_id must be a non-empty string.")
        success = callback_req.get("success")
        if not isinstance(success, bool):
            return error_response("success must be a boolean.")

        if success:
            image_url = callback_req.get("image_url")
            if not isinstance(image_url, str) or not image_url.strip():
                return error_response("image_url must be a non-empty string.")
            api_base_url = str(self.config.get("api_base_url", "")).strip().rstrip("/")
            try:
                image_url = urljoin(f"{api_base_url}/", image_url.strip())
                parsed_url = urlsplit(image_url)
                if (
                    parsed_url.scheme not in ("http", "https")
                    or not parsed_url.hostname
                ):
                    raise ValueError("Image URL has no HTTP(S) host.")
            except ValueError:
                logger.warning(
                    "ImgTag callback has an invalid image URL for image %s", image_id
                )
                return error_response(
                    "image_url must resolve to an HTTP(S) URL using api_base_url."
                )
            tags = callback_req.get("tags")
            if not isinstance(tags, list) or not all(
                isinstance(tag, str) for tag in tags
            ):
                return error_response("tags must be a list of strings.")
            if not isinstance(callback_req.get("description"), str):
                return error_response("description must be a string.")
            message_chain = self._build_analysis_message(callback_req, image_url)
        else:
            error = callback_req.get("error")
            if error is not None and not isinstance(error, str):
                return error_response("error must be a string or null.")
            reason = (
                error.strip()
                if isinstance(error, str) and error.strip()
                else "未知错误"
            )
            logger.error(
                "ImgTag analysis failed for image %s (task %s): %s",
                image_id,
                task_id,
                reason,
            )
            message_chain = MessageChain().message(
                f"图片 ID {image_id} 的 AI 分析失败：{reason}"
            )

        async with self._callback_lock:
            storage_key = f"pending_callback:{request_id}"
            pending = await self.get_kv_data(storage_key, None)
            if pending is None:
                logger.warning(
                    "ImgTag callback has no pending upload for image %s (task %s)",
                    image_id,
                    task_id,
                )
                return error_response(
                    "Upload not found or callback already processed.", status_code=404
                )
            if not isinstance(pending, dict) or not isinstance(pending.get("umo"), str):
                logger.error(
                    "ImgTag callback session record is invalid for image %s", image_id
                )
                return error_response(
                    "Invalid callback session record.", status_code=500
                )
            if not await self.context.send_message(pending["umo"], message_chain):
                logger.error(
                    "ImgTag callback could not find a platform for image %s", image_id
                )
                return error_response(
                    "No platform available for the originating session.",
                    status_code=503,
                )
            await self.delete_kv_data(storage_key)

        return json_response({"image_id": image_id, "task_id": task_id})
