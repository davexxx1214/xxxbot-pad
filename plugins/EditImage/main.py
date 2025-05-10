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


class EditImage(PluginBase):
    description = "垫图插件"
    author = "老夏"
    version = "1.0.0"
    is_ai_platform = False

    def __init__(self):
        super().__init__()
        try:
            with open("plugins/EditImage/config.toml", "rb") as f:
                config = tomllib.load(f)
            plugin_config = config["EditImage"]
            self.enable = plugin_config["enable"]
            self.robot_names = plugin_config.get("robot_names", [])
            self.edit_image_prefix = plugin_config.get("edit_image_prefix", "垫图")
            self.openai_image_api_key = plugin_config.get("openai_image_api_key", None)
            self.openai_image_api_base = plugin_config.get("openai_image_api_base", None)
            self.image_model = plugin_config.get("image_model", "gpt-image-1")
        except Exception as e:
            logger.error(f"加载垫图插件配置文件失败: {e}")
            raise
        # 记录待垫图状态: {user_or_group_id: {timestamp, prompt}}
        self.waiting_edit_image = {}
        # 图片缓存，防止重复处理
        self.image_msgid_cache = set()
        self.image_cache_timeout = 60
        self.image_cache = {}

    def is_at_message(self, message: dict) -> bool:
        if not message.get("IsGroup"):
            return False
        content = message.get("Content", "")
        # 新增：先去掉"昵称: 换行"前缀
        content = regex.sub(r"^[^@\n]+:\s*\n", "", content)
        logger.info(f"EditImage is_at_message: content repr={repr(content)} robot_names={self.robot_names}")
        for robot_name in self.robot_names:
            if regex.match(f"^@{robot_name}[\\p{{Zs}}\\s]*", content):
                return True
        return False

    def get_waiting_key(self, message: dict):
        # 群聊只用群聊ID，所有人共用同一个垫图状态
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
        is_trigger = False
        user_prompt = None
        # 改为：只要内容包含edit_image_prefix即可
        if self.edit_image_prefix in content:
            is_trigger = True
            idx = content.find(self.edit_image_prefix)
            user_prompt = content[idx + len(self.edit_image_prefix):].strip()
        if is_trigger:
            key = self.get_waiting_key(message)
            if not user_prompt:
                user_prompt = "请描述您要编辑图片的内容。"
            self.waiting_edit_image[key] = {
                "timestamp": time.time(),
                "prompt": user_prompt
            }
            tip = "💡已开启图片编辑模式(gpt-4o)，您接下来第一张图片会进行编辑。\n当前的提示词为：\n" + user_prompt
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
        logger.info(f"EditImage: 收到图片消息: MsgId={msg_id}, FromWxid={from_wxid}, SenderWxid={sender_wxid}, ContentType={type(xml_content)}")
        # 消息ID去重
        if not msg_id or msg_id in self.image_msgid_cache:
            logger.info(f"EditImage: 消息ID {msg_id} 已处理或无效，跳过")
            return True
        key = f"{from_wxid}|{sender_wxid}" if message.get("IsGroup") else sender_wxid
        waiting_info = self.waiting_edit_image.get(key)
        if not waiting_info:
            logger.info(f"EditImage: 当前无待垫图状态: {key}")
            return True
        user_prompt = waiting_info.get("prompt", "请描述您要编辑图片的内容。")
        image_bytes = b""
        # 1. xml格式，分段下载
        if isinstance(xml_content, str) and "<img " in xml_content:
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(xml_content)
                img_elem = root.find("img")
                if img_elem is not None:
                    length = int(img_elem.get("length", "0"))
                    logger.info(f"EditImage: 解析图片XML成功: length={length}")
                    if length and msg_id:
                        chunk_size = 65536
                        chunks = (length + chunk_size - 1) // chunk_size
                        logger.info(f"EditImage: 开始分段下载图片，总大小: {length} 字节，分 {chunks} 段下载")
                        for i in range(chunks):
                            start_pos = i * chunk_size
                            try:
                                chunk = await bot.get_msg_image(msg_id, from_wxid, length, start_pos=start_pos)
                                if chunk:
                                    image_bytes += chunk
                                    logger.debug(f"EditImage: 第 {i+1}/{chunks} 段下载成功，大小: {len(chunk)} 字节")
                                else:
                                    logger.error(f"EditImage: 第 {i+1}/{chunks} 段下载失败，数据为空")
                            except Exception as e:
                                logger.error(f"EditImage: 下载第 {i+1}/{chunks} 段时出错: {e}")
                        logger.info(f"EditImage: 分段下载图片成功，总大小: {len(image_bytes)} 字节")
            except Exception as e:
                logger.warning(f"EditImage: 解析图片XML失败: {e}")
        # 2. base64格式，直接解码
        elif isinstance(xml_content, str):
            try:
                if len(xml_content) > 100 and not xml_content.strip().startswith("<?xml"):
                    logger.info("EditImage: 尝试base64解码图片内容")
                    import base64
                    image_bytes = base64.b64decode(xml_content)
            except Exception as e:
                logger.warning(f"EditImage: base64解码失败: {e}")

        # 校验图片有效性
        if image_bytes and len(image_bytes) > 0:
            try:
                Image.open(io.BytesIO(image_bytes))
                logger.info(f"EditImage: 图片校验通过，准备垫图，大小: {len(image_bytes)} 字节")
                await self.handle_edit_image(image_bytes, bot, message, user_prompt)
            except Exception as e:
                logger.error(f"EditImage: 图片校验失败: {e}, image_bytes前100字节: {image_bytes[:100]}")
        else:
            logger.warning("EditImage: 未能获取到有效的图片数据，未垫图")
        # 状态清理
        self.waiting_edit_image.pop(key, None)
        self.image_msgid_cache.add(msg_id)
        logger.info(f"EditImage: 垫图流程结束: MsgId={msg_id}")
        return False

    async def handle_edit_image(self, image_bytes, bot, message, prompt):
        """调用图片编辑API并返回结果"""
        import uuid
        import tempfile
        import base64
        # 保存图片到临时文件
        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp_file:
            tmp_file.write(image_bytes)
            tmp_file_path = tmp_file.name
        try:
            # 发送请求前的提示
            tip_msg = f"🎨 gpt-image-1垫图请求已进入队列，预计需要30-150秒完成。请稍候...\n提示词：{prompt}"
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip_msg, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip_msg)
            # 构建API请求
            url = f"{self.openai_image_api_base}/images/edits"
            headers = {
                "Authorization": f"Bearer {self.openai_image_api_key}"
            }
            data = aiohttp.FormData()
            data.add_field('image', open(tmp_file_path, 'rb'), filename='image.png', content_type='image/png')
            data.add_field('model', self.image_model)
            data.add_field('prompt', prompt)
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=data, timeout=1200) as resp:
                    if resp.status != 200:
                        try:
                            error_json = await resp.json()
                            if "error" in error_json and "code" in error_json["error"]:
                                if error_json["error"]["code"] == "moderation_blocked" or "safety" in error_json["error"]["message"].lower():
                                    error_message = "触发了图片的安全审查，请尝试使用其他图片或修改提示词。"
                                else:
                                    error_message = f"图片编辑失败: {await resp.text()}"
                            else:
                                error_message = f"图片编辑失败: {await resp.text()}"
                        except:
                            error_message = f"图片编辑失败: {await resp.text()}"
                        if message["IsGroup"]:
                            await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
                        else:
                            await bot.send_text_message(message["FromWxid"], error_message)
                        return
                    result = await resp.json()
                    if "data" in result and len(result["data"]) > 0:
                        image_data = result["data"][0]
                        if "b64_json" in image_data and image_data["b64_json"]:
                            image_bytes = base64.b64decode(image_data["b64_json"])
                            # 直接发送图片字节
                            if message["IsGroup"]:
                                await bot.send_image_message(message["FromWxid"], image_bytes)
                                await bot.send_at_message(message["FromWxid"], "🖼️ 您的图片已编辑完成！", [message["SenderWxid"]])
                            else:
                                await bot.send_image_message(message["FromWxid"], image_bytes)
                        else:
                            error_message = "图片编辑失败，API没有返回图片数据"
                            if message["IsGroup"]:
                                await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
                            else:
                                await bot.send_text_message(message["FromWxid"], error_message)
                    else:
                        error_message = "图片编辑失败，API返回格式不正确"
                        if message["IsGroup"]:
                            await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
                        else:
                            await bot.send_text_message(message["FromWxid"], error_message)
        except Exception as e:
            logger.error(f"EditImage: 图片编辑服务异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            error_message = f"图片编辑服务出错: {str(e)}"
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], error_message)
        finally:
            try:
                os.remove(tmp_file_path)
            except Exception:
                pass
