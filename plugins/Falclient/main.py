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
        try:
            url = f"https://fal.run/fal-ai/{self.fal_kling_text_model}"
            headers = {
                "Authorization": f"Key {self.fal_api_key}",
                "Content-Type": "application/json"
            }
            data = {"prompt": prompt}
            logger.info(f"Falclient: 文生视频API请求 url={url} headers={headers} data={data}")
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=data) as resp:
                    logger.info(f"Falclient: 文生视频API响应状态: {resp.status}")
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
            import traceback
            logger.error(f"Falclient: 文生视频API调用异常: {e}\n{traceback.format_exc()}")
            await self.send_video_url(bot, message, f"API调用异常: {e}")

    def _generate_cover_image_file(self) -> str:
        tmp_dir = os.path.join(os.path.dirname(__file__), 'tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        cover_filename = f"cover_{uuid.uuid4().hex}.jpg"  # 改为jpg格式
        cover_path = os.path.join(tmp_dir, cover_filename)

        # 使用常见的视频封面尺寸 16:9
        img = Image.new('RGB', (640, 360), color=(255, 255, 255))  # 改为更标准的尺寸
        draw = ImageDraw.Draw(img)
        
        # 简化绘制，只画一个简单的矩形框
        draw.rectangle([10, 10, 630, 350], outline=(100, 100, 100), width=2)
        
        try:
            # 尝试使用系统字体
            import platform
            if platform.system() == "Windows":
                font = ImageFont.truetype("arial.ttf", 36)
            elif platform.system() == "Linux":
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
            else:
                font = ImageFont.load_default()
        except (IOError, OSError):
            font = ImageFont.load_default()
        
        # 居中绘制文字
        text = "视频封面"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (640 - text_width) // 2
        y = (360 - text_height) // 2
        draw.text((x, y), text, fill=(0, 0, 0), font=font)
        
        # 保存为JPEG格式，质量设为95
        img.save(cover_path, format='JPEG', quality=95)
        logger.info(f"临时封面已生成: {cover_path}")
        return cover_path

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
        tmp_file_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp_file:
                tmp_file.write(image_bytes)
                tmp_file_path = tmp_file.name

            client = fal_client.SyncClient(key=self.fal_api_key)
            image_url = client.upload_file(tmp_file_path)
            if not image_url:
                await self.send_video_url(bot, message, "图片上传失败")
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
                    async with aiohttp.ClientSession() as session:
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
                    
                    # 尝试几种不同的发送方式
                    send_success = False
                    
                    # 方案1：使用自定义封面
                    try:
                        cover_path = self._generate_cover_image_file()
                        logger.info(f"[img2video] 方案1：使用自定义生成的封面: {cover_path}")
                        if message.get("IsGroup"):
                            await bot.send_video_message(message["FromWxid"], Path(video_tmp_path), image=Path(cover_path))
                            await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                        else:
                            await bot.send_video_message(message["FromWxid"], Path(video_tmp_path), image=Path(cover_path))
                        send_success = True
                        logger.info("[img2video] 方案1发送成功")
                    except Exception as e1:
                        logger.warning(f"[img2video] 方案1发送失败: {e1}")
                        
                        # 方案2：不使用封面 
                        try:
                            logger.info("[img2video] 方案2：不使用封面，传入None")
                            if message.get("IsGroup"):
                                await bot.send_video_message(message["FromWxid"], Path(video_tmp_path), image=None)
                                await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                            else:
                                await bot.send_video_message(message["FromWxid"], Path(video_tmp_path), image=None)
                            send_success = True
                            logger.info("[img2video] 方案2发送成功")
                        except Exception as e2:
                            logger.warning(f"[img2video] 方案2发送失败: {e2}")
                            
                            # 方案3：尝试不传image参数
                            try:
                                logger.info("[img2video] 方案3：不传image参数")
                                if message.get("IsGroup"):
                                    await bot.send_video_message(message["FromWxid"], Path(video_tmp_path))
                                    await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                                else:
                                    await bot.send_video_message(message["FromWxid"], Path(video_tmp_path))
                                send_success = True
                                logger.info("[img2video] 方案3发送成功")
                            except Exception as e3:
                                logger.error(f"[img2video] 方案3也发送失败: {e3}")
                                # 所有方案都失败了，抛出最后一个异常
                                raise e3
                                
                    if not send_success:
                        raise Exception("所有发送方案都失败了")
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
                await self.send_video_url(bot, message, video_url)
            else:
                await self.send_video_url(bot, message, "未获取到视频URL")
        except Exception as e:
            await self.send_video_url(bot, message, f"API调用异常: {e}")

    async def send_video_url(self, bot, message, video_url):
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
            async with aiohttp.ClientSession() as session:
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

            # 尝试几种不同的发送方式
            send_success = False
            
            # 方案1：使用自定义封面
            try:
                cover_path = self._generate_cover_image_file()
                logger.info(f"[img2video] 方案1：使用自定义生成的封面: {cover_path}")
                if message.get("IsGroup"):
                    await bot.send_video_message(message["FromWxid"], Path(tmp_file_path), image=Path(cover_path))
                    await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                else:
                    await bot.send_video_message(message["FromWxid"], Path(tmp_file_path), image=Path(cover_path))
                send_success = True
                logger.info("[img2video] 方案1发送成功")
            except Exception as e1:
                logger.warning(f"[img2video] 方案1发送失败: {e1}")
                
                # 方案2：不使用封面 
                try:
                    logger.info("[img2video] 方案2：不使用封面，传入None")
                    if message.get("IsGroup"):
                        await bot.send_video_message(message["FromWxid"], Path(tmp_file_path), image=None)
                        await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                    else:
                        await bot.send_video_message(message["FromWxid"], Path(tmp_file_path), image=None)
                    send_success = True
                    logger.info("[img2video] 方案2发送成功")
                except Exception as e2:
                    logger.warning(f"[img2video] 方案2发送失败: {e2}")
                    
                    # 方案3：尝试不传image参数
                    try:
                        logger.info("[img2video] 方案3：不传image参数")
                        if message.get("IsGroup"):
                            await bot.send_video_message(message["FromWxid"], Path(tmp_file_path))
                            await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                        else:
                            await bot.send_video_message(message["FromWxid"], Path(tmp_file_path))
                        send_success = True
                        logger.info("[img2video] 方案3发送成功")
                    except Exception as e3:
                        logger.error(f"[img2video] 方案3也发送失败: {e3}")
                        # 所有方案都失败了，抛出最后一个异常
                        raise e3
                        
            if not send_success:
                raise Exception("所有发送方案都失败了")
                
        except Exception as e:
            logger.error(f"Falclient: 视频下载或发送失败: {e}")
            if message.get("IsGroup"):
                await bot.send_at_message(message["FromWxid"], f"视频生成失败：{video_url}", [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], f"视频生成失败：{video_url}")
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
