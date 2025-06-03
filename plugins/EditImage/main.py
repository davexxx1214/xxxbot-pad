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
import regex  # 不是re，是regex库，支持\\p{Zs}
import asyncio # 新增
import google.generativeai as genai # 新增
# Revert to importing the types module and aliasing it
from google.generativeai import types as genai_types
# import tempfile # No longer needed for this version


class EditImage(PluginBase):
    description = "垫图、修图和多图编辑插件" # 修改描述
    author = "老夏"
    version = "1.1.0" # 修改版本
    is_ai_platform = False

    def __init__(self):
        super().__init__()
        self.files_dir = "files"  # Define files directory for MD5 lookup
        os.makedirs(self.files_dir, exist_ok=True)
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

            # 新增 Gemini 相关配置
            self.inpaint_prefix = plugin_config.get("inpaint_prefix", "修图")
            self.google_api_key = plugin_config.get("google_api_key", None)
            self.gemini_model_name = plugin_config.get("gemini_model_name", "models/gemini-pro-vision") # 默认使用 vision

            # 新增：多图编辑配置
            self.blend_prefix = plugin_config.get("blend_prefix", "/b")
            self.end_prefix = plugin_config.get("end_prefix", "/e")

        except Exception as e:
            logger.error(f"加载垫图/修图插件配置文件失败: {e}") # 修改日志
            raise
        # 记录待垫图状态: {user_or_group_id: {timestamp, prompt}}
        self.waiting_edit_image = {}
        # 新增：记录待修图状态
        self.waiting_inpaint_image = {}
        # 新增：记录多图编辑状态: {user_or_group_id: {timestamp, prompt, images}}
        self.waiting_blend = {}

        # 图片缓存，防止重复处理
        self.image_msgid_cache = set()
        self.image_cache_timeout = 60 # 未使用
        self.image_cache = {} # 未使用

        # 初始化Google Gemini客户端
        if self.google_api_key:
            try:
                genai.configure(api_key=self.google_api_key)
                self.gemini_client = genai.GenerativeModel(self.gemini_model_name)
                logger.info(f"[EditImage] Google Gemini client initialized with model {self.gemini_model_name}.")
            except Exception as e:
                logger.error(f"[EditImage] Failed to initialize Google Gemini client: {e}")
                self.gemini_client = None
        else:
            logger.warning("[EditImage] Google API key not provided, Gemini修图功能将不可用。")
            self.gemini_client = None


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
        
        key = self.get_waiting_key(message)
        
        # 处理 "垫图" 指令
        if self.edit_image_prefix in content:
            idx = content.find(self.edit_image_prefix)
            user_prompt = content[idx + len(self.edit_image_prefix):].strip()
            if not user_prompt:
                user_prompt = "请描述您要编辑图片的内容。"
            self.waiting_edit_image[key] = {
                "timestamp": time.time(),
                "prompt": user_prompt
            }
            # 清除可能存在的修图状态
            if key in self.waiting_inpaint_image:
                del self.waiting_inpaint_image[key]
            tip = f"💡已开启图片编辑模式({self.image_model})，您接下来第一张图片会进行编辑。\n当前的提示词为：\n" + user_prompt
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            return False

        # 新增：处理 "修图" (Gemini Inpaint) 指令
        if self.inpaint_prefix in content:
            if not self.gemini_client:
                tip = "抱歉，Gemini修图服务当前不可用，请联系管理员检查配置。"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], tip)
                return False

            idx = content.find(self.inpaint_prefix)
            user_prompt = content[idx + len(self.inpaint_prefix):].strip()
            if not user_prompt:
                user_prompt = "请描述您要对图片进行的修改。" # Gemini 的提示可以更通用
            self.waiting_inpaint_image[key] = {
                "timestamp": time.time(),
                "prompt": user_prompt
            }
            # 清除可能存在的垫图状态
            if key in self.waiting_edit_image:
                del self.waiting_edit_image[key]
            tip = f"💡已开启Gemini修图模式({self.gemini_model_name})，您接下来第一张图片会进行修图。\n当前的提示词为：\n" + user_prompt
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            return False
            
        # 新增：多图编辑功能
        if content.startswith(self.blend_prefix):
            user_prompt = content[len(self.blend_prefix):].strip()
            if not user_prompt:
                tip = f"💡欢迎使用多图编辑功能，指令格式为:\n\n{self.blend_prefix} + 空格 + 图片描述\n\n📝 示例：\n{self.blend_prefix} 把两只猫融合在一起\n{self.blend_prefix} 将第一张图的人物放到第二张图的背景中"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], tip)
                return False
            
            # 清理之前的状态（如果存在）
            self.waiting_blend[key] = {
                "timestamp": time.time(),
                "prompt": user_prompt,
                "images": []
            }
            tip = f"✨ 多图编辑模式已开启\n✏ 请发送至少2张图片，然后发送 '{self.end_prefix}' 结束上传并开始处理。\n当前提示词：{user_prompt}"
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            return False
        
        # 新增：结束多图编辑模式
        if content.startswith(self.end_prefix):
            waiting_blend_info = self.waiting_blend.get(key)
            if waiting_blend_info:
                images = waiting_blend_info.get("images", [])
                prompt = waiting_blend_info.get("prompt", "多图编辑")
                if len(images) >= 2:
                    logger.info(f"EditImage: 开始多图编辑，用户 {key}，{len(images)} 张图片")
                    # 先回复收到请求
                    notice = "您的多图编辑请求已经收到，请稍候..."
                    if message["IsGroup"]:
                        await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
                    else:
                        await bot.send_text_message(message["FromWxid"], notice)
                    
                    await self.handle_blend_service(images, prompt, message, bot)
                    # 清理状态
                    self.waiting_blend.pop(key, None)
                else:
                    tip = f"✨ 多图编辑模式\n✏ 您需要发送至少2张图片才能开始多图编辑。当前已发送 {len(images)} 张。请继续发送图片或重新开始。"
                    if message["IsGroup"]:
                        await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                    else:
                        await bot.send_text_message(message["FromWxid"], tip)
            return False
            
        return True

    @on_at_message(priority=30)
    async def handle_at(self, bot, message: dict):
        if not self.enable:
            return True
        content = message["Content"].strip()
        # 移除@前缀，方便匹配
        cleaned_content = regex.sub(f"^@[^\\s]+\\s*", "", content).strip()

        key = self.get_waiting_key(message)

        # 处理 "垫图" 指令
        if self.edit_image_prefix in cleaned_content:
            idx = cleaned_content.find(self.edit_image_prefix)
            user_prompt = cleaned_content[idx + len(self.edit_image_prefix):].strip()
            if not user_prompt:
                user_prompt = "请描述您要编辑图片的内容。"
            self.waiting_edit_image[key] = {
                "timestamp": time.time(),
                "prompt": user_prompt
            }
            if key in self.waiting_inpaint_image:
                del self.waiting_inpaint_image[key]
            tip = f"💡已开启图片编辑模式({self.image_model})，您接下来第一张图片会进行编辑。\n当前的提示词为：\n" + user_prompt
            await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            return False

        # 新增：处理 "修图" (Gemini Inpaint) 指令
        if self.inpaint_prefix in cleaned_content:
            if not self.gemini_client:
                tip = "抱歉，Gemini修图服务当前不可用，请联系管理员检查配置。"
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                return False
                
            idx = cleaned_content.find(self.inpaint_prefix)
            user_prompt = cleaned_content[idx + len(self.inpaint_prefix):].strip()
            if not user_prompt:
                user_prompt = "请描述您要对图片进行的修改。"
            self.waiting_inpaint_image[key] = {
                "timestamp": time.time(),
                "prompt": user_prompt
            }
            if key in self.waiting_edit_image:
                del self.waiting_edit_image[key]
            tip = f"💡已开启Gemini修图模式({self.gemini_model_name})，您接下来第一张图片会进行修图。\n当前的提示词为：\n" + user_prompt
            await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            return False
            
        # 新增：多图编辑功能
        if self.blend_prefix in cleaned_content:
            idx = cleaned_content.find(self.blend_prefix)
            user_prompt = cleaned_content[idx + len(self.blend_prefix):].strip()
            if not user_prompt:
                tip = f"💡欢迎使用多图编辑功能，指令格式为:\n\n{self.blend_prefix} + 空格 + 图片描述\n\n📝 示例：\n{self.blend_prefix} 把两只猫融合在一起\n{self.blend_prefix} 将第一张图的人物放到第二张图的背景中"
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                return False
            
            # 清理之前的状态（如果存在）
            self.waiting_blend[key] = {
                "timestamp": time.time(),
                "prompt": user_prompt,
                "images": []
            }
            tip = f"✨ 多图编辑模式已开启\n✏ 请发送至少2张图片，然后发送 '{self.end_prefix}' 结束上传并开始处理。\n当前提示词：{user_prompt}"
            await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            return False
        
        # 新增：结束多图编辑模式
        if self.end_prefix in cleaned_content:
            waiting_blend_info = self.waiting_blend.get(key)
            if waiting_blend_info:
                images = waiting_blend_info.get("images", [])
                prompt = waiting_blend_info.get("prompt", "多图编辑")
                if len(images) >= 2:
                    logger.info(f"EditImage: 开始多图编辑，用户 {key}，{len(images)} 张图片")
                    # 先回复收到请求
                    notice = "您的多图编辑请求已经收到，请稍候..."
                    await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
                    
                    await self.handle_blend_service(images, prompt, message, bot)
                    # 清理状态
                    self.waiting_blend.pop(key, None)
                else:
                    tip = f"✨ 多图编辑模式\n✏ 您需要发送至少2张图片才能开始多图编辑。当前已发送 {len(images)} 张。请继续发送图片或重新开始。"
                    await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            return False
            
        return True

    @on_image_message(priority=30)
    async def handle_image(self, bot, message: dict):
        if not self.enable:
            return True
        msg_id = message.get("MsgId")
        from_wxid = message.get("FromWxid")
        # sender_wxid = message.get("SenderWxid") # 在具体处理函数中使用
        xml_content = message.get("Content")
        logger.info(f"EditImage: 收到图片消息: MsgId={msg_id}, FromWxid={from_wxid}, ContentType={type(xml_content)}")
        
        if not msg_id or msg_id in self.image_msgid_cache:
            logger.info(f"EditImage: 消息ID {msg_id} 已处理或无效，跳过")
            return True
            
        key = self.get_waiting_key(message)
        
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
                    image_bytes = base64.b64decode(xml_content)
            except Exception as e:
                logger.warning(f"EditImage: base64解码失败: {e}")

        # 校验图片有效性
        if image_bytes and len(image_bytes) > 0:
            try:
                Image.open(io.BytesIO(image_bytes))
                logger.info(f"EditImage: 图片校验通过，准备处理，大小: {len(image_bytes)} 字节")
            except Exception as e:
                logger.error(f"EditImage: 图片校验失败: {e}, image_bytes前100字节: {image_bytes[:100]}")
                return True # 允许其他插件处理或不处理
        else:
            logger.warning("EditImage: 未能获取到有效的图片数据，跳过处理")
            return True # 允许其他插件处理或不处理

        # 检查是否有垫图任务
        waiting_edit_info = self.waiting_edit_image.get(key)
        if waiting_edit_info:
            user_prompt = waiting_edit_info.get("prompt", "请描述您要编辑图片的内容。")
            logger.info(f"EditImage: 检测到垫图任务 for key {key}, prompt: {user_prompt}")
            await self.handle_edit_image_openai(image_bytes, bot, message, user_prompt) # 修改函数名以区分
            self.waiting_edit_image.pop(key, None)
            self.image_msgid_cache.add(msg_id)
            logger.info(f"EditImage: 垫图流程结束: MsgId={msg_id}")
            return False # 阻止后续插件处理

        # 新增：检查是否有Gemini修图任务
        waiting_inpaint_info = self.waiting_inpaint_image.get(key)
        if waiting_inpaint_info:
            if not self.gemini_client:
                logger.warning(f"EditImage: Gemini修图任务 for key {key} 但客户端未初始化。")
                # 可以选择回复用户或静默失败
                self.waiting_inpaint_image.pop(key, None)
                return True # 允许其他插件处理

            user_prompt = waiting_inpaint_info.get("prompt", "请描述您要对图片进行的修改。")
            logger.info(f"EditImage: 检测到Gemini修图任务 for key {key}, prompt: {user_prompt}")
            await self.handle_inpaint_image_with_gemini(image_bytes, bot, message, user_prompt)
            self.waiting_inpaint_image.pop(key, None)
            self.image_msgid_cache.add(msg_id)
            logger.info(f"EditImage: Gemini修图流程结束: MsgId={msg_id}")
            return False # 阻止后续插件处理

        # 新增：检查是否有多图编辑任务
        waiting_blend_info = self.waiting_blend.get(key)
        if waiting_blend_info:
            # 将图片保存到临时文件
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp_file:
                tmp_file.write(image_bytes)
                tmp_file_path = tmp_file.name
            
            # 将图片路径添加到多图编辑状态中
            self.waiting_blend[key]["images"].append(tmp_file_path)
            num_images = len(self.waiting_blend[key]["images"])
            tip = f"✅ 已收到第 {num_images} 张图片。\n请继续发送图片，或发送 '{self.end_prefix}' 开始多图编辑。"
            if message.get("IsGroup"):
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            
            self.image_msgid_cache.add(msg_id)
            logger.info(f"EditImage: 多图编辑收集图片: MsgId={msg_id}, 当前共{num_images}张")
            return False # 阻止后续插件处理
            
        logger.info(f"EditImage: MsgId={msg_id} 无待处理的编辑或修图任务")
        return True

    async def find_image_by_md5(self, md5: str) -> bytes | None:
        """Finds an image by its MD5 hash in the local files directory."""
        if not md5:
            logger.warning("EditImage: MD5 is empty, cannot find image.")
            return None
        common_extensions = ["jpeg", "jpg", "png", "gif", "webp"]
        for ext in common_extensions:
            file_path = os.path.join(self.files_dir, f"{md5}.{ext}")
            if os.path.exists(file_path):
                try:
                    with open(file_path, "rb") as f:
                        image_data = f.read()
                    logger.info(f"EditImage: Found image by MD5: {file_path}, size: {len(image_data)} bytes")
                    return image_data
                except Exception as e:
                    logger.error(f"EditImage: Failed to read image file {file_path} by MD5: {e}")
                    return None
        logger.warning(f"EditImage: Image file with MD5 {md5} not found in {self.files_dir} with common extensions.")
        return None

    @on_quote_message(priority=31)
    async def handle_quote_edit_or_inpaint(self, bot, message: dict):
        if not self.enable:
            return True

        current_msg_id = message.get("MsgId")
        if current_msg_id and current_msg_id in self.image_msgid_cache:
            logger.info(f"EditImage (quote): Message ID {current_msg_id} already processed, skipping.")
            return True

        content = message["Content"].strip()
        quote_info = message.get("Quote", {})

        if not (quote_info.get("MsgType") == 3): # Must be quoting an image
            return True

        is_edit_task = self.edit_image_prefix in content
        is_inpaint_task = self.inpaint_prefix in content

        if not (is_edit_task or is_inpaint_task):
            return True # Not a quote for edit or inpaint

        logger.info(f"EditImage (quote): Detected prefix in quote message for an image. MsgId: {current_msg_id}")

        user_prompt = ""
        task_type = ""

        if is_edit_task:
            task_type = "垫图"
            idx = content.find(self.edit_image_prefix)
            user_prompt = content[idx + len(self.edit_image_prefix):].strip()
            if not user_prompt:
                user_prompt = "请描述您要编辑图片的内容。"
        elif is_inpaint_task:
            task_type = "修图"
            if not self.gemini_client:
                tip = "抱歉，Gemini修图服务当前不可用，请联系管理员检查配置。"
                if message["IsGroup"]: await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                else: await bot.send_text_message(message["FromWxid"], tip)
                if current_msg_id: self.image_msgid_cache.add(current_msg_id) # Cache to prevent retry
                return False # Handled (error reported)
            
            idx = content.find(self.inpaint_prefix)
            user_prompt = content[idx + len(self.inpaint_prefix):].strip()
            if not user_prompt:
                user_prompt = "请描述您要对图片进行的修改。"
        
        logger.info(f"EditImage (quote): Task: {task_type}, User prompt: '{user_prompt}'")

        quoted_xml_content = quote_info.get("Content")
        if not quoted_xml_content:
            logger.warning(f"EditImage (quote): Quoted message XML content is missing for MsgId: {current_msg_id}.")
            # Optionally inform user
            return True # Let other handlers try if they can make sense of it

        image_bytes = b""
        md5 = None
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(quoted_xml_content)
            img_elem = root.find("img")
            if img_elem is not None:
                md5 = img_elem.get("md5")
                length_str = img_elem.get("length", "0")
                logger.info(f"EditImage (quote): Parsed quoted image XML: md5={md5}, length={length_str}")
                if md5:
                    image_bytes = await self.find_image_by_md5(md5)
                    if image_bytes:
                        logger.info(f"EditImage (quote): Image found locally by MD5: {md5}, size: {len(image_bytes)}")
                    else:
                        logger.warning(f"EditImage (quote): Image with MD5 {md5} not found locally.")
                else:
                    logger.warning(f"EditImage (quote): MD5 not found in quoted image XML.")
            else:
                logger.warning(f"EditImage (quote): No <img> element in quoted XML for MsgId: {current_msg_id}.")
        except Exception as e:
            logger.error(f"EditImage (quote): Failed to parse quoted XML or find by MD5 for MsgId: {current_msg_id}: {e}")
            image_bytes = b""

        if image_bytes and len(image_bytes) > 0:
            try:
                Image.open(io.BytesIO(image_bytes)) # Validate image
                logger.info(f"EditImage (quote): Quoted image (MD5: {md5}) validated. Proceeding with {task_type}.")

                key_to_clear = self.get_waiting_key(message) # Get key before async calls

                if is_edit_task:
                    await self.handle_edit_image_openai(image_bytes, bot, message, user_prompt)
                elif is_inpaint_task:
                    await self.handle_inpaint_image_with_gemini(image_bytes, bot, message, user_prompt)

                # Clear any pending states for this user/group to avoid conflicts
                if key_to_clear in self.waiting_edit_image:
                    self.waiting_edit_image.pop(key_to_clear, None)
                    logger.info(f"EditImage (quote): Cleared pending edit state for key: {key_to_clear}")
                if key_to_clear in self.waiting_inpaint_image:
                    self.waiting_inpaint_image.pop(key_to_clear, None)
                    logger.info(f"EditImage (quote): Cleared pending inpaint state for key: {key_to_clear}")
                
                if current_msg_id: self.image_msgid_cache.add(current_msg_id)
                return False # Handled
            except Exception as e:
                logger.error(f"EditImage (quote): Quoted image (MD5: {md5}) processing/validation failed for {task_type}: {e}")
                reply_content = f"处理引用的图片时出错 ({task_type})，无法完成操作。"
                if message["IsGroup"]: await bot.send_at_message(message["FromWxid"], reply_content, [message["SenderWxid"]])
                else: await bot.send_text_message(message["FromWxid"], reply_content)
                if current_msg_id: self.image_msgid_cache.add(current_msg_id)
                return False # Handled (error reported)
        else:
            logger.warning(f"EditImage (quote): Failed to get valid image bytes from quote (MD5: {md5}) for {task_type}.")
            reply_content = "未能从本地获取引用的图片数据，无法进行操作。请确保图片最近已发送过。"
            if message["IsGroup"]: await bot.send_at_message(message["FromWxid"], reply_content, [message["SenderWxid"]])
            else: await bot.send_text_message(message["FromWxid"], reply_content)
            if current_msg_id: self.image_msgid_cache.add(current_msg_id)
            return False # Handled (error reported)

        return True # Fallback, should not be reached if conditions for edit/inpaint were met.

    async def handle_edit_image_openai(self, image_bytes, bot, message, prompt): # 重命名原函数
        """调用OpenAI图片编辑API并返回结果"""
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

    async def handle_inpaint_image_with_gemini(self, image_bytes: bytes, bot, message: dict, prompt: str):
        """使用Google Gemini API编辑图片"""
        if not self.gemini_client:
            logger.error("[EditImage] Gemini client not initialized, skipping inpaint.")
            # 可以选择向用户发送错误消息
            return

        tip_msg = f"🎨 Gemini修图服务({self.gemini_model_name})请求已提交，请稍候...\n提示词：{prompt}"
        if message["IsGroup"]:
            await bot.send_at_message(message["FromWxid"], tip_msg, [message["SenderWxid"]])
        else:
            await bot.send_text_message(message["FromWxid"], tip_msg)

        # temp_file_path = None # No longer using temporary file
        try:
            # Revert to creating PIL.Image directly from image_bytes
            pil_image = Image.open(io.BytesIO(image_bytes))
            logger.info("[EditImage] PIL.Image created directly from image_bytes.")
            
            # Revert safety_settings to a list of dictionaries
            safety_settings = [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ]

            # Directly use the proven dictionary format for generation_config
            generation_config = {
                "response_modalities": ["TEXT", "IMAGE"]
            }
            logger.info(f"[EditImage] Using direct dictionary for generation_config: {generation_config}")

            response = await asyncio.to_thread(
                self.gemini_client.generate_content,
                contents=[prompt, pil_image],
                safety_settings=safety_settings,
                generation_config=generation_config # Pass the created or fallback config
            )
            
            # 处理响应 (参考 stability.py)
            if (hasattr(response, 'candidates') and response.candidates and
                hasattr(response.candidates[0], 'finish_reason')):
                finish_reason_str = str(response.candidates[0].finish_reason)
                if 'SAFETY' in finish_reason_str.upper() :
                    logger.error(f"[EditImage] Gemini: Detected image safety issue: {finish_reason_str}")
                    error_message = "由于图像安全策略限制，无法处理该图像。请尝试使用其他图片或修改提示词。"
                    if message["IsGroup"]:
                        await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
                    else:
                        await bot.send_text_message(message["FromWxid"], error_message)
                    return

            edited_image_bytes = None
            text_parts_content = [] # To collect any text parts

            if not (hasattr(response, 'candidates') and response.candidates and
                    response.candidates[0].content and
                    hasattr(response.candidates[0].content, 'parts') and
                    response.candidates[0].content.parts):
                logger.error("[EditImage] Gemini: Response has no parts or invalid structure.")
                if response.prompt_feedback and response.prompt_feedback.block_reason:
                    logger.error(f"[EditImage] Gemini: Prompt blocked due to {response.prompt_feedback.block_reason}")
                    error_message = f"请求被安全策略阻止: {response.prompt_feedback.block_reason}。请修改提示词。"
                else:
                    error_message = "Gemini修图失败，API返回的响应结构无效。"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], error_message)
                return
            else:
                logger.info(f"[EditImage] Gemini: Iterating through {len(response.candidates[0].content.parts)} parts in response.")
                for part_idx, part in enumerate(response.candidates[0].content.parts):
                    logger.info(f"[EditImage] Gemini: Processing part {part_idx + 1} of {len(response.candidates[0].content.parts)}.")
                    if hasattr(part, 'text') and part.text:
                        # Corrected f-string: pre-format the text part
                        log_text_part = part.text[:200].replace('\n', ' ')
                        logger.info(f"[EditImage] Gemini: Part {part_idx + 1} is a text part: '{log_text_part}...'")
                        text_parts_content.append(part.text)
                    
                    if hasattr(part, 'inline_data') and part.inline_data and hasattr(part.inline_data, 'data') and part.inline_data.data:
                        edited_image_bytes = part.inline_data.data
                        logger.info(f"[EditImage] Gemini: Part {part_idx + 1} is image data. Successfully received.")
                        # If we find an image, we might not need to report text parts unless for debugging.
                        # For now, let's prioritize image.
                        # User wants text part sent first if available, so we don't break here if text also exists.
                        # However, if an image part is found, we store its bytes and continue to ensure all text parts are collected.
                        # We will send text first, then image.
                    else:
                        logger.info(f"[EditImage] Gemini: Part {part_idx + 1} does not contain image data.")
            
            # --- New Response Sending Logic ---
            sent_something = False

            # 1. Send collected text parts, if any
            if text_parts_content:
                full_text_response = "\n".join(text_parts_content).strip() # Join with newlines for readability
                logger.info(f"[EditImage] Gemini: Sending a_text_response_to_user: {full_text_response[:200]}...")
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], full_text_response, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], full_text_response)
                sent_something = True

            # 2. Send image, if any
            if edited_image_bytes:
                logger.info("[EditImage] Gemini: Sending image_to_user.")
                if message["IsGroup"]:
                    await bot.send_image_message(message["FromWxid"], edited_image_bytes)
                else:
                    await bot.send_image_message(message["FromWxid"], edited_image_bytes)
                sent_something = True
            
            # 3. Handle cases where nothing was sent (e.g., API error before part processing, or empty parts)
            if not sent_something:
                # This path should ideally be covered by earlier error checks (no parts, safety blocks, etc.)
                # But as a fallback if no text or image was suitable to send from parts.
                logger.error("[EditImage] Gemini: No suitable text or image data found in response parts to send to user.")
                error_message = "Gemini修图失败，API没有返回可识别的内容。"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], error_message)
            
            # Removed the generic success message: "🖼️ 您的图片已由Gemini修图完成！"

        except Exception as e:
            logger.error(f"[EditImage] Gemini inpaint service exception: {e}")
            logger.error(traceback.format_exc())
            error_message = f"Gemini修图服务出错: {str(e)}"
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], error_message)
        # finally:
            # if temp_file_path and os.path.exists(temp_file_path):
            #     try:
            #         os.remove(temp_file_path)
            #         logger.info(f"[EditImage] Temporary image file {temp_file_path} deleted.")
            #     except Exception as e:
            #         logger.error(f"[EditImage] Error deleting temporary image file {temp_file_path}: {e}")

    async def handle_blend_service(self, image_paths, prompt, message, bot):
        """使用gpt-image-1进行多图编辑/混合，参考stability.py实现"""
        logger.info(f"EditImage: 开始多图编辑服务，用户: {self.get_waiting_key(message)}")

        if not self.openai_image_api_key or not self.openai_image_api_base:
            error_msg = "OpenAI API配置不完整，请在配置文件中设置openai_image_api_key和openai_image_api_base"
            if message.get("IsGroup"):
                await bot.send_at_message(message["FromWxid"], error_msg, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], error_msg)
            # 清理临时图片文件
            for path in image_paths:
                try:
                    os.remove(path)
                    logger.info(f"EditImage: 多图编辑cleanup，文件 {path} 已删除")
                except Exception as e:
                    logger.error(f"EditImage: 多图编辑cleanup，删除文件 {path} 失败: {e}")
            return

        try:
            # 发送请求前的提示
            tip_msg = f"🎨 gpt-image-1多图编辑请求已进入队列，预计需要30-150秒完成, 请稍候...\n提示词：{prompt}"
            if message.get("IsGroup"):
                await bot.send_at_message(message["FromWxid"], tip_msg, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip_msg)
            
            # 构建API请求URL
            url = f"{self.openai_image_api_base}/images/edits"
            
            # 构建请求头
            headers = {
                "Authorization": f"Bearer {self.openai_image_api_key}"
            }
            
            # 准备多图文件和请求数据
            data = aiohttp.FormData()
            
            # 添加模型和提示词
            data.add_field('model', self.image_model)
            data.add_field('prompt', prompt)
            
            # 添加所有图片
            for i, image_path in enumerate(image_paths):
                try:
                    file_key = f'image' if i == 0 else f'image[{i}]'
                    data.add_field(file_key, open(image_path, 'rb'), filename=f'image{i}.png', content_type='image/png')
                except Exception as e:
                    logger.error(f"EditImage: 读取图片失败 {image_path}: {e}")
                    error_msg = f"处理图片 {os.path.basename(image_path)} 时出错，多图编辑失败。"
                    if message.get("IsGroup"):
                        await bot.send_at_message(message["FromWxid"], error_msg, [message["SenderWxid"]])
                    else:
                        await bot.send_text_message(message["FromWxid"], error_msg)
                    # 清理临时图片文件
                    for path in image_paths:
                        try:
                            os.remove(path)
                        except Exception as remove_e:
                            logger.error(f"EditImage: 多图编辑error cleanup，删除文件 {path} 失败: {remove_e}")
                    return
            
            # 发送POST请求
            logger.info("[EditImage] 发送多图编辑请求到API")
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=data, timeout=1200) as response:
                    # 检查响应状态
                    if response.status != 200:
                        logger.error(f"[EditImage] API请求失败，状态码 {response.status}: {await response.text()}")
                        
                        # 检查是否是安全系统拒绝的错误
                        error_message = "多图编辑失败"
                        try:
                            error_json = await response.json()
                            if "error" in error_json and "code" in error_json["error"]:
                                if error_json["error"]["code"] == "moderation_blocked" or "safety" in error_json["error"]["message"].lower():
                                    error_message = "触发了图片的安全审查，请尝试使用其他图片或修改提示词。"
                                else:
                                    error_message = f"{error_message}: {await response.text()}"
                            else:
                                error_message = f"{error_message}: {await response.text()}"
                        except:
                            error_message = f"{error_message}: {await response.text()}"
                        
                        if message.get("IsGroup"):
                            await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
                        else:
                            await bot.send_text_message(message["FromWxid"], error_message)
                        return
                    
                    # 解析JSON响应
                    result = await response.json()
                    
                    # 处理返回结果
                    if "data" in result and len(result["data"]) > 0:
                        image_data = result["data"][0]
                        
                        if "b64_json" in image_data and image_data["b64_json"]:
                            # 从base64获取图片数据
                            import base64
                            edited_image_bytes = base64.b64decode(image_data["b64_json"])
                            
                            logger.info(f"[EditImage] 多图编辑完成，结果大小: {len(edited_image_bytes)} 字节")
                            
                            # 发送编辑后的图像
                            if message.get("IsGroup"):
                                await bot.send_image_message(message["FromWxid"], edited_image_bytes)
                                await bot.send_at_message(message["FromWxid"], "🖼️ 您的多图编辑已完成！", [message["SenderWxid"]])
                            else:
                                await bot.send_image_message(message["FromWxid"], edited_image_bytes)
                        else:
                            logger.error("[EditImage] API响应中没有b64_json")
                            error_msg = "多图编辑失败，API没有返回图片数据"
                            if message.get("IsGroup"):
                                await bot.send_at_message(message["FromWxid"], error_msg, [message["SenderWxid"]])
                            else:
                                await bot.send_text_message(message["FromWxid"], error_msg)
                    else:
                        logger.error("[EditImage] API响应格式无效")
                        error_msg = "多图编辑失败，API返回格式不正确"
                        if message.get("IsGroup"):
                            await bot.send_at_message(message["FromWxid"], error_msg, [message["SenderWxid"]])
                        else:
                            await bot.send_text_message(message["FromWxid"], error_msg)

        except Exception as e:
            logger.error(f"[EditImage] 多图编辑服务异常: {e}")
            import traceback
            logger.error(traceback.format_exc())

            error_msg = f"多图编辑服务内部出错: {str(e)}"
            if message.get("IsGroup"):
                await bot.send_at_message(message["FromWxid"], error_msg, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], error_msg)
        finally:
            # 清理临时图片文件
            for path in image_paths:
                try:
                    os.remove(path)
                    logger.info(f"EditImage: 多图编辑cleanup，文件 {path} 已删除")
                except Exception as e:
                    logger.error(f"EditImage: 多图编辑cleanup，删除文件 {path} 失败: {e}")
