import io
import json
import re
import tomllib
import time
from dataclasses import dataclass
import aiohttp
from loguru import logger
import os
from utils.plugin_base import PluginBase
import traceback
from PIL import Image
import base64
from utils.decorators import on_text_message, on_at_message, on_quote_message, on_image_message
import regex  # 不是re，是regex库，支持\p{Zs}


class Falclient(PluginBase):
    description = "Falclient插件"
    author = "老夏"
    version = "1.0.0"
    is_ai_platform = False

    def __init__(self):
        super().__init__()
        try:
            with open("plugins/Falclient/config.toml", "rb") as f:
                config = tomllib.load(f)
            plugin_config = config["Falclient"]
            self.enable = plugin_config["enable"]
            self.robot_names = plugin_config.get("robot_names", [])
            self.vision_prefix = plugin_config.get("vision_prefix", "识图")
            self.vision_api_key = plugin_config.get("vision_api_key", None)
            self.vision_api_base = plugin_config.get("vision_api_base", None)
            self.vision_model = plugin_config.get("vision_model", "o3")
            self.fal_img_prefix = plugin_config.get("fal_img_prefix", "图生视频")
            self.fal_text_prefix = plugin_config.get("fal_text_prefix", "文生视频")
            self.fal_kling_img_model = plugin_config.get("fal_kling_img_model", "kling-video/v2/master/image-to-video")
            self.fal_kling_text_model = plugin_config.get("fal_kling_text_model", "kling-video/v2/master/text-to-video")
        except Exception as e:
            logger.error(f"加载Falclient插件配置文件失败: {e}")
            raise
        # 记录待识图状态: {user_or_group_id: timestamp}
        self.waiting_vision = {}
        self.waiting_video = {}  # 新增，记录待生成视频的状态
        self.image_msgid_cache = set()
        self.image_cache_timeout = 60
        self.image_cache = {}

    def is_at_message(self, message: dict) -> bool:
        if not message.get("IsGroup"):
            return False
        content = message.get("Content", "")
        # 新增：先去掉"昵称: 换行"前缀
        content = regex.sub(r"^[^@\n]+:\s*\n", "", content)
        logger.info(f"Sum4all content unicode: {[hex(ord(c)) for c in content]}")
        logger.info(f"Sum4all is_at_message: content repr={repr(content)} robot_names={self.robot_names}")
        for robot_name in self.robot_names:
            if regex.match(f"^@{robot_name}[\\p{{Zs}}\\s]*", content):
                return True
        return False

    def get_waiting_key(self, message: dict):
        if message.get("IsGroup"):
            logger.info(f"【DEBUG】get_waiting_key: 群聊用 {message['FromWxid']} 作为key")
            return message["FromWxid"]
        else:
            logger.info(f"【DEBUG】get_waiting_key: 单聊用 {message['SenderWxid']} 作为key")
            return message["SenderWxid"]

    @on_text_message(priority=30)
    async def handle_text(self, bot, message: dict):
        if not self.enable:
            return True
        content = message["Content"].strip()
        if not content:
            return True
        # 图生视频
        if content.startswith(self.fal_img_prefix):
            user_prompt = content[len(self.fal_img_prefix):].strip()
            key = self.get_waiting_key(message)
            self.waiting_video[key] = {
                "timestamp": time.time(),
                "prompt": user_prompt,
                "type": "img2video"
            }
            tip = f"💡已开启图生视频模式，您接下来第一张图片会生成视频。\n当前的提示词为：\n" + (user_prompt or "无")
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            return False
        # 文生视频
        if content.startswith(self.fal_text_prefix):
            user_prompt = content[len(self.fal_text_prefix):].strip()
            await self.handle_text2video(bot, message, user_prompt)
            return False
        return True

    @on_at_message(priority=30)
    async def handle_at(self, bot, message: dict):
        if not self.enable:
            return True
        content = message["Content"].strip()
        logger.info(f"Sum4all (@message) content unicode: {[hex(ord(c)) for c in content]}")
        is_trigger = False
        user_prompt = None
        if self.vision_prefix in content:
            is_trigger = True
            # 提取"识图"后面的内容作为提示词
            idx = content.find(self.vision_prefix)
            user_prompt = content[idx + len(self.vision_prefix):].strip()
        if is_trigger:
            key = self.get_waiting_key(message)
            if not user_prompt:
                user_prompt = "请识别这张图片的内容。"
            self.waiting_vision[key] = {
                "timestamp": time.time(),
                "prompt": user_prompt
            }
            logger.info(f"Sum4all (@message): 记录待识图状态: {key}, prompt: {user_prompt}")
            tip = "💡已开启识图模式(o3)，您接下来第一张图片会进行识别。\n当前的提示词为：\n" + user_prompt
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            return False  # 阻止后续插件处理
        return True  # 允许后续插件处理

    @on_image_message(priority=30)
    async def handle_image(self, bot, message: dict):
        if not self.enable:
            return True
        msg_id = message.get("MsgId")
        from_wxid = message.get("FromWxid")
        sender_wxid = message.get("SenderWxid")
        xml_content = message.get("Content")
        logger.info(f"Sum4all: 收到图片消息: MsgId={msg_id}, FromWxid={from_wxid}, SenderWxid={sender_wxid}, ContentType={type(xml_content)}")
        # 消息ID去重
        if not msg_id or msg_id in self.image_msgid_cache:
            logger.info(f"Sum4all: 消息ID {msg_id} 已处理或无效，跳过")
            return True
        key = self.get_waiting_key(message)
        waiting_info = self.waiting_video.get(key)
        if not waiting_info or waiting_info.get("type") != "img2video":
            logger.info(f"Sum4all: 当前无待生成视频状态: {key}")
            return True
        user_prompt = waiting_info.get("prompt", "")
        image_bytes = b""
        # 1. xml格式，分段下载
        if isinstance(xml_content, str) and "<img " in xml_content:
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(xml_content)
                img_elem = root.find("img")
                if img_elem is not None:
                    length = int(img_elem.get("length", "0"))
                    logger.info(f"Sum4all: 解析图片XML成功: length={length}")
                    if length and msg_id:
                        chunk_size = 65536
                        chunks = (length + chunk_size - 1) // chunk_size
                        logger.info(f"Sum4all: 开始分段下载图片，总大小: {length} 字节，分 {chunks} 段下载")
                        for i in range(chunks):
                            start_pos = i * chunk_size
                            try:
                                chunk = await bot.get_msg_image(msg_id, from_wxid, length, start_pos=start_pos)
                                if chunk:
                                    image_bytes += chunk
                                    logger.debug(f"Sum4all: 第 {i+1}/{chunks} 段下载成功，大小: {len(chunk)} 字节")
                                else:
                                    logger.error(f"Sum4all: 第 {i+1}/{chunks} 段下载失败，数据为空")
                            except Exception as e:
                                logger.error(f"Sum4all: 下载第 {i+1}/{chunks} 段时出错: {e}")
                        logger.info(f"Sum4all: 分段下载图片成功，总大小: {len(image_bytes)} 字节")
            except Exception as e:
                logger.warning(f"Sum4all: 解析图片XML失败: {e}")
        # 2. base64格式，直接解码
        elif isinstance(xml_content, str):
            try:
                # 只要内容长度大于100且不是xml，基本就是base64图片
                if len(xml_content) > 100 and not xml_content.strip().startswith("<?xml"):
                    logger.info("Sum4all: 尝试base64解码图片内容")
                    import base64
                    image_bytes = base64.b64decode(xml_content)
            except Exception as e:
                logger.warning(f"Sum4all: base64解码失败: {e}")

        # 校验图片有效性
        if image_bytes and len(image_bytes) > 0:
            try:
                Image.open(io.BytesIO(image_bytes))
                logger.info(f"Sum4all: 图片校验通过，准备生成视频，大小: {len(image_bytes)} 字节")
                await self.handle_img2video(bot, message, image_bytes, user_prompt)
            except Exception as e:
                logger.error(f"Sum4all: 图片校验失败: {e}, image_bytes前100字节: {image_bytes[:100]}")
        else:
            logger.warning("Sum4all: 未能获取到有效的图片数据，未生成视频")
        # 状态清理
        self.waiting_video.pop(key, None)
        self.image_msgid_cache.add(msg_id)
        logger.info(f"Sum4all: 生成视频流程结束: MsgId={msg_id}")
        return False

    async def handle_text2video(self, bot, message, prompt):
        # 文生视频API调用
        try:
            url = f"https://fal.run/fal-ai/{self.fal_kling_text_model}"
            headers = {
                "Authorization": f"Key {self.vision_api_key}",
                "Content-Type": "application/json"
            }
            data = {"prompt": prompt}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=data) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        video_url = result.get("video", {}).get("url")
                        if video_url:
                            await self.send_video_url(bot, message, video_url)
                        else:
                            await self.send_video_url(bot, message, "未获取到视频URL")
                    else:
                        await self.send_video_url(bot, message, f"API请求失败: {resp.status}")
        except Exception as e:
            await self.send_video_url(bot, message, f"API调用异常: {e}")

    async def handle_img2video(self, bot, message, image_bytes, prompt):
        # 图生视频API调用
        try:
            # 上传图片到fal，获取url
            upload_url = f"https://fal.run/upload"
            headers = {"Authorization": f"Key {self.vision_api_key}"}
            data = aiohttp.FormData()
            data.add_field('file', image_bytes, filename='image.png', content_type='image/png')
            async with aiohttp.ClientSession() as session:
                async with session.post(upload_url, headers=headers, data=data) as resp:
                    if resp.status == 200:
                        upload_result = await resp.json()
                        image_url = upload_result.get("url")
                        if not image_url:
                            await self.send_video_url(bot, message, "图片上传失败")
                            return
                    else:
                        await self.send_video_url(bot, message, f"图片上传失败: {resp.status}")
                        return
                # 调用生成视频API
                api_url = f"https://fal.run/fal-ai/{self.fal_kling_img_model}"
                payload = {"prompt": prompt, "image_url": image_url}
                async with session.post(api_url, headers=headers, json=payload) as resp2:
                    if resp2.status == 200:
                        result = await resp2.json()
                        video_url = result.get("video", {}).get("url")
                        if video_url:
                            await self.send_video_url(bot, message, video_url)
                        else:
                            await self.send_video_url(bot, message, "未获取到视频URL")
                    else:
                        await self.send_video_url(bot, message, f"API请求失败: {resp2.status}")
        except Exception as e:
            await self.send_video_url(bot, message, f"API调用异常: {e}")

    async def send_video_url(self, bot, message, video_url):
        # 直接发送视频URL
        if message.get("IsGroup"):
            await bot.send_at_message(message["FromWxid"], f"[VIDEO_URL]{video_url}", [message["SenderWxid"]])
        else:
            await bot.send_text_message(message["FromWxid"], f"[VIDEO_URL]{video_url}")
