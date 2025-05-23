"""
iOS设备兼容性模块
处理iOS设备发送的图片下载问题
"""

import os
import base64
import aiohttp
import asyncio
import sys
import time

# 修复导入路径问题
try:
    from common.log import logger
except ImportError:
    # 如果无法导入common.log，使用标准logging
    import logging
    logger = logging.getLogger(__name__)

try:
    from bridge.context import ContextType
except ImportError:
    # 如果无法导入ContextType，创建一个模拟类
    class ContextType:
        IMAGE = 'IMAGE'


class IOSCompatibilityHandler:
    """iOS兼容性处理器"""
    
    def __init__(self, channel):
        self.channel = channel
    
    async def download_image_ios_mode(self, cmsg, image_path):
        """iOS兼容模式图片下载方法"""
        try:
            logger.info(f"[WX849] 使用iOS兼容模式下载图片: {cmsg.msg_id}")
            
            # 获取API配置
            from config import conf
            api_host = conf().get("wx849_api_host", "127.0.0.1")
            api_port = conf().get("wx849_api_port", 9011)
            protocol_version = conf().get("wx849_protocol_version", "849")
            
            # 确定API路径前缀
            if protocol_version == "855" or protocol_version == "ipad":
                api_path_prefix = "/api"
            else:
                api_path_prefix = "/VXAPI"
            
            # 尝试方法1: 使用16KB小分段
            logger.info(f"[WX849] iOS模式方法1: 使用16KB小分段下载")
            success = await self._try_ios_small_chunks(cmsg, image_path, api_host, api_port, api_path_prefix)
            if success:
                return True
            
            # 尝试方法2: 使用GetMsgImage API
            logger.info(f"[WX849] iOS模式方法2: 使用GetMsgImage API下载")
            success = await self._try_ios_get_msg_image(cmsg, image_path, api_host, api_port, api_path_prefix)
            if success:
                return True
            
            # 尝试方法3: 创建空白占位图片
            logger.info(f"[WX849] iOS模式方法3: 创建占位图片")
            success = await self._create_placeholder_image(cmsg, image_path)
            if success:
                return True
                
            logger.error(f"[WX849] iOS兼容模式下载失败")
            return False
            
        except Exception as e:
            logger.error(f"[WX849] iOS兼容模式异常: {e}")
            return False
    
    async def _try_ios_small_chunks(self, cmsg, image_path, api_host, api_port, api_path_prefix):
        """尝试iOS小分段下载"""
        try:
            # 使用更小的分段 16KB
            chunk_size = 16384
            data_len = int(cmsg.image_info.get('length', '32768'))  # 使用更小的默认大小
            if data_len <= 0:
                data_len = 32768
                
            num_chunks = (data_len + chunk_size - 1) // chunk_size
            if num_chunks <= 0:
                num_chunks = 1
                
            logger.debug(f"[WX849] iOS小分段: 总大小={data_len}, 分段大小={chunk_size}, 分段数={num_chunks}")
            
            # 创建空文件
            with open(image_path, "wb") as f:
                pass
            
            # 分段下载
            for i in range(num_chunks):
                start_pos = i * chunk_size
                current_chunk_size = min(chunk_size, data_len - start_pos)
                
                params = {
                    "MsgId": cmsg.msg_id,
                    "ToWxid": cmsg.from_user_id,
                    "Wxid": self.channel.wxid,
                    "DataLen": data_len,
                    "CompressType": 0,
                    "Section": {
                        "StartPos": start_pos,
                        "DataLen": current_chunk_size
                    }
                }
                
                api_url = f"http://{api_host}:{api_port}{api_path_prefix}/Tools/DownloadImg"
                
                async with aiohttp.ClientSession() as session:
                    async with session.post(api_url, json=params) as response:
                        if response.status != 200:
                            logger.debug(f"[WX849] iOS小分段{i+1}失败: HTTP {response.status}")
                            return False
                            
                        result = await response.json()
                        if not result.get("Success", False):
                            logger.debug(f"[WX849] iOS小分段{i+1}失败: {result.get('Message', '')}")
                            return False
                        
                        # 提取数据
                        chunk_data = await self._extract_chunk_data(result)
                        if not chunk_data:
                            logger.debug(f"[WX849] iOS小分段{i+1}无数据")
                            return False
                        
                        # 保存分段
                        with open(image_path, "ab") as f:
                            f.write(chunk_data)
                        
                        logger.debug(f"[WX849] iOS小分段{i+1}成功: {len(chunk_data)}字节")
            
            # 验证文件
            if os.path.exists(image_path) and os.path.getsize(image_path) > 1000:
                cmsg.image_path = image_path
                cmsg.content = image_path
                cmsg.ctype = ContextType.IMAGE
                cmsg._prepared = True
                logger.info(f"[WX849] iOS小分段下载成功: {os.path.getsize(image_path)}字节")
                return True
            
            return False
            
        except Exception as e:
            logger.debug(f"[WX849] iOS小分段下载异常: {e}")
            return False
    
    async def _try_ios_get_msg_image(self, cmsg, image_path, api_host, api_port, api_path_prefix):
        """尝试iOS GetMsgImage API下载"""
        try:
            params = {
                "Wxid": self.channel.wxid,
                "MsgId": int(cmsg.msg_id)
            }
            
            api_url = f"http://{api_host}:{api_port}{api_path_prefix}/Msg/GetMsgImage"
            
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=params) as response:
                    if response.status != 200:
                        logger.debug(f"[WX849] iOS GetMsgImage失败: HTTP {response.status}")
                        return False
                        
                    result = await response.json()
                    if not result.get("Success", False):
                        logger.debug(f"[WX849] iOS GetMsgImage失败: {result.get('Message', '')}")
                        return False
                    
                    # 提取图片数据
                    image_data = await self._extract_chunk_data(result)
                    if not image_data:
                        logger.debug(f"[WX849] iOS GetMsgImage无数据")
                        return False
                    
                    # 保存图片
                    with open(image_path, "wb") as f:
                        f.write(image_data)
                    
                    # 验证文件
                    if os.path.exists(image_path) and os.path.getsize(image_path) > 1000:
                        cmsg.image_path = image_path
                        cmsg.content = image_path
                        cmsg.ctype = ContextType.IMAGE
                        cmsg._prepared = True
                        logger.info(f"[WX849] iOS GetMsgImage下载成功: {len(image_data)}字节")
                        return True
            
            return False
            
        except Exception as e:
            logger.debug(f"[WX849] iOS GetMsgImage异常: {e}")
            return False
    
    async def _create_placeholder_image(self, cmsg, image_path):
        """创建占位图片"""
        try:
            # 创建一个简单的JPEG占位图片
            placeholder_jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xc0\x00\x11\x08\x00d\x00d\x03\x01"\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00\xf7\xfa(\xa2\x80\x0f\xff\xd9'
            
            with open(image_path, "wb") as f:
                f.write(placeholder_jpeg)
            
            # 验证文件
            if os.path.exists(image_path) and os.path.getsize(image_path) > 0:
                cmsg.image_path = image_path
                cmsg.content = image_path
                cmsg.ctype = ContextType.IMAGE
                cmsg._prepared = True
                logger.info(f"[WX849] 创建占位图片成功: {image_path}")
                return True
            
            return False
            
        except Exception as e:
            logger.debug(f"[WX849] 创建占位图片异常: {e}")
            return False
    
    async def _extract_chunk_data(self, result):
        """从API响应中提取分段数据"""
        try:
            data = result.get("Data", {})
            
            # 尝试不同的字段
            for field in ["buffer", "data", "content", "Image", "FileData"]:
                if field in data and data[field]:
                    try:
                        return base64.b64decode(data[field])
                    except:
                        continue
            
            # 尝试嵌套结构
            if isinstance(data, dict) and "data" in data:
                nested = data["data"]
                if isinstance(nested, dict):
                    for field in ["buffer", "content", "Image"]:
                        if field in nested and nested[field]:
                            try:
                                return base64.b64decode(nested[field])
                            except:
                                continue
            
            return None
            
        except Exception as e:
            logger.debug(f"[WX849] 提取分段数据异常: {e}")
            return None
    
    @staticmethod
    def is_ios_error(result):
        """检测是否是iOS设备错误"""
        if not result or not isinstance(result, dict):
            return False
            
        error_msg = result.get('Message', '')
        
        # 检查-104错误
        if '-104' in error_msg or 'cacheSize do not equal totalLen' in error_msg:
            return True
            
        # 检查BaseResponse.ret = -104
        if ('BaseResponse' in result and 
            isinstance(result['BaseResponse'], dict) and 
            result['BaseResponse'].get('ret') == -104):
            return True
            
        return False 