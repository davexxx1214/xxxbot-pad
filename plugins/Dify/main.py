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

# 只保留必要的常量
DIFY_ERROR_MESSAGE = "🙅对不起，Dify出现错误！\n"
XYBOT_PREFIX = "-----老夏的金库-----\n"

class Dify(PluginBase):
    description = "Dify插件"
    author = "老夏的金库"
    version = "2.0.0"
    is_ai_platform = True

    def __init__(self):
        super().__init__()
        try:
            with open("main_config.toml", "rb") as f:
                config = tomllib.load(f)
            self.admins = config["XYBot"].get("admins", [])
        except Exception as e:
            logger.error(f"加载主配置文件失败: {e}")
            raise

        try:
            with open("plugins/Dify/config.toml", "rb") as f:
                config = tomllib.load(f)
            plugin_config = config["Dify"]
            self.enable = plugin_config["enable"]
            self.default_model_api_key = plugin_config["api-key"]
            self.default_model_base_url = plugin_config["base-url"]
            self.http_proxy = plugin_config.get("http-proxy", None)
            self.robot_names = plugin_config.get("robot-names", [])
            self.image_generation_enabled = bool(plugin_config.get("openai_image_api_key", None))
            self.openai_image_api_key = plugin_config.get("openai_image_api_key", None)
            self.openai_image_api_base = plugin_config.get("openai_image_api_base", "https://api.openai.com/v1")
            self.image_model = plugin_config.get("image_model", "dall-e-3")
            # 读取识图API配置
            self.vision_api_key = plugin_config.get("vision_api_key", None)
            self.vision_api_base = plugin_config.get("vision_api_base", None)
            self.vision_model = plugin_config.get("vision_model", "o3")
            # 图片缓存
            self.image_cache = {}
            self.image_cache_timeout = 60  # 秒
        except Exception as e:
            logger.error(f"加载Dify插件配置文件失败: {e}")
            raise

    @staticmethod
    def is_at_message(message: dict, robot_names=None) -> bool:
        if not message.get("IsGroup"):
            return False
        content = message.get("Content", "")
        if robot_names:
            for robot_name in robot_names:
                if content.startswith(f'@{robot_name}') or f'@{robot_name}' in content:
                    return True
        return False

    async def generate_openai_image(self, bot, message, prompt: str):
        if not self.image_generation_enabled or not self.openai_image_api_key:
            err_msg = "OpenAI画图功能未配置API密钥或未启用，请联系管理员。"
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], f"\n{err_msg}", [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], err_msg)
            return
        start_message = f"🎨 正在使用 {self.image_model} 为您绘画，请稍候...\n提示词：{prompt}"
        if message["IsGroup"]:
            await bot.send_at_message(message["FromWxid"], f"\n{start_message}", [message["SenderWxid"]])
        else:
            await bot.send_text_message(message["FromWxid"], start_message)
        headers = {
            "Authorization": f"Bearer {self.openai_image_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "gpt-image-1",
            "prompt": prompt,
            "n": 1,
            "output_format": "png",
            "background": "auto",
            "size": "auto"
        }
        api_base = self.openai_image_api_base.rstrip('/')
        if not api_base.endswith('/v1'):
            if '/v1' not in api_base:
                api_base = f"{api_base}/v1"
        api_url = f"{api_base}/images/generations"
        try:
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.post(api_url, headers=headers, json=payload, timeout=300) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("data") and len(data["data"]) > 0 and data["data"][0].get("b64_json"):
                            image_b64 = data["data"][0]["b64_json"]
                            image_bytes = base64.b64decode(image_b64)
                            await bot.send_image_message(message["FromWxid"], image_bytes)
                            if message["IsGroup"]:
                                await bot.send_at_message(message["FromWxid"], "\n🖼️ 您的图像已生成！", [message["SenderWxid"]])
                            # 在 handle_image 里
                            self.image_cache[message["SenderWxid"]] = {"content": image_bytes, "timestamp": time.time()}
                            if message["FromWxid"] != message["SenderWxid"]:
                                self.image_cache[message["FromWxid"]] = {"content": image_bytes, "timestamp": time.time()}
                            logger.info(f"图片缓存: sender_wxid={message['SenderWxid']}, from_wxid={message['FromWxid']}, 大小={len(image_bytes)}")
                        else:
                            err_msg = "画图失败：API响应格式不正确。"
                            if message["IsGroup"]:
                                await bot.send_at_message(message["FromWxid"], f"\n{err_msg}", [message["SenderWxid"]])
                            else:
                                await bot.send_text_message(message["FromWxid"], err_msg)
                    else:
                        error_text = await resp.text()
                        err_msg = "画图失败，请稍后再试。"
                        if message["IsGroup"]:
                            await bot.send_at_message(message["FromWxid"], f"\n{err_msg}", [message["SenderWxid"]])
                        else:
                            await bot.send_text_message(message["FromWxid"], err_msg)
        except Exception as e:
            logger.error(f"OpenAI画图失败: {e}")
            err_msg = "画图遇到未知错误，请联系管理员。"
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], f"\n{err_msg}", [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], err_msg)

    @on_text_message(priority=20)
    async def handle_text(self, bot, message: dict):
        if not self.enable:
            return
        content = message["Content"].strip()
        if not content:
            return
        # 私聊直接回复
        if not message["IsGroup"]:
            if content.startswith("画") and self.image_generation_enabled:
                prompt = content[len("画"):].strip()
                if prompt:
                    await self.generate_openai_image(bot, message, prompt)
                else:
                    await bot.send_text_message(message["FromWxid"], "请输入绘画内容。")
                return
            await self.dify(bot, message, content)
            return
        # 群聊@机器人时回复
        if self.is_at_message(message, self.robot_names):
            query = content
            for robot_name in self.robot_names:
                query = query.replace(f"@{robot_name}", "").strip()
            if query.startswith("画") and self.image_generation_enabled:
                prompt = query[len("画"):].strip()
                if prompt:
                    await self.generate_openai_image(bot, message, prompt)
                else:
                    await bot.send_at_message(message["FromWxid"], "\n请输入绘画内容。", [message["SenderWxid"]])
                return
            await self.dify(bot, message, query)
        # 其他群聊消息不处理

    @on_at_message(priority=20)
    async def handle_at(self, bot, message: dict):
        if not self.enable:
            return
        content = message["Content"].strip()
        query = content
        for robot_name in self.robot_names:
            query = query.replace(f"@{robot_name}", "").strip()
        if query.startswith("画") and self.image_generation_enabled:
            prompt = query[len("画"):].strip()
            if prompt:
                await self.generate_openai_image(bot, message, prompt)
            else:
                await bot.send_at_message(message["FromWxid"], "\n请输入绘画内容。", [message["SenderWxid"]])
            return False
        await self.dify(bot, message, query)
        return False

    @on_quote_message(priority=20)
    async def handle_quote(self, bot, message: dict):
        if not self.enable:
            return

        content = message["Content"].strip()
        quote_info = message.get("Quote", {})
        quoted_content = quote_info.get("Content", "")
        quoted_msgtype = quote_info.get("MsgType", None)

        # 判断引用的是否为图片消息
        is_quoted_image = quoted_msgtype == 3
        if not is_quoted_image and "img" in quoted_content:
            is_quoted_image = True

        # 获取引用图片的发送者wxid（优先 chatusr、fromusr、SenderWxid）
        quoted_sender = quote_info.get("chatusr") or quote_info.get("fromusr") or quote_info.get("SenderWxid") or message.get("SenderWxid")
        logger.info(f"handle_quote: 引用图片的发送者wxid={quoted_sender}, group_id={message.get('FromWxid')}, sender={message.get('SenderWxid')}, is_quoted_image={is_quoted_image}, content='{content}'")

        # 群聊
        if message["IsGroup"]:
            group_id = message["FromWxid"]
            user_wxid = message["SenderWxid"]
            is_at = self.is_at_message(message, self.robot_names)
            is_at_bot = False
            if content.startswith('@'):
                for robot_name in self.robot_names:
                    if content.startswith(f'@{robot_name}'):
                        is_at_bot = True
                        break
            if is_at and is_at_bot and is_quoted_image and content:
                # 去掉@机器人名
                query = content
                for robot_name in self.robot_names:
                    if query.startswith(f'@{robot_name}'):
                        query = query[len(f'@{robot_name}') :].strip()
                # 优先用引用图片的发送者wxid取缓存
                image_content = await self.get_cached_image(quoted_sender)
                logger.info(f"handle_quote: 用 quoted_sender 命中图片缓存={image_content is not None}")
                if not image_content:
                    image_content = await self.get_cached_image(group_id)
                    logger.info(f"handle_quote: 用 group_id 命中图片缓存={image_content is not None}")
                if not image_content:
                    image_content = await self.get_cached_image(user_wxid)
                    logger.info(f"handle_quote: 用 SenderWxid 命中图片缓存={image_content is not None}")
                if image_content:
                    base64_img = self.encode_image_to_base64(image_content)
                    await self.handle_vision_image(base64_img, query, bot, message)
                    return False
        # 私聊
        elif is_quoted_image and content:
            image_content = await self.get_cached_image(quoted_sender)
            logger.info(f"handle_quote: 用 quoted_sender 命中图片缓存={image_content is not None}")
            if not image_content:
                image_content = await self.get_cached_image(message["FromWxid"])
                logger.info(f"handle_quote: 用 FromWxid 命中图片缓存={image_content is not None}")
            if not image_content:
                image_content = await self.get_cached_image(message["SenderWxid"])
                logger.info(f"handle_quote: 用 SenderWxid 命中图片缓存={image_content is not None}")
            if image_content:
                base64_img = self.encode_image_to_base64(image_content)
                await self.handle_vision_image(base64_img, content, bot, message)
                return False

        # 其他情况走原有 dify 流程
        if not content:
            query = f"请回复这条消息: '{quoted_content}'"
        else:
            query = f"{content} (引用消息: '{quoted_content}')"
        await self.dify(bot, message, query)
        return False

    @on_image_message(priority=20)
    async def handle_image(self, bot, message: dict):
        """处理图片消息并缓存图片内容"""
        try:
            msg_id = message.get("MsgId")
            from_wxid = message.get("FromWxid")
            sender_wxid = message.get("SenderWxid")
            logger.info(f"收到图片消息: 消息ID:{msg_id} 来自:{from_wxid} 发送人:{sender_wxid}")

            # 解析图片XML，获取图片大小
            xml_content = message.get("Content")
            length = None
            if isinstance(xml_content, str) and "<img " in xml_content:
                import xml.etree.ElementTree as ET
                try:
                    root = ET.fromstring(xml_content)
                    img_elem = root.find("img")
                    if img_elem is not None:
                        length = int(img_elem.get("length", "0"))
                        logger.info(f"解析图片XML成功: length={length}")
                except Exception as e:
                    logger.warning(f"解析图片XML失败: {e}")

            # 分段下载图片
            image_bytes = b""
            if length and msg_id:
                chunk_size = 65536
                chunks = (length + chunk_size - 1) // chunk_size
                logger.info(f"开始分段下载图片，总大小: {length} 字节，分 {chunks} 段下载")
                for i in range(chunks):
                    start_pos = i * chunk_size
                    try:
                        chunk = await bot.get_msg_image(msg_id, from_wxid, length, start_pos=start_pos)
                        if chunk:
                            image_bytes += chunk
                            logger.debug(f"第 {i+1}/{chunks} 段下载成功，大小: {len(chunk)} 字节")
                        else:
                            logger.error(f"第 {i+1}/{chunks} 段下载失败，数据为空")
                    except Exception as e:
                        logger.error(f"下载第 {i+1}/{chunks} 段时出错: {e}")
                logger.info(f"分段下载图片成功，总大小: {len(image_bytes)} 字节")
            else:
                logger.warning("未能获取图片长度或消息ID，无法分段下载图片")

            # 校验图片有效性
            if image_bytes:
                try:
                    Image.open(io.BytesIO(image_bytes))
                    logger.info(f"图片校验成功，准备缓存，大小: {len(image_bytes)} 字节")
                except Exception as e:
                    logger.error(f"图片校验失败: {e}")
                    image_bytes = None
            else:
                logger.warning("未能获取到有效的图片数据，未缓存")

            # 缓存图片
            if image_bytes:
                self.image_cache[sender_wxid] = {"content": image_bytes, "timestamp": time.time()}
                if from_wxid != sender_wxid:
                    self.image_cache[from_wxid] = {"content": image_bytes, "timestamp": time.time()}
                logger.info(f"图片缓存: sender_wxid={sender_wxid}, from_wxid={from_wxid}, 大小={len(image_bytes)}")
            else:
                logger.warning("handle_image: 未能缓存图片，因为图片数据无效")
        except Exception as e:
            logger.error(f"handle_image: 处理图片消息异常: {e}")

    async def dify(self, bot, message: dict, query: str):
        headers = {"Authorization": f"Bearer {self.default_model_api_key}", "Content-Type": "application/json"}
        payload = {
            "inputs": {},
            "query": query,
            "response_mode": "streaming",
            "user": message["FromWxid"],
            "auto_generate_name": False,
        }
        ai_resp = ""
        try:
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.post(url=f"{self.default_model_base_url}/chat-messages", headers=headers, data=json.dumps(payload)) as resp:
                    if resp.status in (200, 201):
                        async for line in resp.content:
                            line = line.decode("utf-8").strip()
                            if not line or line == "event: ping":
                                continue
                            elif line.startswith("data: "):
                                line = line[6:]
                            try:
                                resp_json = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            event = resp_json.get("event", "")
                            if event == "message":
                                ai_resp += resp_json.get("answer", "")
                            elif event == "message_replace":
                                ai_resp = resp_json.get("answer", "")
                            elif event == "message_end":
                                pass
                    else:
                        await bot.send_text_message(message["FromWxid"], f"Dify接口错误: {resp.status}")
                        return
            if ai_resp:
                await self.dify_handle_text(bot, message, ai_resp)
            else:
                await bot.send_text_message(message["FromWxid"], "未获取到有效回复。")
        except Exception as e:
            logger.error(f"Dify API 调用失败: {e}")
            await bot.send_text_message(message["FromWxid"], f"Dify API 调用失败: {e}")

    async def dify_handle_text(self, bot, message: dict, text: str):
        # 只发送文字内容
        if text:
            paragraphs = text.split("//n")
            for paragraph in paragraphs:
                if paragraph.strip():
                    await bot.send_text_message(message["FromWxid"], paragraph.strip())

    def encode_image_to_base64(self, image_bytes):
        return base64.b64encode(image_bytes).decode('utf-8')

    async def get_cached_image(self, user_wxid: str):
        """获取用户最近的图片，仿原有逻辑"""
        logger.info(f"尝试获取图片缓存: key={user_wxid}, 当前缓存keys={list(self.image_cache.keys())}")
        cache = self.image_cache.get(user_wxid)
        if cache:
            if time.time() - cache["timestamp"] <= self.image_cache_timeout:
                return cache["content"]
            else:
                del self.image_cache[user_wxid]
        return None

    async def handle_vision_image(self, base64_image, prompt, bot, message):
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.vision_api_key}"
        }
        payload = {
            "model": self.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ]
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.vision_api_base}/chat/completions", headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        response_json = await resp.json()
                        if "choices" in response_json and len(response_json["choices"]) > 0:
                            first_choice = response_json["choices"][0]
                            if "message" in first_choice and "content" in first_choice["message"]:
                                reply_content = first_choice["message"]["content"].strip()
                            else:
                                reply_content = "Content not found in the OpenAI API response"
                        else:
                            reply_content = "No choices available in the OpenAI API response"
                    else:
                        reply_content = f"识图API请求失败: {resp.status}"
        except Exception as e:
            reply_content = f"识图API调用异常: {e}"
        await bot.send_text_message(message["FromWxid"], reply_content)
