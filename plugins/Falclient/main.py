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
from PIL import Image, ImageDraw, ImageFont
import base64
from utils.decorators import on_text_message, on_at_message, on_quote_message, on_image_message
import regex  # 不是re，是regex库，支持\p{Zs}
import tempfile
import fal_client
from pathlib import Path
import random
import uuid


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
            self.fal_edit_prefix = plugin_config.get("fal_edit_prefix", "/p")
            self.jimeng_prefix = plugin_config.get("jimeng_prefix", "jimeng")
            self.fal_kling_img_model = plugin_config.get("fal_kling_img_model", "kling-video/v2/master/image-to-video")
            self.fal_kling_text_model = plugin_config.get("fal_kling_text_model", "kling-video/v2/master/text-to-video")
            self.fal_edit_model = plugin_config.get("fal_edit_model", "flux-pro/kontext")
            self.fal_api_key = plugin_config.get("fal_api_key", None)
            self.jimeng_api_key = plugin_config.get("jimeng_api_key", None)
            self.jimeng_url = plugin_config.get("jimeng_url", None)
            self.openai_image_api_key = plugin_config.get("openai_image_api_key", None)
            self.openai_image_api_base = plugin_config.get("openai_image_api_base", None)
            self.veo3_prefix = plugin_config.get("veo3_prefix", "veo3")
            self.veo3_retry_times = plugin_config.get("veo3_retry_times", 30)
            
            # 配置选项
            self.debug_mode = plugin_config.get("debug_mode", True)
        except Exception as e:
            logger.error(f"加载Falclient插件配置文件失败: {e}")
            raise
        # 检查pymediainfo依赖
        try:
            from pymediainfo import MediaInfo
            self.has_mediainfo = True
        except ImportError:
            self.has_mediainfo = False
            if self.debug_mode:
                logger.warning("pymediainfo未安装，视频时长将使用默认值。建议安装: pip install pymediainfo")
        
        # 记录待生成视频的状态: {user_or_group_id: timestamp}
        self.waiting_video = {}
        # 新增：记录待编辑图片的状态: {user_or_group_id: {timestamp, prompt, type}}
        self.waiting_edit = {}
        self.image_msgid_cache = set()
        self.image_cache_timeout = 60
        self.image_cache = {}
        
        # 文件目录，用于MD5查找
        self.files_dir = "files"
        os.makedirs(self.files_dir, exist_ok=True)

    def get_waiting_key(self, message: dict):
        if message.get("IsGroup"):
            return message["FromWxid"]
        else:
            return message["SenderWxid"]

    async def find_image_by_md5(self, md5: str) -> bytes | None:
        """通过MD5在本地文件目录中查找图片"""
        if not md5:
            logger.warning("Falclient: MD5为空，无法查找图片")
            return None
        
        common_extensions = ["jpeg", "jpg", "png", "gif", "webp"]
        for ext in common_extensions:
            file_path = os.path.join(self.files_dir, f"{md5}.{ext}")
            if os.path.exists(file_path):
                try:
                    with open(file_path, "rb") as f:
                        image_data = f.read()
                    logger.info(f"Falclient: 通过MD5找到图片: {file_path}, 大小: {len(image_data)} 字节")
                    return image_data
                except Exception as e:
                    logger.error(f"Falclient: 读取图片文件失败 {file_path}: {e}")
                    return None
        
        logger.warning(f"Falclient: 未找到MD5为 {md5} 的图片文件")
        return None

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
            tip = f"💡已开启kling2.1图生视频模式（kling2.1 image-to-video），您接下来第一张图片会生成视频。\n当前的提示词为：\n" + (user_prompt or "无")
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            return False
        # 文生视频
        if content.startswith(self.fal_text_prefix):
            user_prompt = content[len(self.fal_text_prefix):].strip()
            # 新增：先回复收到请求
            notice = "您的文生视频的请求已经收到，请稍候..."
            tip = "💡已开启kling2.1文生视频模式（kling2.1 text-to-video），将根据您的描述生成视频。"
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], notice)
            await self.handle_text2video(bot, message, user_prompt)
            return False
        # 新增：veo3视频生成
        if content.startswith(self.veo3_prefix):
            user_prompt = content[len(self.veo3_prefix):].strip()
            if not user_prompt:
                tip = f"💡欢迎使用veo3视频生成，指令格式为:\n\n{self.veo3_prefix} + 空格 + 视频描述（支持中文）\n例如：{self.veo3_prefix} 一个宇航员在月球上跳舞"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], tip)
                return False
            # 新增：展示用户提示词
            tip = f"💡已开启veo3视频生成模式，将根据您的描述生成视频。\n当前的提示词为：\n" + (user_prompt or "无")
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            await self.handle_veo3_video(bot, message, user_prompt)
            return False
        
        # 新增：图片编辑
        if content.startswith(self.fal_edit_prefix):
            user_prompt = content[len(self.fal_edit_prefix):].strip()
            if not user_prompt:
                # 用户只发送了 @机器人 /p，提示正确的使用方法
                tip = "欢迎使用flux-pro/kontext图片编辑！\n正确的编辑指令是：/p + 要编辑的提示词\n\n例如：\n/p 在图片中添加一个甜甜圈\n/p 把背景改成蓝色"
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                return False
            
            key = self.get_waiting_key(message)
            self.waiting_edit[key] = {
                "timestamp": time.time(),
                "prompt": user_prompt,
                "type": "edit_image"
            }
            tip = f"💡已开启flux-pro/kontext图片编辑模式，您接下来第一张图片会进行编辑。\n当前的提示词为：\n" + (user_prompt or "编辑图片")
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            return False
        
        # 新增：即梦AI文字生成图片
        if content.startswith(self.jimeng_prefix):
            user_prompt = content[len(self.jimeng_prefix):].strip()
            if not user_prompt:
                tip = f"💡欢迎使用即梦AI绘图3.0，指令格式为:\n\n{self.jimeng_prefix} + 空格 + 主题(支持中文)\n例如：{self.jimeng_prefix} 一只可爱的猫"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], tip)
                return False
            
            # 先回复收到请求
            notice = "您的即梦AI绘图请求已经收到，请稍候..."
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], notice)
            await self.handle_jimeng_service(bot, message, user_prompt)
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
            tip = f"💡已开启kling2.1图生视频模式（kling2.1 image-to-video），您接下来第一张图片会生成视频。\n当前的提示词为：\n" + (user_prompt or "无")
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            # 新增：先回复收到请求
            notice = "您的图生视频的请求已经收到，请稍候..."
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
            tip = "💡已开启kling2.1文生视频模式（kling2.1 text-to-video），将根据您的描述生成视频。"
            notice = "您的文生视频的请求已经收到，请稍候..."
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], notice)
            await self.handle_text2video(bot, message, user_prompt)
            return False
        # 新增：veo3视频生成
        if self.veo3_prefix in content:
            idx = content.find(self.veo3_prefix)
            user_prompt = content[idx + len(self.veo3_prefix):].strip()
            if not user_prompt:
                tip = f"💡欢迎使用veo3视频生成，指令格式为:\n\n{self.veo3_prefix} + 空格 + 视频描述（支持中文）\n例如：{self.veo3_prefix} 一个宇航员在月球上跳舞"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], tip)
                return False
            # 新增：展示用户提示词
            tip = f"💡已开启veo3视频生成模式，将根据您的描述生成视频。\n当前的提示词为：\n" + (user_prompt or "无")
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            await self.handle_veo3_video(bot, message, user_prompt)
            return False
        
        # 新增：图片编辑
        if self.fal_edit_prefix in content:
            idx = content.find(self.fal_edit_prefix)
            user_prompt = content[idx + len(self.fal_edit_prefix):].strip()
            if not user_prompt:
                # 用户只发送了引用+/p，提示正确的使用方法
                tip = "欢迎使用flux-pro/kontext图片编辑！\n正确的编辑指令是：/p + 要编辑的提示词\n\n例如：\n/p 在图片中添加一个甜甜圈\n/p 把背景改成蓝色"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], tip)
                if current_msg_id:
                    self.image_msgid_cache.add(current_msg_id)
                return False
            
            key = self.get_waiting_key(message)
            self.waiting_edit[key] = {
                "timestamp": time.time(),
                "prompt": user_prompt,
                "type": "edit_image"
            }
            tip = f"💡已开启flux-pro/kontext图片编辑模式，您接下来第一张图片会进行编辑。\n当前的提示词为：\n" + (user_prompt or "编辑图片")
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], tip)
            return False
        
        # 新增：即梦AI文字生成图片
        if self.jimeng_prefix in content:
            idx = content.find(self.jimeng_prefix)
            user_prompt = content[idx + len(self.jimeng_prefix):].strip()
            if not user_prompt:
                tip = f"💡欢迎使用即梦AI绘图3.0，指令格式为:\n\n{self.jimeng_prefix} + 空格 + 主题(支持中文)\n例如：{self.jimeng_prefix} 一只可爱的猫"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], tip)
                return False
            
            # 先回复收到请求
            notice = "您的即梦AI绘图请求已经收到，请稍候..."
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], notice)
            await self.handle_jimeng_service(bot, message, user_prompt)
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
        
        # 检查图生视频任务
        waiting_video_info = self.waiting_video.get(key)
        # 检查图片编辑任务
        waiting_edit_info = self.waiting_edit.get(key)
        
        if not waiting_video_info and not waiting_edit_info:
            return True
        
        # 确定任务类型
        if waiting_video_info and waiting_video_info.get("type") == "img2video":
            task_type = "img2video"
            user_prompt = waiting_video_info.get("prompt", "")
        elif waiting_edit_info and waiting_edit_info.get("type") == "edit_image":
            task_type = "edit_image"
            user_prompt = waiting_edit_info.get("prompt", "")
        else:
            return True
        
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
            # 立即清除等待状态，避免重复处理
            if task_type == "img2video":
                self.waiting_video.pop(key, None)  # 立即清除状态
                await self.handle_img2video(bot, message, image_bytes, user_prompt)
            elif task_type == "edit_image":
                self.waiting_edit.pop(key, None)  # 立即清除状态
                await self.handle_edit_image(bot, message, image_bytes, user_prompt)
        
        self.image_msgid_cache.add(msg_id)
        return False

    @on_quote_message(priority=31)
    async def handle_quote_tasks(self, bot, message: dict):
        """处理引用图片进行各种任务（图生视频、文生视频、图片编辑）"""
        if not self.enable:
            return True

        current_msg_id = message.get("MsgId")
        if current_msg_id and current_msg_id in self.image_msgid_cache:
            logger.info(f"Falclient (quote): 消息ID {current_msg_id} 已处理，跳过")
            return True

        content = message["Content"].strip()
        quote_info = message.get("Quote", {})

        # 必须是引用图片消息
        if not (quote_info.get("MsgType") == 3):
            return True

        # 检查是否包含各种前缀
        is_img2video_task = self.fal_img_prefix in content
        is_text2video_task = self.fal_text_prefix in content
        is_edit_task = self.fal_edit_prefix in content

        if not (is_img2video_task or is_text2video_task or is_edit_task):
            return True

        logger.info(f"Falclient (quote): 检测到引用图片的任务请求，MsgId: {current_msg_id}")

        # 处理文生视频（引用图片但使用文生视频指令）
        if is_text2video_task:
            idx = content.find(self.fal_text_prefix)
            user_prompt = content[idx + len(self.fal_text_prefix):].strip()
            if not user_prompt:
                tip = "请在文生视频指令后添加描述文字"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], tip)
                if current_msg_id:
                    self.image_msgid_cache.add(current_msg_id)
                return False
            
            # 直接进行文生视频，忽略引用的图片
            notice = "您的文生视频的请求已经收到，请稍候..."
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], notice)
            
            await self.handle_text2video(bot, message, user_prompt)
            if current_msg_id:
                self.image_msgid_cache.add(current_msg_id)
            return False

        # 处理图生视频
        if is_img2video_task:
            idx = content.find(self.fal_img_prefix)
            user_prompt = content[idx + len(self.fal_img_prefix):].strip()
            if not user_prompt:
                user_prompt = "生成视频"

            logger.info(f"Falclient (quote): 图生视频任务，提示词: '{user_prompt}'")

            # 从引用的XML中提取图片
            quoted_xml_content = quote_info.get("Content")
            if not quoted_xml_content:
                logger.warning(f"Falclient (quote): 引用消息缺少XML内容，MsgId: {current_msg_id}")
                return True

            image_bytes = b""
            md5 = None
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(quoted_xml_content)
                img_elem = root.find("img")
                if img_elem is not None:
                    md5 = img_elem.get("md5")
                    length_str = img_elem.get("length", "0")
                    logger.info(f"Falclient (quote): 解析引用图片XML: md5={md5}, length={length_str}")
                    
                    if md5:
                        image_bytes = await self.find_image_by_md5(md5)
                        if image_bytes:
                            logger.info(f"Falclient (quote): 通过MD5找到图片: {md5}, 大小: {len(image_bytes)} 字节")
                        else:
                            logger.warning(f"Falclient (quote): 未找到MD5为 {md5} 的图片")
                    else:
                        logger.warning(f"Falclient (quote): 引用图片XML中未找到MD5")
                else:
                    logger.warning(f"Falclient (quote): 引用XML中没有<img>元素，MsgId: {current_msg_id}")
            except Exception as e:
                logger.error(f"Falclient (quote): 解析引用XML或查找图片失败，MsgId: {current_msg_id}: {e}")
                image_bytes = b""

            if image_bytes and len(image_bytes) > 0:
                try:
                    # 验证图片
                    from PIL import Image
                    Image.open(io.BytesIO(image_bytes))
                    logger.info(f"Falclient (quote): 引用图片 (MD5: {md5}) 验证通过，开始图生视频")

                    # 清除可能存在的等待状态
                    key_to_clear = self.get_waiting_key(message)
                    if key_to_clear in self.waiting_video:
                        self.waiting_video.pop(key_to_clear, None)
                        logger.info(f"Falclient (quote): 清除用户 {key_to_clear} 的等待状态")

                    # 处理图生视频
                    await self.handle_img2video(bot, message, image_bytes, user_prompt)

                    if current_msg_id:
                        self.image_msgid_cache.add(current_msg_id)
                    return False
                except Exception as e:
                    logger.error(f"Falclient (quote): 引用图片 (MD5: {md5}) 处理失败: {e}")
                    reply_content = f"处理引用的图片时出错，无法完成图生视频操作"
                    if message["IsGroup"]:
                        await bot.send_at_message(message["FromWxid"], reply_content, [message["SenderWxid"]])
                    else:
                        await bot.send_text_message(message["FromWxid"], reply_content)
                    if current_msg_id:
                        self.image_msgid_cache.add(current_msg_id)
                    return False
            else:
                logger.warning(f"Falclient (quote): 未能从引用获取有效图片数据 (MD5: {md5})")
                reply_content = "未能从本地获取引用的图片数据，无法进行图生视频。请确保图片最近已发送过。"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], reply_content, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], reply_content)
                if current_msg_id:
                    self.image_msgid_cache.add(current_msg_id)
                return False

        # 新增：处理图片编辑
        if is_edit_task:
            idx = content.find(self.fal_edit_prefix)
            user_prompt = content[idx + len(self.fal_edit_prefix):].strip()
            if not user_prompt:
                # 用户只发送了引用+/p，提示正确的使用方法
                tip = "欢迎使用flux-pro/kontext图片编辑！\n正确的编辑指令是：/p + 要编辑的提示词\n\n例如：\n/p 在图片中添加一个甜甜圈\n/p 把背景改成蓝色"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], tip)
                if current_msg_id:
                    self.image_msgid_cache.add(current_msg_id)
                return False

            logger.info(f"Falclient (quote): 图片编辑任务，提示词: '{user_prompt}'")

            # 从引用的XML中提取图片
            quoted_xml_content = quote_info.get("Content")
            if not quoted_xml_content:
                logger.warning(f"Falclient (quote): 引用消息缺少XML内容，MsgId: {current_msg_id}")
                return True

            image_bytes = b""
            md5 = None
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(quoted_xml_content)
                img_elem = root.find("img")
                if img_elem is not None:
                    md5 = img_elem.get("md5")
                    length_str = img_elem.get("length", "0")
                    logger.info(f"Falclient (quote): 解析引用图片XML: md5={md5}, length={length_str}")
                    
                    if md5:
                        image_bytes = await self.find_image_by_md5(md5)
                        if image_bytes:
                            logger.info(f"Falclient (quote): 通过MD5找到图片: {md5}, 大小: {len(image_bytes)} 字节")
                        else:
                            logger.warning(f"Falclient (quote): 未找到MD5为 {md5} 的图片")
                    else:
                        logger.warning(f"Falclient (quote): 引用图片XML中未找到MD5")
                else:
                    logger.warning(f"Falclient (quote): 引用XML中没有<img>元素，MsgId: {current_msg_id}")
            except Exception as e:
                logger.error(f"Falclient (quote): 解析引用XML或查找图片失败，MsgId: {current_msg_id}: {e}")
                image_bytes = b""

            if image_bytes and len(image_bytes) > 0:
                try:
                    # 验证图片
                    from PIL import Image
                    Image.open(io.BytesIO(image_bytes))
                    logger.info(f"Falclient (quote): 引用图片 (MD5: {md5}) 验证通过，开始图片编辑")

                    # 清除可能存在的等待状态
                    key_to_clear = self.get_waiting_key(message)
                    if key_to_clear in self.waiting_edit:
                        self.waiting_edit.pop(key_to_clear, None)
                        logger.info(f"Falclient (quote): 清除用户 {key_to_clear} 的图片编辑等待状态")

                    # 处理图片编辑
                    await self.handle_edit_image(bot, message, image_bytes, user_prompt)

                    if current_msg_id:
                        self.image_msgid_cache.add(current_msg_id)
                    return False
                except Exception as e:
                    logger.error(f"Falclient (quote): 引用图片 (MD5: {md5}) 编辑失败: {e}")
                    reply_content = f"处理引用的图片时出错，无法完成图片编辑操作"
                    if message["IsGroup"]:
                        await bot.send_at_message(message["FromWxid"], reply_content, [message["SenderWxid"]])
                    else:
                        await bot.send_text_message(message["FromWxid"], reply_content)
                    if current_msg_id:
                        self.image_msgid_cache.add(current_msg_id)
                    return False
            else:
                logger.warning(f"Falclient (quote): 未能从引用获取有效图片数据 (MD5: {md5})")
                reply_content = "未能从本地获取引用的图片数据，无法进行图片编辑。请确保图片最近已发送过。"
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], reply_content, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], reply_content)
                if current_msg_id:
                    self.image_msgid_cache.add(current_msg_id)
                return False

        return True

    async def handle_text2video(self, bot, message, prompt):
        try:
            url = f"https://fal.run/fal-ai/{self.fal_kling_text_model}"
            headers = {
                "Authorization": f"Key {self.fal_api_key}",
                "Content-Type": "application/json"
            }
            data = {"prompt": prompt}
            logger.info(f"Falclient: 文生视频API请求 url={url} headers={headers} data={data}")
            
            # 设置超时配置 - 视频生成需要很长时间
            timeout = aiohttp.ClientTimeout(
                total=1800,  # 总超时时间30分钟
                connect=30,  # 连接超时30秒
                sock_read=1800  # 读取超时30分钟
            )
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=data) as resp:
                    logger.info(f"Falclient: 文生视频API响应状态: {resp.status}")
                    if resp.status == 200:
                        result = await resp.json()
                        video_url = result.get("video", {}).get("url")
                        if video_url:
                            await self.send_video_url(bot, message, video_url, prompt)
                        else:
                            await self.send_video_url(bot, message, "未获取到视频URL", prompt)
                    else:
                        await self.send_video_url(bot, message, f"API请求失败: {resp.status}", prompt)
        except Exception as e:
            import traceback
            logger.error(f"Falclient: 文生视频API调用异常: {e}\n{traceback.format_exc()}")
            await self.send_video_url(bot, message, f"API调用异常: {e}", prompt)

    def _get_video_cover(self, video_path) -> str:
        """智能获取视频封面，默认尝试提取视频帧，失败时使用默认封面"""
        try:
            return self._extract_video_frame_as_cover(video_path)
        except Exception as e:
            if self.debug_mode:
                logger.warning(f"视频帧提取失败，使用默认封面: {e}")
            return self._generate_cover_image_file()

    def _extract_video_frame_as_cover(self, video_path) -> str:
        """从视频文件中提取第一帧作为封面"""
        import subprocess
        
        tmp_dir = os.path.join(os.path.dirname(__file__), 'tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        
        cover_filename = f"frame_cover_{uuid.uuid4().hex}.jpg"
        cover_path = os.path.join(tmp_dir, cover_filename)
        
        # 使用ffmpeg提取视频第一帧
        cmd = [
            'ffmpeg', '-i', video_path, 
            '-vf', 'scale=640:360',  # 缩放到标准尺寸
            '-vframes', '1',         # 只提取1帧
            '-q:v', '2',             # 高质量
            '-y',                    # 覆盖输出文件
            cover_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and os.path.exists(cover_path):
            if self.debug_mode:
                logger.info(f"视频帧封面提取成功: {cover_path}")
            return cover_path
        else:
            if self.debug_mode:
                logger.warning(f"ffmpeg提取失败: {result.stderr}")
            # 失败时抛出异常，让上层方法处理
            raise Exception(f"ffmpeg提取失败: {result.stderr}")

    def _generate_cover_image_file(self) -> str:
        tmp_dir = os.path.join(os.path.dirname(__file__), 'tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        
        # 使用固定的文件名，避免每次都生成新文件
        cover_filename = "fallback_cover.png"  # 改回PNG格式，保持与API一致
        cover_path = os.path.join(tmp_dir, cover_filename)
        
        # 如果已经存在，直接返回
        if os.path.exists(cover_path):
            return cover_path

        # 生成一个简单、标准的封面图片
        # 使用微信常见的视频封面尺寸
        img = Image.new('RGB', (480, 270), color=(240, 240, 240))  # 浅灰色背景
        draw = ImageDraw.Draw(img)
        
        # 绘制一个简单的播放按钮图标
        center_x, center_y = 240, 135
        triangle_size = 30
        
        # 画一个圆形背景
        draw.ellipse([center_x-40, center_y-40, center_x+40, center_y+40], 
                    fill=(100, 100, 100), outline=(80, 80, 80), width=2)
        
        # 画播放三角形
        triangle_points = [
            (center_x-15, center_y-20),
            (center_x-15, center_y+20), 
            (center_x+20, center_y)
        ]
        draw.polygon(triangle_points, fill=(255, 255, 255))
        
        # 保存为PNG格式，确保兼容性
        img.save(cover_path, format='PNG', optimize=True)
        logger.info(f"标准封面已生成: {cover_path}")
        return cover_path

    def diagnose_video_file(self, video_path):
        """诊断视频文件，输出详细信息"""
        try:
            import subprocess
            import json
            
            if not os.path.exists(video_path):
                return f"视频文件不存在: {video_path}"
            
            # 获取文件基本信息
            file_size = os.path.getsize(video_path)
            
            diagnosis = [
                f"文件路径: {video_path}",
                f"文件大小: {file_size} 字节 ({file_size/1024/1024:.2f} MB)"
            ]
            
            # 尝试使用ffprobe获取视频信息（如果可用）
            try:
                cmd = [
                    'ffprobe', '-v', 'quiet', '-print_format', 'json', 
                    '-show_format', '-show_streams', video_path
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    info = json.loads(result.stdout)
                    if 'streams' in info:
                        for stream in info['streams']:
                            if stream.get('codec_type') == 'video':
                                diagnosis.extend([
                                    f"视频编码: {stream.get('codec_name', 'unknown')}",
                                    f"分辨率: {stream.get('width', '?')}x{stream.get('height', '?')}",
                                    f"帧率: {stream.get('r_frame_rate', 'unknown')}",
                                    f"时长: {stream.get('duration', 'unknown')} 秒"
                                ])
                                break
                else:
                    diagnosis.append("ffprobe检查失败")
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
                diagnosis.append("ffprobe不可用，跳过详细检查")
            
            # 检查文件头
            with open(video_path, 'rb') as f:
                header = f.read(20)
                if header.startswith(b'\x00\x00\x00'):
                    diagnosis.append("文件格式: 可能是MP4")
                elif header.startswith(b'ftyp'):
                    diagnosis.append("文件格式: MP4容器")
                else:
                    diagnosis.append(f"文件头: {header[:10].hex()}")
            
            return "\n".join(diagnosis)
            
        except Exception as e:
            return f"视频诊断失败: {e}"

    def get_tmp_video_path(self):
        # 确保 plugins/Falclient/tmp 目录存在
        tmp_dir = os.path.join(os.path.dirname(__file__), 'tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        filename = f"video_{uuid.uuid4().hex}.mp4"
        return os.path.join(tmp_dir, filename)

    async def handle_img2video(self, bot, message, image_bytes, prompt):
        # 图生视频API调用
        import tempfile, os, aiohttp
        logger.info(f"[img2video] bot.send_video_message 实际类型: {type(bot)}，方法: {getattr(bot, 'send_video_message', None)}")
        
        # 添加请求已收到的提示
        notice = "您的图生视频请求已经收到，请稍候..."
        if message.get("IsGroup"):
            await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
        else:
            await bot.send_text_message(message["FromWxid"], notice)
        
        tmp_file_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp_file:
                tmp_file.write(image_bytes)
                tmp_file_path = tmp_file.name

            client = fal_client.SyncClient(key=self.fal_api_key)
            image_url = client.upload_file(tmp_file_path)
            if not image_url:
                await self.send_video_url(bot, message, "图片上传失败", prompt)
                return

            # 用SDK的subscribe方法调用
            result = client.subscribe(
                f"fal-ai/{self.fal_kling_img_model}",
                arguments={
                    "prompt": prompt,
                    "image_url": image_url
                },
                with_logs=False
            )
            video_url = result.get("video", {}).get("url")
            if video_url and video_url.startswith("http"):
                # 先下载到本地再发
                video_tmp_path = self.get_tmp_video_path()
                cover_path = None
                try:
                    # 设置下载超时配置
                    download_timeout = aiohttp.ClientTimeout(
                        total=600,  # 总超时时间10分钟
                        connect=30,  # 连接超时30秒
                        sock_read=300  # 读取超时5分钟
                    )
                    
                    async with aiohttp.ClientSession(timeout=download_timeout) as session:
                        async with session.get(video_url) as resp:
                            if resp.status == 200:
                                content = await resp.read()
                                logger.info(f"下载视频内容长度: {len(content)} 字节")
                                if len(content) == 0:
                                    logger.error(f"下载视频内容为空！url={video_url}")
                                    if message.get("IsGroup"):
                                        await bot.send_at_message(message["FromWxid"], f"视频生成失败：下载内容为空", [message["SenderWxid"]])
                                    else:
                                        await bot.send_text_message(message["FromWxid"], f"视频生成失败：下载内容为空")
                                    return
                                with open(video_tmp_path, 'wb') as f:
                                    f.write(content)
                                logger.info(f"视频已下载到本地: {video_tmp_path}, 大小: {os.path.getsize(video_tmp_path)} 字节")
                            else:
                                raise Exception(f"视频下载失败，状态码: {resp.status}")
                    
                    # 调试模式下输出视频诊断信息
                    if self.debug_mode:
                        diagnosis = self.diagnose_video_file(video_tmp_path)
                        logger.info(f"视频文件诊断:\n{diagnosis}")

                    # 使用自定义发送逻辑发送视频
                    cover_path = self._get_video_cover(video_tmp_path)
                    logger.info(f"使用自定义发送逻辑，封面: {cover_path}")
                    await self.send_video_with_custom_logic(bot, message["FromWxid"], video_tmp_path, cover_path)
                    logger.info("视频发送成功")
                except Exception as e:
                    logger.error(f"Falclient: 图生视频下载或发送失败: {e}")
                    if message.get("IsGroup"):
                        await bot.send_at_message(message["FromWxid"], f"视频生成失败：{video_url}", [message["SenderWxid"]])
                    else:
                        await bot.send_text_message(message["FromWxid"], f"视频生成失败：{video_url}")
                finally:
                    # 删除临时视频文件
                    if video_tmp_path and os.path.exists(video_tmp_path):
                        try:
                            os.remove(video_tmp_path)
                            logger.info(f"临时视频文件已删除: {video_tmp_path}")
                        except Exception as e_rem:
                            logger.warning(f"删除临时视频文件失败: {video_tmp_path}, error: {e_rem}")
                    
                    # 删除临时封面文件
                    if cover_path and os.path.exists(cover_path):
                        try:
                            os.remove(cover_path)
                            logger.info(f"临时封面文件已删除: {cover_path}")
                        except Exception as e_rem:
                            logger.warning(f"删除临时封面文件失败: {cover_path}, error: {e_rem}")
            elif video_url:
                await self.send_video_url(bot, message, video_url, prompt)
            else:
                await self.send_video_url(bot, message, "未获取到视频URL", prompt)
        except Exception as e:
            await self.send_video_url(bot, message, f"API调用异常: {e}", prompt)

    async def send_video_url(self, bot, message, video_url, prompt=""):
        # 直接发送视频文件，先下载到本地再发
        logger.info(f"bot.send_video_message 实际类型: {type(bot)}，方法: {getattr(bot, 'send_video_message', None)}")
        if not video_url or not video_url.startswith("http"):
            # 不是有效链接，直接提示
            if message.get("IsGroup"):
                await bot.send_at_message(message["FromWxid"], f"视频生成失败：{video_url}", [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], f"视频生成失败：{video_url}")
            return

        tmp_file_path = self.get_tmp_video_path()
        cover_path = None
        try:
            # 下载视频到本地临时文件
            # 设置下载超时配置
            download_timeout = aiohttp.ClientTimeout(
                total=600,  # 总超时时间10分钟
                connect=30,  # 连接超时30秒
                sock_read=300  # 读取超时5分钟
            )
            
            async with aiohttp.ClientSession(timeout=download_timeout) as session:
                async with session.get(video_url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        logger.info(f"下载视频内容长度: {len(content)} 字节")
                        if len(content) == 0:
                            logger.error(f"下载视频内容为空！url={video_url}")
                            if message.get("IsGroup"):
                                await bot.send_at_message(message["FromWxid"], f"视频生成失败：下载内容为空", [message["SenderWxid"]])
                            else:
                                await bot.send_text_message(message["FromWxid"], f"视频生成失败：下载内容为空")
                            return
                        with open(tmp_file_path, 'wb') as f:
                            f.write(content)
                        logger.info(f"视频已下载到本地: {tmp_file_path}, 大小: {os.path.getsize(tmp_file_path)} 字节")
                    else:
                        raise Exception(f"视频下载失败，状态码: {resp.status}")

            # 调试模式下输出视频诊断信息
            if self.debug_mode:
                diagnosis = self.diagnose_video_file(tmp_file_path)
                logger.info(f"视频文件诊断:\n{diagnosis}")

            # 使用自定义发送逻辑发送视频
            cover_path = self._get_video_cover(tmp_file_path)
            logger.info(f"使用自定义发送逻辑，封面: {cover_path}")
            await self.send_video_with_custom_logic(bot, message["FromWxid"], tmp_file_path, cover_path)
            logger.info("视频发送成功")
                
        except Exception as e:
            logger.error(f"Falclient: 视频下载或发送失败: {e}")
            if message.get("IsGroup"):
                await bot.send_at_message(message["FromWxid"], f"视频生成失败：{e}", [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], f"视频生成失败：{e}")
        finally:
            # 删除临时视频文件
            if tmp_file_path and os.path.exists(tmp_file_path):
                try:
                    os.remove(tmp_file_path)
                    logger.info(f"临时视频文件已删除: {tmp_file_path}")
                except Exception as e_rem:
                    logger.warning(f"删除临时视频文件失败: {tmp_file_path}, error: {e_rem}")
            
            # 删除临时封面文件
            if cover_path and os.path.exists(cover_path):
                try:
                    os.remove(cover_path)
                    logger.info(f"临时封面文件已删除: {cover_path}")
                except Exception as e_rem:
                    logger.warning(f"删除临时封面文件失败: {cover_path}, error: {e_rem}")

    async def send_video_with_custom_logic(self, bot, wxid, video_path, cover_path):
        """自定义视频发送逻辑，避开微信API的bug"""
        import aiohttp
        import base64
        
        try:
            # 读取视频文件
            with open(video_path, "rb") as f:
                video_data = f.read()
            
            # 读取封面文件  
            with open(cover_path, "rb") as f:
                image_data = f.read()
            
            # 转换为base64
            video_base64 = base64.b64encode(video_data).decode()
            image_base64 = base64.b64encode(image_data).decode()
            
            # 获取视频时长，使用默认5秒避免MediaInfo问题
            duration_ms = 5000  # 默认5秒，毫秒
            if self.has_mediainfo:
                try:
                    from pymediainfo import MediaInfo
                    media_info = MediaInfo.parse(video_path)
                    if media_info.tracks:
                        track_duration = media_info.tracks[0].duration
                        if track_duration and track_duration > 0:
                            duration_ms = track_duration
                            if duration_ms > 60000:  # 如果超过60秒，设为5秒
                                duration_ms = 5000
                except Exception as e:
                    logger.warning(f"获取视频时长失败，使用默认值: {e}")
            
            # 转换为秒（微信API需要秒为单位）
            duration_seconds = int(duration_ms / 1000)
            
            if self.debug_mode:
                logger.info(f"时长信息: 原始={duration_ms}ms, 转换后={duration_seconds}秒")
            
            # 直接调用微信API，使用正确的格式
            json_param = {
                "Wxid": bot.wxid,
                "ToWxid": wxid, 
                "Base64": f"data:video/mp4;base64,{video_base64}",  # 添加前缀
                "ImageBase64": f"data:image/jpeg;base64,{image_base64}",  # 添加前缀
                "PlayLength": duration_seconds  # 使用秒为单位
            }
            
            file_size = len(video_data)
            predict_time = int(file_size / 1024 / 300)
            logger.info(f"自定义发送视频: 对方wxid:{wxid} 文件大小:{file_size}字节 预计耗时:{predict_time}秒 视频时长:{duration_seconds}秒")
            
            # 尝试多个可能的API端点
            possible_endpoints = [
                f'http://{bot.ip}:{bot.port}/api/Msg/SendVideo',    # Client2/Client3
                f'http://{bot.ip}:{bot.port}/VXAPI/Msg/SendVideo',  # Client (老版本)
            ]
            
            success = False
            last_error = None
            
            for api_url in possible_endpoints:
                try:
                    logger.info(f"尝试API端点: {api_url}")
                    
                    # 设置超时配置 - 图片生成可能需要一定时间
                    timeout = aiohttp.ClientTimeout(
                        total=300,  # 总超时时间5分钟
                        connect=30,  # 连接超时30秒
                        sock_read=300  # 读取超时5分钟
                    )
                    
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(api_url, json=json_param) as resp:
                            if resp.status == 404:
                                logger.warning(f"端点不存在: {api_url}")
                                continue
                            if resp.status != 200:
                                raise Exception(f"HTTP错误: {resp.status}")
                            
                            try:
                                json_resp = await resp.json()
                            except:
                                logger.warning(f"端点返回非JSON: {api_url}")
                                continue
                    
                    if json_resp.get("Success"):
                        logger.info(f"自定义视频发送成功: 对方wxid:{wxid} 时长:{duration_seconds}秒, 使用端点: {api_url}")
                        data = json_resp.get("Data", {})
                        success = True
                        return data.get("clientMsgId"), data.get("newMsgId")
                    else:
                        error_msg = json_resp.get("ErrorMsg") or json_resp.get("Message", "未知错误")
                        last_error = f"API错误: {error_msg}"
                        logger.warning(f"端点 {api_url} 返回错误: {error_msg}")
                        
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"端点 {api_url} 请求失败: {e}")
                    continue
            
            # 所有端点都失败了
            if not success:
                raise Exception(f"所有API端点都失败，最后错误: {last_error}")
                
        except Exception as e:
            logger.error(f"自定义视频发送失败: {e}")
            raise e

    async def handle_edit_image(self, bot, message, image_bytes, prompt):
        """处理图片编辑任务，调用fal-ai/flux-pro/kontext模型"""
        import tempfile
        logger.info(f"[edit_image] 开始处理图片编辑任务，提示词: {prompt}")
        
        # 添加请求已收到的提示
        notice = "您的图片编辑请求已经收到，请稍候..."
        if message.get("IsGroup"):
            await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
        else:
            await bot.send_text_message(message["FromWxid"], notice)
        
        tmp_file_path = None
        try:
            # 保存图片到临时文件
            with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp_file:
                tmp_file.write(image_bytes)
                tmp_file_path = tmp_file.name

            # 使用fal_client上传图片并调用编辑API
            client = fal_client.SyncClient(key=self.fal_api_key)
            image_url = client.upload_file(tmp_file_path)
            if not image_url:
                await self.send_edit_error(bot, message, "图片上传失败")
                return

            logger.info(f"[edit_image] 图片上传成功: {image_url}")

            # 定义队列更新回调函数（可选）
            def on_queue_update(update):
                if isinstance(update, fal_client.InProgress):
                    for log in update.logs:
                        logger.info(f"[edit_image] 队列日志: {log.get('message', '')}")

            # 调用flux-pro/kontext模型进行图片编辑
            result = client.subscribe(
                f"fal-ai/{self.fal_edit_model}",
                arguments={
                    "prompt": prompt,
                    "image_url": image_url
                },
                with_logs=True,
                on_queue_update=on_queue_update
            )
            
            logger.info(f"[edit_image] API响应: {result}")
            
            # 处理返回结果
            if isinstance(result, dict):
                # 检查是否有images字段（数组格式）
                if "images" in result and isinstance(result["images"], list) and len(result["images"]) > 0:
                    edited_image_url = result["images"][0].get("url")
                    if edited_image_url and edited_image_url.startswith("http"):
                        await self.download_and_send_image(bot, message, edited_image_url, "图片编辑")
                        return
                
                # 检查是否有image字段（单个对象格式）
                elif "image" in result and isinstance(result["image"], dict):
                    edited_image_url = result["image"].get("url")
                    if edited_image_url and edited_image_url.startswith("http"):
                        await self.download_and_send_image(bot, message, edited_image_url, "图片编辑")
                        return
                
                # 检查是否直接返回了url字段
                elif "url" in result:
                    edited_image_url = result["url"]
                    if edited_image_url and edited_image_url.startswith("http"):
                        await self.download_and_send_image(bot, message, edited_image_url, "图片编辑")
                        return
            
            # 如果上述格式都不匹配，记录完整响应并报错
            logger.error(f"[edit_image] 未能从API响应中获取图片URL，完整响应: {result}")
            await self.send_edit_error(bot, message, "API返回的响应格式不正确，未找到编辑后的图片")
            
        except Exception as e:
            logger.error(f"[edit_image] 图片编辑API调用异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            await self.send_edit_error(bot, message, f"图片编辑服务出错: {str(e)}")
        finally:
            # 删除临时文件
            if tmp_file_path and os.path.exists(tmp_file_path):
                try:
                    os.remove(tmp_file_path)
                    logger.info(f"[edit_image] 临时文件已删除: {tmp_file_path}")
                except Exception as e_rem:
                    logger.warning(f"[edit_image] 删除临时文件失败: {tmp_file_path}, error: {e_rem}")

    async def download_and_send_image(self, bot, message, image_url, task_name="图片处理"):
        """下载图片并发送给用户"""
        try:
            import aiohttp
            # 设置下载超时配置
            download_timeout = aiohttp.ClientTimeout(
                total=120,  # 总超时时间2分钟
                connect=30,  # 连接超时30秒
                sock_read=120  # 读取超时2分钟
            )
            
            async with aiohttp.ClientSession(timeout=download_timeout) as session:
                async with session.get(image_url) as resp:
                    if resp.status == 200:
                        image_data = await resp.read()
                        logger.info(f"[{task_name}] 图片下载成功，大小: {len(image_data)} 字节")
                        
                        # 发送图片
                        if message.get("IsGroup"):
                            await bot.send_image_message(message["FromWxid"], image_data)
                        else:
                            await bot.send_image_message(message["FromWxid"], image_data)
                        return True
                    else:
                        raise Exception(f"图片下载失败，状态码: {resp.status}")
        except Exception as e:
            logger.error(f"[{task_name}] 图片下载或发送失败: {e}")
            await self.send_edit_error(bot, message, f"{task_name}完成但图片下载失败: {str(e)}")
            return False

    async def send_edit_error(self, bot, message, error_msg):
        """发送图片编辑错误消息"""
        full_error = f"图片编辑失败：{error_msg}"
        if message.get("IsGroup"):
            await bot.send_at_message(message["FromWxid"], full_error, [message["SenderWxid"]])
        else:
            await bot.send_text_message(message["FromWxid"], full_error)

    async def handle_jimeng_service(self, bot, message, prompt):
        """处理即梦AI文字生成图片任务"""
        logger.info(f"[jimeng] 开始处理即梦AI绘图任务，提示词: {prompt}")
        
        if not self.jimeng_api_key or not self.jimeng_url:
            error_msg = "即梦AI配置不完整，请检查jimeng_api_key和jimeng_url配置"
            logger.error(f"[jimeng] {error_msg}")
            await self.send_jimeng_error(bot, message, error_msg)
            return
        
        try:
            import aiohttp
            
            # 发送API请求
            url = f"{self.jimeng_url}/v1/images/generations"
            headers = {
                "Authorization": f"Bearer {self.jimeng_api_key}"
            }
            data = {
                "model": "jimeng-3.0",
                "prompt": prompt
            }
            
            logger.info(f"[jimeng] API请求 url={url} headers={headers} data={data}")
            
            # 设置超时配置 - 图片生成可能需要一定时间
            timeout = aiohttp.ClientTimeout(
                total=300,  # 总超时时间5分钟
                connect=30,  # 连接超时30秒
                sock_read=300  # 读取超时5分钟
            )
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=data) as resp:
                    logger.info(f"[jimeng] API响应状态: {resp.status}")
                    
                    if resp.status == 200:
                        result = await resp.json()
                        logger.info(f"[jimeng] API响应: {result}")
                        # 新增：处理code -2007
                        if isinstance(result, dict) and result.get('code') == -2007:
                            await self.send_jimeng_error(bot, message, "您输入的文字不符合平台规则，请修改后重试")
                            return
                        data_list = result.get('data', [])
                        if data_list:
                            # 遍历所有生成的图片URL并发送
                            for item in data_list:
                                url = item.get('url')
                                if url:
                                    logger.info(f"[jimeng] 图片URL: {url}")
                                    await self.download_and_send_image(bot, message, url, "即梦AI绘图")
                            
                            # 发送完成提示
                            success_msg = "即梦AI图片生成完毕。"
                            if message.get("IsGroup"):
                                await bot.send_at_message(message["FromWxid"], success_msg, [message["SenderWxid"]])
                            else:
                                await bot.send_text_message(message["FromWxid"], success_msg)
                        else:
                            await self.send_jimeng_error(bot, message, "API返回数据为空")
                    else:
                        error_text = await resp.text()
                        logger.error(f"[jimeng] API请求失败: {resp.status} - {error_text}")
                        await self.send_jimeng_error(bot, message, f"API请求失败: {resp.status}")
                        
        except Exception as e:
            logger.error(f"[jimeng] 即梦AI绘图API调用异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            await self.send_jimeng_error(bot, message, f"服务出错: {str(e)}")

    async def send_jimeng_error(self, bot, message, error_msg):
        """发送即梦AI绘图错误消息"""
        full_error = f"即梦AI绘图失败：{error_msg}"
        if message.get("IsGroup"):
            await bot.send_at_message(message["FromWxid"], full_error, [message["SenderWxid"]])
        else:
            await bot.send_text_message(message["FromWxid"], full_error)

    async def handle_veo3_video(self, bot, message, prompt):
        """处理veo3视频生成任务，非流式，提取prompt和视频url，分别回复用户和下载视频"""
        import aiohttp
        import asyncio
        import re
        max_retries = self.veo3_retry_times if hasattr(self, 'veo3_retry_times') else 30
        api_key = self.openai_image_api_key
        api_base = self.openai_image_api_base or "https://api.tu-zi.com/v1"
        url = f"{api_base}/chat/completions"
        headers = {
            'Accept': 'application/json',
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
        data = {
            "temperature": 0.7,
            "messages": [
                {"content": prompt, "role": "user"}
            ],
            "model": "veo3",
            "stream": False
        }
        retry = 0
        while retry < max_retries:
            try:
                timeout = aiohttp.ClientTimeout(total=300, connect=30, sock_read=300)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, headers=headers, json=data) as resp:
                        resp_text = await resp.text()
                        if resp.status != 200:
                            logger.warning(f"veo3接口返回非200: {resp.status}, {resp_text}")
                            retry += 1
                            await asyncio.sleep(2)
                            continue
                        try:
                            result = json.loads(resp_text)
                        except Exception as e:
                            logger.warning(f"veo3响应解析失败: {e}, 内容: {resp_text}")
                            retry += 1
                            await asyncio.sleep(2)
                            continue
                        # 提取prompt
                        prompt_text = None
                        try:
                            prompt_text = result["choices"][0]["message"]["content"]
                        except Exception:
                            pass
                        # 回复prompt
                        if prompt_text:
                            tip = f"💡veo3模型理解您的描述如下：\n{prompt_text}"
                            if message.get("IsGroup"):
                                await bot.send_at_message(message["FromWxid"], tip, [message["SenderWxid"]])
                            else:
                                await bot.send_text_message(message["FromWxid"], tip)
                        # 提取视频url
                        video_url = None
                        # 先找高质量视频
                        match = re.search(r'https?://[\w\-\./]+\.mp4', resp_text)
                        if match:
                            video_url = match.group(0)
                        if video_url:
                            logger.info(f"veo3视频url获取成功: {video_url}")
                            await self.send_video_url(bot, message, video_url, prompt)
                            return
                        else:
                            logger.error(f"veo3未获取到视频url, resp: {resp_text}")
                            await self.send_video_url(bot, message, "未获取到视频URL", prompt)
                            return
            except Exception as e:
                logger.warning(f"veo3请求异常: {e}")
                retry += 1
                await asyncio.sleep(2)
        # 超过重试次数
        error_tip = f"veo3接口重试{max_retries}次仍失败，可能是服务器繁忙或内容不合规。请稍后重试，或更换描述内容。"
        if message.get("IsGroup"):
            await bot.send_at_message(message["FromWxid"], error_tip, [message["SenderWxid"]])
        else:
            await bot.send_text_message(message["FromWxid"], error_tip)
        await self.send_video_url(bot, message, f"veo3接口重试{max_retries}次仍失败", prompt)
