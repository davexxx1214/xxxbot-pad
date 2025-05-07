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
from utils.decorators import on_text_message, on_at_message, on_quote_message

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
        if not content:
            query = f"请回复这条消息: '{quoted_content}'"
        else:
            query = f"{content} (引用消息: '{quoted_content}')"
        await self.dify(bot, message, query)
        return False

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
