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


class Sum4all(PluginBase):
    description = "Sum4all插件"
    author = "老夏"
    version = "1.0.0"
    is_ai_platform = False

    def __init__(self):
        super().__init__()
        self.files_dir = "files" # Define files directory for MD5 lookup
        os.makedirs(self.files_dir, exist_ok=True)
        try:
            with open("plugins/Sum4all/config.toml", "rb") as f:
                config = tomllib.load(f)
            plugin_config = config["Sum4all"]
            self.enable = plugin_config["enable"]
            self.robot_names = plugin_config.get("robot_names", [])
            self.vision_prefix = plugin_config.get("vision_prefix", "识图")
            self.vision_api_key = plugin_config.get("vision_api_key", None)
            self.vision_api_base = plugin_config.get("vision_api_base", None)
            self.vision_model = plugin_config.get("vision_model", "o3")
        except Exception as e:
            logger.error(f"加载Sum4all插件配置文件失败: {e}")
            raise
        # 记录待识图状态: {user_or_group_id: timestamp}
        self.waiting_vision = {}
        # 图片缓存，防止重复处理
        self.image_msgid_cache = set()
        self.image_cache_timeout = 60
        self.image_cache = {}

    async def find_image_by_md5(self, md5: str) -> bytes | None:
        """Finds an image by its MD5 hash in the local files directory."""
        if not md5:
            logger.warning("Sum4all: MD5 is empty, cannot find image.")
            return None

        # Construct the full path to the files directory relative to the current working directory
        # Assumes XYBot runs from a root where 'files' is a subdirectory.
        # If files_dir is absolute, os.path.join behaves correctly.
        # For robustness, ensure files_dir is treated as relative to CWD if not absolute.
        # However, simple self.files_dir might be enough if CWD is consistently the workspace root.
        # Using os.path.join(os.getcwd(), self.files_dir) is safer if CWD can vary.
        # For now, let's assume self.files_dir is correctly relative or absolute as needed.
        # The log suggests /root/xxxbot-pad/files/, so direct path might be fine.

        common_extensions = ["jpeg", "jpg", "png", "gif", "webp"]
        for ext in common_extensions:
            # Path construction: The log shows an absolute path. 
            # If self.files_dir is "files", it will be relative to CWD.
            # To match "/root/xxxbot-pad/files/", self.files_dir would need to be absolute
            # or CWD must be /root/xxxbot-pad.
            # Let's try a direct relative path first, then consider getcwd if issues persist.
            # The user log example path is /root/xxxbot-pad/files/MD5.jpeg
            # This implies that just using self.files_dir for the md5 lookup should be relative to wherever the bot is running.
            # Or, more simply, the file paths are usually relative to the workspace root.
            file_path = os.path.join(self.files_dir, f"{md5}.{ext}")
            if os.path.exists(file_path):
                try:
                    with open(file_path, "rb") as f:
                        image_data = f.read()
                    logger.info(f"Sum4all: Found image by MD5: {file_path}, size: {len(image_data)} bytes")
                    return image_data
                except Exception as e:
                    logger.error(f"Sum4all: Failed to read image file {file_path} by MD5: {e}")
                    return None # Error reading file
        logger.warning(f"Sum4all: Image file with MD5 {md5} not found in {self.files_dir} with common extensions.")
        return None

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
        is_trigger = False
        user_prompt = None
        # 群聊和私聊都直接判断是否以vision_prefix开头
        if content.startswith(self.vision_prefix):
            is_trigger = True
            user_prompt = content[len(self.vision_prefix):].strip()
        if is_trigger:
            key = self.get_waiting_key(message)
            if not user_prompt:
                user_prompt = "请识别这张图片的内容。"
            self.waiting_vision[key] = {
                "timestamp": time.time(),
                "prompt": user_prompt
            }
            logger.info(f"Sum4all: 记录待识图状态: {key}, prompt: {user_prompt}")
            tip = "💡已开启识图模式(o3)，您接下来第一张图片会进行识别。\n当前的提示词为：\n" + user_prompt
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            return False  # 阻止后续插件处理
        return True  # 允许后续插件处理

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

    @on_quote_message(priority=31)
    async def handle_quote_vision(self, bot, message: dict):
        if not self.enable:
            return True

        current_msg_id = message.get("MsgId")
        # Prevent re-processing the same quote message
        if current_msg_id and current_msg_id in self.image_msgid_cache:
            logger.info(f"Sum4all (quote): Message ID {current_msg_id} already processed, skipping.")
            return True

        content = message["Content"].strip()
        quote_info = message.get("Quote", {})

        # Check if vision_prefix is in the text and the quoted message is an image
        if not (self.vision_prefix in content and quote_info.get("MsgType") == 3):
            return True  # Conditions not met, let other handlers try

        logger.info(f"Sum4all: Detected vision prefix in quote message for an image. MsgId: {current_msg_id}")

        # Extract user prompt
        user_prompt = "请识别这张图片的内容。"  # Default prompt
        try:
            idx = content.find(self.vision_prefix)
            if idx != -1:
                extracted_prompt = content[idx + len(self.vision_prefix):].strip()
                if extracted_prompt:
                    user_prompt = extracted_prompt
        except Exception as e:
            logger.warning(f"Sum4all (quote): Error extracting prompt: {e}. Using default.")
        
        logger.info(f"Sum4all (quote): User prompt: '{user_prompt}'")

        quoted_xml_content = quote_info.get("Content")
        # quoted_msg_id_for_download = quote_info.get("NewMsgId") # ServerID - problematic for download
        # quoted_from_wxid = quote_info.get("FromWxid") # Original sender/group of the image

        if not quoted_xml_content:
            logger.warning(f"Sum4all (quote): Quoted message XML content is missing.")
            return True

        image_bytes = b""
        md5 = None
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(quoted_xml_content)
            img_elem = root.find("img")
            if img_elem is not None:
                md5 = img_elem.get("md5")
                length_str = img_elem.get("length", "0") # Get length for logging if needed
                logger.info(f"Sum4all (quote): Parsed quoted image XML: md5={md5}, length={length_str}")
                if md5:
                    image_bytes = await self.find_image_by_md5(md5)
                    if image_bytes:
                        logger.info(f"Sum4all (quote): Image found locally by MD5: {md5}, size: {len(image_bytes)}")
                    else:
                        logger.warning(f"Sum4all (quote): Image with MD5 {md5} not found locally. Cannot perform vision.")
                else:
                    logger.warning(f"Sum4all (quote): MD5 not found in quoted image XML.")
            else:
                logger.warning(f"Sum4all (quote): No <img> element in quoted XML.")
        except Exception as e:
            logger.error(f"Sum4all (quote): Failed to parse quoted image XML or find by MD5: {e}")
            logger.error(traceback.format_exc())
            image_bytes = b"" # Ensure image_bytes is empty on error

        if image_bytes and len(image_bytes) > 0:
            try:
                Image.open(io.BytesIO(image_bytes))  # Validate image
                logger.info(f"Sum4all (quote): Quoted image (MD5: {md5}) validated, size: {len(image_bytes)}. Proceeding with vision.")
                
                await self.handle_vision_image(image_bytes, bot, message, user_prompt)

                key = self.get_waiting_key(message)
                if key in self.waiting_vision:
                    logger.info(f"Sum4all (quote): Clearing pending vision state for key: {key}")
                    self.waiting_vision.pop(key, None)
                
                if current_msg_id:
                    self.image_msgid_cache.add(current_msg_id)
                    logger.info(f"Sum4all (quote): Added quote MsgId {current_msg_id} to cache.")

                return False  # Handled, stop further processing
            except Exception as e:
                logger.error(f"Sum4all (quote): Quoted image (MD5: {md5}) validation failed: {e}, image_bytes_prefix: {image_bytes[:100]}")
                reply_content = f"引用的图片解析失败或无效，无法识图。" 
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], reply_content, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], reply_content)
                if current_msg_id:
                    self.image_msgid_cache.add(current_msg_id)
                return False 
        else:
            # This block is reached if image_bytes is empty (not found by MD5 or other errors)
            logger.warning(f"Sum4all (quote): Failed to get valid image bytes from quote (MD5: {md5}). Not performing vision.")
            reply_content = "未能从本地缓存中获取引用的图片数据，无法识图。请确保图片最近发送过。"
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], reply_content, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], reply_content)
            if current_msg_id:
                self.image_msgid_cache.add(current_msg_id)
            return False # Stop processing, as we intended to handle it but failed.

        # Fallback, should ideally not be reached if logic is correct for quote + vision_prefix
        return True 

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
        waiting_info = self.waiting_vision.get(key)
        if not waiting_info:
            logger.info(f"Sum4all: 当前无待识图状态: {key}")
            return True
        user_prompt = waiting_info.get("prompt", "请识别这张图片的内容。")
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
                logger.info(f"Sum4all: 图片校验通过，准备识图，大小: {len(image_bytes)} 字节")
                await self.handle_vision_image(image_bytes, bot, message, user_prompt)
            except Exception as e:
                logger.error(f"Sum4all: 图片校验失败: {e}, image_bytes前100字节: {image_bytes[:100]}")
                reply_content = "图片文件无效或已损坏，识图失败。"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], reply_content, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], reply_content)
        else:
            logger.warning("Sum4all: 未能获取到有效的图片数据，未识图")
            reply_content = "无法下载图片，识图失败。"
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], reply_content, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], reply_content)
        # 状态清理
        self.waiting_vision.pop(key, None)
        self.image_msgid_cache.add(msg_id)
        logger.info(f"Sum4all: 识图流程结束: MsgId={msg_id}")
        return False

    async def handle_vision_image(self, image_bytes, bot, message, prompt):
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
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
                                reply_content = "未获取到有效识图内容"
                        else:
                            reply_content = "未获取到有效识图内容"
                    else:
                        reply_content = f"识图API请求失败: {resp.status}"
        except Exception as e:
            reply_content = f"识图API调用异常: {e}"
        if message["IsGroup"]:
            await bot.send_at_message(message["FromWxid"], reply_content, [message["SenderWxid"]])
        else:
            await bot.send_text_message(message["FromWxid"], reply_content)
