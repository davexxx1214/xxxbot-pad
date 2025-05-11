import asyncio
import base64
import os
from asyncio import Future
from asyncio import Queue, sleep
from io import BytesIO
from pathlib import Path
from typing import Union

import aiohttp
import pysilk
from loguru import logger
from pydub import AudioSegment
from pymediainfo import MediaInfo

from .base import *
from .protect import protector
from ..errors import *


class MessageMixin(WechatAPIClientBase):
    def __init__(self, ip: str, port: int):
        # 初始化消息队列
        super().__init__(ip, port)
        self._message_queue = Queue()
        self._is_processing = False

    async def _process_message_queue(self):
        """
        处理消息队列的异步方法
        """
        if self._is_processing:
            return

        self._is_processing = True
        while True:
            if self._message_queue.empty():
                self._is_processing = False
                break

            func, args, kwargs, future = await self._message_queue.get()
            try:
                result = await func(*args, **kwargs)
                future.set_result(result)
            except Exception as e:
                future.set_exception(e)
            finally:
                self._message_queue.task_done()
                await sleep(1)  # 消息发送间隔1秒

    async def _queue_message(self, func, *args, **kwargs):
        """
        将消息添加到队列
        """
        future = Future()
        await self._message_queue.put((func, args, kwargs, future))

        if not self._is_processing:
            asyncio.create_task(self._process_message_queue())

        return await future

    async def revoke_message(self, wxid: str, client_msg_id: int, create_time: int, new_msg_id: int) -> bool:
        """撤回消息。

        Args:
            wxid (str): 接收人wxid
            client_msg_id (int): 发送消息的返回值
            create_time (int): 发送消息的返回值
            new_msg_id (int): 发送消息的返回值

        Returns:
            bool: 成功返回True，失败返回False

        Raises:
            UserLoggedOut: 未登录时调用
            BanProtection: 登录新设备后4小时内操作
            根据error_handler处理错误
        """
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "ClientMsgId": client_msg_id, "CreateTime": create_time,
                          "NewMsgId": new_msg_id}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/Revoke', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                logger.info("消息撤回成功: 对方wxid:{} ClientMsgId:{} CreateTime:{} NewMsgId:{}",
                            wxid,
                            client_msg_id,
                            new_msg_id)
                return True
            else:
                self.error_handler(json_resp)

    async def send_text_message(self, wxid: str, content: str, at: Union[list, str] = "") -> tuple[int, int, int]:
        """发送文本消息。

        Args:
            wxid (str): 接收人wxid
            content (str): 消息内容
            at (list, str, optional): 要@的用户

        Returns:
            tuple[int, int, int]: 返回(ClientMsgid, CreateTime, NewMsgId)

        Raises:
            UserLoggedOut: 未登录时调用
            BanProtection: 登录新设备后4小时内操作
            根据error_handler处理错误
        """
        return await self._queue_message(self._send_text_message, wxid, content, at)

    async def _send_text_message(self, wxid: str, content: str, at: list[str] = None) -> tuple[int, int, int]:
        """
        实际发送文本消息的方法
        """
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        if isinstance(at, str):
            at_str = at
        elif isinstance(at, list):
            if at is None:
                at = []
            at_str = ",".join(at)
        else:
            raise ValueError("Argument 'at' should be str or list")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "Content": content, "Type": 1, "At": at_str}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/SendTxt', json=json_param)
            json_resp = await response.json()
            if json_resp.get("Success"):
                logger.info("发送文字消息: 对方wxid:{} at:{} 内容:{}", wxid, at, content)
                data = json_resp.get("Data")
                return data.get("List")[0].get("ClientMsgid"), data.get("List")[0].get("Createtime"), data.get("List")[
                    0].get("NewMsgId")
            else:
                self.error_handler(json_resp)

    async def send_image_message(self, wxid: str, image: Union[str, bytes, os.PathLike]) -> dict:
        """发送图片消息。

        Args:
            wxid (str): 接收人wxid
            image (str, byte, os.PathLike): 图片，支持base64字符串，图片byte，图片路径

        Returns:
            tuple[int, int, int]: 返回(ClientImgId, CreateTime, NewMsgId)

        Raises:
            UserLoggedOut: 未登录时调用
            BanProtection: 登录新设备后4小时内操作
            ValueError: image_path和image_base64都为空或都不为空时
            根据error_handler处理错误
        """
        return await self._queue_message(self._send_image_message, wxid, image)

    async def _send_image_message(self, wxid: str, image: Union[str, bytes, os.PathLike]) -> dict:
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        if isinstance(image, str):
            pass
        elif isinstance(image, bytes):
            image = base64.b64encode(image).decode()
        elif isinstance(image, os.PathLike):
            with open(image, 'rb') as f:
                image = base64.b64encode(f.read()).decode()
        else:
            raise ValueError("Argument 'image' can only be str, bytes, or os.PathLike")

        # Ensure the image_base64 string has a data URI prefix
        if not image.startswith("data:image"):
            logger.debug("Image base64 does not have a data URI prefix. Adding 'data:image/jpeg;base64,'")
            image = "data:image/jpeg;base64," + image
        elif image.startswith("data:image/png;base64,"): # Correctly handle if it was already PNG
            logger.debug("Image base64 already has PNG data URI prefix.")
        elif image.startswith("data:image/jpeg;base64,"):
             logger.debug("Image base64 already has JPEG data URI prefix.")
        # else: if it's some other data:image/xxx;base64, leave it as is.

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "Base64": image}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/UploadImg', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                json_param.pop('Base64')
                logger.info("发送图片消息: 对方wxid:{} 图片base64略", wxid)
                # 返回完整的响应结果
                return json_resp
            else:
                self.error_handler(json_resp)

    async def send_video_message(self, wxid: str, video: Union[str, bytes, os.PathLike],
                                 image: [str, bytes, os.PathLike] = None):
        """发送视频消息。不推荐使用，上传速度很慢300KB/s。如要使用，可压缩视频，或者发送链接卡片而不是视频。

                Args:
                    wxid (str): 接收人wxid
                    video (str, bytes, os.PathLike): 视频 接受base64字符串，字节，文件路径
                    image (str, bytes, os.PathLike): 视频封面图片 接受base64字符串，字节，文件路径

                Returns:
                    tuple[int, int]: 返回(ClientMsgid, NewMsgId)

                Raises:
                    UserLoggedOut: 未登录时调用
                    BanProtection: 登录新设备后4小时内操作
                    ValueError: 视频或图片参数都为空或都不为空时
                    根据error_handler处理错误
                """
        if not image:
            image = Path(os.path.join(Path(__file__).resolve().parent, "fallback.png"))
        # get video base64 and duration
        if isinstance(video, str):
            vid_base64 = video
            video = base64.b64decode(video)
            file_len = len(video)
            media_info = MediaInfo.parse(BytesIO(video))
        elif isinstance(video, bytes):
            vid_base64 = base64.b64encode(video).decode()
            file_len = len(video)
            media_info = MediaInfo.parse(BytesIO(video))
        elif isinstance(video, os.PathLike):
            with open(video, "rb") as f:
                file_len = len(f.read())
                vid_base64 = base64.b64encode(f.read()).decode()
            media_info = MediaInfo.parse(video)
        else:
            raise ValueError("video should be str, bytes, or path")
        duration = media_info.tracks[0].duration

        # Ensure vid_base64 (video content) has the data URI prefix
        if not vid_base64.startswith("data:video/mp4;base64,"):
            logger.debug(f"[MessageMixin] Video base64 (len: {len(vid_base64)}) does not have data URI. Adding 'data:video/mp4;base64,'")
            vid_base64 = "data:video/mp4;base64," + vid_base64
        else:
            logger.debug(f"[MessageMixin] Video base64 (len: {len(vid_base64)}) already has data URI.")

        # get image base64
        if isinstance(image, str):
            image_base64 = image
        elif isinstance(image, bytes):
            image_base64 = base64.b64encode(image).decode()
        elif isinstance(image, os.PathLike):
            with open(image, "rb") as f:
                image_base64 = base64.b64encode(f.read()).decode()
        else:
            raise ValueError("image should be str, bytes, or path")

        # Ensure the image_base64 string for the cover has a data URI prefix
        if not image_base64: # If image_base64 is empty or None after processing (e.g. fallback.png failed)
            logger.warning("[MessageMixin] Cover image_base64 is empty. Using 1x1 transparent PNG as fallback.")
            image_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
            # Now the following block will add the jpeg prefix to this 1x1 png

        if image_base64: # Only proceed if image_base64 is not empty or None (it should be the 1x1 png if it was empty before)
            if not image_base64.startswith("data:image"):
                logger.debug(f"[MessageMixin] Cover image base64 (len: {len(image_base64)}) does not have a data URI prefix. Adding 'data:image/jpeg;base64,'")
                image_base64 = "data:image/jpeg;base64," + image_base64
            elif image_base64.startswith("data:image/png;base64,"):
                logger.debug(f"[MessageMixin] Cover image base64 (len: {len(image_base64)}) already has PNG data URI prefix.")
            elif image_base64.startswith("data:image/jpeg;base64,"):
                logger.debug(f"[MessageMixin] Cover image base64 (len: {len(image_base64)}) already has JPEG data URI prefix.")
        # No explicit else here, as image_base64 should now always be populated if it reached this point from an empty state.

        # 打印预估时间，300KB/s
        predict_time = int(file_len / 1024 / 300)
        logger.info("开始发送视频: 对方wxid:{} 视频base64略 图片base64略 预计耗时:{}秒", wxid, predict_time)

        logger.info(f'使用的封面图片路径: {image}, 绝对路径: {os.path.abspath(image)}')

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "Base64": vid_base64, "ImageBase64": image_base64,
                          "PlayLength": duration}
            async with session.post(f'http://{self.ip}:{self.port}/api/Msg/SendVideo', json=json_param) as resp:
                json_resp = await resp.json()

        if json_resp.get("Success"):
            json_param.pop('Base64')
            json_param.pop('ImageBase64')
            logger.info("发送视频成功: 对方wxid:{} 时长:{} 视频base64略 图片base64略", wxid, duration)
            data = json_resp.get("Data")
            return data.get("clientMsgId"), data.get("newMsgId")
        else:
            self.error_handler(json_resp)

    async def send_voice_message(self, wxid: str, voice: Union[str, bytes, os.PathLike], format: str = "amr") -> \
            tuple[int, int, int]:
        """发送语音消息。

        Args:
            wxid (str): 接收人wxid
            voice (str, bytes, os.PathLike): 语音 接受base64字符串，字节，文件路径
            format (str, optional): 语音格式，支持amr/wav/mp3. Defaults to "amr".

        Returns:
            tuple[int, int, int]: 返回(ClientMsgid, CreateTime, NewMsgId)

        Raises:
            UserLoggedOut: 未登录时调用
            BanProtection: 登录新设备后4小时内操作
            ValueError: voice_path和voice_base64都为空或都不为空时，或format不支持时
            根据error_handler处理错误
        """
        return await self._queue_message(self._send_voice_message, wxid, voice, format)

    async def _send_voice_message(self, wxid: str, voice: Union[str, bytes, os.PathLike], format: str = "amr") -> \
            tuple[int, int, int]:
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")
        elif format not in ["amr", "wav", "mp3"]:
            raise ValueError("format must be one of amr, wav, mp3")

        # read voice to byte
        if isinstance(voice, str):
            voice_byte = base64.b64decode(voice)
        elif isinstance(voice, bytes):
            voice_byte = voice
        elif isinstance(voice, os.PathLike):
            with open(voice, "rb") as f:
                voice_byte = f.read()
        else:
            raise ValueError("voice should be str, bytes, or path")

        # get voice duration and b64
        if format.lower() == "amr":
            audio = AudioSegment.from_file(BytesIO(voice_byte), format="amr")
            voice_base64 = base64.b64encode(voice_byte).decode()
        elif format.lower() == "wav":
            audio = AudioSegment.from_file(BytesIO(voice_byte), format="wav").set_channels(1)
            audio = audio.set_frame_rate(self._get_closest_frame_rate(audio.frame_rate))
            voice_base64 = base64.b64encode(
                await pysilk.async_encode(audio.raw_data, sample_rate=audio.frame_rate)).decode()
        elif format.lower() == "mp3":
            audio = AudioSegment.from_file(BytesIO(voice_byte), format="mp3").set_channels(1)
            audio = audio.set_frame_rate(self._get_closest_frame_rate(audio.frame_rate))
            voice_base64 = base64.b64encode(
                await pysilk.async_encode(audio.raw_data, sample_rate=audio.frame_rate)).decode()
        else:
            raise ValueError("format must be one of amr, wav, mp3")

        duration = len(audio)

        format_dict = {"amr": 0, "wav": 4, "mp3": 4}

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "Base64": voice_base64, "VoiceTime": duration,
                          "Type": format_dict[format]}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/SendVoice', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                json_param.pop('Base64')
                logger.info("发送语音消息: 对方wxid:{} 时长:{} 格式:{} 音频base64略", wxid, duration, format)
                data = json_resp.get("Data")
                # 不尝试将ClientMsgId转换为整数，因为它可能包含群聊ID和时间戳
                return data.get("ClientMsgId"), data.get("CreateTime"), data.get("NewMsgId")
            else:
                self.error_handler(json_resp)

    @staticmethod
    def _get_closest_frame_rate(frame_rate: int) -> int:
        supported = [8000, 12000, 16000, 24000]
        closest_rate = None
        smallest_diff = float('inf')
        for num in supported:
            diff = abs(frame_rate - num)
            if diff < smallest_diff:
                smallest_diff = diff
                closest_rate = num

        return closest_rate

    async def send_link_message(self, wxid: str, url: str, title: str = "", description: str = "",
                                thumb_url: str = "") -> tuple[str, int, int]:
        """发送链接消息。

        Args:
            wxid (str): 接收人wxid
            url (str): 跳转链接
            title (str, optional): 标题. Defaults to "".
            description (str, optional): 描述. Defaults to "".
            thumb_url (str, optional): 缩略图链接. Defaults to "".

        Returns:
            tuple[str, int, int]: 返回(ClientMsgid, CreateTime, NewMsgId)

        Raises:
            UserLoggedOut: 未登录时调用
            BanProtection: 登录新设备后4小时内操作
            根据error_handler处理错误
        """
        return await self._queue_message(self._send_link_message, wxid, url, title, description, thumb_url)

    async def _send_link_message(self, wxid: str, url: str, title: str = "", description: str = "",
                                 thumb_url: str = "") -> tuple[int, int, int]:
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "Url": url, "Title": title, "Desc": description,
                          "ThumbUrl": thumb_url}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/ShareLink', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                logger.info("发送链接消息: 对方wxid:{} 链接:{} 标题:{} 描述:{} 缩略图链接:{}",
                            wxid,
                            url,
                            title,
                            description,
                            thumb_url)
                data = json_resp.get("Data")
                return data.get("clientMsgId"), data.get("createTime"), data.get("newMsgId")
            else:
                self.error_handler(json_resp)

    async def _send_location_message(self, wxid: str, Infourl: str, Label: str = "", Poiname: str = "",
                                 Scale: int = 0,X: int = 0,Y: int = 0) -> tuple[int, int, int]:
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "Infourl": Infourl, "Label": Label, "Scale": Scale,
                          "X": X,"Y": Y, "Poiname": Poiname}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/ShareLocation', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                logger.info("发送定位消息: 对方wxid:{} 链接:{} 标题:{} 描述:{} 比例:{} X:{} Y:{}",
                            wxid,
                            Infourl,
                            Label,
                            Poiname,
                            Scale,
                            X,
                            Y)
                data = json_resp.get("Data")
                return data.get("clientMsgId"), data.get("createTime"), data.get("newMsgId")
            else:
                self.error_handler(json_resp)

    async def send_emoji_message(self, wxid: str, md5: str, total_length: int) -> list[dict]:
        """发送表情消息。

        Args:
            wxid (str): 接收人wxid
            md5 (str): 表情md5值
            total_length (int): 表情总长度

        Returns:
            list[dict]: 返回表情项列表(list of emojiItem)

        Raises:
            UserLoggedOut: 未登录时调用
            BanProtection: 登录新设备后4小时内操作
            根据error_handler处理错误
        """
        return await self._queue_message(self._send_emoji_message, wxid, md5, total_length)

    async def _send_emoji_message(self, wxid: str, md5: str, total_length: int) -> tuple[int, int, int]:
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "Md5": md5, "TotalLen": total_length}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/SendEmoji', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                logger.info("发送表情消息: 对方wxid:{} md5:{} 总长度:{}", wxid, md5, total_length)
                return json_resp.get("Data").get("emojiItem")
            else:
                self.error_handler(json_resp)

    async def send_card_message(self, wxid: str, card_wxid: str, card_nickname: str, card_alias: str = "") -> tuple[
        int, int, int]:
        """发送名片消息。

        Args:
            wxid (str): 接收人wxid
            card_wxid (str): 名片用户的wxid
            card_nickname (str): 名片用户的昵称
            card_alias (str, optional): 名片用户的备注. Defaults to "".

        Returns:
            tuple[int, int, int]: 返回(ClientMsgid, CreateTime, NewMsgId)

        Raises:
            UserLoggedOut: 未登录时调用
            BanProtection: 登录新设备后4小时内操作
            根据error_handler处理错误
        """
        return await self._queue_message(self._send_card_message, wxid, card_wxid, card_nickname, card_alias)

    async def _send_card_message(self, wxid: str, card_wxid: str, card_nickname: str, card_alias: str = "") -> tuple[
        int, int, int]:
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "CardWxid": card_wxid, "CardAlias": card_alias,
                          "CardNickname": card_nickname}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/SendCard', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                logger.info("发送名片消息: 对方wxid:{} 名片wxid:{} 名片备注:{} 名片昵称:{}", wxid,
                            card_wxid,
                            card_alias,
                            card_nickname)
                data = json_resp.get("Data")
                return data.get("List")[0].get("ClientMsgid"), data.get("List")[0].get("Createtime"), data.get("List")[
                    0].get("NewMsgId")
            else:
                self.error_handler(json_resp)

    async def send_app_message(self, wxid: str, xml: str, type: int) -> tuple[str, int, int]:
        """发送应用消息。

        Args:
            wxid (str): 接收人wxid
            xml (str): 应用消息的xml内容
            type (int): 应用消息类型

        Returns:
            tuple[str, int, int]: 返回(ClientMsgid, CreateTime, NewMsgId)

        Raises:
            UserLoggedOut: 未登录时调用
            BanProtection: 登录新设备后4小时内操作
            根据error_handler处理错误
        """
        return await self._queue_message(self._send_app_message, wxid, xml, type)

    async def _send_app_message(self, wxid: str, xml: str, type: int) -> tuple[int, int, int]:
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "Xml": xml, "Type": type}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/SendApp', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                json_param["Xml"] = json_param["Xml"].replace("\n", "")
                logger.info("发送app消息: 对方wxid:{} 类型:{} xml:{}", wxid, type, json_param["Xml"])
                return json_resp.get("Data").get("clientMsgId"), json_resp.get("Data").get(
                    "createTime"), json_resp.get("Data").get("newMsgId")
            else:
                self.error_handler(json_resp)

    async def send_cdn_file_msg(self, wxid: str, xml: str) -> dict:
        """转发文件消息。

        Args:
            wxid (str): 接收人wxid
            xml (str): 要转发的文件消息xml内容

        Returns:
            tuple[str, int, int]: 返回(ClientMsgid, CreateTime, NewMsgId)

        Raises:
            UserLoggedOut: 未登录时调用
            BanProtection: 登录新设备后4小时内操作
            根据error_handler处理错误
        """
        return await self._queue_message(self._send_cdn_file_msg, wxid, xml)

    async def _send_cdn_file_msg(self, wxid: str, xml: str) -> dict:
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "Content": xml}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/SendCDNFile', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                logger.info("转发文件消息: 对方wxid:{} xml:{}", wxid, xml)
                # 返回完整的响应结果
                return json_resp
            else:
                self.error_handler(json_resp)

    async def send_cdn_img_msg(self, wxid: str, xml: str) -> tuple[str, int, int]:
        """转发图片消息。

        Args:
            wxid (str): 接收人wxid
            xml (str): 要转发的图片消息xml内容

        Returns:
            tuple[str, int, int]: 返回(ClientImgId, CreateTime, NewMsgId)

        Raises:
            UserLoggedOut: 未登录时调用
            BanProtection: 登录新设备后4小时内操作
            根据error_handler处理错误
        """
        return await self._queue_message(self._send_cdn_img_msg, wxid, xml)

    async def _send_cdn_img_msg(self, wxid: str, xml: str) -> tuple[int, int, int]:
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "Content": xml}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/SendCDNImg', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                logger.info("转发图片消息: 对方wxid:{} xml:{}", wxid, xml)
                data = json_resp.get("Data")
                return data.get("ClientImgId").get("string"), data.get("CreateTime"), data.get("Newmsgid")
            else:
                self.error_handler(json_resp)

    async def send_cdn_video_msg(self, wxid: str, xml: str) -> tuple[str, int]:
        """转发视频消息。

        Args:
            wxid (str): 接收人wxid
            xml (str): 要转发的视频消息xml内容

        Returns:
            tuple[str, int]: 返回(ClientMsgid, NewMsgId)

        Raises:
            UserLoggedOut: 未登录时调用
            BanProtection: 登录新设备后4小时内操作
            根据error_handler处理错误
        """
        return await self._queue_message(self._send_cdn_video_msg, wxid, xml)

    async def _send_cdn_video_msg(self, wxid: str, xml: str) -> tuple[int, int]:
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "Content": xml}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/SendCDNVideo', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                logger.info("转发视频消息: 对方wxid:{} xml:{}", wxid, xml)
                data = json_resp.get("Data")
                return data.get("clientMsgId"), data.get("newMsgId")
            else:
                self.error_handler(json_resp)

    async def send_emoji_message(self, wxid: str, md5: str, total_len: int) -> dict:
        """发送表情消息。

        Args:
            wxid (str): 接收人的wxid
            md5 (str): 表情文件的MD5值
            total_len (int): 表情文件的总长度

        Returns:
            bool: 发送成功返回True，失败返回False

        Raises:
            UserLoggedOut: 未登录时调用
            BanProtection: 登录新设备后4小时内操作
            根据error_handler处理错误
        """
        return await self._queue_message(self._send_emoji_message, wxid, md5, total_len)

    async def _send_emoji_message(self, wxid: str, md5: str, total_len: int) -> dict:
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid, "ToWxid": wxid, "Md5": md5, "TotalLen": total_len}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/SendEmoji', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                logger.info("发送表情消息: 对方wxid:{} MD5:{} 大小:{}", wxid, md5, total_len)
                return json_resp
            else:
                self.error_handler(json_resp)

    async def sync_message(self) -> dict:
        """同步消息。

        Returns:
            dict: 返回同步到的消息数据

        Raises:
            UserLoggedOut: 未登录时调用
            根据error_handler处理错误
        """
        if not self.wxid:
            raise UserLoggedOut("请先登录")

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            json_param = {"Wxid": self.wxid, "Scene": 0, "Synckey": ""}
            response = await session.post(f'http://{self.ip}:{self.port}/api/Msg/Sync', json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                return True,json_resp.get("Data")
            else:
                return False,json_resp.get("Message")
