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
import tempfile
import fal_client


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
            self.fal_img_prefix = plugin_config.get("fal_img_prefix", "图生视频")
            self.fal_text_prefix = plugin_config.get("fal_text_prefix", "文生视频")
            self.fal_kling_img_model = plugin_config.get("fal_kling_img_model", "kling-video/v2/master/image-to-video")
            self.fal_kling_text_model = plugin_config.get("fal_kling_text_model", "kling-video/v2/master/text-to-video")
            self.fal_api_key = plugin_config.get("fal_api_key", None)
        except Exception as e:
            logger.error(f"加载Falclient插件配置文件失败: {e}")
            raise
        # 记录待生成视频的状态: {user_or_group_id: timestamp}
        self.waiting_video = {}
        self.image_msgid_cache = set()
        self.image_cache_timeout = 60
        self.image_cache = {}

    def get_waiting_key(self, message: dict):
        if message.get("IsGroup"):
            return message["FromWxid"]
        else:
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
            # 新增：先回复收到请求
            notice = "你的图生视频的请求已经收到，请稍候..."
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], notice)
            return False
        # 文生视频
        if content.startswith(self.fal_text_prefix):
            user_prompt = content[len(self.fal_text_prefix):].strip()
            # 新增：先回复收到请求
            notice = "你的文生视频的请求已经收到，请稍候..."
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], notice)
            await self.handle_text2video(bot, message, user_prompt)
            return False
        return True

    @on_at_message(priority=30)
    async def handle_at(self, bot, message: dict):
        if not self.enable:
            return True
        content = message["Content"].strip()
        # 图生视频
        if self.fal_img_prefix in content:
            idx = content.find(self.fal_img_prefix)
            user_prompt = content[idx + len(self.fal_img_prefix):].strip()
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
            # 新增：先回复收到请求
            notice = "你的图生视频的请求已经收到，请稍候..."
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], notice)
            return False
        # 文生视频
        if self.fal_text_prefix in content:
            idx = content.find(self.fal_text_prefix)
            user_prompt = content[idx + len(self.fal_text_prefix):].strip()
            # 新增：先回复收到请求
            notice = "你的文生视频的请求已经收到，请稍候..."
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], notice)
            await self.handle_text2video(bot, message, user_prompt)
            return False
        return True

    @on_image_message(priority=30)
    async def handle_image(self, bot, message: dict):
        if not self.enable:
            return True
        msg_id = message.get("MsgId")
        from_wxid = message.get("FromWxid")
        sender_wxid = message.get("SenderWxid")
        xml_content = message.get("Content")
        if not msg_id or msg_id in self.image_msgid_cache:
            return True
        key = self.get_waiting_key(message)
        waiting_info = self.waiting_video.get(key)
        if not waiting_info or waiting_info.get("type") != "img2video":
            return True
        user_prompt = waiting_info.get("prompt", "")
        image_bytes = b""
        if isinstance(xml_content, str) and "<img " in xml_content:
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(xml_content)
                img_elem = root.find("img")
                if img_elem is not None:
                    length = int(img_elem.get("length", "0"))
                    if length and msg_id:
                        chunk_size = 65536
                        chunks = (length + chunk_size - 1) // chunk_size
                        for i in range(chunks):
                            start_pos = i * chunk_size
                            try:
                                chunk = await bot.get_msg_image(msg_id, from_wxid, length, start_pos=start_pos)
                                if chunk:
                                    image_bytes += chunk
                            except Exception as e:
                                logger.error(f"Falclient: 下载第 {i+1}/{chunks} 段时出错: {e}")
            except Exception as e:
                logger.warning(f"Falclient: 解析图片XML失败: {e}")
        elif isinstance(xml_content, str):
            try:
                if len(xml_content) > 100 and not xml_content.strip().startswith("<?xml"):
                    import base64
                    image_bytes = base64.b64decode(xml_content)
            except Exception as e:
                logger.warning(f"Falclient: base64解码失败: {e}")
        if image_bytes and len(image_bytes) > 0:
            await self.handle_img2video(bot, message, image_bytes, user_prompt)
        self.waiting_video.pop(key, None)
        self.image_msgid_cache.add(msg_id)
        return False

    async def handle_text2video(self, bot, message, prompt):
        # 文生视频API调用
        try:
            url = f"https://fal.run/fal-ai/{self.fal_kling_text_model}"
            headers = {
                "Authorization": f"Key {self.fal_api_key}",
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
        tmp_file_path = None
        try:
            # 1. 保存图片为临时文件
            with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp_file:
                tmp_file.write(image_bytes)
                tmp_file_path = tmp_file.name

            # 2. 上传图片获取URL
            client = fal_client.SyncClient(key=self.fal_api_key)
            image_url = client.upload_file(tmp_file_path)
            if not image_url:
                await self.send_video_url(bot, message, "图片上传失败")
                return

            # 3. 调用生成视频API
            api_url = f"https://fal.run/fal-ai/{self.fal_kling_img_model}"
            headers = {
                "Authorization": f"Key {self.fal_api_key}",
                "Content-Type": "application/json"
            }
            payload = {"prompt": prompt, "image_url": image_url}
            async with aiohttp.ClientSession() as session:
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
        finally:
            try:
                if tmp_file_path:
                    os.remove(tmp_file_path)
            except Exception:
                pass

    async def send_video_url(self, bot, message, video_url):
        # 直接发送视频URL
        if message.get("IsGroup"):
            await bot.send_at_message(message["FromWxid"], f"[VIDEO_URL]{video_url}", [message["SenderWxid"]])
        else:
            await bot.send_text_message(message["FromWxid"], f"[VIDEO_URL]{video_url}")
