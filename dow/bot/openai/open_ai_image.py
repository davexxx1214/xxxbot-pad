import time
import base64
import os
import uuid
import requests
import json

from bridge.reply import Reply, ReplyType

from common.log import logger
from common.token_bucket import TokenBucket
from common.tmp_dir import TmpDir
from config import conf


# OPENAI提供的画图接口
class OpenAIImage(object):
    def __init__(self):
        self.api_base = conf().get("open_ai_api_base")
        self.api_key = conf().get("open_ai_api_key")
        if conf().get("rate_limit_dalle"):
            self.tb4dalle = TokenBucket(conf().get("rate_limit_dalle", 50))

    def create_img(self, query, retry_count=0, api_key=None, context=None):
        """
        参数：
        - context: 用于发送画图开始的提示消息
        """
        try:
            if conf().get("rate_limit_dalle") and not self.tb4dalle.get_token():
                return False, "请求太快了，请休息一下再问我吧"
            
            logger.info("[OPEN_AI] image_query={}".format(query))
            
            # 发送画图开始的提示
            self.send_start_prompt(context, query)
            
            # 构建API请求URL
            url = f"{self.api_base}/images/generations"
            
            # 构建请求头
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key or self.api_key}"
            }
            
            # 构建请求体
            payload = {
                "model": "gpt-image-1",
                "prompt": query,
                "n": 1,
                "output_format": "png",
                "background": "auto",
                "size": "auto"
            }
            
            # 发送POST请求
            logger.info("[OPEN_AI] Sending request to API")
            response = requests.post(
                url, 
                headers=headers, 
                json=payload,
                timeout=300  # 设置较长的超时时间，因为图像生成可能需要较长时间
            )
            
            # 检查响应状态
            if response.status_code != 200:
                logger.error(f"[OPEN_AI] API request failed with status code {response.status_code}: {response.text}")
                if "rate limit" in response.text.lower():
                    if retry_count < 1:
                        time.sleep(5)
                        logger.warn("[OPEN_AI] ImgCreate RateLimit exceed, 第{}次重试".format(retry_count + 1))
                        return self.create_img(query, retry_count + 1, api_key, context)
                    else:
                        return False, "画图出现问题，请休息一下再问我吧"
                elif "safety" in response.text.lower() or "content policy" in response.text.lower():
                    return False, "画图出现问题，关键字没有通过安全审核"
                else:
                    return False, "画图出现问题，请休息一下再问我吧"
            
            # 解析JSON响应
            result = response.json()
            
            # 处理返回结果
            if "data" in result and len(result["data"]) > 0:
                image_data = result["data"][0]
                
                if "b64_json" in image_data and image_data["b64_json"]:
                    # 从base64获取图片数据
                    image_bytes = base64.b64decode(image_data["b64_json"])
                    
                    # 保存到临时目录
                    imgpath = TmpDir().path() + "gpt-image-" + str(uuid.uuid4()) + ".png"
                    with open(imgpath, 'wb') as file:
                        file.write(image_bytes)
                    
                    logger.info("[OPEN_AI] image saved to {}".format(imgpath))
                    return True, imgpath
                    
                elif "url" in image_data and image_data["url"]:
                    # 如果返回了URL而不是base64
                    image_url = image_data["url"]
                    logger.info("[OPEN_AI] image_url={}".format(image_url))
                    
                    # 下载图片
                    img_response = requests.get(image_url, timeout=60)
                    if img_response.status_code == 200:
                        # 保存到临时目录
                        imgpath = TmpDir().path() + "gpt-image-" + str(uuid.uuid4()) + ".png"
                        with open(imgpath, 'wb') as file:
                            file.write(img_response.content)
                        
                        logger.info("[OPEN_AI] image saved to {}".format(imgpath))
                        return True, imgpath
                    else:
                        logger.error(f"[OPEN_AI] Failed to download image, status code: {img_response.status_code}")
                        return False, "画图出现问题，无法下载生成的图片"
                else:
                    logger.error("[OPEN_AI] No image data or URL in response")
                    return False, "画图出现问题，API没有返回图片数据"
            else:
                logger.error("[OPEN_AI] Invalid API response format")
                return False, "画图出现问题，API返回格式不正确"
                
        except requests.exceptions.Timeout:
            logger.error("[OPEN_AI] Request timeout")
            if retry_count < 1:
                time.sleep(5)
                logger.warn("[OPEN_AI] ImgCreate timeout, 第{}次重试".format(retry_count + 1))
                return self.create_img(query, retry_count + 1, api_key, context)
            else:
                return False, "画图请求超时，请稍后再试"
        except requests.exceptions.RequestException as e:
            logger.error(f"[OPEN_AI] Request error: {e}")
            return False, "画图网络请求出错，请检查网络连接"
        except Exception as e:
            logger.exception(e)
            error_msg = str(e)
            if "safety" in error_msg.lower() or "content policy" in error_msg.lower():
                return False, "画图出现问题，关键字没有通过安全审核"
            else:
                return False, "画图出现问题，请休息一下再问我吧"

    def send_start_prompt(self, context, query):
        """发送画图开始的提示"""
        if not context:
            return
        try:
            channel = context.get("channel")
            reply = Reply(ReplyType.TEXT, f"🎨 gpt-image-1画图请求已进入队列，预计需要30-150秒完成。\n\n提示词：{query}")
            channel.send(reply, context)
        except Exception as e:
            logger.error(e)