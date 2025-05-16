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
import regex

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
        # 新增：本地会话ID存储dict
        self._conversation_ids = {}
        # 新增：本地问题历史存储dict
        self._question_history = {}
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
            self.robot_names = plugin_config.get("robot_names", [])
            self.image_generation_enabled = bool(plugin_config.get("openai_image_api_key", None))
            self.openai_image_api_key = plugin_config.get("openai_image_api_key", None)
            self.openai_image_api_base = plugin_config.get("openai_image_api_base", "https://api.openai.com/v1")
            self.image_model = plugin_config.get("image_model", "dall-e-3")
        except Exception as e:
            logger.error(f"加载Dify插件配置文件失败: {e}")
            raise

    def is_at_message(self, message: dict) -> bool:
        if not message.get("IsGroup"):
            return False
        content = message.get("Content", "")
        content = regex.sub(r"^[^@\n]+:\s*\n", "", content)
        logger.info(f"Dify is_at_message: content repr={repr(content)} robot_names={self.robot_names}")
        if self.robot_names:
            for robot_name in self.robot_names:
                if regex.match(f"^@{robot_name}[\p{{Zs}}\s]*", content):
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
            return True # Allow other plugins if Dify is disabled

        raw_content = message["Content"]
        # 1. Remove "Nick:\\n" prefix
        content_after_nick_strip = regex.sub(r"^[^@\\n]+:\\s*\\n", "", raw_content).strip()

        if not content_after_nick_strip:
            return True # Empty after nick stripping, let others handle

        # 2. Attempt to remove "@BotName " prefix from content_after_nick_strip.
        # This makes handle_text more robust for messages that are effectively @-mentions
        # but might be routed as text messages by the core dispatcher.
        final_query = content_after_nick_strip
        was_explicit_at_mention_processed = False # Flag to see if we stripped a known bot name

        if content_after_nick_strip.startswith("@"): # Optimization: only loop if it starts with @
            for robot_name in self.robot_names:
                match = regex.match(f"^@{robot_name}[\\p{{Zs}}\\s]*", content_after_nick_strip)
                if match:
                    final_query = content_after_nick_strip[match.end():].strip()
                    was_explicit_at_mention_processed = True
                    break
        
        # Scenario: User message was "@BotName" or "@BotName 画" which became empty after stripping.
        if was_explicit_at_mention_processed and not final_query:
            # Check if original command (before stripping @BotName) was just "画"
            original_command_after_at = ""
            if content_after_nick_strip.startswith("@"):
                 for robot_name in self.robot_names: # Re-evaluate to get the part after @BotName
                    match = regex.match(f"^@{robot_name}[\\p{{Zs}}\\s]*", content_after_nick_strip)
                    if match:
                        original_command_after_at = content_after_nick_strip[match.end():].strip()
                        break
            
            if original_command_after_at == "画": # User typed "@BotName 画"
                 if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], "\\n请输入绘画内容。", [message["SenderWxid"]])
                 else:
                    await bot.send_text_message(message["FromWxid"], "请输入绘画内容。")
                 return False # Handled: empty "画" command after @BotName

            # If it was just "@BotName" (and not "@BotName 画"), then final_query is empty.
            # Don't send to Dify. Let other plugins handle or ignore.
            return True

        # If final_query is empty here, it means original content_after_nick_strip was empty.
        if not final_query:
            return True

        # 3. Process "画" command with the (potentially further cleaned) final_query
        if final_query.startswith("画") and self.image_generation_enabled:
            prompt = final_query[len("画"):].strip()
            if prompt:
                await self.generate_openai_image(bot, message, prompt)
            else: # "画" or "画 " (or if "@BotName 画" was handled above, this is for "画" alone)
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], "\\n请输入绘画内容。", [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], "请输入绘画内容。")
            return False # Handled "画" command

        # 4. If not "画" and final_query is not empty, send to Dify
        await self.dify(bot, message, final_query)
        return False # Indicate message handled by Dify

    @on_at_message(priority=20)
    async def handle_at(self, bot, message: dict):
        if not self.enable:
            return

        raw_content = message["Content"]
        # 1. Remove "Nick:\n" prefix
        processed_content = regex.sub(r"^[^@\n]+:\s*\n", "", raw_content).strip()

        # 2. Remove "@BotName " prefix from the remaining content
        actual_query = processed_content
        for robot_name in self.robot_names:
            match = regex.match(f"^@{robot_name}[\p{{Zs}}\s]*", actual_query)
            if match:
                actual_query = actual_query[match.end():].strip()
                break
        
        # Now use actual_query for logic
        if actual_query.startswith("画") and self.image_generation_enabled:
            prompt = actual_query[len("画"):].strip()
            if prompt:
                await self.generate_openai_image(bot, message, prompt)
            else:
                await bot.send_at_message(message["FromWxid"], "\n请输入绘画内容。", [message["SenderWxid"]])
            return False  # Indicate message handled

        await self.dify(bot, message, actual_query) # Pass the fully cleaned query
        return False # Indicate message handled

    @on_quote_message(priority=20)
    async def handle_quote(self, bot, message: dict):
        if not self.enable:
            return
        # 只在群聊且@了机器人时才处理
        if message.get("IsGroup") and not self.is_at_message(message):
            return False

        raw_content = message["Content"] # This is the text part of the quote message

        # 1. Remove "Nick:\n" prefix
        processed_content = regex.sub(r"^[^@\n]+:\s*\n", "", raw_content).strip()

        # 2. Remove "@BotName " prefix
        actual_text_part = processed_content
        for robot_name in self.robot_names:
            match = regex.match(f"^@{robot_name}[\p{{Zs}}\s]*", actual_text_part)
            if match:
                actual_text_part = actual_text_part[match.end():].strip()
                break

        quote_info = message.get("Quote", {})
        quoted_content = quote_info.get("Content", "")

        query_for_dify = ""
        if not actual_text_part: # If the text part after @BotName is empty
            query_for_dify = f"请回复这条消息: '{quoted_content}'"
        else:
            query_for_dify = f"{actual_text_part} (引用消息: '{quoted_content}')"
        
        await self.dify(bot, message, query_for_dify)
        return False

    async def dify(self, bot, message: dict, query: str):
        # 新实现：群聊用群ID，私聊用用户ID，整个群为单位存储
        if message.get("IsGroup"):
            context_key = f"group_{message['FromWxid']}"
        else:
            context_key = f"private_{message['FromWxid']}"
        
        # --- 关键的调试打印 --- logger.info(f"Dify method: Received query parameter: '{query}'")

        # 维护问题历史，只存最近3条
        history = self._question_history.get(context_key, [])
        history.append(query) # query 应该是清理过的，例如 "你好"
        if len(history) > 3:
            history = history[-3:]
        self._question_history[context_key] = history

        # --- 再次确认 history[-1] --- logger.info(f"Dify method: history[-1] before assembling prompt: '{history[-1]}'")

        # 组装内容
        prompt_prefix = ''
        if len(history) > 1:
            prev_questions = history[:-1]
            prev_str = ' '.join([f"{i+1}. {q}" for i, q in enumerate(prev_questions)])
            prompt_prefix = f"用户之前作为参考的对话：\n{prev_str}\n"
        
        final_prompt_for_api = f"{prompt_prefix}回答用户最新的问题：\n{history[-1]}\n" # history[-1] 应该是纯净的 "你好"

        # --- 打印将要发送的 prompt --- logger.info(f"Dify method: Final prompt for API: '{final_prompt_for_api}'")

        headers = {"Authorization": f"Bearer {self.default_model_api_key}", "Content-Type": "application/json"}
        payload = {
            "inputs": {},
            "query": final_prompt_for_api, # 使用这个最终构建的 prompt
            "response_mode": "streaming",
            "user": message["FromWxid"], # For Dify: this is the end-user identifier
            "auto_generate_name": False,
        }
        # 这条日志现在应该能准确反映 final_prompt_for_api 的内容
        logger.info(f"Dify Plugin: Payload sent to Dify API (re-check): {json.dumps(payload, ensure_ascii=False)}")

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
                                answer_chunk = resp_json.get("answer", "")
                                # Add logging for raw answer chunk from Dify
                                logger.info(f"Dify Plugin: Raw answer chunk from Dify (event=message): {answer_chunk}")
                                ai_resp += answer_chunk
                            elif event == "message_replace":
                                ai_resp = resp_json.get("answer", "")
                                # Add logging for raw answer from Dify for message_replace
                                logger.info(f"Dify Plugin: Raw answer from Dify (event=message_replace): {ai_resp}")
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
                    if message.get("IsGroup"):
                        # 群聊中@发消息的人
                        await bot.send_at_message(
                            message["FromWxid"],
                            "\n" + paragraph.strip(),
                            [message["SenderWxid"]]
                        )
                    else:
                        await bot.send_text_message(message["FromWxid"], paragraph.strip())
