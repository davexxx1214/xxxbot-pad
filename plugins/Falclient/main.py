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
            
            # 新增配置选项
            self.debug_mode = plugin_config.get("debug_mode", True)
            self.fallback_to_url = plugin_config.get("fallback_to_url", True)
            self.try_video_conversion = plugin_config.get("try_video_conversion", False)
        except Exception as e:
            logger.error(f"加载Falclient插件配置文件失败: {e}")
            raise
        # 记录待生成视频的状态: {user_or_group_id: timestamp}
        self.waiting_video = {}
        self.image_msgid_cache = set()
        self.image_cache_timeout = 60
        self.image_cache = {}
        
        # 检查pymediainfo依赖
        try:
            from pymediainfo import MediaInfo
            self.has_mediainfo = True
        except ImportError:
            self.has_mediainfo = False
            if self.debug_mode:
                logger.warning("pymediainfo未安装，视频时长将使用默认值。建议安装: pip install pymediainfo")

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
        
        # 添加调试命令
        if content.startswith("测试视频发送"):
            if self.debug_mode:
                # 创建一个测试视频URL（使用最近成功的URL）
                test_url = "https://v3.fal.media/files/tiger/albVXs7mcha3swmTzNFt6_output.mp4"
                notice = "开始测试视频发送功能..."
                if message["IsGroup"]:
                    await bot.send_at_message(message["FromWxid"], notice, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], notice)
                await self.send_video_url(bot, message, test_url)
                return False
            else:
                tip = "调试模式未启用，无法使用测试命令"
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
                            await self.send_video_url(bot, message, video_url, prompt)
                        else:
                            await self.send_video_url(bot, message, "未获取到视频URL", prompt)
                    else:
                        await self.send_video_url(bot, message, f"API请求失败: {resp.status}", prompt)
        except Exception as e:
            import traceback
            logger.error(f"Falclient: 文生视频API调用异常: {e}\n{traceback.format_exc()}")
            await self.send_video_url(bot, message, f"API调用异常: {e}", prompt)

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
                    
                    # 调试模式下输出视频诊断信息
                    if self.debug_mode:
                        diagnosis = self.diagnose_video_file(video_tmp_path)
                        logger.info(f"视频文件诊断:\n{diagnosis}")

                    # 尝试几种不同的发送方式
                    send_success = False
                    
                    # 方案0：使用自定义发送逻辑（避开微信API的bug）
                    try:
                        cover_path = self._generate_cover_image_file()
                        logger.info(f"方案0：使用自定义发送逻辑，封面: {cover_path}")
                        await self.send_video_with_custom_logic(bot, message["FromWxid"], video_tmp_path, cover_path)
                        send_success = True
                        logger.info("方案0发送成功")
                        if message.get("IsGroup"):
                            await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                        else:
                            await bot.send_text_message(message["FromWxid"], "视频已生成，点击上方播放。")
                    except Exception as e0:
                        logger.warning(f"方案0发送失败: {e0}")
                        
                        # 方案1：使用自定义封面
                        try:
                            if not cover_path:  # 如果方案0没有生成封面
                                cover_path = self._generate_cover_image_file()
                            logger.info(f"方案1：使用自定义生成的封面: {cover_path}")
                            if message.get("IsGroup"):
                                await bot.send_video_message(message["FromWxid"], Path(video_tmp_path), image=Path(cover_path))
                                await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                            else:
                                await bot.send_video_message(message["FromWxid"], Path(video_tmp_path), image=Path(cover_path))
                            send_success = True
                            logger.info("方案1发送成功")
                        except Exception as e1:
                            logger.warning(f"方案1发送失败: {e1}")
                            
                            # 方案2：不使用封面 
                            try:
                                logger.info("方案2：不使用封面，传入None")
                                if message.get("IsGroup"):
                                    await bot.send_video_message(message["FromWxid"], Path(video_tmp_path), image=None)
                                    await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                                else:
                                    await bot.send_video_message(message["FromWxid"], Path(video_tmp_path), image=None)
                                send_success = True
                                logger.info("方案2发送成功")
                            except Exception as e2:
                                logger.warning(f"方案2发送失败: {e2}")
                                
                                # 方案3：尝试不传image参数
                                try:
                                    logger.info("方案3：不传image参数")
                                    if message.get("IsGroup"):
                                        await bot.send_video_message(message["FromWxid"], Path(video_tmp_path))
                                        await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                                    else:
                                        await bot.send_video_message(message["FromWxid"], Path(video_tmp_path))
                                    send_success = True
                                    logger.info("方案3发送成功")
                                except Exception as e3:
                                    logger.error(f"方案3也发送失败: {e3}")
                                    # 所有方案都失败了，抛出最后一个异常
                                    raise e3
                                
                    if not send_success:
                        # 所有视频发送方案都失败了，发送视频链接作为备用方案
                        logger.warning("所有视频发送方案都失败，改为发送视频链接")
                        
                        # 尝试发送更友好的链接卡片格式
                        video_msg = f"🎬 视频生成完成\n\n▶️ 点击查看视频：\n{video_url}\n\n📝 提示词：{prompt}"
                        
                        if message.get("IsGroup"):
                            await bot.send_at_message(message["FromWxid"], video_msg, [message["SenderWxid"]])
                        else:
                            await bot.send_text_message(message["FromWxid"], video_msg)
                        return  # 成功发送链接，不抛出异常
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

            # 调试模式下输出视频诊断信息
            if self.debug_mode:
                diagnosis = self.diagnose_video_file(tmp_file_path)
                logger.info(f"视频文件诊断:\n{diagnosis}")

            # 尝试几种不同的发送方式
            send_success = False
            
            # 方案0：使用自定义发送逻辑（避开微信API的bug）
            try:
                cover_path = self._generate_cover_image_file()
                logger.info(f"方案0：使用自定义发送逻辑，封面: {cover_path}")
                await self.send_video_with_custom_logic(bot, message["FromWxid"], tmp_file_path, cover_path)
                send_success = True
                logger.info("方案0发送成功")
                if message.get("IsGroup"):
                    await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], "视频已生成，点击上方播放。")
            except Exception as e0:
                logger.warning(f"方案0发送失败: {e0}")
                
                # 方案1：使用自定义封面
                try:
                    if not cover_path:  # 如果方案0没有生成封面
                        cover_path = self._generate_cover_image_file()
                    logger.info(f"方案1：使用自定义生成的封面: {cover_path}")
                    if message.get("IsGroup"):
                        await bot.send_video_message(message["FromWxid"], Path(tmp_file_path), image=Path(cover_path))
                        await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                    else:
                        await bot.send_video_message(message["FromWxid"], Path(tmp_file_path), image=Path(cover_path))
                    send_success = True
                    logger.info("方案1发送成功")
                except Exception as e1:
                    logger.warning(f"方案1发送失败: {e1}")
                    
                    # 方案2：不使用封面 
                    try:
                        logger.info("方案2：不使用封面，传入None")
                        if message.get("IsGroup"):
                            await bot.send_video_message(message["FromWxid"], Path(tmp_file_path), image=None)
                            await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                        else:
                            await bot.send_video_message(message["FromWxid"], Path(tmp_file_path), image=None)
                        send_success = True
                        logger.info("方案2发送成功")
                    except Exception as e2:
                        logger.warning(f"方案2发送失败: {e2}")
                        
                        # 方案3：尝试不传image参数
                        try:
                            logger.info("方案3：不传image参数")
                            if message.get("IsGroup"):
                                await bot.send_video_message(message["FromWxid"], Path(tmp_file_path))
                                await bot.send_at_message(message["FromWxid"], "视频已生成，点击上方播放。", [message["SenderWxid"]])
                            else:
                                await bot.send_video_message(message["FromWxid"], Path(tmp_file_path))
                            send_success = True
                            logger.info("方案3发送成功")
                        except Exception as e3:
                            logger.error(f"方案3也发送失败: {e3}")
                            # 所有方案都失败了，抛出最后一个异常
                            raise e3
                                
            if not send_success:
                # 所有视频发送方案都失败了，发送视频链接作为备用方案
                logger.warning("所有视频发送方案都失败，改为发送视频链接")
                
                # 尝试发送更友好的链接卡片格式
                video_msg = f"🎬 视频生成完成\n\n▶️ 点击查看视频：\n{video_url}\n\n📝 提示词：{prompt}"
                
                if message.get("IsGroup"):
                    await bot.send_at_message(message["FromWxid"], video_msg, [message["SenderWxid"]])
                else:
                    await bot.send_text_message(message["FromWxid"], video_msg)
                return  # 成功发送链接，不抛出异常
                
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
            duration = 5000  # 默认5秒，毫秒
            if self.has_mediainfo:
                try:
                    from pymediainfo import MediaInfo
                    media_info = MediaInfo.parse(video_path)
                    if media_info.tracks:
                        track_duration = media_info.tracks[0].duration
                        if track_duration and track_duration > 0:
                            duration = track_duration
                            if duration > 60000:  # 如果超过60秒，设为5秒
                                duration = 5000
                except Exception as e:
                    logger.warning(f"获取视频时长失败，使用默认值: {e}")
            
            # 直接调用微信API，使用正确的格式
            json_param = {
                "Wxid": bot.wxid,
                "ToWxid": wxid, 
                "Base64": f"data:video/mp4;base64,{video_base64}",  # 添加前缀
                "ImageBase64": f"data:image/jpeg;base64,{image_base64}",  # 添加前缀
                "PlayLength": duration
            }
            
            file_size = len(video_data)
            predict_time = int(file_size / 1024 / 300)
            logger.info(f"自定义发送视频: 对方wxid:{wxid} 文件大小:{file_size}字节 预计耗时:{predict_time}秒")
            
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
                    
                    async with aiohttp.ClientSession() as session:
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
                        logger.info(f"自定义视频发送成功: 对方wxid:{wxid} 时长:{duration}ms, 使用端点: {api_url}")
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
