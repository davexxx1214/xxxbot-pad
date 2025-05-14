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


class EditImage(PluginBase):
    description = "垫图和修图插件" # 修改描述
    author = "老夏"
    version = "1.0.1" # 修改版本
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

            # 新增 Gemini 相关配置
            self.inpaint_prefix = plugin_config.get("inpaint_prefix", "修图")
            self.google_api_key = plugin_config.get("google_api_key", None)
            self.gemini_model_name = plugin_config.get("gemini_model_name", "models/gemini-pro-vision") # 默认使用 vision

        except Exception as e:
            logger.error(f"加载垫图/修图插件配置文件失败: {e}") # 修改日志
            raise
        # 记录待垫图状态: {user_or_group_id: {timestamp, prompt}}
        self.waiting_edit_image = {}
        # 新增：记录待修图状态
        self.waiting_inpaint_image = {}

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
            
        logger.info(f"EditImage: MsgId={msg_id} 无待处理的编辑或修图任务")
        return True


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

        try:
            pil_image = Image.open(io.BytesIO(image_bytes))
            
            # Revert safety_settings to a list of dictionaries
            safety_settings = [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ]

            # Attempt to add generation_config
            generation_config = None
            try:
                # Try to use the typed object if available in this version
                generation_config = genai_types.GenerationConfig(
                    response_modalities=[genai_types.GenerateContentResponseMimeType.IMAGE] # Assuming enum structure
                )
                logger.info("[EditImage] Using genai_types.GenerationConfig for response modality.")
            except AttributeError:
                logger.warning("[EditImage] genai_types.GenerationConfig or GenerateContentResponseMimeType not found, falling back to dict for generation_config.")
                # Fallback to dictionary if typed object is not available or causes error
                # Match the modalities used in stability.py: ['Text', 'Image']
                generation_config = {
                    "response_modalities": ["Text", "Image"] 
                }
            except Exception as e:
                logger.error(f"[EditImage] Error preparing generation_config: {e}. Proceeding without explicit generation_config.")

            logger.info(f"[EditImage] Sending request to Gemini with prompt: {prompt}")

            # Use asyncio.to_thread 执行阻塞的API调用
            response = await asyncio.to_thread(
                self.gemini_client.generate_content,
                contents=[prompt, pil_image],
                safety_settings=safety_settings,
                generation_config=generation_config # Add generation_config
            )
            
            # 处理响应 (参考 stability.py)
            if (hasattr(response, 'candidates') and response.candidates and
                hasattr(response.candidates[0], 'finish_reason')):
                finish_reason_str = str(response.candidates[0].finish_reason)
                # FinishReason enums: FINISH_REASON_UNSPECIFIED, STOP, MAX_TOKENS, SAFETY, RECITATION, OTHER
                if 'SAFETY' in finish_reason_str.upper() : # 更通用的安全检查
                    logger.error(f"[EditImage] Gemini: Detected image safety issue: {finish_reason_str}")
                    error_message = "由于图像安全策略限制，无法处理该图像。请尝试使用其他图片或修改提示词。"
                    if message["IsGroup"]:
                        await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
                    else:
                        await bot.send_text_message(message["FromWxid"], error_message)
                    return

            if not (hasattr(response, 'candidates') and response.candidates and
                    response.candidates[0].content and
                    hasattr(response.candidates[0].content, 'parts')):
                logger.error("[EditImage] Gemini: Invalid response structure or no content/parts.")
                # 检查prompt_feedback是否有阻塞信息
                if response.prompt_feedback and response.prompt_feedback.block_reason:
                    logger.error(f"[EditImage] Gemini: Prompt blocked due to {response.prompt_feedback.block_reason}")
                    error_message = f"请求被安全策略阻止: {response.prompt_feedback.block_reason}。请修改提示词。"
                else:
                    error_message = "Gemini修图失败，未能生成图片或返回了无效的响应。"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], error_message)
                return
            
            edited_image_bytes = None
            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.data:
                    edited_image_bytes = part.inline_data.data
                    logger.info("[EditImage] Gemini: Successfully received image data.")
                    break
            
            if edited_image_bytes:
                # 发送图片
                if message["IsGroup"]:
                    await bot.send_image_message(message["FromWxid"], edited_image_bytes)
                    await bot.send_at_message(message["FromWxid"], "🖼️ 您的图片已由Gemini修图完成！", [message["SenderWxid"]])
                else:
                    await bot.send_image_message(message["FromWxid"], edited_image_bytes)
                    await bot.send_text_message(message["FromWxid"], "🖼️ 您的图片已由Gemini修图完成！")
            else:
                logger.error("[EditImage] Gemini: No image data found in response parts.")
                error_message = "Gemini修图失败，API没有返回有效的图片数据。"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], error_message)

        except Exception as e:
            logger.error(f"[EditImage] Gemini inpaint service exception: {e}")
            logger.error(traceback.format_exc())
            error_message = f"Gemini修图服务出错: {str(e)}"
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], error_message, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], error_message)
